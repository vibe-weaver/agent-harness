"""每日 AI 配额持久化服务 — 重启不丢失。

公网场景：内存限流器（rate_limiter）重启即清零，攻击者可借服务重启绕过每日配额。
本模块将"每日配额消费计数"落到数据库（ai_daily_usage 表），
分钟级限流与全局并发计数仍由内存 rate_limiter 承担（重启影响可忽略）。
"""

import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

_CHAT_COL = "chat_count"


def _col(kind: str) -> str:
    if kind == "chat":
        return _CHAT_COL
    raise ValueError(f"未知的 usage kind: {kind!r}（期望 chat）")


def get_daily_usage(db: Session, user_id: int, kind: str) -> int:
    """查询用户当日消费次数。

    行存在但该 kind 的列为 NULL 时回退 0：两种 kind（chat/image）共用同一行，
    另一种 kind 先消费会 INSERT 该行（本 kind 列为 NULL 默认值），
    此前直接返回 row[0]=None，导致 check_daily_quota 里 `None >= limit` 抛
    TypeError、进而变成 HTTP 500（用户聊过天后再点生图必现）。
    """
    today = datetime.date.today().isoformat()
    row = db.execute(
        text(
            f"SELECT {_col(kind)} FROM ai_daily_usage "
            "WHERE user_id = :uid AND usage_date = :d"
        ),
        {"uid": user_id, "d": today},
    ).first()
    return row[0] if (row and row[0] is not None) else 0


def check_daily_quota(db: Session, user_id: int, kind: str, limit: int, count: int = 1) -> tuple[bool, str]:
    """检查用户当日配额是否够用。

    count：本次请求需要消费的次数。默认 1，保持旧行为逐字不变；
    count>1 时除「已用尽」外再判一次「剩余不足」，避免批量请求超额扣费。
    """
    # 防御性 or 0：即使 get_daily_usage 因故返回 None/异常值也不至于抛 TypeError
    used = get_daily_usage(db, user_id, kind) or 0
    if used >= limit:
        label = {"chat": "对话"}.get(kind, "使用")
        return False, f"今日{label}次数已达上限（{limit}次）"
    if count > 1 and used + count > limit:
        label = {"chat": "对话"}.get(kind, "使用")
        return False, f"今日{label}剩余次数不足（剩余 {limit - used} 次，本次需要 {count} 次）"
    return True, ""


def increment_daily_usage(db: Session, user_id: int, kind: str, count: int = 1) -> None:
    """记录消费（upsert 自增，MySQL ON DUPLICATE KEY）；count = 本次消费次数。"""
    if count <= 0:
        return
    today = datetime.date.today().isoformat()
    db.execute(
        text(
            f"INSERT INTO ai_daily_usage (user_id, usage_date, {_col(kind)}) "
            f"VALUES (:uid, :d, :c) ON DUPLICATE KEY UPDATE {_col(kind)} = {_col(kind)} + :c"
        ),
        {"uid": user_id, "d": today, "c": count},
    )
    db.commit()
