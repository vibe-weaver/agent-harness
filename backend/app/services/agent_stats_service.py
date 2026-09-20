"""Agent 运营统计（分析项 #9）

数据分两层：
- 实时层：dsh_agent_tasks 表内仍存在的行（终态任务 2 天后被每日清理，#7）
- 历史层：dsh_agent_daily_stats 按天滚动汇总（清理前落账，一行一天）

查询时两层按日期相加即全量：rollup 只含已删除的任务、实时层只含仍在表里的
任务，二者天然不相交。同一天的任务可能分两批清理（finished_at 越过 cutoff
的先后不同），因此落账对已有行做累加合并而不是覆盖。
"""

import datetime
import json

from sqlalchemy.orm import Session

from ..models import AgentTask, AgentCostAlert, DshAgentDailyStats, User

# 与 chat.py _TERMINAL_TASK_STATUSES / 轮数上限保持一致
_TERMINAL_STATUSES = ("done", "failed", "cancelled")
_DEFAULT_MAX_ROUNDS = 40


def _parse_json(raw, fallback):
    """容错解析 JSON 列：空/损坏/类型不符时返回 fallback。"""
    try:
        v = json.loads(raw) if raw else None
    except (ValueError, TypeError):
        return fallback
    return v if isinstance(v, type(fallback)) else fallback


def _empty_day() -> dict:
    """单日指标骨架（rollup metrics 与内存聚合共用同一结构）。"""
    return {
        "agent": 0, "chat": 0,
        "done": 0, "failed": 0, "cancelled": 0, "running": 0,
        "duration_sum": 0.0, "duration_count": 0,
        "dur_lt10": 0, "dur_10_60": 0, "dur_1_5m": 0, "dur_5_20m": 0, "dur_gt20m": 0,
        "rounds_sum": 0, "rounds_count": 0, "hit_cap": 0,
        "rnd_0": 0, "rnd_1_3": 0, "rnd_4_10": 0, "rnd_11_20": 0, "rnd_21_39": 0,
        "tools": {},            # tool -> [calls, failures]
        "switch_total": 0,
        "switch_status": {},    # HTTP 状态码 -> 次数
        "switch_target": {},    # 备选模型名 -> 次数
        # 性能优化1：token 用量（NULL 行不计，仅累计 provider 有上报的任务）
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        # 性能优化7：prompt cache 命中 tokens（prompt_tokens 的子集，折扣计费）
        "cache_read_tokens": 0,
        "token_tasks": 0,       # 有 token 上报的任务数（算均值用）
        "users": {},            # user_id(str) -> {"tasks":0,"failed":0,"tool_calls":0}
    }


def _is_agent_task(task: AgentTask) -> bool:
    """Agent 模式判定：优先 enable_tools 标记；迁移前的旧行（NULL）以
    是否发生过工具调用兜底（零工具调用的旧 Agent 任务会被计入纯聊天，
    旧行 2 天内自然清空，误差不累积）。"""
    if task.enable_tools:
        return True
    return bool(_parse_json(task.tool_events, []))


def _aggregate_task_rows(tasks) -> dict:
    """把一批 AgentTask 行按 date(created_at) 聚合为 {date_str: day_metrics}。"""
    from .llm_service import _is_tool_failure  # 失败标记与 C2 连续失败检测共用一套约定

    days: dict[str, dict] = {}
    for t in tasks:
        d = days.setdefault(
            (t.created_at or datetime.datetime.utcnow()).strftime("%Y-%m-%d"),
            _empty_day(),
        )
        # 模型降级链触发对 Agent/纯聊天都成立（chat_stream 回调层面的事），先记
        for s in _parse_json(t.model_switches, []):
            if not isinstance(s, dict):
                continue
            d["switch_total"] += 1
            status = str(s.get("status", "?"))
            d["switch_status"][status] = d["switch_status"].get(status, 0) + 1
            target = str(s.get("to", "?"))
            d["switch_target"][target] = d["switch_target"].get(target, 0) + 1

        # 性能优化1：token 用量 — Agent 与纯聊天都计费，统一累计（成本视角）；
        # NULL（provider 未上报/迁移前旧行）不计，与真实 0 区分
        _tt = getattr(t, "total_tokens", None)
        if _tt:
            d["prompt_tokens"] += int(getattr(t, "prompt_tokens", 0) or 0)
            d["completion_tokens"] += int(getattr(t, "completion_tokens", 0) or 0)
            d["total_tokens"] += int(_tt)
            # 性能优化7：缓存命中（迁移前旧行/无缓存 = NULL → 0）
            d["cache_read_tokens"] += int(getattr(t, "cache_read_tokens", 0) or 0)
            d["token_tasks"] += 1

        if not _is_agent_task(t):
            d["chat"] += 1
            continue
        d["agent"] += 1

        if t.status in _TERMINAL_STATUSES:
            d[t.status] += 1
        elif t.status == "running":
            d["running"] += 1

        if t.created_at and t.finished_at:
            sec = (t.finished_at - t.created_at).total_seconds()
            d["duration_sum"] += sec
            d["duration_count"] += 1
            if sec < 10:
                d["dur_lt10"] += 1
            elif sec < 60:
                d["dur_10_60"] += 1
            elif sec < 300:
                d["dur_1_5m"] += 1
            elif sec < 1200:
                d["dur_5_20m"] += 1
            else:
                d["dur_gt20m"] += 1

        events = [e for e in _parse_json(t.tool_events, []) if isinstance(e, dict)]
        if events:
            rounds = [e.get("round") for e in events if isinstance(e.get("round"), int)]
            if rounds:
                max_round = max(rounds)
                d["rounds_sum"] += max_round
                d["rounds_count"] += 1
                cap = next(
                    (e["max_rounds"] for e in events
                     if isinstance(e.get("max_rounds"), int)),
                    _DEFAULT_MAX_ROUNDS,
                )
                if max_round >= cap:
                    d["hit_cap"] += 1
                if max_round == 0:
                    d["rnd_0"] += 1
                elif max_round <= 3:
                    d["rnd_1_3"] += 1
                elif max_round <= 10:
                    d["rnd_4_10"] += 1
                elif max_round <= 20:
                    d["rnd_11_20"] += 1
                elif max_round < cap:
                    d["rnd_21_39"] += 1
            for e in events:
                stat = d["tools"].setdefault(str(e.get("tool") or "(未知)"), [0, 0])
                stat[0] += 1
                if _is_tool_failure(str(e.get("result") or "")):
                    stat[1] += 1

        u = d["users"].setdefault(
            str(t.user_id), {"tasks": 0, "failed": 0, "tool_calls": 0})
        u["tasks"] += 1
        if t.status == "failed":
            u["failed"] += 1
        u["tool_calls"] += len(events)
    return days


