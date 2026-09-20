from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import logging
import re
import datetime
import httpx
import sys
import threading
from pathlib import Path

from ...core.database import get_db
from ...models import AIProvider, AIRateLimit, DshConfig, DshChatModel, DshEmbeddingModel, DshSkill, User, ChatSession
from ...schemas.ai import (
    ProviderCreate,
    ProviderRead,
    ProviderUpdate,
    RateLimitRead,
    RateLimitUpdate,
    DshConfigRead,
    DshConfigUpdate,
    ChatModelCreate,
    ChatModelRead,
    ChatModelUpdate,
    ChatModelTestRequest,
    ChatModelTestResult,
    EmbeddingModelCreate,
    EmbeddingModelRead,
    EmbeddingModelUpdate,
    EmbeddingCurrentUpdate,
    ModelDiscoveryRequest,
    ModelDiscoveryResponse,
    DiscoveredModelItem,
    SkillCreate,
    SkillRead,
    SkillUpdate,
    PackUploadResult,
    RepoImportResult,
    RepoSkillItem,
)

router = APIRouter()
logger = logging.getLogger(__name__)


def _mask_key(key: str) -> str:
    """掩码 API Key。"""
    if len(key) <= 8:
        return "****"
    return key[:4] + "*" * (len(key) - 8) + key[-4:]


def _to_provider_read(model: AIProvider) -> ProviderRead:
    """将 ORM 模型转为 ProviderRead，掩码 API Key。"""
    return ProviderRead(
        id=model.id,
        name=model.name,
        base_url=model.base_url,
        api_key_masked=_mask_key(model.api_key),
        api_type=model.api_type or "openai",
        is_active=model.is_active,
        created_at=model.created_at.strftime("%Y-%m-%d %H:%M:%S") if model.created_at else "",
    )


# ════════════════════════════════════════
#  厂商管理 CRUD
# ════════════════════════════════════════

@router.get("/admin/ai/providers", response_model=list[ProviderRead])
def list_providers(db: Session = Depends(get_db)):
    """获取所有厂商配置。"""
    providers = db.query(AIProvider).order_by(AIProvider.created_at.desc()).all()
    return [_to_provider_read(p) for p in providers]


@router.post("/admin/ai/providers", response_model=ProviderRead)
def create_provider(payload: ProviderCreate, db: Session = Depends(get_db)):
    """新增厂商。"""
    model = AIProvider(
        name=payload.name,
        base_url=payload.base_url,
        api_key=payload.api_key,
        api_type=payload.api_type or "openai",
        is_active=payload.is_active,
    )
    db.add(model)
    db.commit()
    db.refresh(model)
    return _to_provider_read(model)


@router.put("/admin/ai/providers/{provider_id}", response_model=ProviderRead)
def update_provider(provider_id: int, payload: ProviderUpdate, db: Session = Depends(get_db)):
    """编辑厂商。"""
    model = db.query(AIProvider).filter(AIProvider.id == provider_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="厂商不存在")

    if payload.name is not None:
        model.name = payload.name
    if payload.base_url is not None:
        model.base_url = payload.base_url
    if payload.api_key is not None:
        model.api_key = payload.api_key
    if payload.api_type is not None:
        model.api_type = payload.api_type
    if payload.is_active is not None:
        model.is_active = payload.is_active

    db.commit()
    db.refresh(model)
    return _to_provider_read(model)


@router.delete("/admin/ai/providers/{provider_id}")
def delete_provider(provider_id: int, db: Session = Depends(get_db)):
    """删除厂商（同时删除其下对话模型与嵌入模型，并解除相关引用）。"""
    model = db.query(AIProvider).filter(AIProvider.id == provider_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="厂商不存在")
    # 级联删除其下对话模型：先解除历史会话对模型的引用（外键约束），再删除
    chat_model_ids = [
        m.id
        for m in db.query(DshChatModel).filter(DshChatModel.provider_id == provider_id).all()
    ]
    removed_chat_models = len(chat_model_ids)
    if chat_model_ids:
        db.query(ChatSession).filter(ChatSession.model_id.in_(chat_model_ids)).update(
            {ChatSession.model_id: None}
        )
        db.query(DshChatModel).filter(DshChatModel.provider_id == provider_id).delete()
    # 级联删除其下嵌入模型（记忆模块）。
    # 必须显式删：dsh_embedding_models.provider_id 指向 ai_providers 有外键，
    # 不先删子记录会让整个删除操作 IntegrityError 失败（500）。
    # 同时要解除 DshConfig 的引用，否则留下悬空 id —— 表现为「管理端显示已配置、
    # 实际取不到模型」，记忆语义检索静默降级为关键词匹配，很难排查。
    embedding_ids = [
        m.id
        for m in db.query(DshEmbeddingModel)
        .filter(DshEmbeddingModel.provider_id == provider_id).all()
    ]
    removed_embeddings = len(embedding_ids)
    cleared_embedding_config = False
    if embedding_ids:
        cfg = db.query(DshConfig).filter(DshConfig.id == 1).first()
        if cfg and cfg.embedding_model_id is not None and cfg.embedding_model_id in embedding_ids:
            cfg.embedding_model_id = None
            cleared_embedding_config = True
        db.query(DshEmbeddingModel).filter(
            DshEmbeddingModel.provider_id == provider_id
        ).delete()
    db.delete(model)
    db.commit()

    return {
        "message": "删除成功",
        "removed_chat_models": removed_chat_models,
        "removed_embedding_models": removed_embeddings,
        "cleared_embedding_config": cleared_embedding_config,
    }


# ════════════════════════════════════════
#  频率限制（含并发限制和时段限制）
# ════════════════════════════════════════

@router.get("/admin/ai/rate-limits", response_model=RateLimitRead)
def get_rate_limits(db: Session = Depends(get_db)):
    """获取频率限制配置。"""
    limits = db.query(AIRateLimit).filter(AIRateLimit.id == 1).first()
    if not limits:
        limits = AIRateLimit(id=1)
        db.add(limits)
        db.commit()
        db.refresh(limits)
    return limits


@router.put("/admin/ai/rate-limits", response_model=RateLimitRead)
def update_rate_limits(payload: RateLimitUpdate, db: Session = Depends(get_db)):
    """修改频率限制。"""
    limits = db.query(AIRateLimit).filter(AIRateLimit.id == 1).first()
    if not limits:
        limits = AIRateLimit(id=1)
        db.add(limits)
    limits.max_concurrent = payload.max_concurrent
    db.commit()
    db.refresh(limits)
    return limits


# ════════════════════════════════════════
#  访客统计
# ════════════════════════════════════════

@router.get("/admin/ai/visitors")
def list_visitors(db: Session = Depends(get_db)):
    """获取访客使用情况列表，包含用户邮箱。"""
    from ...services.rate_limiter import rate_limiter
    visitors = rate_limiter.get_visitors(limit=50)
    # 批量查询用户邮箱
    user_ids = [v["user_id"] for v in visitors if v.get("user_id")]
    email_map = {}
    if user_ids:
        users = db.query(User).filter(User.id.in_(user_ids)).all()
        email_map = {u.id: u.email for u in users}
    for v in visitors:
        uid = v.get("user_id")
        v["email"] = email_map.get(uid, "") if uid else ""
    return visitors


