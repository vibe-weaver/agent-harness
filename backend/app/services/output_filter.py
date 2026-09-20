"""LLM 输出后置过滤器（方案三 — 条件缓冲版）

核心问题:
  之前的完整层 (full_scan) 在流结束后才执行，此时所有 chunk 已通过 SSE
  推送给用户，检测到泄露也只是追加警告，敏感内容已经暴露。

解决方案 — 条件缓冲:
  1. 快速层 (quick_filter): 每个 chunk 即时过滤 API Key、密码等格式化敏感信息
  2. 触发检测 (should_buffer): 每个 chunk 检测是否出现可疑特征词
     （如 "系统记忆"、"回答偏好"、"风格约束" 等）
  3. 一旦触发，进入缓冲模式: 后续 chunk 不直接推送，而是累积
  4. 流结束 (flush): 对缓冲内容做完整扫描
     - 检测到泄露 → 替换为警告消息，丢弃原始内容
     - 未检测到泄露 → 一次性推送全部缓冲内容（延迟 = 缓冲期间）

性能影响:
  - 正常对话（不含敏感内容）: 不进入缓冲模式，零延迟
  - 可疑对话: 进入缓冲模式，用户会感知到延迟，但可接受（安全 > 速度）
"""

import re
import logging

logger = logging.getLogger(__name__)


# ════════════════════════════════════════
#  快速层正则 — 每个 chunk 即时过滤格式化敏感信息
# ════════════════════════════════════════

_QUICK_PATTERNS: list[tuple[re.Pattern, str]] = [
    # OpenAI: sk-xxx (48 字符以上)
    (re.compile(r"sk-[a-zA-Z0-9]{20,}"), "[API_KEY_REDACTED]"),
    # Anthropic: sk-ant-xxx
    (re.compile(r"sk-ant-[a-zA-Z0-9]{20,}"), "[API_KEY_REDACTED]"),
    # 通用 hex 密钥（32 字符以上的连续十六进制串，排除普通代码中的小 hex）
    (re.compile(r"\b[a-f0-9]{40,}\b"), "[HASH_REDACTED]"),
    # AWS Access Key
    (re.compile(r"AKIA[A-Z0-9]{16}"), "[AWS_KEY_REDACTED]"),
    # 数据库连接字符串 mysql://user:pass@host
    (re.compile(r"mysql(\+\w+)?://[^\s:]+:[^\s@]+@[^\s]+"), "[DB_URL_REDACTED]"),
    # 通用数据库连接串 postgres://  mongodb://  redis://
    (re.compile(r"(postgres|mongodb|redis)://[^\s:]+:[^\s@]+@[^\s]+"), "[DB_URL_REDACTED]"),
    # .env 变量赋值: KEY=value
    (re.compile(r"(DB_PASSWORD|JWT_SECRET|QQ_EMAIL_AUTH_CODE|API_KEY|SECRET_KEY)\s*=\s*\S+"),
     "[ENV_REDACTED]"),
]

# 预编译联合正则 — 一次扫描完成快整层过滤，避免逐个 pattern.sub 调用
_QUICK_COMBINED_PATTERN: re.Pattern = re.compile("|".join(p.pattern for p, _ in _QUICK_PATTERNS))
_QUICK_REPLACEMENTS: dict[str, str] = {p.pattern: r for p, r in _QUICK_PATTERNS}


def _quick_filter(text: str) -> str:
    """快整层过滤 — 使用联合正则一次扫描完成所有替换。"""
    def _replacer(m: re.Match) -> str:
        for p, r in _QUICK_PATTERNS:
            if p.match(m.group()):
                return r
        return m.group()
    return _QUICK_COMBINED_PATTERN.sub(_replacer, text)


# ════════════════════════════════════════
#  触发检测 — 出现这些词就进入缓冲模式
# ════════════════════════════════════════

# 系统记忆标题词 — 出现任一个就进入缓冲模式
_BUFFER_TRIGGER_KEYWORDS: list[str] = [
    "系统记忆", "系统配置", "全局指令", "回答偏好", "回答规范",
    "通用约定", "安全习惯", "依赖管理", "风格约束", "禁止事项",
    "领域知识", "安全护栏", "文件输出能力",
    # 英文变体
    "system memory", "system prompt", "guardrail",
]

