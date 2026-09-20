"""AI 对话 API 路由 — 通过 dsh (DeepSeek Harness) 调用 LLM。"""

import re
import json as _json
import uuid
import threading
import datetime
import asyncio
from fastapi import APIRouter, Request, Depends, HTTPException
from sqlalchemy.orm import Session

from ...core.database import get_db
from ...core.security import get_current_user
from ...models import AIRateLimit, DshChatModel, DshConfig, AIProvider, ChatSession, User, AgentTask, AgentCostAlert
from ...schemas.ai import (
    ChatRequest, ChatResponse, QuotaResponse, QuotaItem, ChatModelRead,
    ChatSessionCreate, ChatSessionRead, ChatSessionUpdate,
)
from ...services.rate_limiter import rate_limiter
from ...services.prompt_guard import prompt_guard
from ...services.usage_service import check_daily_quota, increment_daily_usage
from ...services.sanitize import redact, redact_value
from ...services.agent_config import get_agent_config, agent_concurrency
from ...services.llm_service import AgentCancelled

router = APIRouter()

# Agent 后台任务总时限（方案 B 超时安全网）：任务不随 SSE 断开取消，
# 超过该时长由 llm_service 工具循环强制终止，防后台无限消耗
_AGENT_TASK_TIMEOUT_SECONDS = 20 * 60

# ── 性能优化9：任务级成本告警阈值（只记录不阻断，管理端统计面板展示）──
# 单任务总 token 超过该值 → tokens 告警（200k ≈ 多轮长任务/上下文膨胀信号）
_TOKEN_ALERT_THRESHOLD = 200000
# 单任务耗时超过该值 → duration 告警（贴近 20 分钟总时限，说明跑满了大半程）
_DURATION_ALERT_SECONDS = 15 * 60

# ── 图片视觉优化2：请求体与图片配额的服务端强制点 ──
# /ai/chat/stream 收的是裸 dict（不是 pydantic 模型），前端的 MAX_IMAGE_SIZE(4MB) 与
# max_files_per_message 只活在浏览器里：直连 API 可以 POST 任意大小的 body，base64
# 解成 Python str 还要再翻一倍内存，且视觉模型按图计费 —— 不设闸门等于把 DoS 和
# 账单敞口直接留给公网。
_MAX_BODY_BYTES = 32 * 1024 * 1024    # 请求体硬上限（默认 max_files=5 的合法最坏：5×4MB 图 ≈ 26.7MB base64）
# 管理员把 max_files_per_message 调到 5 以上时，图片字节预算（max_files × 4MB）会超过
# 这个传输上限，此时先撞 413 而不是 400 —— 这是有意的：不为极端配置把上百 MB body 读进内存。
_MAX_IMAGE_BYTES = 4 * 1024 * 1024    # 单张图解码后上限，与前端 MAX_IMAGE_SIZE 对齐
_BASE64_MARKER = ";base64,"
_IMAGE_BLOCK_RE = re.compile(r'<image\s+name="[^"]*"[^>]*>([\s\S]*?)</image>', re.IGNORECASE)
_FILE_BLOCK_RE = re.compile(r'<file\s+name="[^"]*"[^>]*>[\s\S]*?</file>', re.IGNORECASE)
# 捕获 name 属性（_IMAGE_BLOCK_RE 捕获的是标签体）：工具结果展示/落库前把整个 image 块
# 换成可读摘要用，见 _on_tool_result
_IMAGE_NAME_RE = re.compile(r'<image\s+name="([^"]*)"[^>]*>[\s\S]*?</image>', re.IGNORECASE)

# ── Agent 长任务取消事件注册表（方案 B）──
# task_id → threading.Event；SSE 断开不再自动取消任务（刷新/断线任务继续跑），
# 用户主动停止通过 POST /ai/tasks/{task_id}/cancel 置位此事件。
_task_cancel_events: dict[str, "threading.Event"] = {}
_task_cancel_guard = threading.Lock()


def _serialize_tool_events(events: list[dict], max_events: int = 100,
                           max_str_chars: int = 1500, max_total_chars: int = 48000) -> str:
    """把工具调用事件序列化为 JSON 字符串（存 AgentTask.tool_events）。

    三重截断防单条任务记录膨胀（TEXT 列容量有限）：
    - 事件数：只保留最近 max_events 条（40 轮 × 4 并行 = 160 上限，超出的丢最旧）
    - 单条长度：result 与 arguments 里的长字符串（run_python 代码等）截断
    - 总量：JSON 总长超限时从头部丢事件（保证 JSON 始终合法可解析）
    """
    def _cap(s: str) -> str:
        return s if len(s) <= max_str_chars else s[:max_str_chars] + f"...[已截断，共{len(s)}字符]"

    trimmed = []
    for e in events[-max_events:]:
        ev = {"id": e.get("id"), "tool": e.get("tool"), "round": e.get("round"),
              "max_rounds": e.get("max_rounds")}
        args = e.get("arguments")
        if isinstance(args, dict):
            ev["arguments"] = {k: _cap(v) if isinstance(v, str) else v
                               for k, v in args.items()}
        r = e.get("result")
        if r is not None:
            ev["result"] = _cap(str(r))
        trimmed.append(ev)
    while trimmed:
        s = _json.dumps(trimmed, ensure_ascii=False)
        if len(s) <= max_total_chars:
            return s
        trimmed = trimmed[1:]  # 超总量：丢最旧的事件重试（保持 JSON 合法）
    return "[]"


