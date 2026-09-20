"""Token 估算与校准模块 — 严格移植 DSH token-meter 包。

对应 DSH 源码:
  - packages/llm/token-meter/src/estimate.ts   → 估算器
  - packages/llm/token-meter/src/index.ts       → TokenMeter 服务
  - packages/llm/token-meter/src/types.ts       → 类型定义
  - packages/llm/token-meter/src/projection.ts  → 投影定义

DSH 的核心设计:
  1. 固定密度估算: CHARS_PER_TOKEN=4, 每条消息加 ROLE_OVERHEAD=4, 每个内容块加 BLOCK_OVERHEAD=4
  2. 校准锚定: 当 provider 返回 usage 时，用真实 token 数替代估算值作为基准
  3. 增量追踪: 后续消息只计算与锚点之间的 delta，避免每次全量重算
  4. 三维分解: systemTokens + toolsTokens + messageTokens = 总请求压力
"""

import json
import logging
import math
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ════════════════════════════════════════
#  固定密度估算常量 — 对应 estimate.ts
# ════════════════════════════════════════

CHARS_PER_TOKEN = 4       # 对应 estimate.ts: const CHARS_PER_TOKEN = 4
BLOCK_OVERHEAD = 4        # 对应 estimate.ts: const BLOCK_OVERHEAD = 4
ROLE_OVERHEAD = 4         # 对应 estimate.ts: export const ROLE_OVERHEAD = 4

# ── 图片 token 估算常量（图片视觉优化3）──
# provider 一律先把图降采样到 ~1-2MP 再按 tile 计费，成本与实际分辨率弱相关、
# 与 base64 长度基本无关。旧口径 len(url)/16 把一张 4MB 图估成 ~35 万 token
# （真实 vision 计费约 1k），偏差约 300 倍：前端压力条被一张图顶满、历史被无谓
# 裁到只剩尾巴、成本告警误报。改为按解码后体积分档、在上限处饱和。
IMAGE_TOKEN_LOW = 85                  # detail=low 的固定成本（OpenAI 口径 85 token）
IMAGE_TOKEN_HIGH = 1105               # auto/high 档单图上限（2048² 以内的常见封顶值）
IMAGE_TOKEN_SATURATE_BYTES = 256 * 1024  # 解码后达此体积即视为已进入上限档


# ════════════════════════════════════════
#  类型定义 — 对应 types.ts
# ════════════════════════════════════════

@dataclass(frozen=True)
class TokenUsage:
    """Provider 返回的 token 用量 — 对应 DSH TokenUsage。"""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True)
class TokenSurfaceNode:
    """Surface 中一条消息的 token 定价 — 对应 TokenSurfaceNode。"""
    seq: int
    tokens: int


@dataclass(frozen=True)
class TokenMeasurement:
    """一次测量快照 — 对应 TokenMeasurement。"""
    baseline_kind: str  # 'none' | 'estimated' | 'usage'
    baseline_tokens: int
    surface_delta_tokens: int
    total_tokens: int
    surface_tokens: int
    nodes: list  # list[TokenSurfaceNode]

    @property
    def context_breakdown(self) -> dict:
        """三维分解 — 对应 ContextBreakdownProjection。"""
        return {
            "system_tokens": getattr(self, '_system_tokens', 0),
            "tools_tokens": getattr(self, '_tools_tokens', 0),
            "message_tokens": self.surface_tokens,
        }


# ════════════════════════════════════════
#  估算器 — 对应 estimate.ts
# ════════════════════════════════════════

def estimate_text(text: str) -> int:
    """估算纯文本的 token 数 — 对应 estimateContent 中 text 分支。"""
    if not text:
        return 0
    return math.ceil(len(text) / CHARS_PER_TOKEN) + BLOCK_OVERHEAD


def _data_url_payload_len(url) -> int:
    """data URL 中 base64 载荷的字符数；远程 URL 或非法格式返回 0。"""
    if not isinstance(url, str) or not url.startswith("data:"):
        return 0
    marker = url.find(";base64,")
    if marker < 0:
        return 0
    return len(url) - (marker + len(";base64,"))


def estimate_image(block: dict) -> int:
    """估算一个 image_url 块的 token 数 — 口径见上方 IMAGE_TOKEN_* 常量注释。"""
    image_url = block.get("image_url")
    if not isinstance(image_url, dict):
        return BLOCK_OVERHEAD
    if str(image_url.get("detail") or "").lower() == "low":
        return IMAGE_TOKEN_LOW + BLOCK_OVERHEAD
    payload_len = _data_url_payload_len(image_url.get("url", ""))
    if payload_len <= 0:
        # 远程 URL（体积不可知）或非法 data URL：按上限保守计，避免漏算
        return IMAGE_TOKEN_HIGH + BLOCK_OVERHEAD
    decoded_bytes = payload_len * 3 // 4
    ratio = min(1.0, decoded_bytes / IMAGE_TOKEN_SATURATE_BYTES)
    return math.ceil(IMAGE_TOKEN_LOW + (IMAGE_TOKEN_HIGH - IMAGE_TOKEN_LOW) * ratio) + BLOCK_OVERHEAD


