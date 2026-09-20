"""用户记忆管理端点（原 AI 生图模块已整体移除，生图能力由 media-router 技能提供）。"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ...core.database import get_db
from ...core.security import get_current_user
from ...models import User

router = APIRouter()


# ════════════════════════════════════════
#  用户记忆管理（让用户看到/删除 Agent 记的内容）
# ════════════════════════════════════════


@router.get("/ai/memories")
async def list_memories(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """列出当前用户的所有记忆（前端管理面板用）。"""
    from ...services.memory_service import recall_memories
    memories = recall_memories(user.id, db, limit=100)
    return {
        "memories": [
            {
                "id": m.id,
                "memory_type": m.memory_type,
                "content": m.content,
                "source": m.source or "agent",
                "access_count": m.access_count or 0,
                "created_at": m.created_at.isoformat() if m.created_at else None,
                "last_used_at": m.last_used_at.isoformat() if m.last_used_at else None,
                "has_embedding": m.embedding is not None,
            }
            for m in memories
        ]
    }


@router.delete("/ai/memories/{memory_id}")
async def delete_memory_endpoint(
    memory_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """删除一条记忆。"""
    from ...services.memory_service import delete_memory
    ok = delete_memory(user.id, memory_id, db)
    if not ok:
        raise HTTPException(status_code=404, detail="记忆不存在")
    return {"ok": True}


@router.delete("/ai/memories")
async def clear_all_memories(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """清空当前用户的所有记忆（谨慎操作，不可恢复）。"""
    from ...models.ai_config import UserMemory
    deleted = db.query(UserMemory).filter(UserMemory.user_id == user.id).delete(
        synchronize_session=False
    )
    db.commit()
    return {"deleted": deleted}