def _update_task_status(db, task_id: str, status: str, result_text: str = "",
                        error: str = "", tool_events: list | None = None,
                        model_switches: list | None = None,
                        token_usage: dict | None = None):
    """后台任务完成后回写状态（worker 线程调用；失败仅记日志，不影响对话）。"""
    import datetime as _dt
    try:
        if db is None:
            return
        t = db.query(AgentTask).filter(AgentTask.task_id == task_id).first()
        if not t:
            return
        t.status = status
        if result_text:
            t.result = result_text[:20000]
        if error:
            t.error = error[:2000]
        if tool_events is not None:
            t.tool_events = _serialize_tool_events(tool_events)
        if model_switches is not None:
            t.model_switches = _json.dumps(model_switches, ensure_ascii=False)
        # 性能优化1：任务级 token 用量落库（0/缺失记 NULL，与真实 0 区分开）
        if token_usage:
            total = int(token_usage.get("total_tokens") or 0)
            if total > 0:
                t.prompt_tokens = int(token_usage.get("prompt_tokens") or 0)
                t.completion_tokens = int(token_usage.get("completion_tokens") or 0)
                t.total_tokens = total
                # 性能优化7：缓存命中 tokens（0/无缓存记 NULL）
                _cache = int(token_usage.get("cache_read_tokens") or 0)
                if _cache > 0:
                    t.cache_read_tokens = _cache
        t.finished_at = _dt.datetime.utcnow()
        # ── 性能优化9：任务级成本告警（只记录不阻断）──
        # 终态 done/failed 检查阈值；cancelled 是用户主动行为，不算成本异常。
        # 告警插入与状态回写隔离：告警失败仅记日志，不影响任务状态落库。
        try:
            if status in ("done", "failed"):
                _total_tokens = int(t.total_tokens or 0)
                if _total_tokens >= _TOKEN_ALERT_THRESHOLD:
                    db.add(AgentCostAlert(
                        task_id=task_id, user_id=t.user_id, alert_type="tokens",
                        value=_total_tokens, threshold=_TOKEN_ALERT_THRESHOLD,
                        message=f"任务 token 用量 {_total_tokens} 达到阈值 {_TOKEN_ALERT_THRESHOLD}",
                    ))
                if t.created_at:
                    _dur = int((t.finished_at - t.created_at).total_seconds())
                    if _dur >= _DURATION_ALERT_SECONDS:
                        db.add(AgentCostAlert(
                            task_id=task_id, user_id=t.user_id, alert_type="duration",
                            value=_dur, threshold=_DURATION_ALERT_SECONDS,
                            message=f"任务耗时 {_dur}s 达到阈值 {_DURATION_ALERT_SECONDS}s",
                        ))
        except Exception:
            import logging as _log
            _log.getLogger(__name__).warning(
                f"成本告警插入失败 task={task_id}（不影响任务状态）", exc_info=True)
        db.commit()
    except Exception as _e:
        import logging as _log
        _log.getLogger(__name__).warning(f"更新任务状态失败 task={task_id}: {_e}")
    finally:
        with _task_cancel_guard:
            _task_cancel_events.pop(task_id, None)


# ════════════════════════════════════════
#  历史消息防篡改（方案四）
# ════════════════════════════════════════

# 允许的角色
_VALID_ROLES = {"user", "assistant"}

# 单条消息最大长度
_MAX_MSG_LEN = 4000

# 检测特殊 token 注入 — OpenAI / Anthropic 格式
_SPECIAL_TOKEN_RE = re.compile(
    r"<\|im_start\|>|<\|im_end\|>|<\|system\|>|<\|user\|>|<\|assistant\|>"
)

# 检测伪造系统指令
_FAKE_SYSTEM_RE = re.compile(
    r"^\s*system\s*[:：]", re.IGNORECASE | re.MULTILINE
)


def _sanitize_history(history: list) -> list[dict]:
    """清洗前端传入的历史消息，防止注入攻击。

    - 只允许 user/assistant 角色，拒绝 system
    - 截断超长消息
    - 检测并剔除含特殊 token 注入的消息
    - 检测并剔除伪造系统指令的消息
    """
    if not history:
        return []

    clean: list[dict] = []
    for msg in history:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content", "")

        # 只允许 user/assistant
        if role not in _VALID_ROLES:
            continue

        # content 必须是字符串
        if not isinstance(content, str):
            continue

        # 空内容消息无意义，丢弃（部分 API 如智谱 GLM 拒绝空 user content，
        # 返回 400 "未收到有效 prompt"；来源：纯文件/图片消息未输入文字时
        # 历史里会残留 content="" 的 user 消息）
        if not content.strip():
            continue

        # 截断超长消息
        if len(content) > _MAX_MSG_LEN:
            content = content[:_MAX_MSG_LEN]

        # 检测特殊 token 注入
        if _SPECIAL_TOKEN_RE.search(content):
            continue

        # 检测伪造系统指令（仅在 user 消息中检测）
        if role == "user" and _FAKE_SYSTEM_RE.search(content):
            continue

        clean.append({"role": role, "content": content})

    return clean


async def _read_body_capped(request: Request, limit: int = _MAX_BODY_BYTES) -> bytes:
    """带上限读取请求体，超限抛 413（图片视觉优化2 的传输层闸门）。

    先按 Content-Length 预检——能在不分配任何内存的情况下挡掉绝大多数超大请求
    （浏览器 fetch 发字符串 body 时一定会带这个头）；chunked 传输没有
    Content-Length，所以仍要边读边限，两者缺一不可。
    """
    detail = f"请求体过大（上限 {limit // (1024 * 1024)} MB）"
    declared = request.headers.get("content-length")
    if declared:
        try:
            if int(declared) > limit:
                raise HTTPException(status_code=413, detail=detail)
        except ValueError:
            pass  # 头非法时不拦，交给下面的流式上限兜底
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise HTTPException(status_code=413, detail=detail)
        chunks.append(chunk)
    body = b"".join(chunks)
    # 本函数自己消费了 stream，必须回填 Starlette 的 body 缓存，
    # 否则之后任何 await request.body() 都会抛 "Stream consumed"。
    request._body = body
    return body


def _check_message_attachments(message: str, max_files: int) -> None:
    """校验单条消息的附件数量与图片体积（图片视觉优化2 的策略层闸门）。

    只认带闭合标签的完整 <image>/<file> 块，避免把用户正文里粘贴的 XML 片段
    误判成附件。history 不在此校验：_sanitize_history 已把每条截断到 4000 字符，
    完整 base64 块活不下来，总体积由 _read_body_capped 兜住。

    图片块数不能直接和 max_files 比：一份扫描件 PDF 在管理端语义里是"1 个附件"，
    前端却把它展开成 MAX_PDF_IMAGE_PAGES 个 <image> 块。给 <image> 加属性来区分
    会破坏 llm_service._IMAGE_TAG 的解析（它要求 size="..." 后紧跟 >），所以改用
    ceil(块数 / 单份文档页数) —— 这是"至少需要几个逻辑附件才能产生这么多块"的下界，
    永不误拦扫描件；下界偏松的极端情况（大量小图）由总字节预算兜住。
    """
    if "<image" not in message and "<file" not in message:
        return
    images = _IMAGE_BLOCK_RE.findall(message)
    files = len(_FILE_BLOCK_RE.findall(message))
    if not images and not files:
        return
    if max_files <= 0:
        raise HTTPException(status_code=400, detail="管理员已禁用消息附件")

    from ...services.workspace_service import MAX_PDF_IMAGE_PAGES
    logical = files + -(-len(images) // MAX_PDF_IMAGE_PAGES)
    if logical > max_files:
        raise HTTPException(
            status_code=400,
            detail=f"单条消息最多 {max_files} 个附件（当前至少 {logical} 个）",
        )

    total = 0
    for payload in images:
        body = payload.strip()
        marker = body.find(_BASE64_MARKER)
        if not body.startswith("data:") or marker < 0:
            raise HTTPException(
                status_code=400, detail="图片必须是 data:image/...;base64, 格式",
            )
        b64 = body[marker + len(_BASE64_MARKER):]
        # 扣掉末尾 padding 才是精确解码长度；漏扣会多算最多 2 字节，
        # 把前端放行的"正好 4MB"边界图片误拦。
        pad = 2 if b64.endswith("==") else 1 if b64.endswith("=") else 0
        decoded = len(b64) * 3 // 4 - pad
        if decoded > _MAX_IMAGE_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"单张图片不能超过 {_MAX_IMAGE_BYTES // (1024 * 1024)} MB",
            )
        total += decoded
    # 总字节预算 = 前端能合法产生的最坏情况（max_files 张顶格图），这才是账单上界：
    # 按块数限不住"少量巨图"，按单张限不住"很多小图"，两道都要。
    budget = max_files * _MAX_IMAGE_BYTES
    if total > budget:
        raise HTTPException(
            status_code=400,
            detail=f"单条消息图片总量不能超过 {budget // (1024 * 1024)} MB",
        )


