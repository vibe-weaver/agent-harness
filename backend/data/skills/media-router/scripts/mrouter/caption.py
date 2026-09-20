"""结果回传 —— 让纯文本模型也能"看见"产物。

两件事：
  1. 结构化元数据：文件路径、大小、图片宽高、视频时长。用纯标准库读文件头，
     不依赖 Pillow。
  2. 可选的文字描述（VLM caption）：把图片丢给一个 OpenAI 兼容的视觉模型，
     换回一句话描述。这才是让文本模型能接着往下推理的关键。

视频不做 VLM 描述（绝大多数视觉接口不收视频），只回元数据。
"""

from __future__ import annotations

import base64
import json
import struct
from pathlib import Path
from typing import Any

from .config import RouterConfig
from .transport import guess_content_type, request

DEFAULT_CAPTION_PROMPT = (
    "用一两句中文客观描述这张图片：主体内容、风格、构图与配色。"
    "不要客套，直接给描述，不要提到'这张图片'以外的元信息。"
)


# ---------------------------------------------------------------- 尺寸/时长


def _png_size(head: bytes) -> tuple[int, int] | None:
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    if len(head) < 24:
        return None
    return struct.unpack(">II", head[16:24])


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    idx = 2
    total = len(data)
    while idx + 9 < total:
        if data[idx] != 0xFF:
            idx += 1
            continue
        marker = data[idx + 1]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            idx += 2
            continue
        if idx + 4 > total:
            return None
        seg_len = struct.unpack(">H", data[idx + 2: idx + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB):
            if idx + 9 > total:
                return None
            height, width = struct.unpack(">HH", data[idx + 5: idx + 9])
            return width, height
        idx += 2 + seg_len
    return None


def _gif_size(head: bytes) -> tuple[int, int] | None:
    if head[:6] not in (b"GIF87a", b"GIF89a") or len(head) < 10:
        return None
    width, height = struct.unpack("<HH", head[6:10])
    return width, height


def _webp_size(data: bytes) -> tuple[int, int] | None:
    if data[:4] != b"RIFF" or data[8:12] != b"WEBP" or len(data) < 30:
        return None
    chunk = data[12:16]
    if chunk == b"VP8X":
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    if chunk == b"VP8 ":
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return width, height
    if chunk == b"VP8L":
        bits = int.from_bytes(data[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return width, height
    return None


def image_dimensions(path: Path) -> tuple[int, int] | None:
    """读图片文件头拿宽高，失败返回 None。"""
    try:
        with path.open("rb") as fh:
            head = fh.read(64 * 1024)
    except OSError:
        return None
    if not head:
        return None
    if head.startswith(b"\x89PNG"):
        return _png_size(head)
    if head.startswith(b"\xff\xd8"):
        return _jpeg_size(head)
    if head.startswith((b"GIF87a", b"GIF89a")):
        return _gif_size(head)
    if head[:4] == b"RIFF":
        return _webp_size(head)
    return None


def mp4_duration(path: Path) -> float | None:
    """从 mp4 的 mvhd box 读取时长（秒），失败返回 None。"""
    try:
        with path.open("rb") as fh:
            head = fh.read(1024 * 1024)
    except OSError:
        return None
    pos = head.find(b"mvhd")
    if pos < 0:
        return None
    body = pos + 4
    if body + 20 > len(head):
        return None
    version = head[body]
    try:
        if version == 1:
            timescale = int.from_bytes(head[body + 20: body + 24], "big")
            duration = int.from_bytes(head[body + 24: body + 32], "big")
        else:
            timescale = int.from_bytes(head[body + 12: body + 16], "big")
            duration = int.from_bytes(head[body + 16: body + 20], "big")
    except (IndexError, ValueError):
        return None
    if not timescale:
        return None
    return round(duration / timescale, 2)


def _extract_frame(video_path: Path) -> Path | None:
    """如果环境里有 ffmpeg，抽一帧给 VLM 用；没有就跳过描述。"""
    import shutil
    import subprocess
    import tempfile

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    try:
        out = Path(tempfile.gettempdir()) / f"mr_frame_{video_path.stem}.jpg"
        subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-i", str(video_path), "-frames:v", "1", str(out)],
            check=True,
            timeout=60,
        )
        return out if out.exists() else None
    except Exception:  # noqa: BLE001 - 抽帧纯属增强，失败不影响主流程
        return None


# ---------------------------------------------------------------- 文件档案


def profile_files(files: list[str], kind: str) -> list[dict[str, Any]]:
    """给每个产物生成一份档案。"""
    out: list[dict[str, Any]] = []
    for raw in files:
        path = Path(raw)
        entry: dict[str, Any] = {"path": str(path)}
        try:
            stat = path.stat()
            entry["bytes"] = stat.st_size
            entry["size_human"] = _human_size(stat.st_size)
        except OSError:
            entry["bytes"] = 0
        entry["format"] = path.suffix.lstrip(".").lower()
        if kind == "image":
            dims = image_dimensions(path)
            if dims:
                entry["width"], entry["height"] = dims
        else:
            duration = mp4_duration(path)
            if duration:
                entry["duration_seconds"] = duration
        out.append(entry)
    return out


def _human_size(num: int) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{value:.1f}GB"


# ---------------------------------------------------------------- VLM 描述


def caption_config(cfg: RouterConfig) -> dict[str, Any]:
    raw = cfg.defaults.get("caption") or {}
    return raw if isinstance(raw, dict) else {}


def caption_enabled(cfg: RouterConfig) -> str:
    """返回 none | metadata | vlm。"""
    raw = caption_config(cfg)
    mode = str(raw.get("mode") or "metadata").lower()
    if mode in ("off", "none", "false", "disabled"):
        return "none"
    if mode == "vlm":
        return "vlm"
    return "metadata"


def _caption_key(cfg: RouterConfig, raw: dict[str, Any]) -> str:
    import os

    env_name = str(raw.get("api_key_env") or "")
    if env_name and os.environ.get(env_name):
        return os.environ[env_name].strip()
    keys = (cfg.secrets or {}).get("keys") or {}
    if env_name and keys.get(env_name):
        return str(keys[env_name]).strip()
    if raw.get("api_key"):
        return str(raw["api_key"]).strip()
    return ""


def vlm_caption(
    cfg: RouterConfig,
    files: list[str],
    kind: str,
    *,
    goal: str = "",
    timeout: float = 120.0,
) -> tuple[str, str]:
    """调 VLM 产出文字描述。返回 (描述, 错误说明)。"""
    raw = caption_config(cfg)
    endpoint = str(raw.get("endpoint") or "https://api.openai.com/v1/chat/completions")
    model = str(raw.get("model") or "")
    if not model:
        return "", "caption.model 未配置"

    key = _caption_key(cfg, raw)
    if not key:
        env_name = raw.get("api_key_env") or "（未设置 api_key_env）"
        return "", f"未找到 caption 密钥：{env_name}"

    targets: list[Path] = []
    for item in files[:3]:
        path = Path(item)
        if kind == "image":
            targets.append(path)
        else:
            frame = _extract_frame(path)
            if frame:
                targets.append(frame)
    if not targets:
        return "", "没有可送入视觉模型的图像（视频需要 ffmpeg 抽帧）"

    prompt_text = str(raw.get("prompt") or DEFAULT_CAPTION_PROMPT)
    if goal:
        prompt_text += f"\n（背景：这张图服务于以下任务，描述时请侧重相关细节）{goal}"

    content: list[dict[str, Any]] = [{"type": "text", "text": prompt_text}]
    for path in targets:
        try:
            data = base64.b64encode(path.read_bytes()).decode("ascii")
        except OSError as exc:
            return "", f"读取图片失败：{exc}"
        mime = guess_content_type(path)
        content.append(
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}
        )

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0.2,
    }
    try:
        resp = request(
            "POST",
            endpoint,
            headers={"Authorization": f"Bearer {key}"},
            json_body=payload,
            timeout=timeout,
            max_retries=1,
        )
    except Exception as exc:  # noqa: BLE001 - 描述失败不阻断主流程
        return "", f"视觉模型调用失败：{exc}"

    data = resp.json() or {}
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return "", f"视觉模型响应异常：{json.dumps(data, ensure_ascii=False)[:200]}"
    if isinstance(text, list):
        text = " ".join(
            part.get("text", "") for part in text if isinstance(part, dict)
        )
    return str(text).strip(), ""


def build_caption(
    cfg: RouterConfig, files: list[str], kind: str, goal: str = ""
) -> tuple[str, list[str]]:
    """按配置产出描述文本。返回 (描述, 提示信息)。"""
    mode = caption_enabled(cfg)
    notes: list[str] = []
    if mode == "none":
        return "", notes
    if mode == "metadata":
        return "", notes
    text, error = vlm_caption(cfg, files, kind, goal=goal)
    if error:
        notes.append(f"文字描述未生成：{error}")
    return text, notes
