#!/bin/sh
# A 侧的黑盒 healthz 探测 —— sandbox 沙箱链路的"用户视角"体检。
#
# 为什么跑在 A 上而不是 B 上：A 的 sandbox_client 每 40 轮 agent loop 才访问一次
# B，故障窗口可以长达几十分钟才被真实用户撞上；这个脚本每分钟从 A 主动走一遍
# 完整路径（A 的出站网络 → 防火墙 rich rule → B 的 executor → NFS 挂载与哨兵 →
# docker daemon），把"用户先发现"变成"运维先发现"。B 上看不到 A 的出站这半段。
#
# 检查项（每一项都有独立的失败模式，不是摆设）：
#   HTTP 200            连不上（拒绝/超时/DNS）或 503（B 的挂载三层校验没过）
#   ready=true          双保险：healthz 现在用 503 表达 not ready，但"靠状态码"
#                       比"靠解析字段"更容易在重构时被漏掉 —— 两边都钉
#   docker_version 非空 executor 进程活着但 docker daemon 挂了时，healthz 仍会
#                       200（ready 只反映挂载）—— 这一项就是为那个缝补的
#   image == 期望值      B 在用一个 A 不知道的镜像跑（换镜像没同步 / 被篡改）。
#                       不设 SANDBOX_PROBE_EXPECT_IMAGE 则跳过
#
# 依赖：curl + python3（A 本来就有）。刻意不用 jq —— A 不需要为探测多装包。
#
# 退出码：0 = 健康；1 = ALERT。systemd timer 据此把单元标红（systemctl --failed）。
# 告警通道：stderr 的 ALERT 行进 journal；可选 SANDBOX_ALERT_WEBHOOK（Slack 兼容
# {"text": ...}，钉钉/飞书自行包一层转换）。webhook 有冷却窗口（默认 15 分钟），
# 避免 B 宕机一晚上被刷几千条 —— journal 里每次失败仍然完整可见。
#
# 环境变量（全部可选，见 sandbox-healthz-probe.service 的 EnvironmentFile）：
#   SANDBOX_PROBE_URL          默认 http://127.0.0.1:8787/healthz
#                              （装在 A 上时必须指到 B 的内网 IP）
#   SANDBOX_PROBE_TIMEOUT      curl 总超时，默认 12（healthz 内部探测最坏 8s）
#   SANDBOX_PROBE_EXPECT_IMAGE 期望的镜像 tag（来自 executor.env 的 SANDBOX_IMAGE）
#   SANDBOX_ALERT_WEBHOOK      告警 webhook，不设 = 只进 journal
#   SANDBOX_ALERT_COOLDOWN     webhook 冷却秒数，默认 900
#   SANDBOX_PROBE_STATE_DIR    冷却戳文件目录，默认 /var/lib/sandbox-healthz-probe

set -u

URL=${SANDBOX_PROBE_URL:-http://127.0.0.1:8787/healthz}
TIMEOUT=${SANDBOX_PROBE_TIMEOUT:-12}
EXPECT_IMAGE=${SANDBOX_PROBE_EXPECT_IMAGE:-}
WEBHOOK=${SANDBOX_ALERT_WEBHOOK:-}
COOLDOWN=${SANDBOX_ALERT_COOLDOWN:-900}
STATE_DIR=${SANDBOX_PROBE_STATE_DIR:-/var/lib/sandbox-healthz-probe}
STAMP=$STATE_DIR/last_alert

ALERTS=""

alert() {
    # 一条告警：进 stderr（→ journal）+ 攒起来发给 webhook
    echo "ALERT: $1" >&2
    ALERTS="$ALERTS
$1"
}

cooldown_passed() {
    [ -f "$STAMP" ] || return 0
    NOW=$(date +%s 2>/dev/null) || return 0
    LAST=$(stat -c %Y "$STAMP" 2>/dev/null || echo 0)
    [ $((NOW - LAST)) -ge "$COOLDOWN" ]
}

notify() {
    # webhook 通道故障不改变退出码（仍为 1，systemd 仍标红）——
    # "告警发不出去"不该被上层当成"一切正常"。
    [ -n "$WEBHOOK" ] || return 0
    [ -n "$ALERTS" ] || return 0
    cooldown_passed || return 0
    python3 - "$WEBHOOK" "$ALERTS" <<'PYEOF' 2>/dev/null
import json, sys, urllib.request
url, msg = sys.argv[1], sys.argv[2].strip()
req = urllib.request.Request(
    url, data=json.dumps({"text": "[sandbox-probe] " + msg}, ensure_ascii=False)
         .encode("utf-8"),
    headers={"Content-Type": "application/json"})
try:
    urllib.request.urlopen(req, timeout=5).read()
except Exception:
    pass    # 冷却戳照样更新：故障的通道不该被每分钟重试轰炸
PYEOF
    mkdir -p "$STATE_DIR" 2>/dev/null
    : > "$STAMP" 2>/dev/null
}

# ── 1. HTTP 层 ────────────────────────────────────────────────────────────────
# -w 把状态码附在 body 之后（换行分隔），curl 失败时 body 为空、码为 000。
BODY=$(curl -sS --max-time "$TIMEOUT" -w '\n%{http_code}' "$URL" 2>/dev/null)
CURL_RC=$?

if [ "$CURL_RC" -ne 0 ]; then
    case "$CURL_RC" in
        7)  WHY="连接被拒绝 —— executor 没起或端口不对" ;;
        28) WHY="超时 —— B 重启中、NFS hard 挂载阻塞、或防火墙拦了" ;;
        6)  WHY="DNS 解析失败" ;;
        35|56) WHY="连接中断 —— executor 崩溃或中间设备掐线" ;;
        *)  WHY="curl 退出码 $CURL_RC" ;;
    esac
    alert "$URL 探测失败：$WHY"
    notify
    exit 1