@router.get("/admin/ai/stats")
def get_stats():
    """获取全局统计数据。"""
    from ...services.rate_limiter import rate_limiter
    return rate_limiter.get_stats()


@router.get("/admin/ai/agent-stats")
def agent_stats(days: int = 30, db: Session = Depends(get_db)):
    """Agent 运营统计（#9）：任务量/成功率/时长/轮数分布/工具失败率/降级链/活跃用户。

    数据 = dsh_agent_tasks 实时行（保留 2 天）+ dsh_agent_daily_stats 每日滚动
    汇总（清理前落账），按日期相加即全量；days 参数控制统计窗口（1-90 天）。
    """
    from ...services.agent_stats_service import get_agent_stats
    return get_agent_stats(db, days)


# ════════════════════════════════════════
#  DSH 对话配置
# ════════════════════════════════════════

def _vendor_reason(resp) -> str:
    """从厂商响应里提一句**人话**的失败原因。

    直接 dump 原始响应会很难看：有的厂商（如某些网关）返回整页 HTML 错误页，
    塞进前端提示就是一大坨标签；有的返回嵌套 JSON，读起来也费劲。
    优先级：JSON 里的 error.message / message > 一句"网关返回网页"的提示 > 截断文本。
    返回空串表示没有可用信息（如 404 空 body）—— 调用方据此不拼接。
    """
    raw = (resp.text or "").strip()
    if not raw:
        return ""
    # 1) 标准 JSON 错误体：各家字段名不一，按常见位置依次取
    try:
        j = resp.json()
        if isinstance(j, dict):
            err = j.get("error")
            if isinstance(err, dict):
                for k in ("message", "msg", "detail"):
                    if isinstance(err.get(k), str) and err[k].strip():
                        return err[k].strip()[:140]
            if isinstance(err, str) and err.strip():
                return err.strip()[:140]
            for k in ("message", "msg", "detail"):
                if isinstance(j.get(k), str) and j[k].strip():
                    return j[k].strip()[:140]
    except Exception:
        pass
    # 2) HTML 错误页（网关/反代/负载均衡挡下来的，通常意味着接口路径不对）
    low = raw[:300].lower()
    if low.startswith("<!doctype") or low.startswith("<html") or "<html" in low:
        return "厂商网关返回的是网页错误页，而非接口响应（接口路径可能不正确）"
    # 3) 其它纯文本：截断（顺带压掉换行，避免前端提示被撑高）
    return " ".join(raw[:140].split())


def _probe_embeddings(provider: AIProvider, model_name: str) -> int:
    """实调一次 /embeddings 探针，成功返回向量维度，失败抛 400。

    探针是必须的：普通对话模型（如 deepseek-chat）调 /embeddings 不会"静默忽略"，
    而是直接 4xx；不探针的话用户以为配好了，其实每次生成向量都失败并退回关键词匹配，
    表现为"语义检索配了但没效果"，很难排查。

    顺带把实测维度返回给调用方落库 —— 换模型后维度不匹配会让已有记忆的相似度恒为 0
    （旧的沉底），有维度记录才能一眼看出问题。
    """
    import httpx
    try:
        url = f"{provider.base_url.rstrip('/')}/embeddings"
        r = httpx.Client(timeout=15.0).post(url, json={
            "model": model_name, "input": "probe",
        }, headers={"Authorization": f"Bearer {provider.api_key}",
                    "Content-Type": "application/json"})
    except Exception as e:
        raise HTTPException(status_code=400,
            detail=f"无法连接厂商「{provider.name}」（{type(e).__name__}）：{e}")

    emb = None
    if r.status_code == 200:
        try:
            emb = ((r.json().get("data") or [{}])[0] or {}).get("embedding")
        except Exception:
            emb = None
    if r.status_code != 200 or not emb or not isinstance(emb, list):
        # 按状态码给出准确的诊断。别把"账户余额不足/限流"说成"模型不支持"——
        # 那会让管理员去换模型，而真正该做的是充值或等限流窗口过去。
        sc = r.status_code
        if sc in (401, 403):
            _why = (f"「{provider.name}」认证失败（HTTP {sc}）—— "
                    f"API Key 无效或已过期，请到「AI 管理」更新该厂商的 Key")
        elif sc == 429:
            _why = (f"「{provider.name}」限流，或账户余额/配额不足（HTTP 429）—— "
                    f"Key 本身有效，请稍后重试或为该厂商充值")
        elif sc == 404:
            _why = (f"「{provider.name}」找不到该接口或模型（HTTP 404）—— "
                    f"请确认模型标识拼写正确，且厂商 base_url 完整"
                    f"（如 base_url 是否漏了 /v1 之类的路径段）")
        elif sc == 400:
            _why = (f"「{provider.name}」拒绝了该请求（HTTP 400）—— "
                    f"模型标识可能不存在，或该模型不支持 /embeddings")
        elif sc >= 500:
            _why = f"「{provider.name}」服务端异常（HTTP {sc}）—— 请稍后重试"
        elif sc == 200:
            _why = "该模型返回 200，但响应体里没有合法的向量（期望 data[0].embedding）"
        else:
            _why = f"无法用该模型生成向量（HTTP {sc}）"
        _raw = _vendor_reason(r)
        # 附加一条"该怎么办"的指引，与具体状态码相关，避免每次都是一大段通用说明
        if sc in (400, 404):
            _hint = "记忆向量检索需要真正支持 /embeddings 的模型（如 text-embedding-*、bge-*、embedding-*）。"
        elif sc == 429:
            _hint = "也可改用其他有余额的厂商新建一个嵌入模型。"
        elif sc in (401, 403):
            _hint = "可到「AI 管理」页重新填写该厂商的 API Key。"
        else:
            _hint = ""
        raise HTTPException(status_code=400,
            detail=f"{_why}。" + (f"（厂商反馈：{_raw}）" if _raw else "") + _hint)
    return len(emb)


def _get_selectable_embedding_model(emb_id: int, db: Session):
    """取出可用的嵌入模型（存在 + 模型启用 + 厂商启用），不通过则抛 400。

    只做"能不能选"的静态校验，不发网络请求 —— 供 force 路径复用。
    """
    row = (
        db.query(DshEmbeddingModel, AIProvider)
        .join(AIProvider, DshEmbeddingModel.provider_id == AIProvider.id)
        .filter(DshEmbeddingModel.id == emb_id, DshEmbeddingModel.is_active == True,
                AIProvider.is_active == True)
        .first()
    )
    if not row:
        raise HTTPException(status_code=400, detail=f"嵌入模型不存在或已停用（id={emb_id}）")
    return row


def _validate_embedding_model(emb_id: int, db: Session) -> int:
    """校验 emb_id 指向的独立嵌入模型可用，并回填实测维度。返回维度。

    三层校验：模型存在且启用 → 所属厂商启用 → 实调 /embeddings 探针。
    探针成功即视为"已验证"（编辑器里手动填的维度会在此转正）。
    """
    model, provider = _get_selectable_embedding_model(emb_id, db)
    dims = _probe_embeddings(provider, model.name)
    model.dimensions = dims        # 回填实测维度，供管理端展示与排障
    model.is_verified = True
    return dims


