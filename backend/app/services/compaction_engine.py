"""上下文压缩引擎 — 严格移植 DSH compaction + compaction-basic 包。

对应 DSH 源码:
  - packages/compaction/compaction/src/types.ts       → CompactionResult 类型
  - packages/compaction/compaction/src/index.ts        → CompactionEngine 抽象
  - packages/compaction/compaction/src/checkpoint.ts   → 检查点标记
  - packages/compaction/compaction-basic/src/config.ts → 阈值/保留策略
  - packages/compaction/compaction-basic/src/region.ts → 区域选取+替换
  - packages/compaction/compaction-basic/src/summarizer.ts → 7维度摘要

DSH 的核心设计:
  1. thresholdRatio=0.8: 上下文窗口 80% 时触发压缩
  2. retainRatio=0.16: 保留最近 16% 的消息不动
  3. selectCompactableRange: 从尾部往前累积 retainTokens，前面的可压缩
  4. 7维度结构化摘要: Primary Request / Key Concepts / Files / Errors / Pending / Current / Next
  5. 摘要必须比被替换的内容小，否则拒绝
"""

import json
import logging
import math
import uuid
from dataclasses import dataclass, field
from typing import Optional

import httpx
from sqlalchemy.orm import Session

from .token_meter import (
    TokenMeter, TokenUsage, TokenSurfaceNode,
    estimate_message, estimate_text, estimate_system_tokens,
    pressure_from, usage_tokens,
)

logger = logging.getLogger(__name__)


# ════════════════════════════════════════
#  配置 — 对应 compaction-basic/src/config.ts
# ════════════════════════════════════════

DEFAULT_THRESHOLD_RATIO = 0.8   # 对应 config.ts: DEFAULT_THRESHOLD_RATIO = 0.8
DEFAULT_RETAIN_RATIO = 0.16     # 对应 config.ts: DEFAULT_RETAIN_RATIO = 0.16
DEFAULT_MAX_TOKENS = 8192       # 对应 config.ts: 摘要生成 maxTokens 默认 8192
DEFAULT_COMPACTION_RETRIES = 1  # 对应 config.ts: compactionRetries 默认 1


@dataclass(frozen=True)
class CompactSpec:
    """压缩规格 — 对应 ResolvedCompactSpec。"""
    context_window: int
    threshold_tokens: int       # 触发压缩的 token 阈值
    retain_tokens: int          # 保留不压缩的尾部 token 数
    max_tokens: int             # 摘要生成 max_tokens


