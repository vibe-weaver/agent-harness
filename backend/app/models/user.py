import datetime
from sqlalchemy import Column, Integer, String, DateTime, Boolean

from ..core.database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(50), unique=True, nullable=True)  # 兼容旧管理员
    email = Column(String(100), unique=True, nullable=True)   # QQ 邮箱
    password_hash = Column(String(255), nullable=False)
    is_admin = Column(Boolean, default=False)  # 管理员标识
    is_active = Column(Boolean, default=True)  # 账号状态（False=封禁，公网场景）
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
