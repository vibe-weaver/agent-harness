"""工作区 API 路由 — 访客云端工作区文件管理

借鉴 DSH 桌面端 WorkspaceContext 的设计理念：
- 每个用户有独立的工作区
- 文件上传后可在对话中作为上下文注入
- 提供文件树浏览、CRUD 操作
- 支持打包下载整个工作区
"""

from fastapi import APIRouter, UploadFile, File, Form, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
import json
import threading
import time

from ...core.database import get_db
from ...core.security import get_current_user
from ...models import User
from ...services import workspace_service

router = APIRouter()

# ── 上传限流（进程内 per-user 滑动窗口，公网防刷）──
_upload_lock = threading.Lock()
_upload_records: dict[int, list[float]] = {}
UPLOAD_MINUTE_LIMIT = 10  # 每用户每分钟最多 10 次上传请求


def _check_upload_rate(user_id: int) -> tuple[bool, str]:
    now = time.time()
    with _upload_lock:
        ts = [t for t in _upload_records.get(user_id, []) if now - t < 60]
        if len(ts) >= UPLOAD_MINUTE_LIMIT:
            _upload_records[user_id] = ts
            return False, f"上传过于频繁，请稍后再试（每分钟{UPLOAD_MINUTE_LIMIT}次）"
        ts.append(now)
        _upload_records[user_id] = ts
        return True, ""


# ── 聊天图片存档限流（图片视觉优化 9）──
# **刻意独立于上面的 _upload_records**：UPLOAD_MINUTE_LIMIT=10 被 POST /workspace/files
# 与 doc-extract 共享，聊天贴图若挤同一个桶，几条带图消息就会 429；而存档是
# best-effort、失败会被前端静默吞掉，用户永远拿不到缩略图且无从诊断。
_archive_lock = threading.Lock()
_archive_records: dict[int, list[float]] = {}
_ARCHIVE_MINUTE_LIMIT = 20      # 每用户每分钟最多 20 批存档
_ARCHIVE_MAX_PER_BATCH = 20     # 单批图片数上限（对齐 max_files_per_message 的 schema 上限）


def _check_archive_rate(user_id: int) -> tuple[bool, str]:
    now = time.time()
    with _archive_lock:
        ts = [t for t in _archive_records.get(user_id, []) if now - t < 60]
        if len(ts) >= _ARCHIVE_MINUTE_LIMIT:
            _archive_records[user_id] = ts
            return False, f"图片存档过于频繁，请稍后再试（每分钟{_ARCHIVE_MINUTE_LIMIT}次）"
        ts.append(now)
        _archive_records[user_id] = ts
        return True, ""


# ── 请求模型 ──

class CreateDirectoryRequest(BaseModel):
    path: str


# ════════════════════════════════════════
#  文件树 & 统计
# ════════════════════════════════════════