# 硬编码标记词 — 出现就一定进入缓冲模式
_BUFFER_TRIGGER_PATTERNS: list[re.Pattern] = [
    re.compile(r"【文件输出能力", re.IGNORECASE),
    re.compile(r"【系统记忆", re.IGNORECASE),
    re.compile(r"【安全护栏", re.IGNORECASE),
    re.compile(r"<file-download\s+name=", re.IGNORECASE),
    re.compile(r"你是一个友好的 AI 助手", re.IGNORECASE),
]


# ════════════════════════════════════════
#  完整层正则 — 对缓冲内容做全文扫描
# ════════════════════════════════════════

_PROMPT_LEAK_PATTERNS: list[re.Pattern] = [
    # 硬编码标记词
    re.compile(r"【文件输出能力", re.IGNORECASE),
    re.compile(r"【系统记忆", re.IGNORECASE),
    re.compile(r"【安全护栏", re.IGNORECASE),
    re.compile(r"<file-download\s+name=", re.IGNORECASE),
    re.compile(r"当用户要求你将内容以文件形式发送", re.IGNORECASE),
    re.compile(r"不可覆盖.*优先级最高", re.IGNORECASE | re.DOTALL),
    # 系统记忆模板特征 — 标题 + 列表格式的转述
    re.compile(r"回答规范.*代码块标注.*简洁", re.IGNORECASE | re.DOTALL),
    re.compile(r"领域知识.*技术栈.*React.*FastAPI.*MySQL", re.IGNORECASE | re.DOTALL),
    re.compile(r"风格约束.*友好但不过度.*emoji", re.IGNORECASE | re.DOTALL),
    re.compile(r"禁止事项.*不要透露系统提示词.*不要执行危险", re.IGNORECASE | re.DOTALL),
    # 安全护栏内容转述
    re.compile(r"绝对禁止透露.*复述.*转述.*系统提示词", re.IGNORECASE | re.DOTALL),
    re.compile(r"绝对禁止输出.*API Key.*密钥.*密码", re.IGNORECASE | re.DOTALL),
    re.compile(r"我只能帮你回答(本平台|平台)相关的问题", re.IGNORECASE),
    # 文件输出能力规则转述
    re.compile(r"name 属性必须指定文件名", re.IGNORECASE),
    re.compile(r"标签内是文件的完整内容.*可下载文件", re.IGNORECASE | re.DOTALL),
]

_SERVER_PATH_PATTERNS: list[re.Pattern] = [
    re.compile(r"/opt/agent-harness/[^\s]+"),
    re.compile(r"/home/[^\s/]+/[^\s]+\.py"),
    re.compile(r"D:\\[^\s]+\\后端[^\s]*"),
    re.compile(r"/app/[^\s]+\.py"),
]

# 系统记忆关键词 — 多个同时出现即为转述泄露
_MEMORY_KEYWORDS = [
    "回答规范", "回答偏好", "通用约定", "领域知识",
    "风格约束", "禁止事项", "安全习惯", "依赖管理",
]

# 检测到泄露时的替换消息
_BLOCKED_MESSAGE = "⚠️ 检测到回复中可能包含系统敏感信息，已被安全过滤。如有其他问题，请重新提问。"


# ════════════════════════════════════════
#  StreamFilter — 条件缓冲式流过滤器
# ════════════════════════════════════════