def estimate_content(content) -> int:
    """递归估算 content 的 token 数 — 对应 estimateContent。

    content 可以是:
    - str: 纯文本
    - list: OpenAI 多模态 content 数组 [{"type": "text", "text": "..."}, {"type": "image_url", ...}]
    """
    if content is None:
        return 0
    if isinstance(content, str):
        return estimate_text(content)
    if isinstance(content, list):
        tokens = 0
        for block in content:
            if isinstance(block, dict):
                block_type = block.get("type", "")
                if block_type in ("text", "reasoning"):
                    tokens += estimate_text(block.get("text", ""))
                elif block_type == "tool-call":
                    tokens += math.ceil(len(block.get("name", "")) / CHARS_PER_TOKEN)
                    tokens += math.ceil(len(block.get("arguments", "")) / CHARS_PER_TOKEN)
                    tokens += BLOCK_OVERHEAD
                elif block_type == "tool-result":
                    tokens += estimate_content(block.get("content"))
                    tokens += BLOCK_OVERHEAD
                elif block_type == "image_url":
                    tokens += estimate_image(block)
                else:
                    tokens += BLOCK_OVERHEAD + math.ceil(len(json.dumps(block, ensure_ascii=False)) / CHARS_PER_TOKEN)
        return tokens
    # dict 等 fallback
    return BLOCK_OVERHEAD + math.ceil(len(json.dumps(content, ensure_ascii=False, default=str)) / CHARS_PER_TOKEN)


def estimate_message(message: dict) -> int:
    """估算一条消息的 token 数 — 对应 estimateMessage。

    message 格式: {"role": "user"|"assistant"|"system", "content": str|list}
    """
    content = message.get("content", "")
    return estimate_content(content) + ROLE_OVERHEAD


def estimate_system_tokens(system_prompt: Optional[str]) -> int:
    """估算 system prompt 的 token 数 — 对应 estimateSystemTokens。"""
    if not system_prompt:
        return 0
    return math.ceil(len(system_prompt) / CHARS_PER_TOKEN) + ROLE_OVERHEAD


def estimate_tools_tokens(tools: Optional[list]) -> int:
    """估算 tool schema 的 token 数 — 对应 estimateToolsTokens。"""
    if not tools:
        return 0
    return math.ceil(len(json.dumps(tools, ensure_ascii=False)) / CHARS_PER_TOKEN) + BLOCK_OVERHEAD


def estimate_header(system_prompt: Optional[str], tools: Optional[list] = None) -> int:
    """估算完整请求信封的 token 数 — 对应 estimateHeader。"""
    return estimate_system_tokens(system_prompt) + estimate_tools_tokens(tools)


# ════════════════════════════════════════
#  压力投影 — 对应 projection.ts / usage-projection.ts
# ════════════════════════════════════════

@dataclass
class ContextPressure:
    """上下文压力投影 — 对应 ContextPressureProjection。"""
    pressure_tokens: Optional[int] = None  # Provider 报告的最近一次请求的 prompt 侧 token
    projected_tokens: Optional[int] = None  # 下一次请求的预估 prompt token
    context_window: Optional[int] = None   # 模型上下文窗口大小


@dataclass
class ContextBreakdown:
    """上下文组成投影 — 对应 ContextBreakdownProjection。"""
    system_tokens: int = 0
    tools_tokens: int = 0
    message_tokens: int = 0


def usage_tokens(usage: TokenUsage) -> int:
    """Provider usage 的总 token — 对应 usageTokens()。"""
    return (usage.input_tokens
            + (usage.cache_read_tokens or 0)
            + (usage.cache_write_tokens or 0)
            + usage.output_tokens)


def pressure_from(usage: TokenUsage) -> int:
    """Prompt 侧压力 — 对应 pressureFrom()。

    input + cache_read + cache_write，不含 output。
    """
    return (usage.input_tokens
            + (usage.cache_read_tokens or 0)
            + (usage.cache_write_tokens or 0))


# ════════════════════════════════════════
#  TokenMeter 服务 — 对应 index.ts 的 TokenMeter class
# ════════════════════════════════════════

