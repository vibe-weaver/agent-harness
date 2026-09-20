import datetime
from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Text, UniqueConstraint

from ..core.database import Base


class WorkspaceFile(Base):
    """访客工作区文件 — 每行对应工作区中的一个文件或目录

    借鉴 DSH 桌面端 WorkspaceContext 的设计：
    - 每个用户有一个独立的云端工作区（磁盘: data/workspaces/{user_id}/）
    - 文件路径存储相对路径，如 src/index.ts
    - content_hash 用于去重和版本检测（对应 DSH 的 instructionContentSha1）
    - 对话时按字节预算将工作区文件内容注入 system prompt
    """
    __tablename__ = "workspace_files"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    file_path = Column(String(500), nullable=False)          # 工作区内相对路径，如 src/index.ts
    file_size = Column(Integer, default=0)                    # 文件大小（字节），目录为 0
    content_hash = Column(String(40), nullable=True)           # 内容 SHA-1 hash（去重用）
    is_directory = Column(Boolean, default=False)             # 是否为目录
    mime_type = Column(String(100), nullable=True)             # MIME 类型
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)
    # 最近访问时间（读取时节流刷新）— TTL 判断取 max(updated_at, last_accessed_at)，
    # 避免用户只读不写时工作区被 TTL 误清空
    last_accessed_at = Column(DateTime, nullable=True)

    # 唯一约束：同一用户同一路径只保留一个版本
    __table_args__ = (UniqueConstraint('user_id', 'file_path', name='uq_user_filepath'),)
