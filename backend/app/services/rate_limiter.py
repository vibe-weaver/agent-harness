import time
from collections import defaultdict
from dataclasses import dataclass, field
from threading import Lock


@dataclass
class VisitorRecord:
    """单个访客的频率记录。"""
    daily_count: int = 0
    daily_date: str = ""
    minute_timestamps: list = field(default_factory=list)
    ip: str = ""                 # 最后一次请求的 IP（兜底用）
    visitor_id: str = ""          # 访客 ID
    first_seen: float = 0.0       # 首次出现时间戳
    last_seen: float = 0.0        # 最后请求时间戳
    total_count: int = 0          # 历史总生成次数


class RateLimiter:
    """基于内存的频率限制器，支持 visitor_id + IP 双维度识别。

    识别优先级：
      1. visitor_id（前端 localStorage 持久化，精确到浏览器）
      2. IP（兜底，防止 visitor_id 丢失时绕过限制）
    """

    def __init__(self):
        # 以 visitor_id 为主键存储
        self._store: dict[str, VisitorRecord] = defaultdict(lambda: VisitorRecord())
        # IP → visitor_id 的映射（用于 IP 兜底）
        self._ip_map: dict[str, set[str]] = defaultdict(lambda: set())
        self._lock = Lock()
        # 全局并发生成计数
        self._concurrent_count: int = 0

    # ── 访客身份解析 ──

    def _resolve_identity(self, visitor_id: str, ip: str) -> str:
        """解析出主标识 key。

        优先使用 visitor_id；如果 visitor_id 为空或可疑，
        则用 IP 关联的已有 visitor_id 兜底。
        """
        vid = (visitor_id or "").strip()
        ip = (ip or "").strip()

        if vid:
            # 记录 IP → visitor_id 映射
            if ip:
                self._ip_map[ip].add(vid)
            return vid

        # visitor_id 为空，尝试用 IP 找到已关联的 visitor_id
        if ip and ip in self._ip_map and self._ip_map[ip]:
            # 取第一个关联的 visitor_id
            return next(iter(self._ip_map[ip]))

        # 都没有，用 IP 作为 key
        return f"ip:{ip}" if ip else "ip:unknown"

    # ── 时段检查 ──

    @staticmethod
    def is_in_allowed_hours(start: int, end: int) -> tuple[bool, str]:
        """检查当前时间是否在允许生成的时段内。

        start=0, end=24 表示全天允许（默认）。
        """
        if start == 0 and end == 24:
            return True, ""

        current_hour = time.localtime().tm_hour

        if start <= end:
            if not (start <= current_hour < end):
                return False, f"当前不在允许生成时段内（{start:02d}:00 - {end:02d}:00）"
        else:
            if not (current_hour >= start or current_hour < end):
                return False, f"当前不在允许生成时段内（{start:02d}:00 - 次日{end:02d}:00）"

        return True, ""

    # ── 并发检查 ──

    def acquire_concurrent(self, max_concurrent: int) -> tuple[bool, str]:
        """尝试获取一个并发槽位。"""
        with self._lock:
            if self._concurrent_count >= max_concurrent:
                return False, f"当前并发生成数已达上限（{max_concurrent}），请稍后再试"
            self._concurrent_count += 1
            return True, ""

    def release_concurrent(self) -> None:
        """释放一个并发槽位。"""
        with self._lock:
            if self._concurrent_count > 0:
                self._concurrent_count -= 1

    @property
    def current_concurrent(self) -> int:
        """当前并发生成数。"""
        with self._lock:
            return self._concurrent_count

    # ── 频率检查（visitor_id + IP 双维度）──

    def check(self, visitor_id: str, ip: str, daily_limit: int, minute_limit: int) -> tuple[bool, str]:
        """检查是否允许调用。返回 (allowed, error_message)。

        使用 visitor_id 作为主标识，IP 作为兜底。
        """
        now = time.time()
        today = time.strftime("%Y-%m-%d")

        with self._lock:
            key = self._resolve_identity(visitor_id, ip)
            record = self._store[key]

            # 更新访客信息
            if not record.visitor_id and visitor_id:
                record.visitor_id = visitor_id
            if ip:
                record.ip = ip
            if not record.first_seen:
                record.first_seen = now
            record.last_seen = now

            # 跨天清零
            if record.daily_date != today:
                record.daily_count = 0
                record.daily_date = today

            # 清理超过 60 秒的时间戳
            record.minute_timestamps = [t for t in record.minute_timestamps if now - t < 60]

            # 检查每日限制
            if record.daily_count >= daily_limit:
                return False, f"今日生成次数已达上限（{daily_limit}次）"

            # 检查每分钟限制
            if len(record.minute_timestamps) >= minute_limit:
                return False, f"请求过于频繁，请稍后再试（每分钟{minute_limit}次）"

            # 通过，记录
            record.daily_count += 1
            record.total_count += 1
            record.minute_timestamps.append(now)
            return True, ""

    def get_usage(self, visitor_id: str, ip: str) -> dict:
        """获取当前访客的使用情况。"""
        today = time.strftime("%Y-%m-%d")
        with self._lock:
            key = self._resolve_identity(visitor_id, ip)
            record = self._store[key]
            if record.daily_date != today:
                record.daily_count = 0
                record.daily_date = today
            return {"daily": record.daily_count}

    # ── 访客列表（供管理端查看）──

    def get_visitors(self, limit: int = 50) -> list[dict]:
        """获取访客使用情况列表，按最后活跃时间倒序。

        key 格式为 user:{id}（登录用户），返回 user_id 字段。
        """
        now = time.time()
        today = time.strftime("%Y-%m-%d")
        with self._lock:
            records = []
            for key, rec in self._store.items():
                if rec.daily_date != today:
                    rec.daily_count = 0
                    rec.daily_date = today
                # 从 key 中提取 user_id（格式 user:{id}）
                user_id = None
                if key.startswith("user:"):
                    try:
                        user_id = int(key[5:])
                    except ValueError:
                        pass
                records.append({
                    "user_id": user_id,
                    "visitor_id": rec.visitor_id or key,
                    "ip": rec.ip or (key[3:] if key.startswith("ip:") else ""),
                    "daily_count": rec.daily_count,
                    "total_count": rec.total_count,
                    "first_seen": rec.first_seen,
                    "last_seen": rec.last_seen,
                    "last_seen_ago": int(now - rec.last_seen) if rec.last_seen else 0,
                })
            # 按最后活跃时间倒序
            records.sort(key=lambda r: r["last_seen"], reverse=True)
            return records[:limit]

    def get_stats(self) -> dict:
        """获取全局统计数据。"""
        with self._lock:
            total_visitors = len(self._store)
            today = time.strftime("%Y-%m-%d")
            active_today = sum(
                1 for rec in self._store.values()
                if rec.daily_date == today and rec.daily_count > 0
            )
            total_generations = sum(rec.total_count for rec in self._store.values())
            return {
                "total_visitors": total_visitors,
                "active_today": active_today,
                "total_generations": total_generations,
                "current_concurrent": self._concurrent_count,
            }


# 全局单例
rate_limiter = RateLimiter()
