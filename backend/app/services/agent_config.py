"""Agent 办公模式配置读取与并发控制（B2/B3/A3/B5 方案）。

- get_agent_config(db): 从 dsh_configs 单行表读取 Agent 参数。
  列缺失或值为 None 时用默认值兜底（兼容未迁移的旧库）。
- AgentConcurrencyManager: 全局 + 每用户并发限制（B5）。
  基于 Condition 的计数信号量，limit 可在运行时动态调整
  （管理端保存后，下一次请求 configure() 即生效，无需重启后端）。

设计说明：
- 只有 Agent 办公模式（enable_tools=True）的请求参与并发限制，
  纯聊天已有分钟级限流 + 每日配额，不排队。
- 排队用无限等待（acquire 不带超时）：LLM 请求有 httpx 300s 超时、
  工具执行有子进程超时，release 一定会发生，不会死等。
"""

import logging
import threading
import time

from ..models import DshConfig

logger = logging.getLogger(__name__)

# 默认值（与数据库列默认一致，用于缺失列/旧数据兜底）
DEFAULTS = {
    "agent_max_tool_rounds": 40,        # B2: 单个任务最大工具调用轮数
    "agent_max_output_tokens": 0,       # B2: 每轮最大输出 token（0=不限制）
    "agent_concurrent_limit": 4,        # B5: 全局并发 Agent 任务数
    "agent_per_user_concurrent": 1,     # B5: 每用户并发 Agent 任务数
    "agent_workspace_guard": True,      # A3: 工作区内容注入防御开关
    "agent_compact_prompt": True,       # B3: 工具轮次精简 system prompt 开关
    "agent_stable_prefix": True,        # 性能优化7: 前缀稳定（跳过 B3，保 provider 端自动缓存命中）
}


def get_agent_config(db) -> dict:
    """读取 Agent 办公参数（单行表 dsh_configs id=1）。

    列缺失（旧库未迁移）或值为 None 时回退默认值，保证不抛错。
    """
    cfg = db.query(DshConfig).filter(DshConfig.id == 1).first()
    if not cfg:
        return dict(DEFAULTS)
    out = {}
    for key, default in DEFAULTS.items():
        val = getattr(cfg, key, default)
        out[key] = default if val is None else val
    return out


class AgentConcurrencyManager:
    """Agent 请求并发限制 — 全局 + 每用户，limit 运行时动态可调。

    使用 Condition 实现计数信号量：
    - 全局：同时执行的 Agent 任务数 <= agent_concurrent_limit
    - 每用户：同一用户同时执行的 Agent 任务数 <= agent_per_user_concurrent
    - acquire 失败时阻塞等待（排队），直到有槽位释放。
    """

    def __init__(self, global_limit: int = 4, per_user_limit: int = 1):
        self._cond = threading.Condition()
        self._count = 0                       # 全局正在执行的 Agent 任务数
        self._user_counts: dict[int, int] = {}  # user_id -> 该用户正在执行的任务数
        self._global_limit = max(1, int(global_limit))
        self._per_user_limit = max(1, int(per_user_limit))

    def configure(self, global_limit: int, per_user_limit: int):
        """更新 limit（管理端保存后调用，即时生效）。"""
        with self._cond:
            self._global_limit = max(1, int(global_limit or 1))
            self._per_user_limit = max(1, int(per_user_limit or 1))
            self._cond.notify_all()

    def acquire(self, user_id: int, timeout: float | None = None) -> bool:
        """尝试获取并发槽位；无槽位时阻塞排队直到可用。

        Args:
            user_id: 用户 ID
            timeout: 可选等待超时（秒）；None = 无限等待

        Returns:
            True — 已获取槽位（调用方必须配对 release）
            False — 等待超时仍未获取
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while (
                self._count >= self._global_limit
                or self._user_counts.get(user_id, 0) >= self._per_user_limit
            ):
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    self._cond.wait(remaining)
                else:
                    self._cond.wait()
            self._count += 1
            self._user_counts[user_id] = self._user_counts.get(user_id, 0) + 1
            return True

    def release(self, user_id: int):
        """释放并发槽位（与 acquire 配对，必须成对调用）。"""
        with self._cond:
            self._count = max(0, self._count - 1)
            left = max(0, self._user_counts.get(user_id, 0) - 1)
            if left == 0:
                self._user_counts.pop(user_id, None)
            else:
                self._user_counts[user_id] = left
            self._cond.notify_all()


# 全局单例（模块级），各请求共享
agent_concurrency = AgentConcurrencyManager()