fi

HTTP_CODE=$(printf '%s' "$BODY" | tail -n1)
JSON_BODY=$(printf '%s' "$BODY" | sed '$d')

if [ "$HTTP_CODE" != "200" ]; then
    # 503 = B 自己的挂载三层校验没过（mountpoint/fstype/哨兵）—— fail closed
    # 在按设计工作，但 NFS 或 A 的哨兵 cron 出问题了，值得立刻看。
    alert "$URL 返回 HTTP $HTTP_CODE（503=挂载校验未过，见 B 的 /healthz mounts 字段）"
    notify
    exit 1
fi

# ── 2. JSON 字段层 ───────────────────────────────────────────────────────────
# 用 python3 解析而不是 sed/grep：字段类型（bool/int/null）只有 json 解析靠得住。
PROBE=$(printf '%s' "$JSON_BODY" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    assert isinstance(d, dict)
except Exception:
    print("PARSE_ERROR"); sys.exit(0)
print("ok" if d.get("ready") is True else "not_ready")
print(d.get("docker_version") or "")
print(d.get("image") or "")
print(d.get("active", "-"), d.get("capacity", "-"))
' 2>/dev/null)

if [ -z "$PROBE" ] || [ "$(printf '%s' "$PROBE" | head -n1)" = "PARSE_ERROR" ]; then
    alert "healthz 返回了 200 但 body 不是合法 JSON —— executor 半死（进程在、路由坏）"
    notify
    exit 1
fi

READY=$(printf '%s\n' "$PROBE" | sed -n 1p)
DOCKER_VER=$(printf '%s\n' "$PROBE" | sed -n 2p)
IMAGE=$(printf '%s\n' "$PROBE" | sed -n 3p)
LOAD=$(printf '%s\n' "$PROBE" | sed -n 4p)

if [ "$READY" != "ok" ]; then
    alert "healthz ready=false（200 却未就绪 —— 与状态码语义漂移，查 executor 版本）"
fi

if [ -z "$DOCKER_VER" ]; then
    # ready 只反映挂载；docker daemon 挂了时 healthz 仍是 200。这一个空值是
    # 唯一的线索，漏了就要等用户撞上"沙箱容器启动失败"才发现。
    alert "docker_version 为空 —— B 的 docker daemon 异常，run_python 会全挂"
fi

if [ -n "$EXPECT_IMAGE" ] && [ "$IMAGE" != "$EXPECT_IMAGE" ]; then
    alert "镜像漂移：B 在用 $IMAGE，期望 $EXPECT_IMAGE —— 换镜像没同步或被篡改"
fi

if [ -n "$ALERTS" ]; then
    notify
    exit 1
fi

# ── 3. 健康路径：一行摘要进 journal，方便回看历史趋势 ────────────────────────
echo "OK $URL image=$IMAGE docker=$DOCKER_VER load=$LOAD"

exit 0