@router.get("/admin/ai/dsh-config", response_model=DshConfigRead)
def get_dsh_config(db: Session = Depends(get_db)):
    """获取 DSH 对话配置。"""
    config = db.query(DshConfig).filter(DshConfig.id == 1).first()
    if not config:
        config = DshConfig(id=1)
        db.add(config)
        db.commit()
        db.refresh(config)
    return config


@router.put("/admin/ai/dsh-config", response_model=DshConfigRead)
def update_dsh_config(payload: DshConfigUpdate, db: Session = Depends(get_db)):
    """修改 DSH 对话配置，并触发子进程重建。"""
    config = db.query(DshConfig).filter(DshConfig.id == 1).first()
    if not config:
        config = DshConfig(id=1)
        db.add(config)

    config.system_memory = payload.system_memory
    config.session_root = payload.session_root
    config.max_sessions_per_user = payload.max_sessions_per_user
    config.chat_daily_limit = payload.chat_daily_limit
    config.max_files_per_message = payload.max_files_per_message
    config.image_detail = payload.image_detail   # 图片视觉优化5：视觉 detail 档位
    # Agent 办公参数（B2/B3/A3/B5，改动即时生效，下次对话自动加载）
    config.agent_max_tool_rounds = payload.agent_max_tool_rounds
    config.agent_max_output_tokens = payload.agent_max_output_tokens
    config.agent_concurrent_limit = payload.agent_concurrent_limit
    config.agent_per_user_concurrent = payload.agent_per_user_concurrent
    config.agent_workspace_guard = payload.agent_workspace_guard
    config.agent_compact_prompt = payload.agent_compact_prompt
    config.agent_stable_prefix = payload.agent_stable_prefix
    # 记忆模块：embedding 模型配置。**独立于对话模型配置**（放同一行配置里但不互相覆盖）。
    # 语义：字段缺省（None）= 不修改 —— 保护"只改对话/Agent 配置"的调用方不误清向量配置；
    #       显式传 0   = 清空（关闭向量检索，记忆退回关键词匹配）；
    #       传模型 id  = 三层校验通过后设置。
    # 之所以要让 None 变成"不修改"：DshConfigUpdate 的默认值就是 None，
    # 主保存按钮的 payload 不带该字段，若按 None 即清空处理，用户点一次
    # 「保存并重启 DSH」就会把向量配置悄悄抹掉且毫无提示。
    if payload.embedding_model_id is not None:
        emb_id = payload.embedding_model_id or None
        if emb_id is not None:
            _validate_embedding_model(emb_id, db)
        config.embedding_model_id = emb_id
    db.commit()
    db.refresh(config)

    return config


# ════════════════════════════════════════
#  DSH 对话模型管理
# ════════════════════════════════════════

def _to_chat_model_read(m: DshChatModel, provider: AIProvider | None = None) -> ChatModelRead:
    return ChatModelRead(
        id=m.id,
        provider_id=m.provider_id,
        provider_name=provider.name if provider else "",
        name=m.name,
        display_name=m.display_name,
        is_active=m.is_active,
        is_default=m.is_default,
        supports_vision=m.supports_vision,
        context_length=m.context_length or 0,
        reasoning_effort=m.reasoning_effort or "off",
        supported_efforts=m.supported_efforts or "",
        created_at=m.created_at.strftime("%Y-%m-%d %H:%M:%S") if m.created_at else "",
    )


@router.get("/admin/ai/chat-models", response_model=list[ChatModelRead])
def list_chat_models(db: Session = Depends(get_db)):
    """获取所有 DSH 对话模型。"""
    results = (
        db.query(DshChatModel, AIProvider)
        .join(AIProvider, DshChatModel.provider_id == AIProvider.id)
        .order_by(DshChatModel.created_at.desc())
        .all()
    )
    return [_to_chat_model_read(m, p) for m, p in results]


@router.post("/admin/ai/chat-models", response_model=ChatModelRead)
def create_chat_model(payload: ChatModelCreate, db: Session = Depends(get_db)):
    """新增 DSH 对话模型。"""
    provider = db.query(AIProvider).filter(AIProvider.id == payload.provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail="厂商不存在")

    # 校验推理等级在该厂商支持列表内
    _validate_reasoning_effort(provider, payload.reasoning_effort)

    if payload.is_default:
        db.query(DshChatModel).filter(DshChatModel.is_default == True).update({DshChatModel.is_default: False})

    model = DshChatModel(
        provider_id=payload.provider_id,
        name=payload.name,
        display_name=payload.display_name,
        is_active=payload.is_active,
        is_default=payload.is_default,
        supports_vision=payload.supports_vision,
        context_length=payload.context_length,
        reasoning_effort=payload.reasoning_effort,
        supported_efforts=payload.supported_efforts or "",
    )
    db.add(model)
    db.commit()
    db.refresh(model)

    return _to_chat_model_read(model, provider)


@router.put("/admin/ai/chat-models/{model_id}", response_model=ChatModelRead)
def update_chat_model(model_id: int, payload: ChatModelUpdate, db: Session = Depends(get_db)):
    """编辑 DSH 对话模型。"""
    model = db.query(DshChatModel).filter(DshChatModel.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="对话模型不存在")

    # 确定最终的厂商（可能随 payload 变更）与推理等级，校验组合是否合法
    final_provider_id = payload.provider_id if payload.provider_id is not None else model.provider_id
    final_effort = payload.reasoning_effort if payload.reasoning_effort is not None else model.reasoning_effort
    final_provider = db.query(AIProvider).filter(AIProvider.id == final_provider_id).first()
    if not final_provider:
        raise HTTPException(status_code=404, detail="厂商不存在")
    _validate_reasoning_effort(final_provider, final_effort)

    if payload.is_default:
        db.query(DshChatModel).filter(DshChatModel.is_default == True, DshChatModel.id != model_id).update({DshChatModel.is_default: False})

    if payload.provider_id is not None:
        provider = db.query(AIProvider).filter(AIProvider.id == payload.provider_id).first()
        if not provider:
            raise HTTPException(status_code=404, detail="厂商不存在")
        model.provider_id = payload.provider_id
    if payload.name is not None:
        model.name = payload.name
    if payload.display_name is not None:
        model.display_name = payload.display_name
    if payload.is_active is not None:
        model.is_active = payload.is_active
    if payload.is_default is not None:
        model.is_default = payload.is_default
    if payload.supports_vision is not None:
        model.supports_vision = payload.supports_vision
    if payload.context_length is not None:
        model.context_length = payload.context_length
    if payload.reasoning_effort is not None:
        model.reasoning_effort = payload.reasoning_effort
    if payload.supported_efforts is not None:
        model.supported_efforts = payload.supported_efforts or ""

    db.commit()
    db.refresh(model)
    provider = db.query(AIProvider).filter(AIProvider.id == model.provider_id).first()

    return _to_chat_model_read(model, provider)


@router.delete("/admin/ai/chat-models/{model_id}")
def delete_chat_model(model_id: int, db: Session = Depends(get_db)):
    """删除 DSH 对话模型（先解除历史会话引用，会话将回退到默认模型）。"""
    model = db.query(DshChatModel).filter(DshChatModel.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="对话模型不存在")
    # 解除历史会话对模型的引用，避免外键约束失败；前端展示回退到默认模型
    db.query(ChatSession).filter(ChatSession.model_id == model_id).update(
        {ChatSession.model_id: None}
    )
    db.delete(model)
    db.commit()

    return {"message": "删除成功"}


