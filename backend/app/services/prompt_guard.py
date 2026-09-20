"""Prompt Injection 检测器（方案一）

在用户消息到达 LLM 之前，检测和拦截常见的 prompt injection 攻击模式。

拦截策略:
  - 高危模式: 检测到即拒绝（直接返回 400）
  - 中危模式: 标记 + 计数，同一用户短时间内累计触发阈值则拒绝
  - 长度异常: 超长消息可能藏 payload，截断 + 标记

设计原则:
  - 正则预编译，匹配开销 < 0.5ms/条
  - 中危计数使用内存字典 + 时间窗口，自动过期
  - 不阻塞正常对话，只拦截明确攻击模式
"""

import re
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)


# ════════════════════════════════════════
#  高危模式 — 检测到即拒绝
# ════════════════════════════════════════

_HIGH_RISK_PATTERNS: list[re.Pattern] = [
    # "ignore previous instructions" 类
    re.compile(r"ignore\s+(all\s+)?previous\s+(instructions|prompts|rules)", re.IGNORECASE),
    re.compile(r"disregard\s+(everything|all).*(above|before|prior)", re.IGNORECASE),
    # 中文等效表达
    re.compile(r"忽略(之前|以上|前面|上文).*(指令|提示|规则|设定|约束)", re.IGNORECASE),
    re.compile(r"无视(之前|以上|前面).*(指令|提示|规则|设定)", re.IGNORECASE),
    re.compile(r"忘记(之前|以上|前面|上文).*(指令|提示|规则|设定)", re.IGNORECASE),
    # 角色重定义
    re.compile(r"you\s+are\s+now\s+(a|an)\s+", re.IGNORECASE),
    re.compile(r"从现在起(你|请)是", re.IGNORECASE),
    re.compile(r"假装你是|假装你是一个|assume\s+the\s+role\s+of", re.IGNORECASE),
    # 特殊 token 注入 — OpenAI / Anthropic chat 模板标记
    re.compile(r"<\|im_start\|>|<\|im_end\|>|<\|system\|>|<\|user\|>|<\|assistant\|>"),
    # 伪造 system 角色消息
    re.compile(r"^\s*system\s*[:：]", re.IGNORECASE | re.MULTILINE),
    # "enter developer mode" / "jailbreak" 类
    re.compile(r"(developer|debug|root|admin|jailbreak|god)\s+mode", re.IGNORECASE),
    re.compile(r"(开发者|调试|管理员|越狱|上帝)\s*模式", re.IGNORECASE),
    # 直接询问系统记忆/配置内容 — 高危，直接拒绝
    re.compile(r"你的.*(系统记忆|回答偏好|通用约定|安全习惯|依赖管理|风格约束|禁止事项|回答规范|领域知识).*(有哪些|是什么|内容|列出|告诉)", re.IGNORECASE),
    re.compile(r"(系统记忆|系统配置|全局指令).*(有哪些|是什么|内容|列出|告诉)", re.IGNORECASE),
]


# ════════════════════════════════════════
#  中危模式 — 累计计数，超阈值拒绝
# ════════════════════════════════════════

_MEDIUM_RISK_PATTERNS: list[re.Pattern] = [
    # 询问系统提示词内容
    re.compile(r"(show|reveal|display|print|output|share).*(system|prompt|persona|instruction|rule|memory)", re.IGNORECASE),
    re.compile(r"(透露|显示|输出|打印|展示).*(系统|提示词|人格|记忆|配置|指令|设定)", re.IGNORECASE),
    re.compile(r"what\s+(is|are)\s+your.*(instruction|prompt|rule|system|persona|memory)", re.IGNORECASE),
    re.compile(r"你的.*(指令|提示词|系统|设定|配置|记忆).*(是什么|有哪些|内容|多少)", re.IGNORECASE),
    # 询问系统记忆/配置内容 — 更宽泛的匹配
    re.compile(r"(系统记忆|系统配置|全局指令|回答规范|风格约束|禁止事项|领域知识).*(有哪些|是什么|内容|列出|告诉)", re.IGNORECASE),
    re.compile(r"(list|tell me|show me).*(your|the).*(memory|config|rules|instructions|guidelines)", re.IGNORECASE),
    # "你的XX有哪些" 类模式
    re.compile(r"你的.*(回答偏好|通用约定|安全习惯|依赖管理|风格约束|禁止事项)", re.IGNORECASE),
    # 询问敏感信息
    re.compile(r"(api[_\s-]?key|secret|password|token|密钥|密码)", re.IGNORECASE),
    re.compile(r"(\.env|environment\s+var|DATABASE_URL|DB_PASSWORD|JWT_SECRET)", re.IGNORECASE),
    # 要求读取配置文件
    re.compile(r"(read|cat|type|show|display).*(\.env|config\.py|settings\.py|\.env\.|credentials)", re.IGNORECASE),
    re.compile(r"(读取|查看|打开).*(\.env|config|settings|配置文件)", re.IGNORECASE),
]


# ════════════════════════════════════════
#  常量
# ════════════════════════════════════════

# 单条消息最大长度 — 超出截断并标记
MAX_MESSAGE_LEN = 8000

