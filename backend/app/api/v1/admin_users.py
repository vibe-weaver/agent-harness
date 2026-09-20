"""用户管理 API — 查看用户列表、重置密码。"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...core.database import get_db
from ...core.security import hash_password
from ...models import User

router = APIRouter()


class UserRead(BaseModel):
    id: int
    username: str | None = None
    email: str | None = None
    is_admin: bool = False
    is_active: bool = True
    created_at: str

    model_config = {"from_attributes": True}


class PasswordUpdate(BaseModel):
    password: str = Field(min_length=6, max_length=100)


class UserStatusUpdate(BaseModel):
    is_active: bool


@router.get("/admin/users", response_model=list[UserRead])
def list_users(db: Session = Depends(get_db)):
    """获取所有用户列表。"""
    users = db.query(User).order_by(User.created_at.desc()).all()
    return [
        UserRead(
            id=u.id,
            username=u.username,
            email=u.email,
            is_admin=bool(u.is_admin),
            is_active=bool(getattr(u, "is_active", True)),
            created_at=u.created_at.strftime("%Y-%m-%d %H:%M") if u.created_at else "",
        )
        for u in users
    ]


@router.put("/admin/users/{user_id}/status")
def update_user_status(
    user_id: int,
    body: UserStatusUpdate,
    db: Session = Depends(get_db),
):
    """封禁 / 解封用户（公网场景：封禁后登录与所有需要认证的请求立即失效）。"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if user.is_admin:
        raise HTTPException(status_code=400, detail="不能封禁管理员账号")
    user.is_active = body.is_active
    db.commit()
    return {
        "message": "已禁用" if not body.is_active else "已启用",
        "is_active": bool(user.is_active),
    }


@router.put("/admin/users/{user_id}/password")
def update_user_password(
    user_id: int,
    body: PasswordUpdate,
    db: Session = Depends(get_db),
):
    """重置用户密码。"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    user.password_hash = hash_password(body.password)
    db.commit()
    return {"message": "密码已更新"}