# ════════════════════════════════════════
#  向量嵌入模型管理（记忆模块 · 独立于对话模型）
#  刻意单独一张表 / 一套 API：嵌入模型不参与聊天，不应出现在访客可见的对话模型列表里。
# ════════════════════════════════════════

def _current_embedding_id(db: Session) -> int | None:
    cfg = db.query(DshConfig).filter(DshConfig.id == 1).first()
    return cfg.embedding_model_id if cfg else None


def _to_embedding_model_read(m: DshEmbeddingModel, provider: AIProvider | None = None,
                             current_id: int | None = None) -> EmbeddingModelRead:
    return EmbeddingModelRead(
        id=m.id,
        provider_id=m.provider_id,
        provider_name=provider.name if provider else "",
        name=m.name,
        display_name=m.display_name,
        is_active=bool(m.is_active),
        dimensions=m.dimensions or 0,
        is_verified=bool(getattr(m, "is_verified", False)),
        is_current=(current_id is not None and m.id == current_id),
        created_at=m.created_at.strftime("%Y-%m-%d %H:%M:%S") if m.created_at else "",
    )


@router.get("/admin/ai/embedding-models", response_model=list[EmbeddingModelRead])
def list_embedding_models(db: Session = Depends(get_db)):
    """列出所有向量嵌入模型（含"是否当前生效"标记）。"""
    results = (
        db.query(DshEmbeddingModel, AIProvider)
        .join(AIProvider, DshEmbeddingModel.provider_id == AIProvider.id)
        .order_by(DshEmbeddingModel.created_at.desc())
        .all()
    )
    cur = _current_embedding_id(db)
    return [_to_embedding_model_read(m, p, cur) for m, p in results]


@router.post("/admin/ai/embedding-models", response_model=EmbeddingModelRead)
def create_embedding_model(payload: EmbeddingModelCreate, db: Session = Depends(get_db)):
    """手动新增一个向量嵌入模型。

    两种模式：
    - **留空维度**（默认）：先跑探针。不支持的模型标识（如对话模型）直接拒绝、不落库，
      保证"库里存在的嵌入模型一定可用"，避免配了却永远失败的死条目。
    - **手动填写维度**：跳过探针直接落库，并标记 is_verified=False。用于厂商暂时不可用
      （余额不足 429 / 限流 / 网络不通）时也能把配置先建起来 —— 否则管理员会被
      "探针不过就存不了"卡死，而这本质上是账户问题、不是配置问题。
      代价是该条目未经校验，管理端会明确显示「手动填写 · 未验证」。
    """
    provider = db.query(AIProvider).filter(AIProvider.id == payload.provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail="厂商不存在")

    dup = (
        db.query(DshEmbeddingModel)
        .filter(DshEmbeddingModel.provider_id == payload.provider_id,
                DshEmbeddingModel.name == payload.name)
        .first()
    )
    if dup:
        raise HTTPException(status_code=400, detail="该厂商下已存在同名嵌入模型")

    manual_dims = payload.dimensions or 0
    if manual_dims:
        dims, verified = manual_dims, False       # 手动指定 → 跳过探针
    else:
        dims = _probe_embeddings(provider, payload.name)   # 失败会抛 400
        verified = True

    model = DshEmbeddingModel(
        provider_id=payload.provider_id,
        name=payload.name,
        display_name=payload.display_name,
        is_active=payload.is_active,
        dimensions=dims,
        is_verified=verified,
    )
    db.add(model)
    db.commit()
    db.refresh(model)
    return _to_embedding_model_read(model, provider, _current_embedding_id(db))


# ⚠️ 路由顺序要紧：本路由必须注册在 /embedding-models/{model_id} 之前。
# 否则 "current" 会先被 {model_id} 匹配，再因 int 解析失败返回 422。
@router.put("/admin/ai/embedding-models/current")
def set_current_embedding_model(payload: EmbeddingCurrentUpdate, db: Session = Depends(get_db)):
    """设置/清空「当前生效的向量检索模型」——只改 DshConfig.embedding_model_id 一个字段。

    刻意不复用 PUT /dsh-config：那是全量覆盖，
    把「记忆用哪个嵌入模型」和其他配置绑在一起没有道理。
    """
    # 先校验再写库：探针失败（抛 400）时原配置保持不变，不会出现"探针没过但配置已被改"。
    forced = False
    if payload.model_id:
        if payload.force:
            # 强制启用（force=true）：只做静态校验（模型存在且启用），不发探针。
            # 用途：厂商当前不可用（余额不足 429 / 限流 / 网络不通）时，
            # 管理员仍要把已经配好的模型挂上 —— 否则"手动填维度"只能建条目、
            # 永远无法生效，功能就是半截的。
            # 代价：此刻无法确认它真能出向量，管理端会把「未验证」显式标出来。
            _get_selectable_embedding_model(payload.model_id, db)
            forced = True
        else:
            _validate_embedding_model(payload.model_id, db)

    config = db.query(DshConfig).filter(DshConfig.id == 1).first()
    if not config:
        config = DshConfig(id=1)
        db.add(config)
    config.embedding_model_id = payload.model_id or None
    db.commit()
    if not payload.model_id:
        msg = "已关闭向量检索"
    elif forced:
        msg = "已设为当前向量检索模型（未做探针校验，请留意状态提示）"
    else:
        msg = "已设为当前向量检索模型"
    return {
        "message": msg,
        "embedding_model_id": config.embedding_model_id,
        # 本次操作是否经过探针校验。清空操作不存在"校验"这回事 → None。
        "verified": ((not forced) if payload.model_id else None),
    }


@router.put("/admin/ai/embedding-models/{model_id}", response_model=EmbeddingModelRead)
def update_embedding_model(model_id: int, payload: EmbeddingModelUpdate,
                           db: Session = Depends(get_db)):
    """编辑向量嵌入模型。

    维度更新优先级：**手动指定 > 自动探针**。
    - 传了 dimensions(>0) → 采用它、跳过探针，标记 is_verified=False；
    - 否则若改了「厂商」或「模型标识」→ 重新探针（旧维度必然失效，必须重测），标记 is_verified=True；
    - 否则不动维度（仅改展示名/启用状态不触发任何网络请求）。
    """
    model = db.query(DshEmbeddingModel).filter(DshEmbeddingModel.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="嵌入模型不存在")

    final_provider_id = payload.provider_id if payload.provider_id is not None else model.provider_id
    final_name = payload.name if payload.name is not None else model.name
    provider = db.query(AIProvider).filter(AIProvider.id == final_provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail="厂商不存在")

    # 厂商或模型标识变了 → 最终 (厂商, 标识) 组合可能撞已有条目，必须查重
    probe_needed = (payload.provider_id is not None and payload.provider_id != model.provider_id) \
        or (payload.name is not None and payload.name != model.name)
    if probe_needed:
        dup = (
            db.query(DshEmbeddingModel)
            .filter(DshEmbeddingModel.provider_id == final_provider_id,
                    DshEmbeddingModel.name == final_name,
                    DshEmbeddingModel.id != model_id)
            .first()
        )
        if dup:
            raise HTTPException(status_code=400, detail="该厂商下已存在同名嵌入模型")

    manual_dims = payload.dimensions if (payload.dimensions or 0) > 0 else None
    if manual_dims:
        model.dimensions = manual_dims          # 手动指定优先，不探针
        model.is_verified = False
    elif probe_needed:
        model.dimensions = _probe_embeddings(provider, final_name)
        model.is_verified = True

    if payload.provider_id is not None:
        model.provider_id = payload.provider_id
    if payload.name is not None:
        model.name = payload.name
    if payload.display_name is not None:
        model.display_name = payload.display_name
    if payload.is_active is not None:
        model.is_active = payload.is_active
    db.commit()
    db.refresh(model)
    return _to_embedding_model_read(model, provider, _current_embedding_id(db))


