"""Provider 适配器集合。

设计原则：
  - 每个适配器只负责「把一次生成请求翻译成某个 provider 的 HTTP 协议」，
    统一返回 GenResult，路由/熔断/回传由上层处理。
  - 尽量不新增适配器代码就能接新模型：``generic_http`` 是完全配置驱动的，
    绝大多数 REST 接口靠填 YAML 就能接上。
  - ``native`` 是特殊通道：它不调 HTTP，而是把「请调用内置 ImageGen / VideoGen」
    这条指令回给 agent，用来兜住那些把模型选择权收在工具内部的运行环境。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import ModelSpec, RouterConfig
from .transport import (
    HttpError,
    PollTimeout,
    download,
    poll,
    read_upload,
    request,
)

# ---------------------------------------------------------------- 数据结构


@dataclass
class GenRequest:
    kind: str
    prompt: str = ""
    requires: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    size: str = ""
    aspect_ratio: str = ""
    duration: int = 0
    negative_prompt: str = ""
    count: int = 1
    output_dir: Path | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class GenResult:
    status: str  # ok | delegate | error
    model_id: str
    provider: str
    files: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    delegate: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass
class Ctx:
    api_key: str
    key_source: str
    timeout: float
    poll_interval: float
    max_poll: float
    log: Callable[[str], None] = lambda _msg: None


# ---------------------------------------------------------------- 工具函数

_MAGIC: list[tuple[bytes, str]] = [
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
]


def sniff_extension(head: bytes) -> str | None:
    for magic, ext in _MAGIC:
        if head.startswith(magic):
            return ext
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[4:8] == b"ftyp":
        return ".mp4"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return ".webm"
    if head[:3] == b"ID3" or head[:2] == b"\xff\xfb":
        return ".mp3"
    return None


def _safe(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]+", "-", str(text)).strip("-") or "output"


def _ext_from_url(url: str, fallback: str) -> str:
    path = urllib.parse.urlparse(url).path
    suffix = Path(path).suffix.lower()
    if suffix and len(suffix) <= 6 and re.fullmatch(r"\.[0-9a-z]+", suffix):
        return suffix
    return fallback


def _stamp() -> str:
    """毫秒精度时间戳 —— 秒级精度会让同一秒内的两次生成互相覆盖。"""
    now = time.time()
    return time.strftime("%Y%m%d_%H%M%S", time.localtime(now)) + f"{int(now * 1000) % 1000:03d}"


def _unique_path(dest: Path) -> Path:
    """目标文件已存在时追加序号，确保永不覆盖既有产物。"""
    if not dest.exists():
        return dest
    for n in range(1, 10000):
        candidate = dest.with_name(f"{dest.stem}-{n}{dest.suffix}")
        if not candidate.exists():
            return candidate
    return dest


def _fetch_to_local(
    url: str,
    out_dir: Path,
    stem: str,
    index: int,
    kind: str,
    headers: dict[str, str] | None = None,
) -> Path:
    """下载并纠正扩展名（以真实文件头为准）。"""
    default_ext = ".mp4" if kind == "video" else ".png"
    ext = _ext_from_url(url, default_ext)
    dest = _unique_path(out_dir / f"{stem}_{index:02d}{ext}")
    download(url, dest, headers=headers, timeout=300.0)
    head = dest.read_bytes()[:32]
    actual = sniff_extension(head)
    if actual and actual != ext:
        renamed = _unique_path(dest.with_suffix(actual))
        dest.replace(renamed)
        dest = renamed
    return dest


def _write_b64_to_local(
    payload: str, out_dir: Path, stem: str, index: int, kind: str
) -> Path:
    raw = payload
    if raw.startswith("data:"):
        raw = raw.split(",", 1)[-1]
    data = base64.b64decode(raw)
    ext = sniff_extension(data[:32]) or (".mp4" if kind == "video" else ".png")
    dest = _unique_path(out_dir / f"{stem}_{index:02d}{ext}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return dest


def _dig(data: Any, path: str | None, default: Any = None) -> Any:
    """按 ``a.b.0.c`` 取值，支持列表下标。"""
    if not path:
        return data
    cursor = data
    for part in str(path).split("."):
        if cursor is None:
            return default
        if isinstance(cursor, list):
            try:
                cursor = cursor[int(part)]
                continue
            except (ValueError, IndexError):
                return default
        if isinstance(cursor, dict):
            cursor = cursor.get(part, default)
            continue
        return default
    return cursor


def _render(node: Any, variables: dict[str, Any]) -> Any:
    """把配置里的 ``{{prompt}}`` 之类占位符替换成实际值。"""
    if isinstance(node, str):
        pattern = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")

        def sub(match: re.Match[str]) -> str:
            return str(variables.get(match.group(1), "") or "")

        stripped = pattern.sub(sub, node)
        whole = node.strip()
        inner = whole[2:-2].strip() if whole.startswith("{{") and whole.endswith("}}") else None
        if inner and inner in variables:
            return variables[inner]
        return stripped
    if isinstance(node, dict):
        return {k: _render(v, variables) for k, v in node.items()}
    if isinstance(node, list):
        return [_render(v, variables) for v in node]
    return node


def _collect_urls_from_items(
    items: Any, url_field: str = ""
) -> list[str]:
    """把各种形状的结果数组统一成 URL 列表。"""
    urls: list[str] = []
    if items is None:
        return urls
    if isinstance(items, str):
        return [items]
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        return urls
    for item in items:
        if isinstance(item, str):
            urls.append(item)
        elif isinstance(item, dict):
            candidates = (
                [url_field]
                if url_field
                else ["url", "video_url", "image_url", "file_url", "download_url"]
            )
            for key in candidates:
                value = item.get(key)
                if isinstance(value, str) and value:
                    urls.append(value)
                    break
    return urls


_URL_SHAPED = re.compile(r"^(?:https?://|data:image/)", re.I)


def _find_urls_deep(node: Any, limit: int = 8) -> list[str]:
    """在响应里递归找"长得像地址"的字符串。

    给"只填了一个接口地址、没配 result_path"的用户兜底：只要响应里有一个
    形如 http(s)://… 或 data:image/… 的字段就取它。

    **只认地址形状的字符串**，不然会把 task_id、status 这类普通字符串也当成产物。
    """
    out: list[str] = []

    def walk(value: Any) -> None:
        if len(out) >= limit:
            return
        if isinstance(value, str):
            if _URL_SHAPED.match(value.strip()):
                out.append(value.strip())
            return
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
            return
        if isinstance(value, list):
            for item in value:
                walk(item)

    walk(node)
    # 去重但保持顺序
    seen: set[str] = set()
    unique: list[str] = []
    for url in out:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


def _finalize(
    spec: ModelSpec,
    req: GenRequest,
    urls: list[str],
    out_dir: Path | None,
    headers: dict[str, str] | None = None,
    extra_meta: dict[str, Any] | None = None,
) -> GenResult:
    """统一下载产物并组装结果。"""
    stem = f"{req.kind}_{_safe(spec.id)}_{time.strftime('%Y%m%d_%H%M%S')}"
    files: list[str] = []
    if out_dir is not None:
        for idx, url in enumerate(urls, start=1):
            files.append(str(_fetch_to_local(url, out_dir, stem, idx, req.kind, headers)))
    meta = {
        "model": spec.model or spec.id,
        "count": len(urls),
        "kind": req.kind,
    }
    if extra_meta:
        meta.update(extra_meta)
    return GenResult(
        status="ok",
        model_id=spec.id,
        provider=spec.provider,
        files=files,
        urls=urls,
        meta=meta,
    )


def _param_map(spec: ModelSpec, req: GenRequest, keys: list[str]) -> dict[str, Any]:
    """从配置 params 与请求 extra 里挑出关心的字段，请求侧优先。"""
    out: dict[str, Any] = {}
    for key in keys:
        if key in spec.params:
            out[key] = spec.params[key]
    for key in keys:
        if key in req.extra and req.extra[key] not in (None, ""):
            out[key] = req.extra[key]
    return out


# ---------------------------------------------------------------- native


def gen_native(spec: ModelSpec, req: GenRequest, cfg: RouterConfig, ctx: Ctx) -> GenResult:
    """内置工具通道：不调 HTTP，返回给 agent 的委托指令。"""
    tool = "ImageGen" if req.kind == "image" else "VideoGen"
    arguments: dict[str, Any] = {"prompt": req.prompt}
    if req.kind == "image":
        size = req.size or spec.params.get("size")
        if size:
            arguments["size"] = size
        if req.extra.get("quality"):
            arguments["quality"] = req.extra["quality"]
        if req.extra.get("style"):
            arguments["style"] = req.extra["style"]
        for idx, image in enumerate(req.images[:3], start=1):
            arguments[f"image{idx}"] = image
    else:
        if req.images:
            arguments["image"] = req.images[0]
        if req.extra.get("last_image"):
            arguments["last_image"] = req.extra["last_image"]
        if req.aspect_ratio:
            arguments["aspect_ratio"] = req.aspect_ratio
        if req.negative_prompt:
            arguments["negative_prompt"] = req.negative_prompt
        resolution = spec.params.get("resolution") or req.extra.get("resolution")
        if resolution:
            arguments["resolution"] = resolution

    return GenResult(
        status="delegate",
        model_id=spec.id,
        provider="native",
        delegate={
            "tool": tool,
            "arguments": arguments,
            "note": (
                f"模型 {spec.id} 走内置工具通道。请用 ToolSearch 查找 {tool}，"
                f"再用 DeferExecuteTool 以上述 arguments 调用，拿到本地文件路径后继续原任务。"
            ),
        },
    )


# ---------------------------------------------------------------- openai 兼容


def gen_openai(spec: ModelSpec, req: GenRequest, cfg: RouterConfig, ctx: Ctx) -> GenResult:
    """OpenAI 风格图像接口（/v1/images/generations 与 /v1/images/edits）。

    SiliconFlow、Together、以及大多数自建网关都兼容这套协议，
    只要在配置里改 endpoint 就能复用。
    """
    headers = {"Authorization": f"Bearer {ctx.api_key}"}
    edit_mode = bool(req.images) and (
        "img2img" in req.requires or "edit" in req.requires or bool(req.images)
    )

    if edit_mode:
        url = spec.endpoint or "https://api.openai.com/v1/images/generations"
        if "/generations" in url:
            url = url.replace("/generations", "/edits")
        form: dict[str, Any] = {
            "model": spec.model or spec.id,
            "prompt": req.prompt,
            "n": req.count,
        }
        form.update(_param_map(spec, req, ["size", "quality", "background", "input_fidelity"]))
        files = {}
        for idx, image in enumerate(req.images[:4]):
            name, data, ctype = read_upload(image)
            files["image" if idx == 0 else f"image[{idx}]"] = (name, data, ctype)
        ctx.log(f"[openai] edits -> {url}")
        resp = request(
            "POST", url, headers=headers, form=form, files=files, timeout=ctx.timeout
        )
    else:
        url = spec.endpoint or "https://api.openai.com/v1/images/generations"
        body: dict[str, Any] = {
            "model": spec.model or spec.id,
            "prompt": req.prompt,
            "n": req.count,
        }
        body.update(spec.params)
        body.update(_param_map(spec, req, ["size", "quality", "style", "background"]))
        ctx.log(f"[openai] generations -> {url}")
        resp = request("POST", url, headers=headers, json_body=body, timeout=ctx.timeout)

    payload = resp.json() or {}
    if isinstance(payload, dict) and payload.get("error"):
        raise HttpError(f"provider 返回错误：{payload['error']}")

    items = payload.get("data") or []
    urls: list[str] = []
    local_from_b64: list[str] = []
    out_dir = req.output_dir
    stem = f"{req.kind}_{_safe(spec.id)}_{time.strftime('%Y%m%d_%H%M%S')}"
    for idx, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        if item.get("url"):
            urls.append(item["url"])
        elif item.get("b64_json") and out_dir is not None:
            local_from_b64.append(
                str(_write_b64_to_local(item["b64_json"], out_dir, stem, idx, req.kind))
            )

    if local_from_b64 and not urls:
        return GenResult(
            status="ok",
            model_id=spec.id,
            provider=spec.provider,
            files=local_from_b64,
            urls=[],
            meta={"model": spec.model or spec.id, "count": len(local_from_b64), "kind": req.kind},
        )
    if not urls:
        raise HttpError(f"响应中没有图片数据：{json.dumps(payload)[:300]}")
    return _finalize(spec, req, urls, out_dir, extra_meta={"usage": payload.get("usage")})


# ---------------------------------------------------------------- dashscope


DASHSCOPE_BASE = "https://dashscope.aliyuncs.com/api/v1"
DASHSCOPE_IMAGE = f"{DASHSCOPE_BASE}/services/aigc/text2image/image-synthesis"
DASHSCOPE_VIDEO = f"{DASHSCOPE_BASE}/services/aigc/video-generation/video-synthesis"
#: qwen-image 系列（qwen-image / qwen-image-2.0 / qwen-image-edit 等）不走 wanx 异步接口，
#: 走多模态同步接口，请求体是 messages 格式。用错接口会得到 400 "url error"。
DASHSCOPE_QWEN_IMAGE = f"{DASHSCOPE_BASE}/services/aigc/multimodal-generation/generation"


def _is_qwen_image(spec: ModelSpec) -> bool:
    """走同步多模态接口的模型名判定。

    qwen-image 系列（qwen-image / qwen-image-2.0 / qwen-image-edit…）与
    z-image 系列（z-image-turbo 等）都不走 wanx 异步接口，走多模态同步接口，
    请求体是 messages 格式。用错接口会得到 400 "url error"。
    """
    name = (spec.model or spec.id).lower()
    if name.startswith(("qwen-image", "z-image")):
        return True
    return name.startswith("qwen-vl") and "image" in name


def gen_dashscope(spec: ModelSpec, req: GenRequest, cfg: RouterConfig, ctx: Ctx) -> GenResult:
    """阿里云百炼 / 通义万相。qwen-image 系列走同步多模态接口，wanx 系列走异步任务。"""
    if req.kind == "image" and _is_qwen_image(spec):
        return _gen_dashscope_qwen_image(spec, req, cfg, ctx)
    return _gen_dashscope_wanx(spec, req, cfg, ctx)


def _dashscope_size(value: str) -> str:
    """qwen-image / wanx 的 size 都要求 width*height（星号分隔），把 WxH 归一化。

    用户侧习惯写 1536x1024（x），DashScope 只认 1536*1024（*）。不转的话部分端点
    报 400，部分端点静默忽略 —— 服务器不报错、直接给一张默认方图，比报错更坑。
    不匹配 WxH 的值（如 16:9 比例）原样透传。
    """
    v = str(value).strip()
    if re.fullmatch(r"\d{2,5}[xX]\d{2,5}", v):
        return v.replace("x", "*").replace("X", "*")
    return v


def _gen_dashscope_qwen_image(
    spec: ModelSpec, req: GenRequest, cfg: RouterConfig, ctx: Ctx
) -> GenResult:
    """qwen-image：同步多模态接口（messages 进、choices[].message.content[].image 出）。"""
    url = spec.endpoint or DASHSCOPE_QWEN_IMAGE
    content: list[dict[str, Any]] = []
    if req.prompt:
        content.append({"text": req.prompt})
    if req.negative_prompt:
        content.append({"negative_prompt": req.negative_prompt})
    if req.images:
        # 图生图/改图：本地路径转 base64，URL 直接传
        for img in req.images:
            content.append({"image": _image_value(img)})
    parameters: dict[str, Any] = {}
    parameters.update(spec.params)
    parameters.update(_param_map(spec, req, ["size"]))
    # CLI 的 --size 落在 req.size、--aspect-ratio 落在 req.aspect_ratio。
    # 优先级：本次请求 > 模型配置 params > 不指定（平台默认）。
    req_size = req.size or req.aspect_ratio
    if req_size:
        parameters["size"] = _dashscope_size(req_size)
    elif "size" in parameters:
        parameters["size"] = _dashscope_size(parameters["size"])
    if parameters.get("size") in ("", None):
        parameters.pop("size", None)
    body: dict[str, Any] = {
        "model": spec.model or spec.id,
        "input": {"messages": [{"role": "user", "content": content}]},
    }
    if parameters:
        body["parameters"] = parameters

    ctx.log(f"[dashscope-qwen] {url}")
    payload = request(
        "POST", url,
        headers={
            "Authorization": f"Bearer {ctx.api_key}",
            "Content-Type": "application/json",
        },
        json_body=body, timeout=ctx.timeout,
    ).json() or {}

    urls: list[str] = []
    for choice in _dig(payload, "output.choices") or []:
        for item in _dig(choice, "message.content") or []:
            if isinstance(item, dict):
                u = item.get("image") or item.get("image_url")
                if u:
                    urls.append(str(u))
    if not urls:
        raise HttpError(
            f"qwen-image 未返回图片：{json.dumps(payload, ensure_ascii=False)[:300]}"
        )
    return _finalize(spec, req, urls, req.output_dir, extra_meta={"sync": True})


def _image_value(path_or_url: str) -> str:
    """本地路径 → data URI（base64）；http(s) URL 原样。qwen-image 的 image 字段两者都收。"""
    if path_or_url.lower().startswith(("http://", "https://", "data:")):
        return path_or_url
    import base64
    import mimetypes

    p = Path(path_or_url)
    mime = mimetypes.guess_type(p.name)[0] or "image/png"
    data = base64.b64encode(p.read_bytes()).decode()
    return f"data:{mime};base64,{data}"


def _gen_dashscope_wanx(
    spec: ModelSpec, req: GenRequest, cfg: RouterConfig, ctx: Ctx
) -> GenResult:
    """wanx 系列：异步任务（提交拿 task_id，再轮询）。"""
    headers = {
        "Authorization": f"Bearer {ctx.api_key}",
        "Content-Type": "application/json",
        "X-DashScope-Async": "enable",
    }

    if req.kind == "video":
        url = spec.endpoint or DASHSCOPE_VIDEO
        inputs: dict[str, Any] = {"prompt": req.prompt}
        if req.negative_prompt:
            inputs["negative_prompt"] = req.negative_prompt
        if req.images:
            inputs["img_url"] = req.images[0]
        parameters: dict[str, Any] = {}
        parameters.update(spec.params)
        parameters.update(_param_map(spec, req, ["size", "resolution", "duration", "prompt_extend"]))
        req_size = req.size or req.aspect_ratio
        if req_size and "size" not in parameters:
            parameters["size"] = _dashscope_size(req_size)
        elif "size" in parameters:
            parameters["size"] = _dashscope_size(parameters["size"])
        body = {"model": spec.model or spec.id, "input": inputs, "parameters": parameters}
    else:
        url = spec.endpoint or DASHSCOPE_IMAGE
        inputs = {"prompt": req.prompt}
        if req.negative_prompt:
            inputs["negative_prompt"] = req.negative_prompt
        parameters = {"n": req.count}
        parameters.update(spec.params)
        parameters.update(_param_map(spec, req, ["size", "style", "prompt_extend", "watermark"]))
        req_size = req.size or req.aspect_ratio
        if req_size:
            parameters["size"] = _dashscope_size(req_size)
        elif "size" in parameters:
            parameters["size"] = _dashscope_size(parameters["size"])
        body = {"model": spec.model or spec.id, "input": inputs, "parameters": parameters}

    ctx.log(f"[dashscope] submit -> {url}")
    payload = request(
        "POST", url, headers=headers, json_body=body, timeout=ctx.timeout
    ).json() or {}
    task_id = _dig(payload, "output.task_id")
    if not task_id:
        raise HttpError(f"未拿到 task_id：{json.dumps(payload, ensure_ascii=False)[:300]}")

    poll_url = f"{DASHSCOPE_BASE}/tasks/{task_id}"

    def probe() -> list[str] | None:
        data = request(
            "GET", poll_url, headers={"Authorization": f"Bearer {ctx.api_key}"}, timeout=60
        ).json() or {}
        status = str(_dig(data, "output.task_status", "")).upper()
        if status in ("PENDING", "RUNNING", "UNKNOWN", ""):
            return None
        if status in ("FAILED", "CANCELED"):
            raise HttpError(
                f"任务失败：{_dig(data, 'output.message', '') or json.dumps(data)[:200]}"
            )
        found = _collect_urls_from_items(_dig(data, "output.results"))
        if not found:
            single = _dig(data, "output.video_url") or _dig(data, "output.url")
            found = [single] if single else []
        if not found:
            raise HttpError("任务成功但没解析出产物 URL")
        return found

    ctx.log(f"[dashscope] task {task_id} 已提交，开始轮询")
    urls = poll(probe, interval=ctx.poll_interval, max_seconds=ctx.max_poll)
    return _finalize(
        spec, req, urls, req.output_dir, extra_meta={"task_id": task_id}
    )


# ---------------------------------------------------------------- volcengine


ARK_BASE = "https://ark.cn-beijing.volces.com/api/v3"
ARK_IMAGE = f"{ARK_BASE}/images/generations"
ARK_VIDEO_TASKS = f"{ARK_BASE}/contents/generations/tasks"


def gen_volcengine(spec: ModelSpec, req: GenRequest, cfg: RouterConfig, ctx: Ctx) -> GenResult:
    """火山方舟：即梦图片走同步接口，Seedance 视频走异步任务。"""
    headers = {"Authorization": f"Bearer {ctx.api_key}", "Content-Type": "application/json"}

    if req.kind == "video":
        url = spec.endpoint or ARK_VIDEO_TASKS
        text = req.prompt
        ratio = req.aspect_ratio or spec.params.get("ratio")
        duration = req.duration or spec.params.get("duration")
        if ratio:
            text += f" --ratio {ratio}"
        if duration:
            text += f" --dur {duration}"
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        if req.images:
            content.append({"type": "image_url", "image_url": {"url": req.images[0]}})
        ctx.log(f"[volcengine] video submit -> {url}")
        payload = request(
            "POST",
            url,
            headers=headers,
            json_body={"model": spec.model or spec.id, "content": content},
            timeout=ctx.timeout,
        ).json() or {}
        task_id = payload.get("id") or _dig(payload, "data.id")
        if not task_id:
            raise HttpError(f"未拿到任务 id：{json.dumps(payload, ensure_ascii=False)[:300]}")

        poll_url = f"{url.rstrip('/')}/{task_id}"

        def probe() -> list[str] | None:
            data = request("GET", poll_url, headers=headers, timeout=60).json() or {}
            status = str(data.get("status") or "").lower()
            if status in ("queued", "running", "pending", ""):
                return None
            if status in ("failed", "cancelled", "canceled"):
                raise HttpError(
                    f"任务失败：{data.get('error') or json.dumps(data, ensure_ascii=False)[:200]}"
                )
            found = _collect_urls_from_items(_dig(data, "content"))
            if not found:
                single = _dig(data, "content.video_url")
                found = [single] if single else []
            if not found:
                raise HttpError("任务成功但没解析出视频 URL")
            return found

        ctx.log(f"[volcengine] task {task_id} 已提交，开始轮询")
        urls = poll(probe, interval=ctx.poll_interval, max_seconds=ctx.max_poll)
        return _finalize(spec, req, urls, req.output_dir, extra_meta={"task_id": task_id})

    url = spec.endpoint or ARK_IMAGE
    if req.images:
        # 即梦的图生图/参考图接口与文生图不同，配置里可用 endpoint 覆盖
        ctx.log("[volcengine] 检测到输入图片，按 img2img 语义发送")
    body: dict[str, Any] = {"model": spec.model or spec.id, "prompt": req.prompt}
    body.update(spec.params)
    body.update(
        _param_map(spec, req, ["size", "response_format", "watermark", "guidance_scale", "seed"])
    )
    if req.images:
        body["image"] = req.images[0]
    ctx.log(f"[volcengine] image -> {url}")
    payload = request(
        "POST", url, headers=headers, json_body=body, timeout=ctx.timeout
    ).json() or {}
    data_items = payload.get("data") or []
    urls = _collect_urls_from_items(data_items)
    if not urls:
        b64_items = [
            item.get("b64_json")
            for item in data_items
            if isinstance(item, dict) and item.get("b64_json")
        ]
        if b64_items and req.output_dir is not None:
            stem = f"{req.kind}_{_safe(spec.id)}_{time.strftime('%Y%m%d_%H%M%S')}"
            files = [
                str(_write_b64_to_local(v, req.output_dir, stem, i, req.kind))
                for i, v in enumerate(b64_items, start=1)
            ]
            return GenResult(
                status="ok",
                model_id=spec.id,
                provider=spec.provider,
                files=files,
                meta={"model": spec.model or spec.id, "count": len(files), "kind": req.kind},
            )
        raise HttpError(f"响应中没有图片数据：{json.dumps(payload, ensure_ascii=False)[:300]}")
    if body.get("response_format") == "b64_json":
        pass
    return _finalize(spec, req, urls, req.output_dir, extra_meta={"usage": payload.get("usage")})


# ---------------------------------------------------------------- kling


KLING_BASE = "https://api-beijing.klingai.com/v1"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def kling_jwt(access_key: str, secret_key: str, ttl: int = 1800) -> str:
    """可灵用 AK/SK 签 HS256 JWT，这里纯标准库实现，避免引入 PyJWT。"""
    now = int(time.time())
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64url(
        json.dumps({"iss": access_key, "exp": now + ttl, "nbf": now - 5}).encode()
    )
    signing_input = f"{header}.{payload}".encode("ascii")
    signature = hmac.new(secret_key.encode(), signing_input, hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64url(signature)}"


def _kling_credentials(spec: ModelSpec, ctx: Ctx, cfg: RouterConfig) -> tuple[str, str]:
    ak = spec.options.get("access_key") or ""
    sk = spec.options.get("secret_key") or ""
    if ak and sk:
        return str(ak), str(sk)
    env_ak = spec.options.get("access_key_env")
    env_sk = spec.options.get("secret_key_env")
    if env_ak and env_sk:
        import os

        ak = os.environ.get(str(env_ak), "")
        sk = os.environ.get(str(env_sk), "")
        if ak and sk:
            return ak, sk
    if ":" in (ctx.api_key or ""):
        ak, sk = ctx.api_key.split(":", 1)
        return ak.strip(), sk.strip()
    if ctx.api_key and not ak:
        ak = ctx.api_key
    if ak and sk:
        return ak, sk
    raise HttpError(
        "可灵需要 AccessKey 与 SecretKey 两个值："
        "请把 api_key_env 指向的变量写成 'AK:SK' 形式，"
        "或配置 options.access_key_env / options.secret_key_env"
    )


def gen_kling(spec: ModelSpec, req: GenRequest, cfg: RouterConfig, ctx: Ctx) -> GenResult:
    access_key, secret_key = _kling_credentials(spec, ctx, cfg)
    headers = {
        "Authorization": f"Bearer {kling_jwt(access_key, secret_key)}",
        "Content-Type": "application/json",
    }

    if req.kind == "video":
        mode = "image2video" if req.images else "text2video"
        url = spec.endpoint or f"{KLING_BASE}/videos/{mode}"
        body: dict[str, Any] = {"model_name": spec.model or spec.id}
        if req.prompt:
            body["prompt"] = req.prompt
        if req.negative_prompt:
            body["negative_prompt"] = req.negative_prompt
        if req.images:
            body["image"] = req.images[0]
        body.update(spec.params)
        body.update(_param_map(spec, req, ["aspect_ratio", "duration", "mode", "cfg_scale"]))
        if req.aspect_ratio and "aspect_ratio" not in body:
            body["aspect_ratio"] = req.aspect_ratio
        if req.duration and "duration" not in body:
            body["duration"] = str(req.duration)
        poll_base = url
    else:
        url = spec.endpoint or f"{KLING_BASE}/images/generations"
        body = {"model_name": spec.model or spec.id, "prompt": req.prompt, "n": req.count}
        body.update(spec.params)
        body.update(_param_map(spec, req, ["aspect_ratio", "image_fidelity", "n"]))
        if req.negative_prompt:
            body["negative_prompt"] = req.negative_prompt
        if req.images:
            body["image"] = req.images[0]
            body["image_reference"] = body.get("image_reference", "subject")
        poll_base = url

    ctx.log(f"[kling] submit -> {url}")
    payload = request(
        "POST", url, headers=headers, json_body=body, timeout=ctx.timeout
    ).json() or {}
    if payload.get("code") not in (0, None):
        raise HttpError(f"可灵返回错误 {payload.get('code')}: {payload.get('message')}")
    task_id = _dig(payload, "data.task_id")
    if not task_id:
        raise HttpError(f"未拿到 task_id：{json.dumps(payload, ensure_ascii=False)[:300]}")

    poll_url = f"{poll_base.rstrip('/')}/{task_id}"

    def probe() -> list[str] | None:
        data = request("GET", poll_url, headers=headers, timeout=60).json() or {}
        status = str(_dig(data, "data.task_status", "")).lower()
        if status in ("submitted", "processing", "queued", ""):
            return None
        if status == "failed":
            raise HttpError(
                f"任务失败：{_dig(data, 'data.task_status_msg', '') or json.dumps(data)[:200]}"
            )
        result = _dig(data, "data.task_result")
        found = _collect_urls_from_items(
            (result or {}).get("videos") or (result or {}).get("images")
        )
        if not found:
            raise HttpError("任务成功但没解析出产物 URL")
        return found

    ctx.log(f"[kling] task {task_id} 已提交，开始轮询")
    urls = poll(probe, interval=ctx.poll_interval, max_seconds=ctx.max_poll)
    return _finalize(spec, req, urls, req.output_dir, extra_meta={"task_id": task_id})


# ---------------------------------------------------------------- replicate


REPLICATE_BASE = "https://api.replicate.com/v1"


def gen_replicate(spec: ModelSpec, req: GenRequest, cfg: RouterConfig, ctx: Ctx) -> GenResult:
    headers = {
        "Authorization": f"Bearer {ctx.api_key}",
        "Content-Type": "application/json",
    }
    inputs: dict[str, Any] = {"prompt": req.prompt}
    inputs.update(spec.params)
    inputs.update(req.extra.get("inputs") or {})
    if req.negative_prompt:
        inputs.setdefault("negative_prompt", req.negative_prompt)
    if req.aspect_ratio:
        inputs.setdefault("aspect_ratio", req.aspect_ratio)
    if req.images:
        inputs.setdefault("image", req.images[0])
        inputs.setdefault("input_image", req.images[0])

    version = spec.options.get("version") or spec.raw.get("version")
    if version:
        url = spec.endpoint or f"{REPLICATE_BASE}/predictions"
        body = {"version": version, "input": inputs}
    else:
        url = spec.endpoint or f"{REPLICATE_BASE}/models/{spec.model or spec.id}/predictions"
        body = {"input": inputs}

    ctx.log(f"[replicate] submit -> {url}")
    payload = request(
        "POST", url, headers=headers, json_body=body, timeout=ctx.timeout
    ).json() or {}
    status = str(payload.get("status") or "").lower()
    if status == "failed":
        raise HttpError(f"任务失败：{payload.get('error')}")

    if status not in ("succeeded", "failed", "canceled") and payload.get("urls", {}).get("get"):
        get_url = payload["urls"]["get"]

        def probe() -> list[str] | None:
            data = request("GET", get_url, headers=headers, timeout=60).json() or {}
            state = str(data.get("status") or "").lower()
            if state in ("starting", "processing", ""):
                return None
            if state in ("failed", "canceled"):
                raise HttpError(f"任务失败：{data.get('error')}")
            found = _collect_urls_from_items(data.get("output"))
            if not found:
                raise HttpError("任务成功但没有输出")
            return found

        ctx.log("[replicate] 开始轮询")
        urls = poll(probe, interval=ctx.poll_interval, max_seconds=ctx.max_poll)
    else:
        urls = _collect_urls_from_items(payload.get("output"))
        if not urls:
            raise HttpError(f"响应中没有输出：{json.dumps(payload, ensure_ascii=False)[:300]}")

    return _finalize(spec, req, urls, req.output_dir, extra_meta={"task_id": payload.get("id")})


# ---------------------------------------------------------------- fal


def gen_fal(spec: ModelSpec, req: GenRequest, cfg: RouterConfig, ctx: Ctx) -> GenResult:
    headers = {"Authorization": f"Key {ctx.api_key}", "Content-Type": "application/json"}
    url = spec.endpoint or f"https://queue.fal.run/{spec.model or spec.id}"
    body: dict[str, Any] = {"prompt": req.prompt}
    body.update(spec.params)
    body.update(req.extra.get("inputs") or {})
    if req.images:
        body.setdefault("image_url", req.images[0])
    if req.aspect_ratio:
        body.setdefault("aspect_ratio", req.aspect_ratio)
    if req.negative_prompt:
        body.setdefault("negative_prompt", req.negative_prompt)

    ctx.log(f"[fal] submit -> {url}")
    payload = request(
        "POST", url, headers=headers, json_body=body, timeout=ctx.timeout
    ).json() or {}
    status_url = payload.get("status_url")
    response_url = payload.get("response_url")
    if not status_url or not response_url:
        # 有些端点直接同步返回结果
        urls = _collect_urls_from_items(payload.get("images")) or _collect_urls_from_items(
            payload.get("video")
        )
        if urls:
            return _finalize(spec, req, urls, req.output_dir)
        raise HttpError(f"响应缺少 status_url/response_url：{json.dumps(payload)[:300]}")

    def probe() -> list[str] | None:
        data = request("GET", status_url, headers=headers, timeout=60).json() or {}
        state = str(data.get("status") or "").upper()
        if state in ("IN_QUEUE", "IN_PROGRESS", ""):
            return None
        if state not in ("COMPLETED",):
            raise HttpError(f"任务状态异常：{json.dumps(data, ensure_ascii=False)[:200]}")
        result = request("GET", response_url, headers=headers, timeout=120).json() or {}
        found = (
            _collect_urls_from_items(result.get("images"))
            or _collect_urls_from_items(result.get("video"))
            or _collect_urls_from_items(result.get("output"))
        )
        if not found:
            raise HttpError("任务成功但没解析出产物 URL")
        return found

    ctx.log("[fal] 开始轮询")
    urls = poll(probe, interval=ctx.poll_interval, max_seconds=ctx.max_poll)
    return _finalize(
        spec, req, urls, req.output_dir, extra_meta={"request_id": payload.get("request_id")}
    )


# ---------------------------------------------------------------- generic_http


def _apply_auth(
    spec: ModelSpec, ctx: Ctx, headers: dict[str, str], params: dict[str, Any]
) -> None:
    auth = spec.options.get("auth") or {}
    kind = str(auth.get("type") or ("bearer" if ctx.api_key else "none")).lower()
    if not ctx.api_key:
        return
    if kind == "bearer":
        headers["Authorization"] = f"Bearer {ctx.api_key}"
    elif kind == "key":
        headers["Authorization"] = f"Key {ctx.api_key}"
    elif kind == "token":
        headers["Authorization"] = f"Token {ctx.api_key}"
    elif kind == "header":
        headers[str(auth.get("header") or "X-API-Key")] = ctx.api_key
    elif kind == "query":
        params[str(auth.get("query") or "key")] = ctx.api_key
    elif kind == "none":
        return
    else:
        headers["Authorization"] = f"{kind} {ctx.api_key}"


def _generic_http_help(spec: ModelSpec, missing: str) -> str:
    """generic_http 的配置不完整时，给一份能照着抄的说明。

    这个适配器是完全配置驱动的，缺配置就是完全跑不了 —— 光说"缺少 xxx"
    用户没法自己修，所以把可用的键和最小示例一起给出来。
    """
    return (
        f"模型「{spec.label or spec.model or spec.id}」的自定义接口配置不完整：缺少 {missing}。\n"
        "自定义接口需要你自己描述请求长什么样，写在模型条目（或它所属厂商）的 options 下。"
        "最小可用示例：\n"
        "  options:\n"
        "    auth: {type: bearer}                 # 密钥怎么带：bearer | key | header | query | none\n"
        '    body: {prompt: "{{prompt}}", model: "{{model}}"}\n'
        "    result_path: data.images             # 从响应里找产物的路径（点号分隔）\n"
        "    result_url_field: url                # 产物是对象数组时取哪个字段\n"
        "  再加上提交地址：在模型条目里写 endpoint，或在 options.submit.url 里写。\n"
        "  异步接口（先拿 task_id 再轮询）还要：\n"
        "    submit: {task_id_path: data.task_id}\n"
        '    poll: {url: "https://…/{{task_id}}", status_path: data.status, result_path: data.result}\n'
        "字段对照见 references/providers.md 的 generic_http 一节。"
    )


def gen_generic_http(
    spec: ModelSpec, req: GenRequest, cfg: RouterConfig, ctx: Ctx
) -> GenResult:
    """完全配置驱动的适配器：填 YAML 就能接任意 REST 接口，不用改代码。

    关键配置项（写在模型条目的 options 下）：
      auth:   {type: bearer|key|token|header|query|none, header: X-API-Key, query: key}
      sync:   true 表示提交响应里直接就是结果
      submit: {url, method, headers, body, task_id_path}
      poll:   {url, method, interval, status_path, success: [...], failure: [...],
               result_path, result_url_field}

    为照顾"我只知道一个接口地址"的常见情况，做了两条兜底：
      * options.submit.url 没写时，用模型条目的 endpoint（配置页面填的那个地址就是它）
      * 既没写 sync 也没写 poll.url 时，按**同步**处理（提交响应里直接是结果）
    """
    options = spec.options
    variables: dict[str, Any] = {
        "prompt": req.prompt,
        "negative_prompt": req.negative_prompt,
        "model": spec.model or spec.id,
        "size": req.size or spec.params.get("size") or "",
        "aspect_ratio": req.aspect_ratio,
        "duration": req.duration,
        "count": req.count,
        "image": req.images[0] if req.images else "",
        "image_url": req.images[0] if req.images else "",
    }
    variables.update({f"image{i + 1}": img for i, img in enumerate(req.images)})

    submit = options.get("submit") or {}
    # endpoint 兜底：配置页面里那个"图像/视频接口地址"字段落到的就是它。
    # 少了这条，界面上填了地址也依然报"缺少 options.submit.url"，等于白填。
    submit_url = submit.get("url") or spec.endpoint
    if not submit_url:
        raise HttpError(_generic_http_help(spec, "提交地址"))

    submit_headers = {str(k): str(v) for k, v in (submit.get("headers") or {}).items()}
    submit_params: dict[str, Any] = dict(submit.get("params") or {})
    _apply_auth(spec, ctx, submit_headers, submit_params)

    body_template = submit.get("body")
    if body_template is None:
        body_template = {"prompt": "{{prompt}}", "model": "{{model}}"}
        if req.kind == "video":
            body_template["aspect_ratio"] = "{{aspect_ratio}}"
    merged_body = _render(body_template, variables)
    if isinstance(merged_body, dict):
        merged_body.update({k: v for k, v in spec.params.items()})
        for key, value in req.extra.items():
            if key in merged_body and value not in (None, ""):
                merged_body[key] = value

    method = str(submit.get("method") or "POST").upper()
    submit_url = str(_render(submit_url, variables))
    ctx.log(f"[generic_http] {method} {submit_url}")
    payload = request(
        method,
        submit_url,
        headers=submit_headers,
        params=submit_params or None,
        json_body=merged_body if method in ("POST", "PUT", "PATCH") else None,
        timeout=ctx.timeout,
    ).json() or {}

    pl = options.get("poll") or {}
    has_poll = bool(pl.get("url"))
    # 没显式写 sync、也没给 poll.url → 按同步处理。这是"只填了一个接口地址"时
    # 唯一合理的猜测：提交响应里直接带结果。
    sync_mode = bool(options.get("sync")) or (options.get("sync") is None and not has_poll)
    if sync_mode:
        if options.get("sync") is None:
            ctx.log("[generic_http] 未配置 sync/poll，按同步接口处理（响应里应直接带产物）")
        result_path = options.get("result_path")
        urls = _collect_urls_from_items(
            _dig(payload, result_path), options.get("result_url_field") or ""
        )
        if not urls and not result_path:
            # 没告诉我从哪儿取 → 自己在响应里找地址形状的字段
            urls = _find_urls_deep(payload)
            if urls:
                ctx.log(f"[generic_http] 未配置 result_path，自动从响应里取到 {len(urls)} 个地址")
        if not urls:
            raise HttpError(
                f"接口有响应，但从里面找不到产物地址。"
                f"（响应：{json.dumps(payload, ensure_ascii=False)[:300]}）\n"
                + _generic_http_help(spec, "options.result_path / options.result_url_field")
            )
        return _finalize(spec, req, urls, req.output_dir)

    task_id = _dig(payload, submit.get("task_id_path"))
    poll_url_template = pl.get("url")
    if not poll_url_template:
        raise HttpError(_generic_http_help(spec, "options.poll.url"))
    poll_url = str(_render(poll_url_template, {**variables, "task_id": task_id}))
    poll_method = str(pl.get("method") or "GET").upper()
    poll_headers = {str(k): str(v) for k, v in (pl.get("headers") or {}).items()}
    poll_params: dict[str, Any] = dict(pl.get("params") or {})
    _apply_auth(spec, ctx, poll_headers, poll_params)

    success = [str(s).lower() for s in (pl.get("success") or ["succeeded", "success", "done", "completed"])]
    failure = [str(s).lower() for s in (pl.get("failure") or ["failed", "error", "canceled"])]
    status_path = pl.get("status_path")
    result_path = pl.get("result_path")
    url_field = pl.get("result_url_field") or ""

    def probe() -> list[str] | None:
        data = request(
            poll_method,
            poll_url,
            headers=poll_headers,
            params=poll_params or None,
            timeout=60,
        ).json() or {}
        state = str(_dig(data, status_path, "") or "").lower()
        if state in failure:
            raise HttpError(f"任务失败：{json.dumps(data, ensure_ascii=False)[:200]}")
        if status_path and state not in success:
            return None
        found = _collect_urls_from_items(_dig(data, result_path), url_field)
        if not found and not result_path:
            found = _find_urls_deep(data)
        if not found:
            if status_path:
                raise HttpError("任务成功但没解析出产物 URL，请检查 result_path")
            return None
        return found

    ctx.log(f"[generic_http] 轮询 {poll_url}")
    urls = poll(
        probe,
        interval=float(pl.get("interval") or ctx.poll_interval),
        max_seconds=ctx.max_poll,
    )
    return _finalize(spec, req, urls, req.output_dir, extra_meta={"task_id": task_id})


# ---------------------------------------------------------------- 注册与派发


ADAPTERS: dict[str, Callable[[ModelSpec, GenRequest, RouterConfig, Ctx], GenResult]] = {
    "native": gen_native,
    "openai": gen_openai,
    "openai_image": gen_openai,
    "dashscope": gen_dashscope,
    "aliyun": gen_dashscope,
    "bailian": gen_dashscope,
    "volcengine": gen_volcengine,
    "ark": gen_volcengine,
    "doubao": gen_volcengine,
    "kling": gen_kling,
    "keling": gen_kling,
    "replicate": gen_replicate,
    "fal": gen_fal,
    "falai": gen_fal,
    "generic_http": gen_generic_http,
    "generic": gen_generic_http,
    "http": gen_generic_http,
}

#: 不需要配置密钥的 provider
KEYLESS_PROVIDERS = {"native", "generic_http_nokey"}


def known_providers() -> list[str]:
    return sorted(ADAPTERS)


def run_adapter(
    spec: ModelSpec, req: GenRequest, cfg: RouterConfig, ctx: Ctx
) -> GenResult:
    """按 provider 派发；未知 provider 直接报错，避免静默走错通道。"""
    adapter = ADAPTERS.get(spec.provider)
    if adapter is None:
        raise HttpError(
            f"未知 provider {spec.provider!r}（模型 {spec.id}）。"
            f"可用值：{', '.join(known_providers())}；"
            f"或用 generic_http 自行配置任意 REST 接口。"
        )
    return adapter(spec, req, cfg, ctx)


__all__ = [
    "GenRequest",
    "GenResult",
    "Ctx",
    "ADAPTERS",
    "KEYLESS_PROVIDERS",
    "known_providers",
    "run_adapter",
    "PollTimeout",
    "HttpError",
]
