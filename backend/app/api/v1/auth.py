"""认证 API — QQ 邮箱注册/登录。

注册流程：发送验证码 → 验证码+邮箱+密码注册 → 返回 JWT。
登录流程：邮箱+密码验证 → 返回 JWT。

公网加固：send-code / register 按 IP 限流（防邮件轰炸与批量刷号）。
"""

import threading
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ...core.database import get_db
from ...core.security import verify_password, hash_password, create_token, get_current_user
from ...models import User
from ...schemas.auth import LoginRequest, RegisterRequest, SendCodeRequest, Token, UserInfo
from ...services.email_service import send_verification_code, verify_code

router = APIRouter()

# ── 认证接口 IP 限流（进程内，单 worker 部署适用）──
_auth_lock = threading.Lock()
# IP -> 最近调用时间戳列表（滑动窗口）
_send_code_records: dict[str, list[float]] = {}
_register_records: dict[str, list[float]] = {}

SEND_CODE_MINUTE_LIMIT = 1   # 验证码：每 IP 每分钟 1 次
SEND_CODE_DAILY_LIMIT = 10   # 验证码：每 IP 每日 10 次
REGISTER_DAILY_LIMIT = 5     # 注册：每 IP 每日 5 次


def _ip_rate_check(records: dict[str, list[float]], ip: str, window_seconds: int, limit: int) -> bool:
    """滑动窗口限流：窗口内调用次数超过 limit 则拒绝。"""
    now = time.time()
    with _auth_lock:
        ts = [t for t in records.get(ip, []) if now - t < window_seconds]
        if len(ts) >= limit:
            records[ip] = ts
            return False
        ts.append(now)
        records[ip] = ts
        return True


def _get_client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@router.post("/auth/send-code")
def send_code(body: SendCodeRequest, request: Request, db: Session = Depends(get_db)):
    """发送邮箱验证码。"""
    ip = _get_client_ip(request)
    # 限流：防邮件轰炸
    if not _ip_rate_check(_send_code_records, ip, 60, SEND_CODE_MINUTE_LIMIT):
        raise HTTPException(status_code=429, detail="发送过于频繁，请 1 分钟后再试")
    if not _ip_rate_check(_send_code_records, ip, 86400, SEND_CODE_DAILY_LIMIT):
        raise HTTPException(status_code=429, detail="今日验证码发送次数已达上限")

    email = body.email

    # 检查邮箱是否已注册
    existing = db.query(User).filter(User.email == email).first()
    if existing:
        raise HTTPException(status_code=409, detail="该邮箱已注册")

    ok, msg = send_verification_code(email)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)

    return {"message": msg}


@router.post("/auth/register", response_model=Token)
def register(body: RegisterRequest, request: Request, db: Session = Depends(get_db)):
    """注册新用户（QQ 邮箱 + 8 位密码 + 验证码）。"""
    ip = _get_client_ip(request)
    # 限流：防批量注册刷 AI 额度/工作区配额
    if not _ip_rate_check(_register_records, ip, 86400, REGISTER_DAILY_LIMIT):
        raise HTTPException(status_code=429, detail="注册过于频繁，请明天再试")

    email = body.email

    # 检查邮箱是否已注册
    existing = db.query(User).filter(User.email == email).first()
    if existing:
        raise HTTPException(status_code=409, detail="该邮箱已注册")

    # 验证验证码
    if not verify_code(email, body.code):
        raise HTTPException(status_code=400, detail="验证码错误或已过期")

    # 创建用户
    user = User(
        email=email,
        username=email,  # username 兼容旧逻辑，设为邮箱
        password_hash=hash_password(body.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    # 预热工作区目录名（QQ 号邮箱前缀），避免首次使用回退到 id 目录
    try:
        from ..services.workspace_service import preload_workspace_dir
        preload_workspace_dir(user.id, user.email)
    except Exception:
        pass

    return Token(access_token=create_token(user.id), email=user.email)


@router.post("/auth/login", response_model=Token)
def login(body: LoginRequest, db: Session = Depends(get_db)):
    """登录（QQ 邮箱 + 8 位密码）。"""
    user = db.query(User).filter(User.email == body.email).first()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="邮箱或密码错误")
    # 公网场景：封禁账号禁止登录
    if not getattr(user, "is_active", True):
        raise HTTPException(status_code=403, detail="账号已被禁用，请联系管理员")

    return Token(access_token=create_token(user.id), email=user.email)


@router.get("/auth/me", response_model=UserInfo)
def get_me(user: User = Depends(get_current_user)):
    """获取当前登录用户信息。"""
    return UserInfo(id=user.id, email=user.email, username=user.username, is_admin=bool(user.is_admin))
