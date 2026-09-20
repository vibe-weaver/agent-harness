"""QQ 邮箱验证码服务。

通过 SMTP 发送验证码到用户 QQ 邮箱，验证码存储在内存中（5 分钟过期）。
需要在 .env 中配置 QQ 邮箱账号和授权码。
"""

import logging
import random
import smtplib
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from threading import Lock

from ..core.config import settings

logger = logging.getLogger(__name__)

# 验证码存储：{email: (code, expire_timestamp)}
_code_store: dict[str, tuple[str, float]] = {}
_code_lock = Lock()
CODE_EXPIRE_SECONDS = 300  # 5 分钟


def _generate_code() -> str:
    """生成 6 位数字验证码。"""
    return str(random.randint(100000, 999999))


def send_verification_code(email: str) -> tuple[bool, str]:
    """发送验证码到指定邮箱。

    Returns:
        (success, message)
    """
    email = email.strip().lower()

    # 校验 QQ 邮箱格式
    if not email.endswith("@qq.com"):
        return False, "请使用 QQ 邮箱注册"

    # 频率限制：60 秒内不能重复发送
    with _code_lock:
        existing = _code_store.get(email)
        if existing:
            _, sent_time = existing
            if time.time() - sent_time < 60:
                remaining = int(60 - (time.time() - sent_time))
                return False, f"验证码已发送，请 {remaining} 秒后再试"

    code = _generate_code()

    # 读取 SMTP 配置
    smtp_user = getattr(settings, "QQ_EMAIL", "")
    smtp_pass = getattr(settings, "QQ_EMAIL_AUTH_CODE", "")

    if not smtp_user or not smtp_pass:
        logger.error("QQ 邮箱 SMTP 配置缺失，请在 .env 中设置 QQ_EMAIL 和 QQ_EMAIL_AUTH_CODE")
        return False, "邮箱服务未配置，请联系管理员"

    # 构建邮件
    msg = MIMEMultipart("alternative")
    msg["From"] = smtp_user
    msg["To"] = email
    msg["Subject"] = f"{settings.APP_NAME} — 验证码"

    html = f"""
    <div style="padding:24px;font-family:system-ui,sans-serif;max-width:480px;margin:0 auto">
      <h2 style="color:#986638;margin-bottom:16px">邮箱验证码</h2>
      <p style="color:#444;font-size:15px;line-height:1.8">
        您正在注册 {settings.APP_NAME} 账号，验证码为：
      </p>
      <div style="font-size:32px;font-weight:700;letter-spacing:8px;color:#986638;
                  padding:20px 0;text-align:center;background:#FCF9F2;border-radius:12px;margin:16px 0">
        {code}
      </div>
      <p style="color:#999;font-size:13px;line-height:1.6">
        验证码 5 分钟内有效。如非本人操作，请忽略此邮件。
      </p>
    </div>
    """
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=15) as server:
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, [email], msg.as_string())

        # 存储验证码
        with _code_lock:
            _code_store[email] = (code, time.time())

        logger.info(f"验证码已发送到 {email}")
        return True, "验证码已发送，请查收邮箱"
    except smtplib.SMTPAuthenticationError:
        logger.error(f"QQ 邮箱 SMTP 认证失败")
        return False, "邮箱服务认证失败，请联系管理员"
    except Exception as e:
        logger.error(f"发送验证码失败: {e}")
        return False, f"发送验证码失败: {e}"


def verify_code(email: str, code: str) -> bool:
    """验证邮箱验证码是否正确且未过期。

    验证成功后自动删除验证码（一次性）。
    """
    email = email.strip().lower()
    code = code.strip()

    with _code_lock:
        stored = _code_store.get(email)
        if not stored:
            return False

        stored_code, expire_time = stored

        # 检查是否过期
        if time.time() - expire_time > CODE_EXPIRE_SECONDS:
            del _code_store[email]
            return False

        if stored_code != code:
            return False

        # 验证成功，删除验证码
        del _code_store[email]
        return True