def _file_event(user_id: int, name: str, content: str, mime) -> dict:
    """构造 SSE 文件事件：大文件（≥256KB）落盘到工作区 .dsh_generated/，
    SSE 只发元数据，避免单个事件过大 + 前端 localStorage 被大内容撑爆。

    落盘失败时回退原样传输（内容仍是完整文本）。
    在 worker 线程调用（同步 IO 不阻塞事件循环）。
    """
    payload: dict = {"name": name, "content": content, "mime": mime}
    if content and len(content.encode("utf-8")) >= 256 * 1024:
        try:
            from ...services.workspace_service import save_generated_file
            rel = save_generated_file(user_id, name, content)
            payload = {
                "name": name,
                "content": "",
                "mime": mime,
                "size": len(content.encode("utf-8")),
                "generated_path": rel,
            }
        except Exception:
            pass  # 落盘失败：回退原样传输
    return payload


# ── G：AI 生成文件默认写工作区；仅当用户明确要求"发送/下载"时才发聊天框 ──
_FILE_DELIVERY_HINTS = (
    "发给我", "发我", "发到聊天", "发到聊天框", "发送文件", "发文件",
    "给我文件", "发过来", "传给我", "发一份", "发一下", "发到", "下载",
)


def _user_wants_file_delivery(text: str) -> bool:
    """判断用户消息是否明确要求把文件发到聊天/下载（G 方案分流依据）。"""
    return any(h in (text or "") for h in _FILE_DELIVERY_HINTS)


def _write_generated_to_workspace(user_id: int, name: str, content: str, mime, db) -> None:
    """把 AI 生成的 <file-download> 内容写入用户工作区（Agent 模式默认分流）。

    PDF/Word 按文本内容转二进制；其余按 UTF-8 文本写入。走 upload_files_batch
    （建记录 + 磁盘 + 上传后补录兜底），文件在工作区面板可见可下载。
    """
    from ...services.workspace_service import (
        generate_docx_bytes,
        generate_pdf_bytes,
        upload_files_batch,
    )
    name = (name or "").replace("\\", "/").lstrip("/")
    if not name:
        raise ValueError("空文件名")
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext == "pdf":
        data = generate_pdf_bytes(content)
    elif ext == "docx":
        data = generate_docx_bytes(content)
    else:
        data = content.encode("utf-8")
    upload_files_batch(user_id, [(name, data)], db)


def _get_client_ip(request: Request) -> str:
    """获取客户端 IP。"""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _get_rate_limits(db: Session) -> AIRateLimit:
    """获取频率限制配置，不存在则创建默认行。"""
    limits = db.query(AIRateLimit).filter(AIRateLimit.id == 1).first()
    if not limits:
        limits = AIRateLimit(id=1)
        db.add(limits)
        db.commit()
        db.refresh(limits)
    return limits


def _get_max_sessions(db: Session) -> int:
    """从 DshConfig 获取每用户最大会话数。"""
    config = db.query(DshConfig).filter(DshConfig.id == 1).first()
    if config and config.max_sessions_per_user:
        return config.max_sessions_per_user
    return 5


def _get_chat_daily_limit(db: Session) -> int:
    """从 DshConfig 获取每用户每日对话次数上限。"""
    config = db.query(DshConfig).filter(DshConfig.id == 1).first()
    if config and config.chat_daily_limit:
        return config.chat_daily_limit
    return 50


def _get_max_files_per_message(db: Session) -> int:
    """从 DshConfig 获取单条消息附件数上限（0 = 管理员禁用附件）。

    必须用 is not None 而不是真值判断：0 是有效配置，真值判断会把它当成
    "未配置" 回落到 5，管理端的禁用开关在服务端就形同虚设。
    """
    config = db.query(DshConfig).filter(DshConfig.id == 1).first()
    if config and config.max_files_per_message is not None:
        return config.max_files_per_message
    return 5


def _to_session_read(s: ChatSession) -> ChatSessionRead:
    return ChatSessionRead(
        id=s.id,
        session_id=s.session_id,
        visitor_id=s.visitor_id,
        title=s.title,
        model_id=s.model_id,
        is_active=s.is_active,
        created_at=s.created_at.strftime("%Y-%m-%d %H:%M:%S") if s.created_at else "",
        updated_at=s.updated_at.strftime("%Y-%m-%d %H:%M:%S") if s.updated_at else "",
    )


# ════════════════════════════════════════
#  会话窗口管理
# ════════════════════════════════════════

