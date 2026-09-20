#!/usr/bin/env python3
"""exec.jsonl 的窗口化指标与告警 —— 灰度观测的自动化版（跑在 B 上）。

职责：把 `sandbox-executor` 落在 exec.jsonl 里的审计记录聚合成人能看的指标，
在越限时打 ALERT 并以非零退出码结束（systemd timer 据此把单元标成 failed，
`systemctl --failed` 一眼可见）。设计动机：deployment-sandbox.md §7.3 那套
`jq | sort | uniq -c` 是**一次性人工分析**，转全量之后没人会每天手敲 ——
这个脚本把它变成每小时自动跑、越限自动响的东西。

刻意的边界（每一行 stdlib，不进 executor 的 venv）：
  - 只读 exec.jsonl，不碰 docker、不碰网络（除非显式配了 webhook）
  - 不做推送指标的时间序列存储 —— 需要历史趋势时再上 Prometheus，2GB 的 B
    现在塞不下一个 exporter + TSDB，而"当前窗口越不越限"用 cron 就够了
  - 退出码：0 = 正常（含"窗口内无执行"），1 = 有 ALERT，2 = 用法错误
    （argparse 自带）。文件不存在打提示后退出 0 —— SANDBOX_LOG_FILE 可以被
    显式置空关闭审计，那不是故障，不该让 timer 天天报红。

指标口径与 §7.3 的人工 jq 完全一致，避免两套"官方数字"：
  exit_code 分布 / OOM（oom_killed 或 137）/ 超时（timed_out 或 124）/
  busy 拒绝（429）/ 挂载拒绝（503 mount_not_ready）/ auth 拒绝（401）/
  p50-p99 duration_ms / dropped_lines>0 的截断比例 / docker_error。

跑法：
  python3 exec_metrics.py                     # 最近 24h，人类可读
  python3 exec_metrics.py --window 1          # 最近 1h（配 timer 的周期）
  python3 exec_metrics.py --json              # 给脚本消费
环境变量：
  SANDBOX_ALERT_WEBHOOK   可选。设了就在有 ALERT 时 POST {"text": "..."}
                          （Slack 兼容格式；钉钉/飞书自行包一层转换）。
                          POST 失败只打 stderr，不影响退出码 —— 告警通道
                          本身故障不该被当成"没有告警"。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

DEFAULT_LOG = "/var/log/sandbox-executor/exec.jsonl"

# 告警阈值。不是"按机器调"的量，是信号本身的定义，所以放常量不放 env ——
# 要改它等于改"什么算异常"，该走代码评审而不是 .env。
ALERT_OOM_RATE = 0.05        # OOM 比例上限（§9：384m 起步值，观察后收紧）
ALERT_BUSY_RATIO = 0.10      # 429 比例上限（决定要不要调 max_concurrent / 扩 B）
ALERT_P95_MS = 90_000        # p95 上限：120s 超时预算的 75%，超过说明贴着天花板跑
MIN_SAMPLE_EXEC = 20         # 样本太少时比率类告警全是噪声
MIN_SAMPLE_P95 = 5           # p95 至少要 5 个样本才有意义

# exit_code 语义与 executor.py 的常量一致（那边有测试钉住，这边只做展示）
OOM_EXIT = 137
TIMEOUT_EXIT = 124
DOCKER_FAIL_EXIT = 125


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="聚合 sandbox-executor 的 exec.jsonl 审计日志并按阈值告警")
    p.add_argument("--file", default=DEFAULT_LOG,
                   help=f"exec.jsonl 路径（默认 {DEFAULT_LOG}）")
    p.add_argument("--window", type=float, default=24.0, metavar="HOURS",
                   help="统计窗口，小时（默认 24）")
    p.add_argument("--json", action="store_true",
                   help="输出 JSON（供脚本消费，不打印人类可读报告）")
    return p.parse_args(argv)


def load_records(path: Path, window_start: float) -> tuple[list[dict], int, int]:
    """读日志 → (窗口内记录, 窗口外条数, 畸形行数)。

    逐行 try：AuditLog 是"写不动只降级到 stderr"的设计，磁盘写满时 exec.jsonl
    里可能截出半行 JSON —— 一行坏不能带走整个报告。坏行计数进输出，
    比例异常高本身就是一个信号（日志盘快满了）。
    """
    records: list[dict] = []
    outside = 0
    malformed = 0
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if not isinstance(rec, dict):
                        raise ValueError("not an object")
                except ValueError:
                    malformed += 1
                    continue
                ts = rec.get("ts")
                if not isinstance(ts, (int, float)) or ts < window_start:
                    # 没有 ts 的行（理论上不存在，AuditLog 有 setdefault）按窗口外算
                    outside += 1
                    continue
                records.append(rec)
    except FileNotFoundError:
        return [], -1, 0        # -1 是"文件不存在"的哨兵，调用方单独处理
    return records, outside, malformed


def percentile(sorted_vals: list[float], pct: float) -> float | None:
    """最近邻秩（nearest-rank）分位数。与 §7.3 的 awk 实现同口径：
    a[int(NR*0.95)] —— int() 截断即最近邻秩的下取整变体。空列表返回 None。"""
    if not sorted_vals:
        return None
    k = max(1, int(len(sorted_vals) * pct / 100.0))
    return sorted_vals[k - 1]


def summarize(records: list[dict]) -> dict:
    """窗口内记录 → 指标 dict。只依赖 dict 字段，不碰 I/O，可单测。"""
    executes = [r for r in records if r.get("event") == "execute"]
    refused = [r for r in records if r.get("event") == "refused"]

    def refused_where(**kv) -> int:
        return sum(1 for r in refused if all(r.get(k) == v for k, v in kv.items()))

    durations = sorted(r["duration_ms"] for r in executes
                       if isinstance(r.get("duration_ms"), (int, float)))
    exit_codes: dict[int, int] = {}
    for r in executes:
        code = r.get("exit_code")
        if isinstance(code, int) and not isinstance(code, bool):
            exit_codes[code] = exit_codes.get(code, 0) + 1

    oom = sum(1 for r in executes if r.get("oom_killed")
              or r.get("exit_code") == OOM_EXIT)
    timeouts = sum(1 for r in executes if r.get("timed_out")
                   or r.get("exit_code") == TIMEOUT_EXIT)
    docker_fail = sum(1 for r in executes if r.get("exit_code") == DOCKER_FAIL_EXIT
                      or r.get("docker_error"))
    truncated = sum(1 for r in executes if (r.get("dropped_lines") or 0) > 0)
    busy = refused_where(reason="busy")
    mount_refused = refused_where(reason="mount_not_ready")
    auth_refused = refused_where(reason="auth")
    attempts = len(executes) + busy          # busy 是"想做但没做成"，算分母
    stdout_total = sum(r.get("stdout_lines") or 0 for r in executes)

    def rate(num: int, den: int) -> float | None:
        return (num / den) if den else None

    return {
        "n_execute": len(executes),
        "n_refused": len(refused),
        "busy": busy,
        "mount_not_ready": mount_refused,
        "auth_refused": auth_refused,
        "attempts": attempts,
        "busy_ratio": rate(busy, attempts),
        "oom": oom,
        "oom_rate": rate(oom, len(executes)),
        "timeouts": timeouts,
        "timeout_rate": rate(timeouts, len(executes)),
        "docker_fail": docker_fail,
        "truncated": truncated,
        "truncated_ratio": rate(truncated, len(executes)),
        "cancelled": sum(1 for r in executes if r.get("cancelled")),
        "stdout_total": stdout_total,
        "avg_stdout_lines": rate(stdout_total, len(executes)),
        "exit_codes": exit_codes,
        "p50_ms": percentile(durations, 50),
        "p95_ms": percentile(durations, 95),
        "p99_ms": percentile(durations, 99),
        "duration_max_ms": durations[-1] if durations else None,
    }


def evaluate_alerts(s: dict) -> list[str]:
    """指标 → ALERT 文案列表。阈值是模块常量；样本量门槛挡小样本噪声。"""
    alerts: list[str] = []
    n = s["n_execute"]
    if (s["oom_rate"] is not None and n >= MIN_SAMPLE_EXEC
            and s["oom_rate"] > ALERT_OOM_RATE):
        alerts.append(f"OOM 率 {s['oom_rate']:.1%} 超过 {ALERT_OOM_RATE:.0%}"
                      f"（{s['oom']}/{n} 次）—— 考虑上调 SANDBOX_MEMORY_MB")
    if (s["busy_ratio"] is not None and s["attempts"] >= MIN_SAMPLE_EXEC
            and s["busy_ratio"] > ALERT_BUSY_RATIO):
        alerts.append(f"429 比例 {s['busy_ratio']:.1%} 超过 {ALERT_BUSY_RATIO:.0%}"
                      f"（{s['busy']}/{s['attempts']} 次）—— 考虑调 max_concurrent 或扩 B")
    if (s["p95_ms"] is not None and n >= MIN_SAMPLE_P95
            and s["p95_ms"] > ALERT_P95_MS):
        alerts.append(f"p95 耗时 {s['p95_ms']:.0f}ms 超过 {ALERT_P95_MS}ms"
                      " —— 贴着 120s 超时天花板跑，查是否有失控代码")
    if s["mount_not_ready"]:
        # fail-closed 防线起作用了：哨兵/NFS 出问题。窗口内出现一次就值得看。
        alerts.append(f"挂载拒绝 {s['mount_not_ready']} 次（503 mount_not_ready）"
                      " —— NFS 或哨兵 cron 异常，查 /healthz 的 mounts 字段")
    if s["auth_refused"]:
        # 正常情况应为零。非零 = 有人在探测（或轮换没走完/两边不一致）
        alerts.append(f"认证拒绝 {s['auth_refused']} 次（401）"
                      " —— 令牌不一致或被探测，与 A 侧对账")
    if s["docker_fail"]:
        alerts.append(f"容器层故障 {s['docker_fail']} 次（125/docker_error）"
                      " —— 镜像缺失或 daemon 异常，/healthz 的 docker_version 会是 null")
    return alerts


def human_report(s: dict, window_h: float, outside: int, malformed: int) -> str:
    def ms(v):
        return f"{v/1000:.1f}s" if v is not None else "—"

    def pct(v):
        return f"{v:.1%}" if v is not None else "—"

    lines = [
        f"沙箱执行指标（最近 {window_h:g}h）：{s['n_execute']} 次执行，"
        f"{s['n_refused']} 次拒绝",
        f"  耗时   p50={ms(s['p50_ms'])} p95={ms(s['p95_ms'])} "
        f"p99={ms(s['p99_ms'])} max={ms(s['duration_max_ms'])}",
        f"  OOM={s['oom']}({pct(s['oom_rate'])}) 超时={s['timeouts']}"
        f"({pct(s['timeout_rate'])}) 429={s['busy']}({pct(s['busy_ratio'])}) "
        f"503挂载={s['mount_not_ready']} 401认证={s['auth_refused']}",
        f"  截断={s['truncated']}({pct(s['truncated_ratio'])}) "
        f"取消={s['cancelled']} 容器故障={s['docker_fail']} "
        f"avg_stdout={s['avg_stdout_lines'] or 0:.0f}行",
        f"  exit_code 分布："
        + (", ".join(f"{k}×{v}" for k, v in sorted(s["exit_codes"].items(),
                                                    key=lambda kv: -kv[1]))
           or "（无执行）"),
    ]
    notes = []
    if outside:
        notes.append(f"窗口外 {outside} 条")
    if malformed:
        notes.append(f"畸形行 {malformed} 条（日志盘可能写满过，见 AuditLog 降级）")
    if notes:
        lines.append("  注：" + "，".join(notes))
    return "\n".join(lines)


def maybe_webhook(alerts: list[str]) -> None:
    """有告警且配了 SANDBOX_ALERT_WEBHOOK 时 POST 一条 Slack 兼容消息。

    刻意的失败语义：通道故障只打 stderr、不影响退出码 —— 告警通道坏了
    不该被上层当成"一切正常"（退出码仍为 1，systemd 仍会标红）。
    """
    url = os.environ.get("SANDBOX_ALERT_WEBHOOK", "").strip()
    if not url or not alerts:
        return
    body = json.dumps({"text": "[sandbox] " + "；".join(alerts)}).encode("utf-8")
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5.0):
            pass
    except Exception as e:                    # noqa: BLE001 —— 通道故障不带走退出码
        print(f"[exec-metrics] webhook 发送失败（不影响告警状态）: "
              f"{type(e).__name__}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    path = Path(args.file)
    window_start = time.time() - args.window * 3600.0

    records, outside, malformed = load_records(path, window_start)
    if outside == -1:
        # SANDBOX_LOG_FILE 可以被显式置空（关闭审计），文件不存在不该报红
        print(f"[exec-metrics] {path} 不存在 —— 审计可能已关闭，跳过统计",
              file=sys.stderr)
        return 0

    s = summarize(records)
    if args.json:
        print(json.dumps(s, ensure_ascii=False, sort_keys=True))
    else:
        print(human_report(s, args.window, outside, malformed))

    alerts = evaluate_alerts(s)
    for a in alerts:
        print(f"ALERT: {a}", file=sys.stderr)
    maybe_webhook(alerts)
    return 1 if alerts else 0


if __name__ == "__main__":
    sys.exit(main())
