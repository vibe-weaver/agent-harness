"""HTTP 传输层 —— 只用标准库 urllib，保证零依赖。

提供三件事：
  request()   发起一次请求（含重试、超时、multipart、JSON 解析）
  download()  把远程产物下载到本地
  poll()      异步任务的轮询等待
"""

from __future__ import annotations

import json
import mimetypes
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

USER_AGENT = "media-router/1.0 (+skill)"

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


class HttpError(RuntimeError):
    """HTTP 层错误，带上状态码与响应片段方便定位。"""

    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = (body or "")[:800]


class PollTimeout(RuntimeError):
    """轮询超时。"""


class HttpResponse:
    def __init__(self, status: int, headers: dict[str, str], body: bytes):
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        if not self.body:
            return None
        try:
            return json.loads(self.body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise HttpError(f"响应不是合法 JSON：{self.text[:300]}") from exc


def _encode_multipart(
    fields: dict[str, Any], files: dict[str, tuple[str, bytes, str]]
) -> tuple[bytes, str]:
    boundary = "----media-router-" + uuid.uuid4().hex
    buf = bytearray()
    for key, value in (fields or {}).items():
        if value is None:
            continue
        buf += f"--{boundary}\r\n".encode()
        buf += f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
        buf += str(value).encode("utf-8") + b"\r\n"
    for key, (filename, content, ctype) in (files or {}).items():
        buf += f"--{boundary}\r\n".encode()
        buf += (
            f'Content-Disposition: form-data; name="{key}"; filename="{filename}"\r\n'
        ).encode()
        buf += f"Content-Type: {ctype}\r\n\r\n".encode()
        buf += content + b"\r\n"
    buf += f"--{boundary}--\r\n".encode()
    return bytes(buf), f"multipart/form-data; boundary={boundary}"


def request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
    form: dict[str, Any] | None = None,
    files: dict[str, tuple[str, bytes, str]] | None = None,
    timeout: float = 60.0,
    max_retries: int = 2,
    retry_backoff: float = 1.6,
) -> HttpResponse:
    """发起请求；对 429/5xx/网络抖动自动重试。非 2xx 抛 HttpError。"""
    if params:
        query = urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None}, doseq=True
        )
        url = f"{url}{'&' if '?' in url else '?'}{query}"

    final_headers = {"User-Agent": USER_AGENT}
    for key, value in (headers or {}).items():
        if value is not None:
            final_headers[key] = str(value)

    payload: bytes | None = None
    if files:
        payload, content_type = _encode_multipart(form or {}, files)
        final_headers["Content-Type"] = content_type
    elif json_body is not None:
        payload = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        final_headers.setdefault("Content-Type", "application/json")
    elif form:
        payload = urllib.parse.urlencode(form).encode("utf-8")
        final_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")

    attempt = 0
    last_error: Exception | None = None
    while attempt <= max_retries:
        attempt += 1
        req = urllib.request.Request(
            url, data=payload, headers=final_headers, method=method.upper()
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return HttpResponse(
                    resp.status, dict(resp.headers.items()), resp.read()
                )
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass
            if exc.code in RETRY_STATUS and attempt <= max_retries:
                last_error = HttpError(f"HTTP {exc.code}", exc.code, body)
                time.sleep(retry_backoff ** attempt)
                continue
            raise HttpError(f"HTTP {exc.code} {url}", exc.code, body) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt <= max_retries:
                last_error = exc
                time.sleep(retry_backoff ** attempt)
                continue
            raise HttpError(f"网络错误：{exc} ({url})") from exc

    raise HttpError(f"请求失败：{last_error}")


def guess_content_type(path: Path) -> str:
    ctype, _ = mimetypes.guess_type(str(path))
    return ctype or "application/octet-stream"


def read_upload(path_or_url: str) -> tuple[str, bytes, str]:
    """把本地路径或 http(s) URL 读成 (filename, bytes, content_type)，用于 multipart 上传。"""
    if str(path_or_url).startswith(("http://", "https://")):
        resp = request("GET", str(path_or_url), timeout=120)
        name = os.path.basename(urllib.parse.urlparse(str(path_or_url)).path) or "input.bin"
        ctype = resp.headers.get("Content-Type") or guess_content_type(Path(name))
        return name, resp.body, ctype
    local = Path(path_or_url).expanduser()
    if not local.exists():
        raise HttpError(f"输入文件不存在：{local}")
    return local.name, local.read_bytes(), guess_content_type(local)


def download(
    url: str,
    dest: Path,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 300.0,
) -> Path:
    """下载远程产物到本地文件。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    final_headers = {"User-Agent": USER_AGENT}
    for key, value in (headers or {}).items():
        if value is not None:
            final_headers[key] = str(value)

    req = urllib.request.Request(url, headers=final_headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp, dest.open("wb") as fh:
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                fh.write(chunk)
    except urllib.error.HTTPError as exc:
        raise HttpError(f"下载失败 HTTP {exc.code}：{url}", exc.code) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise HttpError(f"下载失败：{exc} ({url})") from exc

    if not dest.exists() or dest.stat().st_size == 0:
        raise HttpError(f"下载得到空文件：{url}")
    return dest


def poll(
    probe: Callable[[], Any],
    *,
    interval: float = 5.0,
    max_seconds: float = 900.0,
    on_tick: Callable[[float, Any], None] | None = None,
) -> Any:
    """反复调用 probe 直到它返回非 None；超时抛 PollTimeout。

    probe 抛异常时向上传播（由适配器决定是否容忍）。
    """
    started = time.time()
    interval = max(0.5, float(interval or 5.0))
    while True:
        value = probe()
        if value is not None:
            return value
        elapsed = time.time() - started
        if on_tick:
            on_tick(elapsed, None)
        if elapsed >= max_seconds:
            raise PollTimeout(f"轮询超过 {max_seconds:.0f} 秒仍未完成")
        time.sleep(interval)