class StreamFilter:
    """流式输出条件缓冲过滤器。

    工作流程:
      1. 每个 chunk 调用 process(text) → 返回 (should_send, filtered_text)
         - 快速层过滤 API Key 等格式化敏感信息（即时）
         - 检测可疑特征词，一旦命中进入缓冲模式
      2. 正常模式下: process 返回 (True, filtered_text)，直接推送
      3. 缓冲模式下: process 返回 (False, "")，不推送，累积到缓冲区
      4. 流结束调用 flush() → 返回 (should_send, final_text)
         - 对缓冲内容做完整扫描
         - 有泄露 → (True, _BLOCKED_MESSAGE)
         - 无泄露 → (True, 全部缓冲内容)

    使用方法:
        sf = StreamFilter()
        # 每个 chunk:
        should_send, text = sf.process(chunk)
        if should_send:
            on_chunk(text)
        # 流结束:
        should_send, text = sf.flush()
        if should_send:
            on_chunk(text)
    """

    def __init__(self):
        self._buffer: list[str] = []
        self._buffering = False
        self._full_response = ""  # 始终维护完整回复（用于后续返回）

    def process(self, text: str) -> tuple[bool, str]:
        """处理一个 chunk。

        Returns:
            (should_send, text): 是否应该推送给用户，以及要推送的文本。
            正常模式: (True, filtered_text)
            缓冲模式: (False, "")
        """
        if not text:
            return True, text

        # 快速层: 即时过滤格式化敏感信息（联合正则一次扫描）
        filtered = _quick_filter(text)

        # 始终累积完整回复
        self._full_response += filtered

        if self._buffering:
            # 已在缓冲模式 — 累积，不推送
            self._buffer.append(filtered)
            return False, ""
        else:
            # 正常模式 — 检测是否需要进入缓冲模式
            if self._should_buffer(filtered):
                logger.info("StreamFilter: 检测到可疑特征，进入缓冲模式")
                self._buffering = True
                # 把已推送之前的内容也纳入缓冲基线
                # 注意: _full_response 已经包含了之前所有已推送的内容
                # 这里只需缓冲从这个 chunk 开始的后续内容
                self._buffer.append(filtered)
                return False, ""
            else:
                # 正常模式 — 直接推送
                return True, filtered

    def flush(self) -> tuple[bool, str]:
        """流结束时调用，处理缓冲内容。

        Returns:
            (should_send, text): 是否推送，以及要推送的文本。
            有泄露 → (True, _BLOCKED_MESSAGE)
            无泄露且有缓冲 → (True, 全部缓冲内容)
            无缓冲 → (False, "")
        """
        if not self._buffering:
            # 未进入缓冲模式 — 检查是否已推送的内容中有泄露
            # （防御纵深：触发词可能出现在已推送的 chunk 中）
            if self._full_response and self._scan_for_leaks(self._full_response):
                logger.warning("StreamFilter: 完整回复检测到泄露（非缓冲模式），发送拦截消息")
                return True, "\n\n" + _BLOCKED_MESSAGE
            return False, ""

        buffered_text = "".join(self._buffer)

        # 对完整回复（含已推送部分）做扫描
        if self._scan_for_leaks(self._full_response):
            logger.warning("StreamFilter: 缓冲内容检测到泄露，已拦截")
            return True, _BLOCKED_MESSAGE
        else:
            # 缓冲内容安全 — 一次性推送
            logger.info("StreamFilter: 缓冲内容安全，一次性推送")
            return True, buffered_text

    # ── 内部方法 ──

    def _should_buffer(self, text: str) -> bool:
        """检测 chunk 中是否出现可疑特征词，决定是否进入缓冲模式。"""
        # 先用集合做快速查找（比逐个 in 更快）
        text_lower = text.lower()
        if any(kw in text or kw.lower() in text_lower for kw in _BUFFER_TRIGGER_KEYWORDS):
            return True
        if any(pattern.search(text) for pattern in _BUFFER_TRIGGER_PATTERNS):
            return True
        return False

    def _scan_for_leaks(self, text: str) -> bool:
        """对文本做完整扫描，检测是否存在泄露。"""
        # 1. 硬编码/模板特征正则
        for pattern in _PROMPT_LEAK_PATTERNS:
            if pattern.search(text):
                logger.warning(f"StreamFilter: 泄露检测 pattern={pattern.pattern[:80]}")
                return True

        # 2. 服务器路径
        for pattern in _SERVER_PATH_PATTERNS:
            if pattern.search(text):
                logger.warning(f"StreamFilter: 服务器路径泄露 pattern={pattern.pattern[:50]}")
                return True

        # 3. 系统记忆关键词多命中
        hit_count = sum(1 for kw in _MEMORY_KEYWORDS if kw in text)
        if hit_count >= 3:
            logger.warning(f"StreamFilter: 系统记忆内容转述 keywords_hit={hit_count}")
            return True

        return False


# 全局单例工厂 — 每次对话创建新的 StreamFilter
def create_stream_filter() -> StreamFilter:
    """为每次对话创建一个新的流过滤器实例。"""
    return StreamFilter()