@router.delete("/admin/ai/embedding-models/{model_id}")
def delete_embedding_model(model_id: int, db: Session = Depends(get_db)):
    """删除向量嵌入模型。若它正是当前生效的模型，一并清空引用（记忆退回关键词匹配）。"""
    model = db.query(DshEmbeddingModel).filter(DshEmbeddingModel.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="嵌入模型不存在")

    cleared = False
    cfg = db.query(DshConfig).filter(DshConfig.id == 1).first()
    if cfg and cfg.embedding_model_id == model_id:
        cfg.embedding_model_id = None   # 防止悬空引用：删了模型却还指着它 → 静默降级
        cleared = True

    db.delete(model)
    db.commit()
    return {
        "message": "删除成功",
        "cleared_current": cleared,
    }


@router.post("/admin/ai/embedding-models/{model_id}/test")
def test_embedding_model(model_id: int, db: Session = Depends(get_db)):
    """测试某个嵌入模型能否正常产出向量，并刷新其维度记录。

    探针成功 → 维度刷新为实测值，并把 is_verified 置 True（手动填写过的条目会在此"转正"）。
    探针失败 → 抛 400，**不修改任何记录**，已有的手动维度保持不变。
    """
    row = (
        db.query(DshEmbeddingModel, AIProvider)
        .join(AIProvider, DshEmbeddingModel.provider_id == AIProvider.id)
        .filter(DshEmbeddingModel.id == model_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="嵌入模型不存在")
    model, provider = row
    dims = _probe_embeddings(provider, model.name)
    model.dimensions = dims
    model.is_verified = True
    db.commit()
    return {"ok": True, "dimensions": dims, "is_verified": True,
            "message": f"可用，向量维度 {dims}"}


# ════════════════════════════════════════
#  推理等级配置 — 借鉴 DSH adapter.ts 的 REASONING_EFFORTS
#  不同厂商支持不同的推理等级：
#  - DeepSeek: off, low, high, max (4 个)
#  - 小米 MiMo: off, minimal, low, medium, high, xhigh, max (7 个)
#  - 通用 OpenAI 兼容: off, low, medium, high, max
# ════════════════════════════════════════

# 厂商 → 支持的推理等级映射
# 借鉴 DSH catalog.ts 的 THINKING_LEVEL_GATE + adapter.ts 的 REASONING_EFFORTS
REASONING_EFFORTS_BY_PROVIDER = {
    # DeepSeek: 4 个等级（DSH adapter.ts REASONING_EFFORTS）
    # off → thinking: {type: 'disabled'}, 不发 reasoning_effort
    # low/high/max → thinking: {type: 'enabled'} + reasoning_effort
    "deepseek": [
        {"value": "off", "label": "关闭"},
        {"value": "low", "label": "低"},
        {"value": "high", "label": "高"},
        {"value": "max", "label": "最大"},
    ],
    # 小米 MiMo: 7 个等级（DSH catalog.ts THINKING_LEVEL_GATE 的全集）
    "mimo": [
        {"value": "off", "label": "关闭"},
        {"value": "minimal", "label": "极低"},
        {"value": "low", "label": "低"},
        {"value": "medium", "label": "中"},
        {"value": "high", "label": "高"},
        {"value": "xhigh", "label": "超高"},
        {"value": "max", "label": "最大"},
    ],
}

# 默认等级（厂商未在映射中时使用）
DEFAULT_REASONING_EFFORTS = [
    {"value": "off", "label": "关闭"},
    {"value": "low", "label": "低"},
    {"value": "medium", "label": "中"},
    {"value": "high", "label": "高"},
    {"value": "max", "label": "最大"},
]

# 智谱"始终思考"系列模型（glm-5.3+）：不支持关闭思考、不认 medium，
# 只接受 low/high/max。下拉框据此只显示合法档位，避免选到无效档导致 400。
ALWAYS_THINK_MODEL_RE = re.compile(r"^glm-(?:5\.[3-9]|[6-9](?:\.\d+)?)", re.IGNORECASE)
ALWAYS_THINK_EFFORTS = [
    {"value": "low", "label": "低"},
    {"value": "high", "label": "高"},
    {"value": "max", "label": "最大"},
]


def _resolve_efforts_for_provider(provider: AIProvider) -> list[dict]:
    """按厂商名匹配其支持的推理等级列表（不区分大小写，未匹配回退通用列表）。"""
    name_lower = (provider.name or "").lower()
    for key, efforts in REASONING_EFFORTS_BY_PROVIDER.items():
        if key in name_lower:
            return efforts
    return DEFAULT_REASONING_EFFORTS


def _resolve_efforts_for_model(provider: AIProvider, model_name: str) -> list[dict]:
    """按厂商 + 模型名解析推理等级列表。

    优先级：
    1. 厂商映射（deepseek / mimo 等已知厂商）
    2. 模型名规则（智谱 glm-5.3+ 始终思考系列 → 仅 low/high/max）
    3. 通用列表
    """
    base = _resolve_efforts_for_provider(provider)
    if len(base) <= 3:
        return base
    if ALWAYS_THINK_MODEL_RE.match((model_name or "").strip()):
        return ALWAYS_THINK_EFFORTS
    return base


def _validate_reasoning_effort(provider: AIProvider, effort: str | None):
    """校验推理等级是否在该厂商支持列表内；off（关闭）恒允许。

    管理端 UI 的下拉已按厂商限制，此校验防止 API 直连绕过。
    """
    if not effort or effort == "off":
        return
    supported = {e["value"] for e in _resolve_efforts_for_provider(provider)}
    if effort not in supported:
        raise HTTPException(
            status_code=422,
            detail=(
                f"厂商 {provider.name} 不支持推理等级 '{effort}'"
                f"（支持: {', '.join(sorted(supported))}）"
            ),
        )


@router.get("/admin/ai/reasoning-efforts")
def get_reasoning_efforts(provider_id: int | None = None, model_name: str | None = None, db: Session = Depends(get_db)):
    """获取指定厂商/模型支持的推理等级列表。

    借鉴 DSH adapter.ts 的 resolveModel():
    - 不同 Provider 的模型支持不同的推理等级
    - DeepSeek: off/low/high/max
    - 小米 MiMo: off/minimal/low/medium/high/xhigh/max
    - 智谱 glm-5.3+（始终思考）: low/high/max
    - 未匹配的厂商使用通用等级列表
    """
    if provider_id:
        provider = db.query(AIProvider).filter(AIProvider.id == provider_id).first()
        if provider:
            return {"efforts": _resolve_efforts_for_model(provider, model_name or "")}
    # 回退到通用列表
    return {"efforts": DEFAULT_REASONING_EFFORTS}


# ════════════════════════════════════════
#  DSH 风格模型自动发现 — GET /models
#  对应 DSH discovery.ts 的 discoverModels()
#  调用 Provider 的 OpenAI 兼容 GET /models 端点，
#  自动获取可用模型列表及其 context_window
# ════════════════════════════════════════

@router.post("/admin/ai/chat-models/discover", response_model=ModelDiscoveryResponse)
async def discover_chat_models(
    payload: ModelDiscoveryRequest,
    db: Session = Depends(get_db),
):
    """探测 Provider 端点的可用模型列表 — DSH 风格自动发现。

    严格移植 DSH discovery.ts 的 discoverModels():
    1. 调用 Provider 的 GET /models 端点（OpenAI 兼容协议）
    2. 解析返回的 {data: [...]} 数组
    3. 从每个条目提取 id, name, context_window/context_length, max_tokens

    优先使用 payload 中的 base_url/api_key（用于测试未保存的厂商），
    否则从 provider_id 查询已保存的厂商信息。
    """
    from ...services.context_discovery import discover_models

    base_url = payload.base_url
    api_key = payload.api_key

    # 如果直接提供了 base_url，优先使用
    if not base_url:
        if not payload.provider_id:
            raise HTTPException(
                status_code=400,
                detail="请提供 provider_id 或 base_url",
            )
        provider = db.query(AIProvider).filter(AIProvider.id == payload.provider_id).first()
        if not provider:
            raise HTTPException(status_code=404, detail="厂商不存在")
        base_url = provider.base_url
        if not api_key:
            api_key = provider.api_key

    if not base_url:
        raise HTTPException(status_code=400, detail="缺少 base_url")

    try:
        models = await discover_models(base_url, api_key)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"探测失败: {e}")

    return ModelDiscoveryResponse(
        models=[
            DiscoveredModelItem(
                id=m.id,
                name=m.name,
                context_window=m.context_window,
                max_tokens=m.max_tokens,
            )
            for m in models
        ],
        total=len(models),
        source="endpoint",
    )