def _merge_day(dst: dict, src: dict) -> dict:
    """把 src 的指标累加进 dst（同构 day dict；兼容旧 rollup 行缺新字段）。

    支持三层：顶层数值、嵌套 dict（工具/降级计数）、嵌套 dict 的 dict（用户）。
    """
    for k, v in src.items():
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            dst[k] = dst.get(k, 0) + v
        elif isinstance(v, dict):
            m = dst.setdefault(k, {})
            for k2, v2 in v.items():
                if isinstance(v2, list) and len(v2) == 2:
                    cur = m.setdefault(k2, [0, 0])
                    cur[0] += v2[0] or 0
                    cur[1] += v2[1] or 0
                elif isinstance(v2, (int, float)):
                    m[k2] = m.get(k2, 0) + v2
                elif isinstance(v2, dict):
                    inner = m.setdefault(k2, {})
                    for k3, v3 in v2.items():
                        if isinstance(v3, (int, float)):
                            inner[k3] = inner.get(k3, 0) + v3
    return dst


def rollup_tasks_to_daily_stats(db: Session, tasks) -> int:
    """把即将删除的终态任务按天滚动落账（与删除同事务，失败则上层放弃删除）。

    同一天分两批清理时对已有行累加合并，保证恰好落账一次。返回涉及的天数。
    """
    days = _aggregate_task_rows(tasks)
    for date_str, metrics in days.items():
        row = (
            db.query(DshAgentDailyStats)
            .filter(DshAgentDailyStats.stat_date == date_str)
            .first()
        )
        if row is None:
            db.add(DshAgentDailyStats(
                stat_date=datetime.date(*map(int, date_str.split("-"))),
                metrics=json.dumps(metrics, ensure_ascii=False),
            ))
        else:
            merged = _merge_day(_parse_json(row.metrics, {}), metrics)
            row.metrics = json.dumps(merged, ensure_ascii=False)
    db.flush()
    return len(days)