def resolve_compact_spec(
    context_window: int,
    threshold_ratio: float = DEFAULT_THRESHOLD_RATIO,
    retain_ratio: float = DEFAULT_RETAIN_RATIO,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> CompactSpec:
    """计算压缩规格 — 对应 resolveCompactSpec()。"""
    threshold_tokens = math.floor(context_window * threshold_ratio)
    retain_tokens = math.floor(context_window * retain_ratio)
    if retain_tokens >= threshold_tokens:
        raise ValueError(
            f"retain_tokens ({retain_tokens}) must be less than threshold_tokens ({threshold_tokens})"
        )
    return CompactSpec(
        context_window=context_window,
        threshold_tokens=threshold_tokens,
        retain_tokens=retain_tokens,
        max_tokens=max_tokens,
    )


# ════════════════════════════════════════
#  摘要指令 — 对应 summarizer.ts 的 COMPACTION_INSTRUCTION
# ════════════════════════════════════════

SUMMARY_OPEN_TAG = "<compacted-summary>"
SUMMARY_CLOSE_TAG = "</compacted-summary>"

# 严格对应 summarizer.ts 的 CHECKPOINT_PREAMBLE
CHECKPOINT_PREAMBLE = (
    "This is an automatically generated checkpoint condensing an earlier span of the conversation "
    "to free up context. Treat the captured context as established background and build on it "
    "without restating it. Continue the task directly from the messages that follow, "
    "without acknowledging this checkpoint."
)

# 严格对应 summarizer.ts 的 COMPACTION_INSTRUCTION — 7 维度结构化摘要
COMPACTION_INSTRUCTION = """You are now acting as a compaction engine for this AI coding assistant. Condense the conversation ABOVE into a structured checkpoint that lets another model resume the work with no loss of essential context.

Output EXACTLY the Markdown structure below: keep every section, in order. Use terse bullets, not prose paragraphs. Write "(none)" for an empty section — never drop a section.

## Primary Request and Intent
- [the user's original and evolving goals; quote verbatim where the exact wording matters]

## Key Technical Concepts
- [technologies, frameworks, patterns, and conventions in play]

## Files and Code
- [exact path: why it matters, key changes or snippets]

## Errors and Fixes
- [error: how it was resolved, plus any related user feedback]

## Pending Jobs
- [explicitly requested work not yet completed]

## Current Work
- [precisely what was in progress at this checkpoint]

## Next Step
- [the single next action, directly in line with the most recent request, or "(none)"]

## Critical Context
- [decisions and their rationale, constraints, user preferences, open questions, data needed to continue]

Rules:
- Write concise English engineering prose. Preserve exact file paths, commands, error strings, identifiers, numeric values, function signatures, and syntax fragments.
- Capture user feedback and explicit instructions faithfully, especially corrections.
- Do NOT mention this summarization request or that the context was compacted.
- Output only the checkpoint text: do not call any tool or take any other action.
- If the conversation already contains a <compacted-summary> block, it is a PRIOR checkpoint. Do not copy it forward verbatim: preserve still-true facts, drop stale ones, and merge newer information into a single consolidated summary under the same structure."""


# ════════════════════════════════════════
#  压缩结果 — 对应 compaction/src/types.ts 的 CompactionResult
# ════════════════════════════════════════

@dataclass
class CompactionResult:
    """压缩结果 — 对应 CompactionResult。"""
    compaction_id: str
    summary: str                           # 安全摘要文本
    shadowed_range: dict                   # {"start": seq, "end": seq}
    shadowed_seqs: list[int]
    shadowed_token_count: int              # 被遮蔽的估算 token 数
    summary_token_count: int               # 摘要的估算 token 数
    compaction_usage: Optional[TokenUsage] = None  # 摘要请求的 usage


# ════════════════════════════════════════
#  区域选取 — 对应 region.ts 的 selectCompactableRange
# ════════════════════════════════════════

def select_compactable_range(
    surface_nodes: list[TokenSurfaceNode],
    retain_tokens: int,
) -> Optional[tuple[int, int]]:
    """选取可压缩范围 — 严格对应 selectCompactableRange()。

    DSH 策略:
    1. 从尾部往前累积 token，直到达到 retain_tokens
    2. 累积点之前的就是可压缩范围
    3. 如果全部消息加起来都没达到 retain_tokens，返回 None（没有可压缩的）

    返回 (start_seq, end_seq) 或 None。
    """
    if not surface_nodes:
        return None

    # 从尾部往前累积，找到保留边界
    accumulated = 0
    keep_from_idx = len(surface_nodes)
    for i in range(len(surface_nodes) - 1, -1, -1):
        accumulated += surface_nodes[i].tokens
        keep_from_idx = i
        if accumulated >= retain_tokens:
            break

    # 如果全部需要保留，没有可压缩的
    if keep_from_idx == 0:
        return None

    first = surface_nodes[0]
    cutoff = surface_nodes[keep_from_idx - 1]
    return (first.seq, cutoff.seq)


# ════════════════════════════════════════
#  摘要生成 — 对应 summarizer.ts 的 summarizeWithLlm
# ════════════════════════════════════════

def frame_summary(summary_text: str) -> str:
    """包装摘要为 checkpoint 消息 — 对应 frameSummary()。"""
    return f"{CHECKPOINT_PREAMBLE}\n\n{SUMMARY_OPEN_TAG}\n{summary_text}\n{SUMMARY_CLOSE_TAG}"


async def summarize_with_llm(
    model_name: str,
    api_key: str,
    base_url: str,
    system_prompt: Optional[str],
    messages: list[dict],
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> tuple[str, Optional[TokenUsage]]:
    """调用 LLM 生成摘要 — 对应 summarizeWithLlm()。

    DSH 策略:
    1. 重放被压缩区域的消息（保持 system prompt + tools 不变，复用 KV cache）
    2. 最后追加 compaction instruction 作为 user 消息
    3. 一次性非流式调用

    返回 (summary_text, usage)。
    """
    # 组装消息: 重放区域消息 + 压缩指令
    api_messages = []
    if system_prompt:
        api_messages.append({"role": "system", "content": system_prompt})
    api_messages.extend(messages)
    api_messages.append({"role": "user", "content": COMPACTION_INSTRUCTION})

    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model_name,
        "messages": api_messages,
        "stream": False,
        "max_tokens": max_tokens,
    }

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()

        summary = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        if not summary or not summary.strip():
            raise ValueError("summarization produced no text summary content")

        usage_data = data.get("usage", {})
        usage = TokenUsage(
            input_tokens=usage_data.get("prompt_tokens", 0),
            output_tokens=usage_data.get("completion_tokens", 0),
            cache_read_tokens=0,
            cache_write_tokens=0,
        )

        return summary.strip(), usage

    except httpx.HTTPStatusError as e:
        logger.error(f"摘要生成 API 错误: {e}")
        raise RuntimeError(f"摘要生成失败: {e}")
    except Exception as e:
        logger.error(f"摘要生成失败: {e}")
        raise RuntimeError(f"摘要生成失败: {e}")


# ════════════════════════════════════════
#  压缩引擎 — 对应 compaction-basic/src/index.ts 的 BasicCompactionEngine
# ════════════════════════════════════════

class CompactionEngine:
    """上下文压缩引擎 — 对应 BasicCompactionEngine。

    完整流程:
    1. 检查压力是否超过 threshold (80%)
    2. selectCompactableRange: 选取可压缩范围（保留尾部 retainTokens）
    3. summarizeWithLlm: 用 LLM 生成 7 维度结构化摘要
    4. 验证: 摘要 token 必须小于被替换的 token
    5. 替换: 用摘要消息替换被压缩的范围
    """

    def __init__(self):
        pass

    def should_compact(
        self,
        meter: TokenMeter,
        spec: CompactSpec,
    ) -> bool:
        """判断是否需要压缩 — 对应 compactIfNeeded 的压力检查。"""
        measurement = meter.measure()
        return measurement.total_tokens >= spec.threshold_tokens

    async def compact_if_needed(
        self,
        meter: TokenMeter,
        spec: CompactSpec,
        messages: list[dict],
        system_prompt: Optional[str],
        model_name: str,
        api_key: str,
        base_url: str,
    ) -> Optional[CompactionResult]:
        """自动压缩 — 对应 compactIfNeeded()。

        如果压力超过阈值，选取范围并生成摘要。
        返回 CompactionResult 或 None（无需压缩）。
        """
        measurement = meter.measure()
        if measurement.total_tokens < spec.threshold_tokens:
            return None

        return await self._compact_region(
            meter, spec, messages, system_prompt,
            model_name, api_key, base_url,
        )

    async def compact_now(
        self,
        meter: TokenMeter,
        spec: CompactSpec,
        messages: list[dict],
        system_prompt: Optional[str],
        model_name: str,
        api_key: str,
        base_url: str,
    ) -> Optional[CompactionResult]:
        """手动压缩 — 对应 compactNow()。

        即使未达到阈值也执行压缩。
        """
        return await self._compact_region(
            meter, spec, messages, system_prompt,
            model_name, api_key, base_url,
            force=True,
        )

    async def _compact_region(
        self,
        meter: TokenMeter,
        spec: CompactSpec,
        messages: list[dict],
        system_prompt: Optional[str],
        model_name: str,
        api_key: str,
        base_url: str,
        force: bool = False,
    ) -> Optional[CompactionResult]:
        """执行压缩 — 对应 compactSurfaceRegion()。"""
        surface_nodes = meter._surface_nodes
        if not surface_nodes:
            return None

        # 1. 选取可压缩范围
        retain = 0 if force else spec.retain_tokens
        result_range = select_compactable_range(surface_nodes, retain)
        if result_range is None:
            return None

        start_seq, end_seq = result_range

        # 找到被遮蔽的消息
        start_idx = None
        end_idx = None
        for i, node in enumerate(surface_nodes):
            if node.seq == start_seq:
                start_idx = i
            if node.seq == end_seq:
                end_idx = i
        if start_idx is None or end_idx is None:
            return None

        shadowed_nodes = surface_nodes[start_idx:end_idx + 1]
        shadowed_tokens = sum(n.tokens for n in shadowed_nodes)
        shadowed_messages = messages[start_idx:end_idx + 1]

        logger.info(
            f"compaction: shadowing {len(shadowed_messages)} messages "
            f"(seqs {start_seq}-{end_seq}, ~{shadowed_tokens} tokens)"
        )

        # 2. 生成摘要
        try:
            summary_text, usage = await summarize_with_llm(
                model_name, api_key, base_url,
                system_prompt, shadowed_messages,
                spec.max_tokens,
            )
        except Exception as e:
            logger.error(f"compaction: summarization failed: {e}")
            raise

        # 3. 验证摘要比被替换内容小 — 对应 summarizer.ts 的 shrink 检查
        framed_summary = frame_summary(summary_text)
        summary_tokens = estimate_message({"role": "user", "content": framed_summary})
        if summary_tokens >= shadowed_tokens:
            logger.warning(
                f"compaction: summary ({summary_tokens} tokens) not smaller than shadowed ({shadowed_tokens} tokens); "
                "keeping original"
            )
            return None

        # 4. 替换 surface 范围
        meter.replace_range(start_seq, end_seq, summary_tokens)

        compaction_id = str(uuid.uuid4())

        return CompactionResult(
            compaction_id=compaction_id,
            summary=framed_summary,
            shadowed_range={"start": start_seq, "end": end_seq},
            shadowed_seqs=[n.seq for n in shadowed_nodes],
            shadowed_token_count=shadowed_tokens,
            summary_token_count=summary_tokens,
            compaction_usage=usage,
        )


# ════════════════════════════════════════
#  智能历史选取 — 对应 region.ts 的滑动窗口策略
# ════════════════════════════════════════

def select_history_window(
    messages: list[dict],
    system_prompt: Optional[str],
    context_window: int,
    threshold_ratio: float = DEFAULT_THRESHOLD_RATIO,
    retain_ratio: float = DEFAULT_RETAIN_RATIO,
    max_output_tokens: int = 4096,
) -> tuple[list[dict], dict]:
    """智能选取历史消息窗口 — DSH 风格滑动窗口。

    DSH 策略:
    1. 估算 system prompt + 所有历史消息的 token 数
    2. 如果总 token < threshold (80% * context_window)，全部发送
    3. 如果超过阈值，保留尾部 retain_ratio (16%) 的消息，前面的丢弃
    4. 返回 (选中消息列表, 上下文压力信息)

    这不是简单地"全部发送"或"只发最近N条"，而是基于 token 估算的精确窗口管理。
    """
    system_tokens = estimate_system_tokens(system_prompt)
    output_budget = max_output_tokens

    # 估算每条消息的 token 数
    msg_tokens = [estimate_message(msg) for msg in messages]
    total_msg_tokens = sum(msg_tokens)
    total_tokens = system_tokens + total_msg_tokens

    threshold = math.floor(context_window * threshold_ratio)
    retain_tokens = math.floor(context_window * retain_ratio)

    # 如果总量没超过阈值，全部发送
    if total_tokens + output_budget <= threshold:
        return messages, {
            "total_tokens": total_tokens,
            "system_tokens": system_tokens,
            "message_tokens": total_msg_tokens,
            "tools_tokens": 0,
            "context_window": context_window,
            "threshold": threshold,
            "pressure_percent": min(100, round(total_tokens / context_window * 100)) if context_window > 0 else 0,
            "selected_count": len(messages),
            "total_count": len(messages),
            "compacted": False,
        }

    # 超过阈值: 从尾部往前累积 retain_tokens
    accumulated = 0
    keep_from = len(messages)
    for i in range(len(messages) - 1, -1, -1):
        accumulated += msg_tokens[i]
        keep_from = i
        if accumulated >= retain_tokens:
            break

    if keep_from == 0:
        # 全部消息加起来都没达到 retain_tokens — 全部发送
        selected = messages
    else:
        selected = messages[keep_from:]

    selected_tokens = sum(msg_tokens[keep_from:]) if keep_from < len(msg_tokens) else total_msg_tokens
    final_total = system_tokens + selected_tokens

    return selected, {
        "total_tokens": final_total,
        "system_tokens": system_tokens,
        "message_tokens": selected_tokens,
        "tools_tokens": 0,
        "context_window": context_window,
        "threshold": threshold,
        "pressure_percent": min(100, round(final_total / context_window * 100)) if context_window > 0 else 0,
        "selected_count": len(selected),
        "total_count": len(messages),
        "compacted": len(selected) < len(messages),
        "dropped_count": len(messages) - len(selected),
    }


# 全局单例
compaction_engine = CompactionEngine()