# 实测推理等级：候选档位全集（每个厂商/模型真实支持的子集由 API 实测决定）
_EFFORT_LABELS = {
    "off": "关闭",
    "minimal": "极低",
    "low": "低",
    "medium": "中",
    "high": "高",
    "xhigh": "超高",
    "max": "最大",
}


async def _probe_model_efforts(base_url: str, api_key: str, model_name: str) -> list[dict]:
    """实测模型支持的推理等级：逐档位串行发最小请求，HTTP 2xx 即支持。

    串行而非并发：部分 API（如智谱）对并发请求限流，并发会导致漏测档位。
    准确识别依据 API 实际响应（而非厂商/模型名规则猜测）：
    - 智谱 glm-5.3 始终思考 → off 被拒、medium 被拒 → 只剩 low/high/max
    - 其他模型按各自 API 的实际接受情况返回
    """
    import httpx as _httpx

    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    results = []
    for eff, label in _EFFORT_LABELS.items():
        payload = {
            "model": model_name,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1,
            "stream": False,
        }
        if eff == "off":
            payload["thinking"] = {"type": "disabled"}
        else:
            payload["thinking"] = {"type": "enabled"}
            payload["reasoning_effort"] = eff
        ok = False
        try:
            async with _httpx.AsyncClient(timeout=8, verify=tls_compat_ctx()) as client:
                r = await client.post(url, headers=headers, json=payload)
            ok = r.status_code < 400
        except Exception:
            ok = False
        results.append({"value": eff, "label": label, "supported": ok})
    return results


@router.post("/admin/ai/chat-models/test", response_model=ChatModelTestResult)
async def test_chat_model_endpoint(
    payload: ChatModelTestRequest,
    db: Session = Depends(get_db),
):
    """测试模型连通性 — 向厂商端点发送最小 chat 请求，验证模型标识真实可用（B1）。

    测试成功后**实测该模型支持的推理等级**（逐档位并发发最小请求，200 即支持），
    返回给管理端下拉框——不靠规则猜测，按 API 实际响应准确识别。
    优先使用 payload 中的 base_url/api_key（用于测试未保存的厂商），
    否则从 provider_id 查询已保存的厂商信息。
    """
    from ...services.context_discovery import test_chat_model

    base_url = payload.base_url
    api_key = payload.api_key

    if not base_url:
        if not payload.provider_id:
            raise HTTPException(
                status_code=400,
                detail="请提供 provider_id 或 base_url",
            )
        provider = db.query(AIProvider).filter(AIProvider.id == payload.provider_id).first()
        if not provider:
            raise HTTPException(status_code=404, detail="厂商不存在")
        base_url = provider.base_url
        if not api_key:
            api_key = provider.api_key

    if not base_url:
        raise HTTPException(status_code=400, detail="缺少 base_url")

    try:
        message = await test_chat_model(base_url, api_key, payload.name)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"测试失败: {e}")

    # ── 实测推理等级（并发探测，准确识别，不靠规则猜测）──
    efforts = []
    try:
        efforts = await _probe_model_efforts(base_url, api_key, payload.name)
    except Exception:
        efforts = []

    # 实测结果落库：若该模型已存在（按 provider_id + name 定位），回写 supported_efforts。
    # 这样对话请求前 _resolve_model 可据此校正 reasoning_effort，避免发非法档位触发 400。
    if efforts:
        try:
            _pid = payload.provider_id
            existing = (
                db.query(DshChatModel)
                .filter(DshChatModel.provider_id == _pid, DshChatModel.name == payload.name)
                .first()
                if _pid else None
            )
            if existing:
                import json as _json
                supported_vals = [e["value"] for e in efforts if e.get("supported")]
                existing.supported_efforts = _json.dumps(supported_vals, ensure_ascii=False)
                db.commit()
        except Exception:
            db.rollback()

    return ChatModelTestResult(ok=True, message=message, efforts=efforts)


# ════════════════════════════════════════
#  DSH Skill 管理
# ════════════════════════════════════════

def _to_skill_read(skill: DshSkill, warnings: list[str] | None = None) -> SkillRead:
    return SkillRead(
        id=skill.id,
        name=skill.name,
        description=skill.description,
        content=skill.content,
        dir_path=skill.dir_path or "",
        pack=skill.pack or "",
        category=skill.category or "",
        is_active=skill.is_active,
        created_at=skill.created_at.strftime("%Y-%m-%d %H:%M:%S") if skill.created_at else "",
        warnings=warnings or [],
    )


@router.get("/admin/ai/skills", response_model=list[SkillRead])
def list_skills(db: Session = Depends(get_db)):
    """获取所有 Skill。"""
    skills = db.query(DshSkill).order_by(DshSkill.created_at.desc()).all()
    return [_to_skill_read(s) for s in skills]


