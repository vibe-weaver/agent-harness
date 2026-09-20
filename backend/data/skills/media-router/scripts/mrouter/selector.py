"""候选模型排序 —— 权重与优先级的具体算法。

三种策略：
  priority_then_weight（默认）
      先按 priority 分档，只在最小 priority 那一档里做加权随机。行为可控：
      priority=1 的模型永远压过 priority=2，但同档多个模型按 weight 分摊流量。
  weight_only
      忽略 priority，全部按 weight 加权随机。适合"就是想让多个模型分流"。
  fallback_chain
      严格按 (priority, -weight) 排序，不做随机。适合视频这类贵且要可预测的场景。

排序结果同时被用作「失败后的降级序列」：第 1 个失败就试第 2 个，以此类推。
"""

from __future__ import annotations

import random
from typing import Any, Iterable

from .config import ModelSpec


def weighted_shuffle(models: Iterable[ModelSpec]) -> list[ModelSpec]:
    """不放回的加权随机抽样，得到一个带优先级的随机序列。"""
    items = list(models)
    ordered: list[ModelSpec] = []
    while items:
        total = sum(max(m.weight, 0.0) for m in items)
        if total <= 0:
            ordered.extend(items)
            break
        point = random.uniform(0.0, total)
        acc = 0.0
        picked = len(items) - 1
        for idx, item in enumerate(items):
            acc += max(item.weight, 0.0)
            if point <= acc:
                picked = idx
                break
        ordered.append(items.pop(picked))
    return ordered


def rank_candidates(
    models: list[ModelSpec],
    strategy: str,
    requires: list[str],
    health: Any,
    now: float | None = None,
) -> tuple[list[ModelSpec], dict[str, Any]]:
    """返回 (按优先级排列的候选模型, 说明信息)。"""
    notes: dict[str, Any] = {
        "capability_relaxed": False,
        "health_relaxed": False,
        "excluded_cooling": [],
    }

    enabled = [m for m in models if m.enabled]
    if not enabled:
        return [], notes

    capable = [m for m in enabled if m.describes(requires)]
    if not capable:
        # 没有模型声明支持所需能力时，放开能力过滤（宁可用可能不匹配的，也不要直接失败）
        capable = enabled
        notes["capability_relaxed"] = True

    healthy = [m for m in capable if not health.is_cooling(m.id, now)]
    notes["excluded_cooling"] = [
        m.id for m in capable if health.is_cooling(m.id, now)
    ]
    if not healthy:
        # 全部在冷却中：与其失败，不如放开熔断并挑最早恢复的那个
        healthy = sorted(capable, key=lambda m: health.cooling_remaining(m.id, now))
        notes["health_relaxed"] = True

    pool = healthy

    if strategy == "fallback_chain":
        ordered = sorted(pool, key=lambda m: (m.priority, -m.weight, m.id))
    elif strategy == "weight_only":
        ordered = weighted_shuffle(pool)
    else:
        ordered = []
        for priority in sorted({m.priority for m in pool}):
            tier = [m for m in pool if m.priority == priority]
            ordered.extend(weighted_shuffle(tier))

    notes["strategy"] = strategy
    notes["requires"] = list(requires)
    notes["candidate_count"] = len(ordered)
    return ordered, notes


def plan_attempts(
    ordered: list[ModelSpec], max_attempts: int
) -> list[ModelSpec]:
    """按 max_attempts 截取实际要尝试的模型序列。"""
    limit = max(1, int(max_attempts or 1))
    return ordered[:limit]