# 中危模式计数阈值 — 在时间窗口内累计触发此数则拒绝
_MEDIUM_RISK_THRESHOLD = 3

# 中危计数时间窗口（秒）
_MEDIUM_RISK_WINDOW = 300  # 5 分钟

# 消息长度异常阈值 — 超过此值标记为可疑
_SUSPICIOUS_LEN = 5000


# ════════════════════════════════════════
#  中危计数器（内存 + 时间窗口）
# ════════════════════════════════════════

# key: user_key -> list[timestamp]
_medium_risk_store: dict[str, list[float]] = {}
_medium_risk_lock_cleanup_done = False


def _record_medium_risk(user_key: str) -> int:
    """记录一次中危触发，返回当前窗口内的计数。"""
    now = time.time()
    # 清理过期记录
    if user_key in _medium_risk_store:
        _medium_risk_store[user_key] = [
            t for t in _medium_risk_store[user_key]
            if now - t < _MEDIUM_RISK_WINDOW
        ]
    else:
        _medium_risk_store[user_key] = []

    _medium_risk_store[user_key].append(now)
    return len(_medium_risk_store[user_key])


def _get_medium_risk_count(user_key: str) -> int:
    """获取当前窗口内的中危计数（不新增记录）。"""
    now = time.time()
    if user_key not in _medium_risk_store:
        return 0
    return len([
        t for t in _medium_risk_store[user_key]
        if now - t < _MEDIUM_RISK_WINDOW
    ])


# ════════════════════════════════════════
#  PromptGuard
# ════════════════════════════════════════


class PromptGuard:
    """用户输入安全过滤器 — 检测 prompt injection 攻击模式。

    使用方法:
        guard = PromptGuard()
        is_safe, reason = guard.check(user_message, user_key="user:1")
        if not is_safe:
            raise HTTPException(400, detail=reason)
    """

    def check(
        self,
        message: str,
        user_key: Optional[str] = None,
    ) -> tuple[bool, str]:
        """检查用户消息，返回 (是否安全, 拒绝原因)。

        Args:
            message: 用户消息原文
            user_key: 用户标识（如 "user:1" 或 IP hash），用于中危计数

        Returns:
            (True, "") — 安全，可放行
            (False, reason) — 不安全，应拒绝
        """
        if not message or not message.strip():
            return True, ""

        # ── 1. 高危模式检测 → 直接拒绝 ──
        for pattern in _HIGH_RISK_PATTERNS:
            if pattern.search(message):
                logger.warning(
                    f"PromptGuard: 高危拦截 user={user_key} pattern={pattern.pattern[:50]}"
                )
                return False, "您的消息包含不安全的内容，请重新表述您的问题。"

        # ── 2. 中危模式检测 → 累计计数 ──
        medium_hits = 0
        for pattern in _MEDIUM_RISK_PATTERNS:
            if pattern.search(message):
                medium_hits += 1

        if medium_hits > 0 and user_key:
            count = _record_medium_risk(user_key)
            if count >= _MEDIUM_RISK_THRESHOLD:
                logger.warning(
                    f"PromptGuard: 中危超阈值 user={user_key} count={count}"
                )
                return False, "您近期多次询问了敏感内容，请稍后再试。"

        # ── 3. 长度异常检测 ──
        if len(message) > _SUSPICIOUS_LEN:
            # 标记但不拒绝 — 只是可疑，不一定是攻击
            logger.info(
                f"PromptGuard: 长度异常 user={user_key} len={len(message)}"
            )

        return True, ""

    def check_history(self, history: list[dict]) -> tuple[bool, str]:
        """检查历史消息中是否有注入内容（可选，通常 _sanitize_history 已处理）。"""
        for msg in history:
            content = msg.get("content", "") if isinstance(msg, dict) else ""
            role = msg.get("role", "") if isinstance(msg, dict) else ""
            if role != "user":
                continue
            for pattern in _HIGH_RISK_PATTERNS:
                if pattern.search(content):
                    return False, "历史消息中包含不安全的内容，请开启新对话。"
        return True, ""


def scan_injection(text: str) -> tuple[bool, str]:
    """纯检测（不计数、不拒绝）：扫描文本是否命中提示注入模式。

    用于工作区文件内容等"不可信数据"的防御性扫描（A3 方案）：
    - 高危模式命中 → 判定为注入，调用方应剥离该内容
    - 中危模式命中 → 判定为可疑（可能是敏感词误伤），调用方记录告警即可

    Args:
        text: 待扫描文本（如单个工作区文件的内容）

    Returns:
        (True, "") — 未命中，可安全注入
        (False, reason) — 命中，reason 为命中的模式描述
    """
    if not text or not text.strip():
        return True, ""
    for pattern in _HIGH_RISK_PATTERNS:
        if pattern.search(text):
            return False, f"高危注入模式: {pattern.pattern[:60]}"
    for pattern in _MEDIUM_RISK_PATTERNS:
        if pattern.search(text):
            return False, f"可疑注入模式: {pattern.pattern[:60]}"
    return True, ""


# 全局单例
prompt_guard = PromptGuard()