@router.post("/admin/ai/skills", response_model=SkillRead)
def create_skill(payload: SkillCreate, db: Session = Depends(get_db)):
    """新增 Skill。"""
    existing = db.query(DshSkill).filter(DshSkill.name == payload.name).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"Skill '{payload.name}' 已存在")

    skill = DshSkill(
        name=payload.name,
        description=payload.description,
        content=payload.content,
        category=(payload.category or "").strip(),
        is_active=payload.is_active,
    )
    db.add(skill)
    db.commit()
    db.refresh(skill)

    return _to_skill_read(skill)


@router.put("/admin/ai/skills/{skill_id}", response_model=SkillRead)
def update_skill(skill_id: int, payload: SkillUpdate, db: Session = Depends(get_db)):
    """编辑 Skill。"""
    skill = db.query(DshSkill).filter(DshSkill.id == skill_id).first()
    if not skill:
        raise HTTPException(status_code=404, detail="Skill 不存在")

    if payload.name is not None:
        existing = db.query(DshSkill).filter(DshSkill.name == payload.name, DshSkill.id != skill_id).first()
        if existing:
            raise HTTPException(status_code=409, detail=f"Skill '{payload.name}' 已存在")
        skill.name = payload.name
    if payload.description is not None:
        skill.description = payload.description
    if payload.content is not None:
        skill.content = payload.content
    if payload.pack is not None:
        skill.pack = payload.pack.strip()
    if payload.category is not None:
        skill.category = payload.category.strip()
    if payload.is_active is not None:
        skill.is_active = payload.is_active

    db.commit()
    db.refresh(skill)

    # 引用健康检查：目录形式技能扫描正文引用可达性（非阻塞，仅提示）
    warnings = []
    try:
        if skill.dir_path:
            from ...services.skill_service import lint_skill_references
            warnings = lint_skill_references(skill.content, skill.dir_path, skill.pack or "")
    except Exception as e:
        logger.warning(f"Skill lint 失败（可忽略）: {e}")

    return _to_skill_read(skill, warnings)


@router.get("/admin/ai/skills/{skill_id}/lint", response_model=list[str])
def lint_skill(skill_id: int, db: Session = Depends(get_db)):
    """对已有技能执行引用健康检查。"""
    skill = db.query(DshSkill).filter(DshSkill.id == skill_id).first()
    if not skill:
        raise HTTPException(status_code=404, detail="Skill 不存在")
    if not skill.dir_path:
        return []
    from ...services.skill_service import lint_skill_references
    return lint_skill_references(skill.content, skill.dir_path, skill.pack or "")


@router.delete("/admin/ai/skills/{skill_id}")
def delete_skill(skill_id: int, db: Session = Depends(get_db)):
    """删除 Skill。"""
    skill = db.query(DshSkill).filter(DshSkill.id == skill_id).first()
    if not skill:
        raise HTTPException(status_code=404, detail="Skill 不存在")

    # db.delete + commit 后实例属性会失效，磁盘清理要用的字段先取出来
    dir_path, pack = skill.dir_path, skill.pack
    db.delete(skill)
    db.commit()

    # 如果删除的是目录形式 skill，清理磁盘文件
    if dir_path:
        import shutil
        from pathlib import Path
        try:
            p = Path(dir_path)
            if p.exists():
                shutil.rmtree(p, ignore_errors=True)
        except Exception:
            pass
        # 包内技能删完后包根常常只剩空目录壳 → 顺带清掉（仍有共享资源则保留）
        if pack:
            from ...services.skill_service import prune_pack_dir_if_empty
            prune_pack_dir_if_empty(pack)

    return {"message": "删除成功"}


# ════════════════════════════════════════
#  Skill 目录上传（ZIP）
# ════════════════════════════════════════

from fastapi import UploadFile, File

@router.post("/admin/ai/skills/upload", response_model=SkillRead)
async def upload_skill_directory(
    file: UploadFile = File(...),
    skill_name: str = "",
    pack: str = "",
    category: str = "",
    is_active: bool = True,
    db: Session = Depends(get_db),
):
    """上传 ZIP 格式的 skill 目录。

    借鉴 DSH skill-filesystem 的目录结构：
    - ZIP 解压到 data/skills/[<pack>/]<name>/
    - 自动检测 SKILL.md 或第一个 .md 文件
    - 解析 frontmatter 获取 name 和 description
    - 存入数据库，content = SKILL.md 正文，dir_path = 磁盘路径
    - pack 非空时作为技能包技能导入：目录位于包根下，
      运行时可通过 ../ 或 @pack/ 访问包内共享资源
    - category 非空时打上分类标签（前端技能面板下拉筛选）；
      留空 = 不改动已有分类，避免重新上传更新正文时把标签静默清掉
    - 导入后执行引用健康检查（lint），warnings 随响应返回
    """
    try:
        if not file.filename:
            raise HTTPException(status_code=400, detail="未提供文件")

        if not file.filename.lower().endswith(".zip"):
            raise HTTPException(status_code=400, detail="请上传 ZIP 格式文件")

        zip_bytes = await file.read()
        if len(zip_bytes) == 0:
            raise HTTPException(status_code=400, detail="文件为空")

        logger.info(f"上传 skill ZIP: {file.filename}, size={len(zip_bytes)} bytes")

        # 从文件名推断 skill_name
        if not skill_name:
            skill_name = file.filename.rsplit(".", 1)[0]

        # 安全的目录名
        safe_name = re.sub(r"[^a-zA-Z0-9_-]", "-", skill_name)
        if not re.match(r"^[a-zA-Z0-9_-]+$", safe_name):
            raise HTTPException(status_code=400, detail=f"无效的 skill 名称: {skill_name}")

        # 导入 ZIP（pack 非空时导入到包根下）
        from ...services.skill_service import import_skill_from_zip
        try:
            result = import_skill_from_zip(zip_bytes, safe_name, db, pack=pack)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            logger.error(f"Skill ZIP 导入失败: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"导入失败: {e}")

        # 如果已存在同名 skill，更新；否则创建
        new_category = (category or "").strip()
        existing = db.query(DshSkill).filter(DshSkill.name == result["name"]).first()
        if existing:
            existing.description = result["description"]
            existing.content = result["content"]
            existing.dir_path = result["dir_path"]
            existing.pack = result.get("pack", "")
            existing.is_active = is_active
            # 留空 = 保持原分类（重传 ZIP 更新正文时不该丢标签）
            if new_category:
                existing.category = new_category
            db.commit()
            db.refresh(existing)
            skill = existing
        else:
            skill = DshSkill(
                name=result["name"],
                description=result["description"],
                content=result["content"],
                dir_path=result["dir_path"],
                pack=result.get("pack", ""),
                category=new_category,
                is_active=is_active,
            )
            db.add(skill)
            db.commit()
            db.refresh(skill)

        # 引用健康检查：扫描正文中的相对路径引用是否可达（非阻塞，仅提示）
        warnings: list[str] = []
        try:
            if skill.dir_path:
                from ...services.skill_service import lint_skill_references
                warnings = lint_skill_references(
                    skill.content, skill.dir_path, skill.pack or ""
                )
        except Exception as e:
            logger.warning(f"Skill lint 失败（可忽略）: {e}")

        return _to_skill_read(skill, warnings)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Skill 上传端点异常: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"服务器内部错误: {e}")