@router.get("/ai/chat/sessions", response_model=list[ChatSessionRead])
async def list_chat_sessions(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """获取当前用户的所有会话窗口。"""
    visitor_id = f"user:{user.id}"
    sessions = (
        db.query(ChatSession)
        .filter(ChatSession.visitor_id == visitor_id, ChatSession.is_active == True)
        .order_by(ChatSession.updated_at.desc())
        .all()
    )
    return [_to_session_read(s) for s in sessions]


@router.post("/ai/chat/sessions", response_model=ChatSessionRead)
async def create_chat_session(
    payload: ChatSessionCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """创建新会话窗口。"""
    visitor_id = f"user:{user.id}"
    max_sessions = _get_max_sessions(db)
    current_count = (
        db.query(ChatSession)
        .filter(ChatSession.visitor_id == visitor_id, ChatSession.is_active == True)
        .count()
    )
    if current_count >= max_sessions:
        raise HTTPException(
            status_code=429,
            detail=f"会话窗口数量已达上限（{max_sessions}个），请先关闭旧会话",
        )

    session_id = f"chat-{uuid.uuid4().hex[:12]}"
    session = ChatSession(
        session_id=session_id,
        visitor_id=visitor_id,
        title=payload.title or "新对话",
        model_id=payload.model_id,
        is_active=True,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return _to_session_read(session)


@router.put("/ai/chat/sessions/{session_pk}", response_model=ChatSessionRead)
async def update_chat_session(
    session_pk: int,
    payload: ChatSessionUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """更新会话窗口（重命名或关闭）。"""
    visitor_id = f"user:{user.id}"
    session = db.query(ChatSession).filter(ChatSession.id == session_pk).first()
    if not session or session.visitor_id != visitor_id:
        raise HTTPException(status_code=404, detail="会话不存在")

    if payload.title is not None:
        session.title = payload.title
    if payload.is_active is not None:
        session.is_active = payload.is_active

    db.commit()
    db.refresh(session)
    return _to_session_read(session)


@router.delete("/ai/chat/sessions/{session_pk}")
async def delete_chat_session(
    session_pk: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """删除会话窗口。"""
    visitor_id = f"user:{user.id}"
    session = db.query(ChatSession).filter(ChatSession.id == session_pk).first()
    if not session or session.visitor_id != visitor_id:
        raise HTTPException(status_code=404, detail="会话不存在")

    db.delete(session)
    db.commit()
    return {"message": "删除成功"}


# ════════════════════════════════════════
#  对话
# ════════════════════════════════════════

@router.post("/ai/chat", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """AI 对话 — 需要登录，用用户 ID 识别身份。"""
    ip = _get_client_ip(request)
    visitor_id = f"user:{user.id}"
    limits = _get_rate_limits(db)

    # 频率检查（每日配额持久化到 DB，分钟限流走内存）
    chat_daily_limit = _get_chat_daily_limit(db)
    allowed, err = check_daily_quota(db, user.id, "chat", chat_daily_limit)
    if not allowed:
        raise HTTPException(status_code=429, detail=err)
    allowed, err = rate_limiter.check(visitor_id, ip, 10**9, limits.chat_minute_limit)
    if not allowed:
        raise HTTPException(status_code=429, detail=err)

    # 3. Prompt Injection 检测（方案一）
    is_safe, reason = prompt_guard.check(req.message, user_key=visitor_id)
    if not is_safe:
        raise HTTPException(status_code=400, detail=reason)

    # 4. 调用 dsh（通过即计一次当日配额）
    increment_daily_usage(db, user.id, "chat")
    session_id = req.session_id or f"chat-{uuid.uuid4().hex[:12]}"

    # 确保会话在数据库中注册，且必须属于当前用户
    if req.session_id:
        existing = (
            db.query(ChatSession)
            .filter(ChatSession.session_id == req.session_id, ChatSession.visitor_id == visitor_id)
            .first()
        )
        if not existing:
            new_session = ChatSession(
                session_id=req.session_id,
                visitor_id=visitor_id,
                title=req.message[:50] + ("..." if len(req.message) > 50 else ""),
                model_id=req.model_id,
                is_active=True,
            )
            db.add(new_session)
            db.commit()
        else:
            existing.title = req.message[:50] + ("..." if len(req.message) > 50 else "")
            if req.model_id:
                existing.model_id = req.model_id
            db.commit()
    else:
        new_session = ChatSession(
            session_id=session_id,
            visitor_id=visitor_id,
            title=req.message[:50] + ("..." if len(req.message) > 50 else ""),
            model_id=req.model_id,
            is_active=True,
        )
        db.add(new_session)
        db.commit()

    try:
        # 引擎：llm_service（纯 Python 直连 LLM API，同时支持 OpenAI / Anthropic 协议）。
        # 非流式封装 —— 直接取它累积好的 response。
        # 注意：本端点是**无状态单轮**（消息内容不落库，历史由调用方维护）；
        # 需要多轮请用 /ai/chat/stream（前端会把 history 一起传上来）。
        from ...services.llm_service import llm_service

        result = await run_in_threadpool(
            llm_service.chat_stream,
            req.message, db,
            session_id=session_id, model_id=req.model_id,
        )
        # chat_stream 返回的 dict 是 ChatResponse 的超集（另含 task_* / history_*
        # 等审计字段），这里只保留 schema 声明过的键。
        payload = {k: v for k, v in result.items() if k in ChatResponse.model_fields}
        return ChatResponse(**payload)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI 对话异常: {e}")


@router.post("/ai/chat/stream")
async def chat_stream(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """AI 对话 — SSE 流式输出，需要登录。"""
    import json as _json
    import logging as _log
    import threading as _threading

    body = await _read_body_capped(request)
    try:
        data = _json.loads(body) if body else {}
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # 请求追踪 ID（贯穿 llm_debug.log + SSE 错误事件，便于前后端对账定位）
    import uuid as _uuid
    request_id = _uuid.uuid4().hex[:8]
    import logging as _req_log
    _req_log.getLogger(__name__).info(f"[req:{request_id}] chat_stream start session={data.get('session_id')}")

    message = data.get("message", "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="消息不能为空")

    # 附件配额（图片视觉优化2）：前端限制可被直连 API 绕过，这里是唯一强制点。
    # 放在并发槽/限流/配额计数之前，非法请求尽早失败，不白占资源。
    _check_message_attachments(message, _get_max_files_per_message(db))

    session_id_in = data.get("session_id")
    model_id = data.get("model_id")
    # 前端传入的会话历史（从 localStorage 恢复）— 清洗后使用，防止注入攻击
    raw_history = data.get("history") or []
    history = _sanitize_history(raw_history)
    # 是否注入工作区上下文
    workspace_context = data.get("workspace_context", False)
    # ── 工作区注入三档（注入开关优化3：新增"仅目录"中间档）──
    # off=不注入 / tree=只注入文件树（不读文件内容）/ full=文件树+预算内文件内容。
    # 旧前端只传 workspace_context 布尔值：True→full，False→off。
    workspace_context_mode = data.get("workspace_context_mode")
    if workspace_context_mode not in ("off", "tree", "full"):
        workspace_context_mode = "full" if workspace_context else "off"
    # ── 工具开关解耦 ──
    # enable_tools 独立于 workspace_context：不开工作区注入也能用 Agent 工具。
    # 旧前端未传 enable_tools 时回退到 workspace_context，保持兼容。
    enable_tools = data.get("enable_tools")
    if enable_tools is None:
        enable_tools = workspace_context
    enable_tools = bool(enable_tools)
    # ── Agent 办公参数（B2/B3/A3/B5，管理端 dsh 对话页面配置）──
    agent_cfg = get_agent_config(db)
    # B5：同步并发限制（全局 + 每用户），管理端保存后即时生效
    agent_concurrency.configure(
        agent_cfg.get("agent_concurrent_limit", 4),
        agent_cfg.get("agent_per_user_concurrent", 1),
    )
    # B1/B：任务取消事件 — 用户停止按钮 → POST /ai/tasks/{task_id}/cancel 置位；
    # SSE 断开（刷新/断线）不再自动取消任务（方案 B：任务后台继续跑，结果可查）
    cancel_event = _threading.Event()
    task_id = f"task-{uuid.uuid4().hex[:12]}"
    with _task_cancel_guard:
        _task_cancel_events[task_id] = cancel_event
    # ── 会话级已加载技能列表（前端 localStorage 持久化）──
    # 清洗：只保留非空字符串，最多 20 个，防止滥用
    raw_loaded = data.get("loaded_skills") or []
    loaded_skills = [str(x).strip() for x in raw_loaded if isinstance(x, str) and x.strip()][:20]
    # ── 技能全文分级注入：首轮（技能加载后第一次发送）注入完整指令，
    # 后续轮次只注入摘要（避免大技能全文反复占用上下文 / 首 token 慢 / 超时重载）──
    skills_first_round = bool(data.get("skills_first_round", True))
    visitor_id = f"user:{user.id}"

    # 频率检查（每日配额持久化到 DB，分钟限流走内存）
    ip = _get_client_ip(request)
    limits = _get_rate_limits(db)
    chat_daily_limit = _get_chat_daily_limit(db)
    allowed, err = check_daily_quota(db, user.id, "chat", chat_daily_limit)
    if not allowed:
        raise HTTPException(status_code=429, detail=err)
    allowed, err = rate_limiter.check(
        visitor_id, ip, 10**9, limits.chat_minute_limit,
    )
    if not allowed:
        raise HTTPException(status_code=429, detail=err)

    # Prompt Injection 检测（方案一）
    is_safe, reason = prompt_guard.check(message, user_key=visitor_id)
    if not is_safe:
        raise HTTPException(status_code=400, detail=reason)

    # 通过即计一次当日配额（持久化到 DB，重启不丢）
    increment_daily_usage(db, user.id, "chat")

    session_id = session_id_in or f"chat-{uuid.uuid4().hex[:12]}"

    # 注册/更新数据库会话，且必须属于当前用户
    if session_id_in:
        existing = (
            db.query(ChatSession)
            .filter(ChatSession.session_id == session_id_in, ChatSession.visitor_id == visitor_id)
            .first()
        )
        if not existing:
            new_session = ChatSession(
                session_id=session_id_in,
                visitor_id=visitor_id,
                title=message[:50] + ("..." if len(message) > 50 else ""),
                model_id=model_id,
                is_active=True,
            )
            db.add(new_session)
            db.commit()
        else:
            existing.title = message[:50] + ("..." if len(message) > 50 else "")
            if model_id:
                existing.model_id = model_id
            db.commit()
    else:
        new_session = ChatSession(
            session_id=session_id,
            visitor_id=visitor_id,
            title=message[:50] + ("..." if len(message) > 50 else ""),
            model_id=model_id,
            is_active=True,
        )
        db.add(new_session)
        db.commit()

    # 提前提取 user_id，避免在 worker 线程中访问 detached 的 user 对象
    _user_id = user.id

    # ── 方案 B：创建 Agent 任务记录（后台执行，刷新/断线不丢）──
    try:
        db.add(AgentTask(
            task_id=task_id, user_id=_user_id, session_id=session_id,
            message=message[:2000], status="running",
            enable_tools=enable_tools,  # 统计侧区分 Agent 任务与纯聊天（#9）
        ))
        db.commit()
    except Exception:
        _log.getLogger(__name__).warning("创建 Agent 任务记录失败", exc_info=True)

    # 用 asyncio.Queue 在 LLM 子线程和 SSE 生成器之间传递数据
    # 比 sync Queue + run_in_threadpool 快得多（无线程池调度开销）
    q: asyncio.Queue = asyncio.Queue()
    _SENTINEL = object()
    loop = asyncio.get_event_loop()

    # ── 优化4：自动上下文压缩 ──
    # 历史压力超过阈值（默认策略会丢弃旧消息）时，先调用 LLM 生成 7 维度结构化摘要
    # 再发送，替代直接丢弃——既控制 token 成本又保留对话连续性。
    # 仅历史足够长（≥10 条）才触发；压缩失败回退默认丢弃策略，不阻塞对话。
    if history and len(history) >= 10:
        try:
            from ...services.llm_service import llm_service as _llm_svc
            from ...services.compaction_engine import (
                select_history_window,
                DEFAULT_THRESHOLD_RATIO, DEFAULT_RETAIN_RATIO,
            )
            _clean = [
                m for m in history
                if m.get("role") in ("user", "assistant") and m.get("content")
            ]
            _persona = _llm_svc._get_persona(db)
            _ctx_len = 0
            try:
                _ctx_len = _llm_svc._resolve_model(model_id, db)[4]
            except Exception:
                _ctx_len = 0
            if _ctx_len > 0:
                _, _ctx = select_history_window(
                    messages=_clean, system_prompt=_persona,
                    context_window=_ctx_len, threshold_ratio=DEFAULT_THRESHOLD_RATIO,
                    retain_ratio=DEFAULT_RETAIN_RATIO, max_output_tokens=4096,
                )
                if _ctx.get("compacted"):
                    _cr = await _llm_svc.compact_session_async(
                        session_id, db, model_id=model_id, history=history,
                    )
                    if _cr.get("compacted") and _cr.get("response"):
                        # 摘要作为首条 user 消息（含 <compacted-summary> 标签），保留最近 6 条
                        history = [{"role": "user", "content": _cr["response"]}] + history[-6:]
                        q.put_nowait(
                            f"📦 上下文已自动压缩（{_cr.get('shadowed_count', 0)} 条历史 → 摘要，"
                            f"节省约 {_cr.get('saved_tokens', 0)} tokens）\n\n")
                        _log.getLogger(__name__).info(
                            f"[req:{request_id}] 自动压缩上下文: shadowed={_cr.get('shadowed_count')} "
                            f"saved={_cr.get('saved_tokens')} tokens"
                        )
        except Exception as _e:
            _log.getLogger(__name__).warning(
                f"[req:{request_id}] 自动上下文压缩失败（回退默认策略）: {_e}")

    def _worker():
        """在子线程中运行直连 LLM API 的流式对话。"""
        concurrency_held = False
        worker_db = None
        # 工具调用痕迹（#10）：声明在 try 外——异常发生在任何工具调用之前时，
        # except 分支引用空列表而不是 NameError
        tool_events: list[dict] = []
        # 模型降级链触发记录（#9 运营统计）：同样声明在 try 外，理由同上
        model_switches: list[dict] = []
        try:
            from ...core.database import SessionLocal
            from ...services.llm_service import llm_service
            from ...services.workspace_service import render_workspace_context
            worker_db = SessionLocal()

            # ── B5：Agent 并发限制（仅 Agent 模式参与排队；纯聊天已有分钟限流）──
            if enable_tools:
                if not agent_concurrency.acquire(_user_id):
                    raise RuntimeError("系统繁忙，Agent 任务已满，请稍后再试")
                concurrency_held = True

            # ── 工作区上下文注入 ──
            # 借鉴 DSH WorkspaceContext: 在对话前将工作区文件列表+内容注入 system prompt
            # 三档（注入开关优化3）：off 完全不注入；tree 只注入文件树（不读内容）；
            # full 注入文件树 + 预算内文件内容。
            workspace_ctx_text = ""
            if workspace_context_mode != "off":
                try:
                    # 传入用户消息作为关键词，优先注入与问题相关的文件
                    # A3：injection_guard 开启时，命中注入模式的文件内容不注入
                    workspace_ctx_text = render_workspace_context(
                        _user_id, worker_db, query=message,
                        injection_guard=bool(agent_cfg.get("agent_workspace_guard", True)),
                        mode=workspace_context_mode,
                    )
                except Exception as e:
                    import logging as _log
                    _log.getLogger(__name__).warning(f"工作区上下文渲染失败: {e}")

            # G：Agent 模式下 AI 生成的文件默认写入工作区（只提示路径），
            # 仅当用户消息明确要求"发送/下载"时才发聊天框文件卡片。
            _allow_delivery = enable_tools and _user_wants_file_delivery(message)

            def _on_file(name, content, mime):
                if enable_tools and not _allow_delivery and content:
                    try:
                        _write_generated_to_workspace(_user_id, name, content, mime, worker_db)
                        loop.call_soon_threadsafe(
                            q.put_nowait,
                            f"\n\n📁 已生成文件：{name}（已写入工作区，可在工作区面板查看/下载）")
                        return
                    except Exception:
                        import logging as _log
                        _log.getLogger(__name__).warning(
                            "AI 文件写入工作区失败，回退聊天卡片", exc_info=True)
                loop.call_soon_threadsafe(
                    q.put_nowait, ("__file__", _file_event(_user_id, name, content, mime)))

            # ── 工具调用痕迹累积（#10：刷新/断线恢复时前端凭此渲染工具卡片）──
            def _on_tool_call(tool_name, args, call_id, round_no, max_rounds):
                redacted_args = redact_value(args)
                tool_events.append({
                    "id": call_id, "tool": tool_name, "arguments": redacted_args,
                    "round": round_no, "max_rounds": max_rounds,
                })
                loop.call_soon_threadsafe(
                    q.put_nowait, ("__tool_call__", {
                        "tool": tool_name,
                        # ── 敏感信息脱敏：工具参数回显前统一过滤路径/密钥（仅影响展示，不影响工具执行）──
                        "arguments": redacted_args,
                        "id": call_id,
                        # B2：轮次信息（前端显示"第 N/M 轮"）
                        "round": round_no,
                        "max_rounds": max_rounds,
                    }))

            def _on_tool_result(tool_name, result_text, call_id):
                # 先整块剥离图片 data URL 再截断/脱敏：read_file 读图与扫描件 PDF 会返回
                # 数 MB base64，不剥离则 UI 工具卡片显示乱码、每条 AgentTask 白涨 1.5KB。
                # 只影响展示与持久化 —— 模型侧走 llm_service 的工具原始返回值，图片照常可看。
                result_text = _IMAGE_NAME_RE.sub(
                    lambda m: f"[图片：{m.group(1)}，已传给视觉模型]", result_text
                )
                redacted_result = redact(
                    (result_text[:8000] + f"\n\n...[工具结果已截断，共 {len(result_text)} 字符]")
                    if len(result_text) > 8000 else result_text,
                )
                # 按 id 归属到对应调用事件（找不到则补一条，结果不丢）
                for _ev in tool_events:
                    if _ev.get("id") == call_id and _ev.get("tool") == tool_name:
                        _ev["result"] = redacted_result
                        break
                else:
                    tool_events.append({
                        "id": call_id, "tool": tool_name, "result": redacted_result,
                    })
                loop.call_soon_threadsafe(
                    q.put_nowait, ("__tool_result__", {
                        "tool": tool_name,
                        # ── 敏感信息脱敏：结果文本（路径/密钥）统一模糊，展示与持久化均不落地原文 ──
                        "result": redacted_result,
                        "id": call_id,
                    }))

            # ── 模型降级链触发记录（#9）：统计侧聚合降级频次与目标模型 ──
            def _on_model_switch(name, status):
                model_switches.append({"to": name, "status": status})
                loop.call_soon_threadsafe(
                    q.put_nowait, ("__model_switch__", {"name": name, "status": status}))

            result = llm_service.chat_stream(
                message, worker_db,
                session_id=session_id, model_id=model_id,
                history=history,
                on_chunk=lambda text: loop.call_soon_threadsafe(q.put_nowait, text),
                on_file=_on_file,
                workspace_context=workspace_ctx_text,
                workspace_context_mode=workspace_context_mode,
                # ── 方案一：Agent 工具循环 ──
                user_id=_user_id,
                enable_tools=enable_tools,
                loaded_skills=loaded_skills,
                cancel_event=cancel_event,  # B1：用户停止 → 中断工具循环
                request_id=request_id,
                on_model_switch=_on_model_switch,
                on_tool_call=_on_tool_call,
                on_tool_result=_on_tool_result,
                # ── 工具实时输出流（run_python stdout，带工具调用 id）──
                on_tool_progress=lambda text, call_id: loop.call_soon_threadsafe(
                    q.put_nowait, ("__tool_progress__", {"text": redact(text), "id": call_id})),
                # ── 推理内容流（reasoning_content）──
                on_reasoning=lambda text: loop.call_soon_threadsafe(
                    q.put_nowait, ("__reasoning__", {"text": text})),
                skills_first_round=skills_first_round,
                task_timeout_seconds=_AGENT_TASK_TIMEOUT_SECONDS,
            )
            if not result.get("session_id"):
                result["session_id"] = session_id
            loop.call_soon_threadsafe(q.put_nowait, ("__result__", result))
            # 方案 B：任务完成，回写结果（供刷新后查询）
            _update_task_status(
                worker_db, task_id, "done",
                result_text=result.get("response") or (result.get("message") or ""),
                tool_events=tool_events, model_switches=model_switches,
                # 性能优化1：任务级 token 用量落库（成本审计）
                token_usage={
                    "prompt_tokens": result.get("task_prompt_tokens") or 0,
                    "completion_tokens": result.get("task_completion_tokens") or 0,
                    "total_tokens": result.get("task_total_tokens") or 0,
                    # 性能优化7：缓存命中 tokens
                    "cache_read_tokens": result.get("task_cache_read_tokens") or 0,
                },
            )
        except AgentCancelled:
            # 用户主动停止（cancel 端点）— 前端已断开连接，静默结束，不再推送任何事件
            import logging as _log
            _log.getLogger(__name__).info(f"Agent 任务被用户取消 session={session_id}")
            _update_task_status(worker_db, task_id, "cancelled",
                                tool_events=tool_events, model_switches=model_switches)
        except Exception as e:
            loop.call_soon_threadsafe(q.put_nowait, ("__error__", str(e)))
            _update_task_status(worker_db, task_id, "failed", error=str(e),
                                tool_events=tool_events, model_switches=model_switches)
        finally:
            # B5：释放并发槽位（必须与 acquire 配对）
            if concurrency_held:
                agent_concurrency.release(_user_id)
            if worker_db is not None:
                worker_db.close()
            with _task_cancel_guard:
                _task_cancel_events.pop(task_id, None)
            loop.call_soon_threadsafe(q.put_nowait, _SENTINEL)

    thread = _threading.Thread(target=_worker, daemon=True)
    thread.start()

    from fastapi.responses import StreamingResponse

    # ── 累积 reasoning 文本，流结束后持久化到数据库 ──
    reasoning_buffer: list[str] = []

    async def _event_stream():
        """SSE 异步生成器 — 从 asyncio.Queue 读取 LLM 推送的文本片段。

        防御：生成器任何异常都先给客户端发 error 事件（避免静默断流
        导致前端出现无法定位的"对话异常结束"），再记录日志。
        """
        try:
            async for evt in _event_stream_inner():
                yield evt
        except asyncio.CancelledError:
            # 客户端断开（刷新/断线/停止）— 方案 B：不置位取消事件，
            # 任务在后台继续执行，刷新后凭 task_id 查询进度与结果。
            raise
        except Exception as e:
            import logging as _log
            _log.getLogger(__name__).exception("SSE 生成器异常（对话流中断）")
            yield f"data: {_json.dumps({'type': 'error', 'error': f'对话流异常中断: {type(e).__name__}: {e}', 'request_id': request_id}, ensure_ascii=False)}\n\n"

    async def _event_stream_inner():
        # 首事件：任务 ID（前端存储；刷新/断线后凭此查询后台任务进度与结果）
        yield f"data: {_json.dumps({'type': 'task', 'task_id': task_id, 'request_id': request_id}, ensure_ascii=False)}\n\n"
        while True:
            try:
                # 直接 await asyncio.Queue.get，无线程池开销
                item = await asyncio.wait_for(q.get(), timeout=15.0)
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"
                continue

            if item is _SENTINEL:
                break

            if isinstance(item, tuple) and len(item) == 2:
                tag, payload = item
                if tag == "__result__":
                    result = payload
                    # ── 流结束：将累积的 reasoning 写入数据库 ──
                    reasoning_text = "".join(reasoning_buffer)
                    if reasoning_text:
                        try:
                            from ...core.database import SessionLocal as _RS_Local
                            _rs_db = _RS_Local()
                            try:
                                _sess = _rs_db.query(ChatSession).filter(
                                    ChatSession.session_id == session_id
                                ).first()
                                if _sess:
                                    _sess.last_reasoning = reasoning_text[:50000]
                                    _rs_db.commit()
                            finally:
                                _rs_db.close()
                        except Exception:
                            import logging as _log
                            _log.getLogger(__name__).warning("写入 last_reasoning 失败", exc_info=True)
                    yield f"data: {_json.dumps({'type': 'done', **result}, ensure_ascii=False)}\n\n"
                    break
                elif tag == "__error__":
                    yield f"data: {_json.dumps({'type': 'error', 'error': payload, 'request_id': request_id}, ensure_ascii=False)}\n\n"
                    break
                elif tag == "__file__":
                    # 文件事件 — 将文件信息通过 SSE 发送给前端
                    yield f"data: {_json.dumps({'type': 'file', **payload}, ensure_ascii=False)}\n\n"
                elif tag == "__tool_call__":
                    # 工具调用事件 — 通知前端 AI 正在调用工具
                    yield f"data: {_json.dumps({'type': 'tool_call', **payload}, ensure_ascii=False)}\n\n"
                elif tag == "__tool_result__":
                    # 工具结果事件 — 将执行结果发送给前端
                    yield f"data: {_json.dumps({'type': 'tool_result', **payload}, ensure_ascii=False)}\n\n"
                elif tag == "__tool_progress__":
                    # 工具实时输出流（run_python stdout）
                    yield f"data: {_json.dumps({'type': 'tool_progress', **payload}, ensure_ascii=False)}\n\n"
                elif tag == "__reasoning__":
                    # 推理内容流（reasoning_content）— 累积到 buffer 供后续持久化
                    reasoning_buffer.append(payload.get("text", ""))
                    yield f"data: {_json.dumps({'type': 'reasoning', **payload}, ensure_ascii=False)}\n\n"
                elif tag == "__model_switch__":
                    # 模型自动降级（HTTP 401/402/403/429 等）— 通知前端同步模型栏
                    yield f"data: {_json.dumps({'type': 'model_switch', **payload}, ensure_ascii=False)}\n\n"
            elif isinstance(item, str) and item:
                yield f"data: {_json.dumps({'type': 'chunk', 'text': item}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/ai/chat/compact")
async def compact_chat_context(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """手动触发 DSH 风格上下文压缩 — 7 维度结构化摘要。"""
    import json
    body = await _read_body_capped(request)
    try:
        data = json.loads(body) if body else {}
    except Exception:
        data = {}
    session_id = data.get("session_id")
    model_id = data.get("model_id")
    raw_history = data.get("history") or []
    history = _sanitize_history(raw_history)

    if not session_id:
        raise HTTPException(status_code=400, detail="缺少 session_id")

    # 校验会话归属：确保 session_id 属于当前用户
    visitor_id = f"user:{user.id}"
    own_session = (
        db.query(ChatSession)
        .filter(ChatSession.session_id == session_id, ChatSession.visitor_id == visitor_id)
        .first()
    )
    if not own_session:
        raise HTTPException(status_code=403, detail="无权操作此会话")

    try:
        from ...services.llm_service import llm_service
        # 直接调用 async 版本 — DSH 风格压缩
        result = await llm_service.compact_session_async(
            session_id, db, model_id=model_id, history=history,
        )
        return result
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"上下文压缩异常: {e}")


@router.get("/ai/chat/models", response_model=list[ChatModelRead])
async def get_chat_models(db: Session = Depends(get_db)):
    """获取前端可用的对话模型列表（仅活跃的，按默认优先排序）。"""
    results = (
        db.query(DshChatModel, AIProvider)
        .join(AIProvider, DshChatModel.provider_id == AIProvider.id)
        .filter(DshChatModel.is_active == True, AIProvider.is_active == True)
        .order_by(DshChatModel.is_default.desc(), DshChatModel.created_at.asc())
        .all()
    )
    return [
        ChatModelRead(
            id=m.id,
            provider_id=m.provider_id,
            provider_name=p.name,
            name=m.name,
            display_name=m.display_name,
            is_active=m.is_active,
            is_default=m.is_default,
            supports_vision=m.supports_vision,
            context_length=m.context_length or 0,
            reasoning_effort=m.reasoning_effort or "off",
            created_at=m.created_at.strftime("%Y-%m-%d %H:%M:%S") if m.created_at else "",
        )
        for m, p in results
    ]


@router.get("/ai/chat/skills")
async def list_available_skills(db: Session = Depends(get_db)):
    """获取当前可用的技能列表（仅活跃技能的摘要，供前端展示/手动加载）。

    content_chars 只回传正文长度、不回传正文本身：前端用它估算注入成本
    （技能面板的体积提示与预算占用条），同时避免技能指令内容外泄。
    """
    from ...services.skill_service import get_active_skills
    skills = get_active_skills(db)
    return [
        {
            "name": s.name,
            "description": s.description,
            "has_resources": bool(s.dir_path),  # 目录形式技能（含参考文件）
            "category": s.category or "",       # 分类标签（空=未分类）
            "pack": s.pack or "",               # 所属技能包（空=独立技能）
            "content_chars": len(s.content or ""),
        }
        for s in skills
    ]


@router.get("/ai/chat/quota", response_model=QuotaResponse)
async def get_chat_quota(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """查询当前用户对话剩余次数。"""
    ip = _get_client_ip(request)
    visitor_id = f"user:{user.id}"
    limits = _get_rate_limits(db)
    chat_daily_limit = _get_chat_daily_limit(db)
    # 从数据库读取当日实际消费（重启不丢）
    from ...services.usage_service import get_daily_usage
    used = get_daily_usage(db, user.id, "chat")

    return QuotaResponse(
        image=QuotaItem(
            daily_limit=chat_daily_limit,
            used=used,
            remaining=max(0, chat_daily_limit - used),
        ),
    )


@router.get("/ai/chat/config")
async def get_chat_config(db: Session = Depends(get_db)):
    """获取前端对话配置（公开接口，仅返回访客需要的字段）。"""
    config = db.query(DshConfig).filter(DshConfig.id == 1).first()
    if not config:
        config = DshConfig(id=1)
        db.add(config)
        db.commit()
        db.refresh(config)
    return {
        "max_files_per_message": config.max_files_per_message if config else 5,
    }


@router.get("/ai/chat/sessions/{session_id}/reasoning")
async def get_session_reasoning(
    session_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """获取会话最近一次 AI 回复的推理过程（reasoning_content）。

    前端刷新页面后，从 localStorage 恢复的消息可能缺少 reasoning 字段
    （旧数据或存储裁剪导致），此接口提供后端兜底恢复。
    """
    visitor_id = f"user:{user.id}"
    session = (
        db.query(ChatSession)
        .filter(ChatSession.session_id == session_id, ChatSession.visitor_id == visitor_id)
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {
        "session_id": session_id,
        "reasoning": session.last_reasoning or "",
    }


# ════════════════════════════════════════
#  Agent 长任务（方案 B）：后台执行，刷新/断线可查
# ════════════════════════════════════════

# 终态任务状态（running 之外的都视为已结束）
_TERMINAL_TASK_STATUSES = ("done", "failed", "cancelled")


def cleanup_old_agent_tasks(db: Session, days: int = 2) -> int:
    """删除 N 天前已结束（done/failed/cancelled）的 Agent 任务记录，返回删除条数。

    每次对话都会插入一条任务记录（result 最长 2 万字符），不清理会无限增长。
    running 状态不删：正常任务 20 分钟内必到终态，长期 running 说明进程异常
    死亡，保留现场便于排查。main.py 启动时调用一次，此后每日循环。

    #9 运营统计：删除前先把这批任务按天滚动汇总进 dsh_agent_daily_stats，
    与删除同一事务提交（要么都生效、要么都回滚），保证恰好落账一次；
    落账抛异常则本次不删除，等调度器 1 小时后重试，宁可慢删不可丢账。
    """
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=days)

    def _stale():
        return db.query(AgentTask).filter(
            AgentTask.status.in_(_TERMINAL_TASK_STATUSES),
            AgentTask.finished_at < cutoff,
        )

    rows = _stale().all()
    if not rows:
        return 0
    from ...services.agent_stats_service import rollup_tasks_to_daily_stats
    rollup_tasks_to_daily_stats(db, rows)
    deleted = _stale().delete(synchronize_session=False)
    db.commit()
    return deleted


@router.get("/ai/tasks/{task_id}")
async def get_agent_task(
    task_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """查询后台任务状态与结果（刷新页面后恢复进度用）。"""
    task = db.query(AgentTask).filter(AgentTask.task_id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.user_id != user.id:
        raise HTTPException(status_code=403, detail="无权访问此任务")
    # 工具调用痕迹（#10）：JSON 数组解析失败时回退空列表，不阻塞状态查询
    try:
        tool_events = _json.loads(task.tool_events) if task.tool_events else []
        if not isinstance(tool_events, list):
            tool_events = []
    except (ValueError, TypeError):
        tool_events = []
    return {
        "task_id": task.task_id,
        "status": task.status,
        "result": task.result,
        "tool_events": tool_events,
        "error": task.error,
        "created_at": task.created_at.strftime("%Y-%m-%d %H:%M:%S") if task.created_at else "",
        "finished_at": task.finished_at.strftime("%Y-%m-%d %H:%M:%S") if task.finished_at else "",
    }


@router.post("/ai/tasks/{task_id}/cancel")
async def cancel_agent_task(
    task_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """取消后台任务（用户点击停止按钮时前端调用）。"""
    task = db.query(AgentTask).filter(AgentTask.task_id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.user_id != user.id:
        raise HTTPException(status_code=403, detail="无权操作此任务")
    with _task_cancel_guard:
        ev = _task_cancel_events.get(task_id)
    if ev:
        ev.set()
    if task.status == "running":
        task.status = "cancelled"
        task.finished_at = datetime.datetime.utcnow()
        db.commit()
    return {"message": "已请求取消", "task_id": task_id}