def get_agent_stats(db: Session, days: int = 30) -> dict:
    """Agent 运营统计总查询：任务量/成功率/时长/轮数/工具失败率/降级/Top 用户。"""
    days = min(max(int(days), 1), 90)
    today = datetime.date.today()
    start = today - datetime.timedelta(days=days - 1)
    start_dt = datetime.datetime.combine(start, datetime.time.min)

    live_days = _aggregate_task_rows(
        db.query(AgentTask)
        .filter(AgentTask.created_at >= start_dt)
        .order_by(AgentTask.created_at.asc())
        .limit(50000)
        .all()
    )
    rollup_days = {
        r.stat_date.strftime("%Y-%m-%d"): _parse_json(r.metrics, {})
        for r in db.query(DshAgentDailyStats)
        .filter(DshAgentDailyStats.stat_date >= start)
        .all()
    }

    combined: dict[str, dict] = {}
    for dt in sorted(set(live_days) | set(rollup_days)):
        base = _empty_day()
        _merge_day(base, rollup_days.get(dt, {}))
        _merge_day(base, live_days.get(dt, {}))
        combined[dt] = base

    def _tot(key: str) -> float:
        return sum(d.get(key, 0) for d in combined.values())

    # 工具/降级/用户为嵌套 dict，跨天合并
    tools: dict[str, list] = {}
    switch_status: dict[str, int] = {}
    switch_target: dict[str, int] = {}
    users: dict[str, dict] = {}
    for d in combined.values():
        for tool, (calls, fails) in (d.get("tools") or {}).items():
            stat = tools.setdefault(tool, [0, 0])
            stat[0] += calls or 0
            stat[1] += fails or 0
        for k, v in (d.get("switch_status") or {}).items():
            switch_status[k] = switch_status.get(k, 0) + v
        for k, v in (d.get("switch_target") or {}).items():
            switch_target[k] = switch_target.get(k, 0) + v
        for uid, u in (d.get("users") or {}).items():
            e = users.setdefault(uid, {"tasks": 0, "failed": 0, "tool_calls": 0})
            for k in e:
                e[k] += u.get(k, 0) or 0

    user_ids = [int(u) for u in users if str(u).isdigit()]
    email_map = {}
    if user_ids:
        email_map = {
            str(u.id): u.email
            for u in db.query(User).filter(User.id.in_(user_ids)).all()
        }

    terminal = int(_tot("done") + _tot("failed") + _tot("cancelled"))
    duration_count = int(_tot("duration_count"))
    rounds_count = int(_tot("rounds_count"))
    token_tasks = int(_tot("token_tasks"))

    # 性能优化9：统计窗口内的任务级成本告警（最新在前，最多 20 条）
    alert_rows = (
        db.query(AgentCostAlert)
        .filter(AgentCostAlert.created_at >= start_dt)
        .order_by(AgentCostAlert.created_at.desc())
        .limit(20)
        .all()
    )

    return {
        "days": days,
        "from_date": start.isoformat(),
        "to_date": today.isoformat(),
        "overview": {
            "agent_tasks": int(_tot("agent")),
            "chat_tasks": int(_tot("chat")),
            "done": int(_tot("done")),
            "failed": int(_tot("failed")),
            "cancelled": int(_tot("cancelled")),
            "running": int(_tot("running")),
            "success_rate": round(_tot("done") / terminal * 100, 1) if terminal else 0,
            "avg_duration_s": round(_tot("duration_sum") / duration_count, 1) if duration_count else 0,
            "avg_rounds": round(_tot("rounds_sum") / rounds_count, 1) if rounds_count else 0,
            "hit_cap": int(_tot("hit_cap")),
            "switch_total": int(_tot("switch_total")),
            # 性能优化1：token 成本维度（仅统计 provider 有上报的任务）
            "prompt_tokens": int(_tot("prompt_tokens")),
            "completion_tokens": int(_tot("completion_tokens")),
            "total_tokens": int(_tot("total_tokens")),
            "token_tasks": token_tasks,
            "avg_tokens_per_task": round(_tot("total_tokens") / token_tasks, 1) if token_tasks else 0,
            # 性能优化7：缓存命中维度（命中率 = 命中/输入，衡量前缀稳定性）
            "cache_read_tokens": int(_tot("cache_read_tokens")),
            "cache_hit_rate": round(_tot("cache_read_tokens") / _tot("prompt_tokens") * 100, 1) if _tot("prompt_tokens") else 0,
        },
        # 每日趋势（含今日，今日数据来自实时层）
        "daily": [
            {
                "date": dt,
                "agent": d.get("agent", 0),
                "chat": d.get("chat", 0),
                "failed": d.get("failed", 0),
            }
            for dt, d in combined.items()
        ],
        "durations": {
            "lt10s": int(_tot("dur_lt10")),
            "10to60s": int(_tot("dur_10_60")),
            "1to5m": int(_tot("dur_1_5m")),
            "5to20m": int(_tot("dur_5_20m")),
            "gt20m": int(_tot("dur_gt20m")),
        },
        "rounds": {
            "r0": int(_tot("rnd_0")),
            "r1_3": int(_tot("rnd_1_3")),
            "r4_10": int(_tot("rnd_4_10")),
            "r11_20": int(_tot("rnd_11_20")),
            "r21_39": int(_tot("rnd_21_39")),
        },
        "tools": [
            {
                "tool": tool,
                "calls": calls,
                "failures": fails,
                "failure_rate": round(fails / calls * 100, 1) if calls else 0,
            }
            for tool, (calls, fails) in sorted(
                tools.items(), key=lambda kv: -kv[1][0])
        ],
        "switches": {
            "by_status": dict(sorted(switch_status.items(), key=lambda kv: -kv[1])),
            "by_target": dict(sorted(switch_target.items(), key=lambda kv: -kv[1])),
        },
        "top_users": [
            {
                "user_id": int(uid),
                "email": email_map.get(uid, ""),
                "agent_tasks": u["tasks"],
                "failed": u["failed"],
                "tool_calls": u["tool_calls"],
            }
            for uid, u in sorted(users.items(), key=lambda kv: -kv[1]["tasks"])[:10]
        ],
        # 性能优化9：任务级成本告警（tokens/duration 超阈值的终态任务）
        "alerts": [
            {
                "task_id": a.task_id,
                "user_id": a.user_id,
                "alert_type": a.alert_type,
                "value": a.value,
                "threshold": a.threshold,
                "message": a.message,
                "created_at": a.created_at.strftime("%Y-%m-%d %H:%M:%S") if a.created_at else "",
            }
            for a in alert_rows
        ],
    }