@router.post("/admin/ai/skills/upload-pack", response_model=PackUploadResult)
async def upload_pack_resources(
    file: UploadFile = File(...),
    pack: str = "",
    db: Session = Depends(get_db),
):
    """上传技能包共享资源 ZIP（解压到 data/skills/<pack>/ 包根）。

    多技能包的公共资源（assets/ scripts/ references/ 等）放在包根下，
    包内技能通过 ../ 或 @pack/ 前缀访问。合并语义：覆盖同名文件。
    通常流程：先上传包资源 ZIP，再逐个上传包内技能（填同一 pack 名）。
    """
    try:
        if not file.filename:
            raise HTTPException(status_code=400, detail="未提供文件")

        if not file.filename.lower().endswith(".zip"):
            raise HTTPException(status_code=400, detail="请上传 ZIP 格式文件")

        if not pack.strip():
            raise HTTPException(status_code=400, detail="请提供技能包名（pack 参数）")

        zip_bytes = await file.read()
        if len(zip_bytes) == 0:
            raise HTTPException(status_code=400, detail="文件为空")

        logger.info(f"上传 skill pack ZIP: {file.filename}, pack={pack}, size={len(zip_bytes)} bytes")

        from ...services.skill_service import import_pack_resources_from_zip
        try:
            files = import_pack_resources_from_zip(zip_bytes, pack)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            logger.error(f"Skill pack ZIP 导入失败: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"导入失败: {e}")

        from ...services.skill_service import _safe_pack_name
        return PackUploadResult(pack=_safe_pack_name(pack), files=files)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Skill pack 上传端点异常: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"服务器内部错误: {e}")


@router.post("/admin/ai/skills/upload-repo", response_model=RepoImportResult)
async def upload_skill_repo(
    file: UploadFile = File(...),
    pack: str = "",
    category: str = "",
    db: Session = Depends(get_db),
):
    """整仓库 ZIP 一键导入（优化6）：自动拆出共享资源 + 注册全部技能。

    上传多技能仓库的完整 ZIP（如 ASu-skills），一次完成：
    - 共享资源解压到 data/skills/<pack>/ 包根（带顶层目录自动展开）
    - 检测并注册所有技能（skills/<name>/SKILL.md 约定，平铺兜底）
    - 逐技能执行引用健康检查（lint），警告随结果返回
    同名技能按 name 更新；无技能的 ZIP 等价纯资源导入。
    category 非空时批量打到本次注册的全部技能上（一个仓库通常同属一个主题）；
    留空 = 不改动已有技能的分类。
    """
    try:
        if not file.filename:
            raise HTTPException(status_code=400, detail="未提供文件")

        if not file.filename.lower().endswith(".zip"):
            raise HTTPException(status_code=400, detail="请上传 ZIP 格式文件")

        if not pack.strip():
            raise HTTPException(status_code=400, detail="请提供技能包名（pack 参数）")

        zip_bytes = await file.read()
        if len(zip_bytes) == 0:
            raise HTTPException(status_code=400, detail="文件为空")

        logger.info(f"上传整仓库 ZIP: {file.filename}, pack={pack}, category={category!r}, size={len(zip_bytes)} bytes")

        from ...services.skill_service import import_skill_repo_from_zip
        try:
            result = import_skill_repo_from_zip(zip_bytes, pack, db, category=(category or "").strip())
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            logger.error(f"整仓库 ZIP 导入失败: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"导入失败: {e}")

        return RepoImportResult(
            pack=result["pack"],
            files=result["files"],
            skills=[RepoSkillItem(**s) for s in result["skills"]],
            skipped=result["skipped"],
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"整仓库上传端点异常: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"服务器内部错误: {e}")


# ═══ media-router Web 配置页（管理员按钮触发）═══
# 技能 media-router 自带 `media_router.py web` 本机配置页（回环 + 一次性 token）。
# 这里在 FastAPI 进程内以线程方式拉起它的 ConfigServer（start_background），
# 与主进程同生命周期，无需管理子进程；进程重启后按钮再按一次即可。
_mr_web_server = None
_mr_web_lock = threading.Lock()


@router.get("/admin/ai/skills/media-router/web-config")
def media_router_web_config_status(db: Session = Depends(get_db)):
    """查询 media-router web 配置页状态（未启动/运行中，运行中附 URL）。"""
    with _mr_web_lock:
        running = _mr_web_server is not None
        url = _mr_web_server.url() if running else None
    return {"running": running, "url": url}


@router.post("/admin/ai/skills/media-router/web-config/start")
def media_router_web_config_start(db: Session = Depends(get_db)):
    """启动 media-router web 配置页（幂等：已在运行直接返回现有 URL）。"""
    global _mr_web_server
    skill = db.query(DshSkill).filter(DshSkill.name == "media-router").first()
    if not skill or not skill.is_active:
        raise HTTPException(status_code=404, detail="media-router 技能未安装或未启用")
    if not skill.dir_path:
        raise HTTPException(status_code=400, detail="media-router 技能无磁盘目录，找不到脚本")

    scripts_dir = Path(skill.dir_path) / "scripts"
    if not (scripts_dir / "media_router.py").is_file():
        raise HTTPException(status_code=400, detail="未找到 scripts/media_router.py")

    with _mr_web_lock:
        if _mr_web_server is not None:
            return {"running": True, "url": _mr_web_server.url(), "note": "已在运行"}
        # 把技能 scripts 目录加进 sys.path 后导入 mrouter.webserver（脚本自带 sys.path 注入，
        # 这里进程内导入需要自己保证包可寻址）
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        try:
            import mrouter.webserver as _mws  # noqa: E402
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"加载 media-router webserver 失败: {e}")
        try:
            server = _mws.ConfigServer(host="127.0.0.1", port=8760, open_browser=False)
            url = server.start_background()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"启动配置页失败: {e}")
        _mr_web_server = server
        logger.info(f"media-router web 配置页已启动: {url}")
        return {"running": True, "url": url}


@router.post("/admin/ai/skills/media-router/web-config/stop")
def media_router_web_config_stop():
    """停止 media-router web 配置页。"""
    global _mr_web_server
    with _mr_web_lock:
        if _mr_web_server is None:
            return {"running": False, "url": None}
        _mr_web_server.stop()
        _mr_web_server = None
    logger.info("media-router web 配置页已停止")
    return {"running": False, "url": None}


@router.get("/admin/ai/skills/media-router/web-config/pool")
def media_router_pool_stats(db: Session = Depends(get_db)):
    """统计模型池中已配置（enabled）的图像/视频模型数量。

    给管理页 tab 用：不启动配置页也能看到池子现状。
    走 mrouter.config.load_raw（allow_missing），配置文件手写坏了也不 500。
    """
    skill = db.query(DshSkill).filter(DshSkill.name == "media-router").first()
    if not skill or not skill.is_active or not skill.dir_path:
        raise HTTPException(status_code=404, detail="media-router 技能未安装或未启用")
    scripts_dir = Path(skill.dir_path) / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    try:
        import mrouter.config as _mrc  # noqa: E402
        raw, _paths, _overlay = _mrc.load_raw(allow_missing=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取模型池配置失败: {e}")

    def _count(kind: str) -> int:
        try:
            models = (raw.get(kind) or {}).get("models") or []
            return sum(1 for m in models if isinstance(m, dict) and m.get("enabled", True))
        except Exception:
            return 0

    return {"image": _count("image"), "video": _count("video")}