@router.get("/workspace/tree")
async def get_file_tree(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """获取用户工作区文件树（访问时惰性检查 TTL 过期清理，时长见 WORKSPACE_TTL_HOURS）"""
    workspace_service.maybe_cleanup_user_workspace(user.id, db)
    return workspace_service.get_file_tree(user.id, db)


@router.get("/workspace/stats")
async def get_workspace_stats(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """获取工作区统计信息"""
    workspace_service.maybe_cleanup_user_workspace(user.id, db)
    return workspace_service.get_stats(user.id, db)


# ════════════════════════════════════════
#  文件上传
# ════════════════════════════════════════

@router.post("/workspace/files")
async def upload_files(
    files: list[UploadFile] = File(...),
    base_path: str = Form(""),
    relative_paths: str = Form(""),  # JSON 数组，对应每个文件的 webkitRelativePath
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """上传文件到工作区（支持多文件和目录上传，保留相对路径）

    当用户上传目录时，前端会把每个文件的 webkitRelativePath（如 "myFolder/src/index.ts"）
    作为 JSON 数组传到 relative_paths 字段。后端优先使用这个路径，回退到 file.filename。
    """
    # 公网加固：上传限流（防快速塞满配额）
    allowed, err = _check_upload_rate(user.id)
    if not allowed:
        raise HTTPException(status_code=429, detail=err)
    # 惰性清理：6 小时前的旧文件先清空再上传
    workspace_service.maybe_cleanup_user_workspace(user.id, db)

    uploaded = []
    errors = []

    # 解析 relative_paths JSON
    rel_paths: list[str] = []
    if relative_paths:
        try:
            rel_paths = json.loads(relative_paths)
        except (json.JSONDecodeError, TypeError):
            rel_paths = []

    # ── 阶段1：读取文件 + 单文件大小软检查（超限只跳过该文件）──
    items: list[tuple[str, bytes]] = []
    for idx, file in enumerate(files):
        if not file.filename:
            continue
        relative_path = None
        try:
            # 优先使用 webkitRelativePath（目录上传时携带完整路径）
            # 回退到 base_path + filename（普通多文件上传）
            if idx < len(rel_paths) and rel_paths[idx]:
                relative_path = rel_paths[idx]
            else:
                relative_path = f"{base_path}/{file.filename}" if base_path else file.filename
            relative_path = relative_path.replace("\\", "/").lstrip("/")

            content = await file.read()
            if len(content) > workspace_service.MAX_FILE_SIZE:
                errors.append(
                    f"{relative_path}: 文件大小超过限制（{workspace_service.MAX_FILE_SIZE // (1024 * 1024)}MB）"
                )
                continue
            items.append((relative_path, content))
        except Exception as e:
            errors.append(f"{relative_path or file.filename}: {str(e)}")

    # ── 阶段2：批量写入（一次锁 + 批量配额检查 + 单次 commit）──
    # 全局配额超限（文件数量/总大小）时整个批次拒绝，避免"部分成功"的困惑
    if items:
        try:
            records = workspace_service.upload_files_batch(user.id, items, db)
            for record in records:
                uploaded.append({
                    "path": record.file_path,
                    "size": record.file_size,
                })
        except ValueError as e:
            errors.append(str(e))
        except Exception as e:
            errors.append(f"批量上传失败: {str(e)}")

    return {
        "uploaded": uploaded,
        "errors": errors,
        "count": len(uploaded),
    }


# ════════════════════════════════════════
#  聊天图片存档 & 原图读取（图片视觉优化 9/14）
# ════════════════════════════════════════

@router.post("/workspace/chat-archive")
async def chat_archive(
    files: list[UploadFile] = File(...),
    paths: str = Form(""),  # JSON 数组，与 files 下标一一对应（前端内容寻址算好）
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """把聊天里用户上传的图片存档到工作区 聊天图片/，让刷新后缩略图能恢复、
    且 Agent 的 list_files / read_file 能发现并复读它们。

    **best-effort 契约**：任何失败都只记进 errors，不抛 5xx。存档失败的唯一后果
    是前端 ref 悬空、缩略图回落到通用图标（即改动前的行为），绝不影响发消息。

    刻意与 POST /workspace/files 分开，两处差异都是有原因的：
    - **不调 maybe_cleanup_user_workspace**：它超 TTL 时会 clear_workspace 连锅端
      （硬删该用户全部 DB 行 + rmtree 整目录）。用户在聊天里贴一张图，不该顺手
      清掉他几小时前上传的工作文件。
    - **同步 IO 走线程池**：既有 upload_files 是 async def 却直接在事件循环上做
      同步磁盘 IO + DB commit，20 张 4MB 图会阻塞事件循环上百毫秒、掐住所有用户
      的流式对话。这里照 download_workspace 的写法丢进 run_in_threadpool。
    """
    from starlette.concurrency import run_in_threadpool

    allowed, err = _check_archive_rate(user.id)
    if not allowed:
        raise HTTPException(status_code=429, detail=err)
    if len(files) > _ARCHIVE_MAX_PER_BATCH:
        raise HTTPException(
            status_code=400, detail=f"单批最多存档 {_ARCHIVE_MAX_PER_BATCH} 张图片"
        )

    try:
        wanted: list[str] = json.loads(paths) if paths else []
        if not isinstance(wanted, list):
            wanted = []
    except (json.JSONDecodeError, TypeError):
        wanted = []

    max_bytes = workspace_service.CHAT_IMAGE_MAX_BYTES
    # 携带原始 files 下标：服务层返回的错误下标是传入 items 的下标，
    # 而 items 会因为超限跳过与 files 错位，靠 rel 反查在多个空 rel 时会串号
    staged: list[tuple[int, str, bytes]] = []
    errors: list[dict] = []
    for idx, file in enumerate(files):
        rel = wanted[idx] if idx < len(wanted) and isinstance(wanted[idx], str) else ""
        # 只读上限内的字节：多读 1 字节就能判超限，不必把整个恶意大文件读进内存
        try:
            content = await file.read(max_bytes + 1)
        except Exception as e:
            errors.append({"index": idx, "detail": f"读取失败: {e}"})
            continue
        if len(content) > max_bytes:
            errors.append({"index": idx, "detail": f"图片超过 {max_bytes // (1024 * 1024)}MB"})
            continue
        staged.append((idx, rel, content))

    archived: list[dict] = []
    if staged:
        ok, svc_errors = await run_in_threadpool(
            workspace_service.archive_chat_images,
            user.id,
            [(rel, content) for _, rel, content in staged],
            db,
        )
        archived = [{"path": p} for p in ok]
        errors.extend(
            {"index": staged[item_idx][0], "detail": detail} for item_idx, detail in svc_errors
        )

    return {"archived": archived, "errors": errors, "count": len(archived)}


@router.get("/workspace/raw")
async def get_raw_image(
    path: str = Query(..., description="工作区相对路径"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """按路径返回图片二进制（缩略图恢复用）。

    前端必须用 fetch 带 Bearer 头取字节再转 dataURL —— `<img src>` 发不出
    Authorization 头，所以这个端点的 URL 不能直接当 src 用。

    三道闸（扩展名白名单 → DB 归属行 → 魔数嗅探）任一不过一律 404，
    不用 403 是为了不泄露文件存在性。media_type 用嗅探结果而非扩展名或
    mime_type 列：前端 prepareImageFile 会把 bmp/ico 转码成 JPEG/PNG，
    扩展名可能与真实字节不符，错了会让浏览器与视觉模型都解码失败。
    """
    from starlette.concurrency import run_in_threadpool

    resolved = await run_in_threadpool(
        workspace_service.read_image_for_raw, user.id, path, db
    )
    if resolved is None:
        raise HTTPException(status_code=404, detail="文件不存在")
    full, mime = resolved
    return FileResponse(
        str(full),
        media_type=mime,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "inline",
            # 路径是内容寻址的（文件名含内容 sha256 前缀）⇒ 内容不可变 ⇒ 可激进缓存
            "Cache-Control": "private, max-age=86400, immutable",
        },
    )


# ════════════════════════════════════════
#  创建目录
# ════════════════════════════════════════

@router.post("/workspace/directory")
async def create_directory(
    req: CreateDirectoryRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """创建目录"""
    try:
        record = workspace_service.create_directory(user.id, req.path, db)
        return {"path": record.file_path, "is_directory": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ════════════════════════════════════════
#  读取文件内容
# ════════════════════════════════════════

@router.get("/workspace/files")
async def read_file(
    path: str = Query(...),
    max_bytes: int = Query(
        workspace_service.PREVIEW_MAX_BYTES,
        ge=1024,
        le=8 * 1024 * 1024,
        description="预览截断字节数：超出只返回开头，响应里 truncated=true",
    ),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """读取文件内容用于预览（单次磁盘读 + 服务端截断，PDF 自动提取文本）"""
    result = workspace_service.read_file_preview(user.id, path, db, max_bytes=max_bytes)
    if result is None:
        raise HTTPException(status_code=404, detail="文件不存在")
    if not result.get("is_text", True):
        raise HTTPException(status_code=400, detail="二进制文件无法以文本形式预览")
    return result


@router.get("/workspace/generated")
async def read_generated_file(
    path: str = Query(...),
    user: User = Depends(get_current_user),
):
    """读取 AI 生成的落盘文件内容（.dsh_generated/ 下，无 DB 记录，直接读磁盘）。

    大文件由 SSE 事件落盘（chat.py 的 _file_event），SSE 只发元数据，
    前端下载时通过本接口取回内容。文件随工作区 TTL 过期。
    """
    content = workspace_service.read_generated_file(user.id, path)
    if content is None:
        raise HTTPException(status_code=404, detail="生成文件不存在或已过期")
    text = content.decode("utf-8", errors="replace")
    return {
        "path": path,
        "content": text,
        "size": len(text.encode("utf-8")),
    }


# ════════════════════════════════════════
#  文档文本提取 — 对话附件统一后端解析
# ════════════════════════════════════════

DOC_EXTRACT_MAX_BYTES = 10 * 1024 * 1024  # 单文档 10MB（与前端 MAX_DOCUMENT_SIZE 一致）


@router.post("/workspace/doc-extract")
async def extract_document(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
):
    """对话附件文档文本提取 — 统一后端解析（PDF / docx / xlsx）。

    前端上传附件时调用，返回提取的纯文本；图片型 PDF（无文本层）
    返回前 N 页图片（base64 data URL），供 vision 模型识别。
    """
    allowed, err = _check_upload_rate(user.id)
    if not allowed:
        raise HTTPException(status_code=429, detail=err)
    if not file.filename:
        raise HTTPException(status_code=400, detail="缺少文件名")
    raw = await file.read()
    if len(raw) > DOC_EXTRACT_MAX_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"文档超过大小限制（{DOC_EXTRACT_MAX_BYTES // (1024 * 1024)}MB）",
        )
    text, is_image_pdf = workspace_service.extract_document_text(file.filename, raw)
    result: dict = {
        "name": file.filename,
        "content": text,
        "total_chars": len(text),
        "is_image_pdf": is_image_pdf,
        "images": [],
    }
    if is_image_pdf:
        images = workspace_service.render_pdf_pages_as_images(raw)
        result["images"] = images
    return result


# ════════════════════════════════════════
#  删除文件/目录
# ════════════════════════════════════════

@router.delete("/workspace/files")
async def delete_file(
    path: str = Query(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """删除文件或目录"""
    success = workspace_service.delete_file(user.id, path, db)
    if not success:
        raise HTTPException(status_code=404, detail="文件或目录不存在")
    return {"message": "删除成功"}


# ════════════════════════════════════════
#  历史版本（编辑快照）— 后端早有 snapshot_file/pick_snapshot/count_snapshots，
#  这里把"列出 + 一键回退"暴露给前端预览面板的"历史版本"抽屉用。
# ════════════════════════════════════════

@router.get("/workspace/snapshots")
async def list_snapshots(
    path: str = Query(...),
    user: User = Depends(get_current_user),
):
    """列出某文件的历史快照（新版在前）。每项 {steps, ts, size}。"""
    return {"versions": workspace_service.list_snapshots(user.id, path)}


@router.post("/workspace/restore")
async def restore_snapshot(
    path: str = Query(...),
    steps: int = Query(1, ge=1),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """把文件回退到倒数第 steps 版（1=最近一版）。回退不改写历史。"""
    result, err = workspace_service.restore_snapshot(user.id, path, steps, db)
    if result is None:
        raise HTTPException(status_code=400, detail=err)
    ts, size = result
    # 回退改了文件，前端文件树需要刷新（previewModal 自己会再读一次内容）
    return {"message": "已回退", "ts": ts.isoformat(), "size": size}


# ════════════════════════════════════════
#  打包下载整个工作区（ZIP）
# ════════════════════════════════════════

@router.get("/workspace/download")
async def download_workspace(
    user: User = Depends(get_current_user),
):
    """将用户整个工作区打包为 ZIP 下载。

    打包是重阻塞 IO（300MB 配额），整体丢进线程池执行，产物落磁盘临时文件，
    再用 FileResponse 发送 —— 事件循环全程不被占用，其他用户的流式对话不受影响。
    临时文件由 BackgroundTask 在响应结束后删除。
    """
    import datetime
    import os

    from starlette.background import BackgroundTask
    from starlette.concurrency import run_in_threadpool

    tmp_path = await run_in_threadpool(workspace_service.build_workspace_zip, user.id)
    if tmp_path is None:
        raise HTTPException(status_code=404, detail="工作区为空，没有可下载的文件")

    # 生成文件名：workspace_YYYYMMDD_HHMMSS.zip（纯 ASCII，无需编码处理）
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return FileResponse(
        str(tmp_path),
        media_type="application/zip",
        filename=f"workspace_{timestamp}.zip",
        background=BackgroundTask(os.unlink, str(tmp_path)),
    )


# ════════════════════════════════════════
#  文档生成 — PDF / Word
# ════════════════════════════════════════

class GenerateDocRequest(BaseModel):
    text: str
    format: str  # "pdf" or "docx"
    filename: str = "document"


@router.post("/workspace/generate-doc")
async def generate_document(
    req: GenerateDocRequest,
    user: User = Depends(get_current_user),
):
    """将纯文本生成 PDF/Word 二进制文件，返回给浏览器下载。

    前端 <file-download> 标签输出的 PDF/Word 内容是纯文本，
    前端调用此接口让后端用 reportlab（含中文字体）生成真正的二进制文件。
    """
    from fastapi.responses import Response
    from urllib.parse import quote

    # 安全文件名：ASCII fallback + RFC 5987 编码的 filename*
    # HTTP header 只允许 latin-1，中文字符必须用 URL 编码
    raw_name = req.filename or "document"
    safe_ascii = raw_name.encode("ascii", "replace").decode("ascii")
    encoded_name = quote(raw_name)

    try:
        if req.format == "pdf":
            pdf_bytes = workspace_service.generate_pdf_bytes(req.text)
            return Response(
                content=pdf_bytes,
                media_type="application/pdf",
                headers={
                    "Content-Disposition": f"attachment; filename=\"{safe_ascii}.pdf\"; filename*=UTF-8''{encoded_name}.pdf",
                },
            )
        elif req.format == "docx":
            docx_bytes = workspace_service.generate_docx_bytes(req.text)
            return Response(
                content=docx_bytes,
                media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                headers={
                    "Content-Disposition": f"attachment; filename=\"{safe_ascii}.docx\"; filename*=UTF-8''{encoded_name}.docx",
                },
            )
        else:
            raise HTTPException(status_code=400, detail="不支持的格式，仅支持 pdf 或 docx")
    except HTTPException:
        raise
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception("generate-doc failed")
        raise HTTPException(status_code=500, detail=f"文档生成失败: {e}")