class TokenMeter:
    """Token 估算与校准服务 — 对应 DSH TokenMeter。

    核心机制:
    1. 为每条 surface 消息维护 token 定价节点 (TokenSurfaceNode)
    2. 当 provider 返回 usage 时，用真实 token 数校准基准 (anchor)
    3. 后续消息只需计算与 anchor 之间的 surface delta
    4. totalTokens = baseline.tokens + surfaceDeltaTokens

    这避免了每次请求都全量重算，同时保持与 provider 报告值的一致性。
    """

    def __init__(self, context_window: int = 65536):
        self._context_window = context_window
        self._reset()

    def _reset(self):
        """重置测量状态 — 对应 ReplayState 初始化。"""
        self._surface_nodes: list[TokenSurfaceNode] = []
        self._surface_tokens: int = 0
        self._system_tokens: int = 0
        self._tools_tokens: int = 0
        # 锚点: 上一次 provider 返回 usage 时的状态
        self._anchor_surface_tokens: Optional[int] = None
        self._anchor_pressure_tokens: Optional[int] = None
        self._seq_counter: int = 0

    def set_context_window(self, window: int):
        """设置模型上下文窗口大小。"""
        self._context_window = window

    @property
    def context_window(self) -> int:
        return self._context_window

    def append_message(self, message: dict) -> TokenSurfaceNode:
        """追加一条消息到 surface — 对应 foldSurfaceTokens 的 append 分支。"""
        tokens = estimate_message(message)
        node = TokenSurfaceNode(seq=self._seq_counter, tokens=tokens)
        self._surface_nodes.append(node)
        self._surface_tokens += tokens
        self._seq_counter += 1
        return node

    def set_header(self, system_prompt: Optional[str], tools: Optional[list] = None):
        """设置请求信封 — 对应 request/header 事件。"""
        self._system_tokens = estimate_system_tokens(system_prompt)
        self._tools_tokens = estimate_tools_tokens(tools)

    def record_usage(self, usage: TokenUsage):
        """记录 provider 返回的 usage — 对应 assistant/message 事件中的 usage 校准。"""
        self._anchor_pressure_tokens = pressure_from(usage)
        self._anchor_surface_tokens = self._surface_tokens

    def measure(self) -> TokenMeasurement:
        """测量当前请求压力 — 对应 TokenMeter.measure()。"""
        if self._anchor_pressure_tokens is not None and self._anchor_surface_tokens is not None:
            # 有 provider 锚点: projected = pressure + delta
            baseline_tokens = self._anchor_pressure_tokens
            surface_delta = self._surface_tokens - self._anchor_surface_tokens
            total = max(0, baseline_tokens + surface_delta)
        elif self._surface_tokens == 0 and self._system_tokens == 0:
            # 空状态
            baseline_tokens = 0
            surface_delta = 0
            total = 0
        else:
            # 纯估算模式
            baseline_tokens = self._system_tokens + self._tools_tokens + self._surface_tokens
            surface_delta = 0
            total = baseline_tokens

        return TokenMeasurement(
            baseline_kind='usage' if self._anchor_pressure_tokens is not None else 'estimated',
            baseline_tokens=baseline_tokens,
            surface_delta_tokens=surface_delta,
            total_tokens=total,
            surface_tokens=self._surface_tokens,
            nodes=list(self._surface_nodes),
        )

    def get_pressure(self) -> ContextPressure:
        """获取上下文压力投影 — 对应 contextPressureProjectionDefinition.view()。"""
        m = self.measure()
        pressure = self._anchor_pressure_tokens
        if pressure is not None:
            projected = max(0, pressure + (self._surface_tokens - (self._anchor_surface_tokens or 0)))
        else:
            projected = m.total_tokens if m.total_tokens > 0 else None
        return ContextPressure(
            pressure_tokens=pressure,
            projected_tokens=projected,
            context_window=self._context_window,
        )

    def get_breakdown(self) -> ContextBreakdown:
        """获取上下文组成投影 — 对应 contextBreakdownProjectionDefinition.view()。"""
        return ContextBreakdown(
            system_tokens=self._system_tokens,
            tools_tokens=self._tools_tokens,
            message_tokens=self._surface_tokens,
        )

    def load_history(self, messages: list[dict], system_prompt: Optional[str] = None):
        """从历史消息列表加载 — 用于会话恢复时重建 surface。"""
        self._reset()
        if system_prompt:
            self.set_header(system_prompt)
        for msg in messages:
            self.append_message(msg)

    def shadow_range(self, start_seq: int, end_seq: int) -> int:
        """遮蔽（替换）一个范围 — 对应 foldSurfaceTokens 的 replace 分支。

        返回被遮蔽的 token 总数。
        """
        start_idx = None
        end_idx = None
        for i, node in enumerate(self._surface_nodes):
            if node.seq == start_seq:
                start_idx = i
            if node.seq == end_seq:
                end_idx = i
        if start_idx is None or end_idx is None or start_idx > end_idx:
            raise ValueError(f"shadow_range: invalid range {start_seq}-{end_seq}")
        shadowed = sum(n.tokens for n in self._surface_nodes[start_idx:end_idx + 1])
        return shadowed

    def replace_range(self, start_seq: int, end_seq: int, summary_tokens: int):
        """用摘要替换一个范围 — 对应 compaction 的 surface replace 操作。"""
        start_idx = None
        end_idx = None
        for i, node in enumerate(self._surface_nodes):
            if node.seq == start_seq:
                start_idx = i
            if node.seq == end_seq:
                end_idx = i
        if start_idx is None or end_idx is None or start_idx > end_idx:
            raise ValueError(f"replace_range: invalid range {start_seq}-{end_seq}")
        shadowed = sum(n.tokens for n in self._surface_nodes[start_idx:end_idx + 1])
        new_node = TokenSurfaceNode(seq=self._seq_counter, tokens=summary_tokens)
        self._surface_nodes[start_idx:end_idx + 1] = [new_node]
        self._surface_tokens += summary_tokens - shadowed
        self._seq_counter += 1
        return shadowed
