"""验证码服务。

使用内存字典存储验证码，Pillow 生成图形验证码。
适用于单实例部署；多实例部署可替换为 Redis 实现。
"""
import base64
import io
import random
import string
import time
import threading
from PIL import Image, ImageDraw, ImageFont

# ---------- 验证码存储 ----------
_CAPTCHA_TTL = 300  # 5 分钟过期
_MAX_STORE = 1000  # 最多存储条目，防止内存溢出

_store: dict[str, tuple[str, float]] = {}
_lock = threading.Lock()


def _cleanup_expired() -> None:
    """清理过期验证码。"""
    now = time.time()
    expired = [k for k, (_, exp) in _store.items() if exp < now]
    for k in expired:
        _store.pop(k, None)


def generate_captcha() -> tuple[str, str]:
    """生成一个验证码，返回 (captcha_id, base64_image)。"""
    # 生成 4 位验证码（字母+数字，去除易混淆字符）
    chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    code = "".join(random.choices(chars, k=4))
    captcha_id = "".join(random.choices(string.ascii_letters + string.digits, k=32))

    with _lock:
        _cleanup_expired()
        if len(_store) >= _MAX_STORE:
            # 简单清理：删掉最早的一半
            items = sorted(_store.items(), key=lambda kv: kv[1][1])
            for k, _ in items[: _MAX_STORE // 2]:
                _store.pop(k, None)
        _store[captcha_id] = (code.upper(), time.time() + _CAPTCHA_TTL)

    image = _render_image(code)
    return captcha_id, image


def verify_captcha(captcha_id: str, code: str) -> bool:
    """校验验证码。校验成功后立即删除（一次性）。"""
    if not captcha_id or not code:
        return False
    with _lock:
        item = _store.pop(captcha_id, None)
    if not item:
        return False
    stored_code, expire = item
    if time.time() > expire:
        return False
    return stored_code == code.strip().upper()


# ---------- 图像渲染 ----------
def _get_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """尝试加载系统字体，失败则使用默认字体。"""
    font_candidates = [
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]
    for path in font_candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _render_image(code: str) -> str:
    """渲染验证码图片，返回 base64 编码的 PNG。"""
    width, height = 120, 48
    img = Image.new("RGB", (width, height), color=(248, 246, 255))
    draw = ImageDraw.Draw(img)

    font = _get_font(32)

    # 绘制干扰线
    for _ in range(4):
        x1 = random.randint(0, width)
        y1 = random.randint(0, height)
        x2 = random.randint(0, width)
        y2 = random.randint(0, height)
        line_color = (
            random.randint(120, 200),
            random.randint(120, 200),
            random.randint(120, 200),
        )
        draw.line([(x1, y1), (x2, y2)], fill=line_color, width=1)

    # 绘制干扰点
    for _ in range(60):
        x = random.randint(0, width - 1)
        y = random.randint(0, height - 1)
        draw.point((x, y), fill=(random.randint(100, 200), random.randint(100, 200), random.randint(100, 200)))

    # 绘制字符（带轻微旋转和颜色变化）
    palette = [(124, 92, 255), (255, 92, 168), (0, 168, 150), (255, 153, 51)]
    char_w = width // (len(code) + 1)
    for i, ch in enumerate(code):
        x = 12 + i * char_w
        y = random.randint(2, 8)
        color = random.choice(palette)
        draw.text((x, y), ch, font=font, fill=color)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
