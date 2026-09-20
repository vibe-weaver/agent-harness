"""A → B 沙箱执行器客户端（同步 httpx + NDJSON 流式消费）。

B 机上的 sandbox-executor 收到脚本文本后 `docker run` 一个全新容器执行，
把 stdout/stderr 逐行包成 NDJSON 事件流回传。本模块只负责 A 这一侧的传输：
发请求、消费事件流、把死因整理成 exit_info。**不做任何文案生成** —— 给 LLM
看的中文文案统一由 tool_registry._exit_message 产出，两个后端共用一份，
否则同一种失败在 local 下说"内存不足"、docker 下说"退出码 137"。

三个刻意的设计决定：

1. **必须是同步 client。** `_tool_run_python_impl` 跑在 llm_service 的
   ThreadPoolExecutor 工作线程里（一轮最多 4 个工具并行，每个一个线程），
   那里没有事件循环；用 AsyncClient 就得自己起 loop，而在已有 loop 的线程里
   run_until_complete 会直接炸。

2. **B 不可达时 fail closed，绝不回落 local。** 回落等于把沙箱重新以 root 跑在
   /opt/agent-harness/后端/.env（JWT_SECRET、DB_PASSWORD）和 MySQL 旁边 —— 安全隔离在
   故障时静默消失，正是本次迁移要消除的状态。宁可这一轮工具调用明确失败。

3. **读流与 on_line 回调之间隔一个有界队列。** on_tool_progress 最终写 SSE，
   客户端慢时会一路反压：SSE 阻塞 → iter_lines 阻塞 → TCP 窗口收满 → B 的
   NDJSON 写阻塞 → B 的转发队列满 → 容器 stdout 管道满 → **容器卡住被超时杀掉**。
   一段完全正确的代码只因为前端渲染慢而超时，是很难查的故障。1000 行的缓冲
   把这两件事解耦；队列真满了才反压，那说明前端确实卡死了。
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from queue import Queue
from typing import Any, Callable, Optional

import httpx

from ..core.config import settings

logger = logging.getLogger(__name__)

# 与 sandbox-executor/executor.py 逐字一致（那边有 WRITTEN_MARKER 常量与反向注释）。
# 这行标记只在最终 output 里被解析，绝不能透给前端 —— 它是一段 JSON 清单，
# 用户看到会莫名其妙，而且里面是文件路径。
WRITTEN_MARKER = "__SANDBOX_WRITTEN_FILES__:"

# 本模块会写进 exit_info["error"] 的全部取值 —— 与 tool_registry._ERROR_TEXT 的键
# 是同一份契约（tests/test_sandbox_client.py 有对账用例）。漏一个的后果不是报错，
# 而是掉进通用文案：通用文案不说是哪种故障，运维只能翻日志才能区分"改配置"和
# "查网络"这类排查方向完全相反的问题。
# container_start_failed 来自 B 的 error 事件，取值是开放的（B 以后可能新增），
# 所以 _exit_message 保留了一条通用兜底。
_KNOWN_ERRORS = frozenset({
    "not_configured",   # A 侧 .env 没配 URL/TOKEN
    "unreachable",      # 连不上 B / 传输中断
    "internal",         # A 侧调用自身出异常
    "auth",             # 401 / 403
    "busy",             # 429，B 的并发槽位满了
    "not_ready",        # 503，B 的挂载新鲜度探测没过
    "rejected",         # 400 / 404 / 413，A 发过去的东西被 B 拒了
    "executor_error",   # 其余 5xx
    "bad_stream",       # 200 但流里没有 exit 事件
    "container_start_failed",
})

# 转发队列深度。B 侧自己的预算是 5 万行 / 2MB，这里 1000 行只是**解耦**用的
# 滑动窗口，不是第二道限额：队列排空的速度远快于容器产出的速度，正常跑不满。
_CALLBACK_QUEUE = 1000

# 连接与写入超时是固定的；读超时按本次沙箱超时动态算（见 _timeouts）。
_CONNECT_TIMEOUT = 5.0
_WRITE_TIMEOUT = 10.0
_POOL_TIMEOUT = 5.0
# 读超时要覆盖：B 的沙箱超时 + B 杀容器的宽限（SANDBOX_STARTUP_GRACE，默认 10s）
# + docker inspect/rm 的往返 + 内网抖动。
_READ_GRACE = 30.0

_client: Optional[httpx.Client] = None
_client_lock = threading.Lock()


def _get_client() -> httpx.Client:
    """模块级单例，懒初始化。

    每次调用新建 Client 会每次都重做 TCP + TLS 握手；一轮 agent 可能有 40 次
    工具调用，keep-alive 复用省掉的是实打实的往返。max_connections=4 对齐
    llm_service 的 ThreadPoolExecutor(max_workers=min(len(calls), 4))。
    """
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = httpx.Client(
                    timeout=_timeouts(60.0),
                    limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
                )
    return _client


def _timeouts(sandbox_timeout: float) -> httpx.Timeout:
    return httpx.Timeout(
        connect=_CONNECT_TIMEOUT,
        read=sandbox_timeout + _READ_GRACE,
        write=_WRITE_TIMEOUT,
        pool=_POOL_TIMEOUT,
    )


def reset_client() -> None:
    """关掉单例。只有测试与进程退出用；生产路径不需要。"""
    global _client
    with _client_lock:
        if _client is not None:
            try:
                _client.close()
            except Exception:                      # pragma: no cover
                pass
            _client = None


def exit_info(**over: Any) -> dict:
    """exit_info 的规范形状。**公开**给 tool_registry 用：两个后端必须返回同一套键，
    post-process 与 _exit_message 才能共用一份逻辑。各写各的字典迟早会漏键。"""

    base: dict[str, Any] = {
        "exit_code": None,      # int | None（None = 根本没跑起来）
        "oom_killed": False,
        "timed_out": False,
        "error": None,          # 机器可读的失败类别，见模块文档
        "detail": None,         # 只进日志，**绝不**进 LLM 上下文
        "request_id": None,     # 与 B 的 exec.jsonl 对账用
        "dropped_lines": 0,
    }
    base.update(over)
    return base


def run(
    script: str,
    workspace_relpath: str,
    timeout: float,
    on_line: Optional[Callable[[str], None]] = None,
) -> tuple[list[str], list[str], dict]:
    """执行一次沙箱调用。

    返回 (stdout 行, stderr 行, exit_info)。**不抛异常** —— 传输层的所有失败都
    翻译成 exit_info["error"]，由调用方决定文案。抛出去的话每个调用点都得包
    try/except，而漏掉一个就是一次 500。

    on_line 只收到 stdout 行，且已经滤掉写入清单标记行。
    """
    url = (settings.SANDBOX_EXECUTOR_URL or "").rstrip("/")
    token = settings.SANDBOX_EXECUTOR_TOKEN or ""
    if not url or not token:
        # 配置缺失是部署错误，不是运行时故障。文案要能直接指到 .env 上。
        logger.error("SANDBOX_BACKEND=docker 但 SANDBOX_EXECUTOR_URL/TOKEN 未配置")
        return [], [], exit_info(error="not_configured")

    rid = uuid.uuid4().hex
    payload = {"script": script, "workspace_relpath": workspace_relpath,
               "timeout": timeout}
    headers = {"X-Sandbox-Token": token, "X-Request-Id": rid}

    q: Queue = Queue(maxsize=_CALLBACK_QUEUE)
    stop = object()
    dispatcher = _start_dispatcher(q, stop, on_line)
    # on_line 为 None 时不启动排空线程，此时**绝不能**往 q 里塞 —— 没人取，
    # 塞满 1000 行就永久阻塞。emit 为 None 就是"不透给前端"。
    emit = (lambda line: q.put(line)) if dispatcher is not None else None

    out_lines: list[str] = []
    err_lines: list[str] = []
    try:
        with _get_client().stream(
            "POST", f"{url}/execute", json=payload, headers=headers,
            timeout=_timeouts(timeout),
        ) as resp:
            if resp.status_code != 200:
                return [], [], _refusal(resp, rid)
            info = _consume(resp, rid, out_lines, err_lines, emit)
    except httpx.PoolTimeout as e:
        # 连接池取不到连接：A 自己的 max_connections=4 被占满（一轮最多 4 个工具
        # 并行，每个一个线程）。这是 A 侧的容量问题，不是 B 慢 —— 报 unreachable
        # 会让人去查内网，报超时会让模型去优化一段根本没送出去的代码。
        logger.error(f"沙箱连接池耗尽 rid={rid}")
        return out_lines, err_lines, exit_info(
            error="busy", request_id=rid, detail=f"{type(e).__name__}: {e}")
    except (httpx.ConnectTimeout, httpx.WriteTimeout) as e:
        # 连不上 / 请求体没发完：脚本根本没送到 B，容器一次都没起。这与"代码跑太久"
        # 是相反的事实。httpx 里这两个都是 TimeoutException 的子类，所以必须写在
        # 下面那支之前，否则会被父类吃掉而误判成超时。
        logger.error(f"沙箱执行器不可达 rid={rid}: {type(e).__name__}")
        return out_lines, err_lines, exit_info(
            error="unreachable", request_id=rid, detail=f"{type(e).__name__}: {e}")
    except httpx.TimeoutException as e:
        # 到这里只剩 ReadTimeout = 连接建好了、脚本发出去了，但 B 比预期慢（或网络
        # 黑洞）。B 侧自己有超时 + docker rm -f，所以容器不会泄漏；A 这边只能报超时。
        logger.error(f"沙箱执行器超时 rid={rid}: {type(e).__name__}")
        # 只置 timed_out、**不置 error**：传输超时与 B 上报的沙箱超时对 LLM 是同一种
        # 结果（代码跑太久），该共用一条文案；置了 error 反而会因为 _exit_message
        # 先查 error、而 _ERROR_TEXT 刻意没有 "timeout" 键，掉进通用失败文案。
        # 两者的区别留在 detail 里给日志（detail 不进 LLM 上下文）。
        return out_lines, err_lines, exit_info(
            timed_out=True, request_id=rid,
            detail=f"transport {type(e).__name__}: {e}")
    except httpx.HTTPError as e:
        # ConnectError / ReadError / RemoteProtocolError 等。**不回落 local。**
        logger.error(f"无法连接沙箱执行器 rid={rid}: {type(e).__name__}: {e}")
        return out_lines, err_lines, exit_info(
            error="unreachable", request_id=rid, detail=f"{type(e).__name__}: {e}")
    except Exception as e:                           # noqa: BLE001
        logger.error(f"沙箱执行器调用异常 rid={rid}: {e}", exc_info=True)
        return out_lines, err_lines, exit_info(
            error="internal", request_id=rid, detail=f"{type(e).__name__}: {e}")
    finally:
        _stop_dispatcher(q, stop, dispatcher)
    return out_lines, err_lines, info


def _refusal(resp: httpx.Response, rid: str) -> dict:
    """把 B 的非 200 响应翻成 exit_info。

    给 LLM 的文案由 A 侧按状态码固定生成，**不复述 B 的响应体**：B 的 error
    字段是我们自己写的、目前不含宿主路径，但让远端响应体直接决定进入 LLM
    上下文的文本，等于把一个可控的注入面留在那里。B 的原文只进 A 的日志。
    """
    try:
        resp.read()
        body = resp.text[:300]
    except Exception:                                # pragma: no cover
        body = ""
    code = resp.status_code
    logger.error(f"沙箱执行器拒绝 rid={rid} status={code} body={body}")
    error = {
        401: "auth", 403: "auth",
        429: "busy",
        503: "not_ready",
    }.get(code)
    if error is None:
        # 400/404/413 = A 发过去的东西被 B 拒了，是 A 侧的 bug 或 NFS 视图不一致
        # （404 尤其：A 认为工作区存在、B 看不到，多半是挂载滞后或 TTL 清理竞态）；
        # 其余 5xx = B 内部错误。
        error = "rejected" if 400 <= code < 500 else "executor_error"
    return exit_info(error=error, request_id=rid, detail=f"status={code} {body}")


def _consume(resp: httpx.Response, rid: str, out_lines: list[str],
             err_lines: list[str], emit: Optional[Callable[[str], None]]) -> dict:
    """逐行消费 NDJSON 事件流，返回 exit_info。"""
    info = exit_info(request_id=rid)
    saw_exit = False
    for raw in resp.iter_lines():
        if not raw:
            continue
        try:
            ev = json.loads(raw)
        except ValueError:
            # 不是 JSON 的行只可能是 B 那边混进了非协议输出（例如 docker 客户端
            # 的告警）。丢掉并留痕，别让一行噪声毁掉整次调用的结果。
            logger.warning(f"沙箱事件流出现非 JSON 行 rid={rid}: {raw[:120]!r}")
            continue
        if not isinstance(ev, dict):
            continue
        kind = ev.get("type")
        if kind == "stdout":
            line = ev.get("line", "")
            out_lines.append(line)
            # 标记行不透给前端：它是一段 JSON 清单，只在最终 output 里被解析掉
            if emit is not None and on_line_wanted(line):
                emit(line)
        elif kind == "stderr":
            err_lines.append(ev.get("line", ""))
        elif kind == "exit":
            saw_exit = True
            info["exit_code"] = ev.get("code")
            info["oom_killed"] = bool(ev.get("oom_killed"))
            info["timed_out"] = bool(ev.get("timed_out"))
            # 用 max 而不是直接赋值：truncated 事件可能先到并把这里置成 ≥1，B 的
            # exit 事件不带 dropped_lines 字段时，直接赋值会写回 0 —— 而 0 的含义是
            # "一行都没丢"，截断就从 A 的日志里彻底消失了。
            info["dropped_lines"] = max(int(ev.get("dropped_lines") or 0),
                                        info["dropped_lines"])
        elif kind == "error":
            # B 的 error 事件（container_start_failed / internal）。exit 事件随后
            # 一定会到 —— 那是 B 侧的不变量，有测试钉住。
            info["error"] = ev.get("code") or "executor_error"
        elif kind == "truncated":
            info["dropped_lines"] = max(info["dropped_lines"], 1)
            logger.warning(f"沙箱输出被 B 侧预算截断 rid={rid}: {ev.get('reason')}")
        elif kind == "done":
            break
    if not saw_exit and info["error"] is None:
        # 流断了却没收到 exit：B 崩了、连接被中间设备掐了。这种情况**必须**报错，
        # 否则 out_lines 为空 + 无 exit 会掉进"（代码执行完成，无输出。）"——
        # 对 LLM 撒谎，正是本次迁移要修的缺陷。
        logger.error(f"沙箱事件流未收到 exit 事件 rid={rid}")
        info["error"] = "bad_stream"
    return info


def on_line_wanted(line: str) -> bool:
    """这行要不要透给前端。抽成函数是为了两个后端共用同一条判定。

    只滤写入清单标记行 —— 空行**要**放行：local 后端今天逐行转发所有 stdout
    （含空行），空行在 SSE 流里承担段落间距，滤掉会让两个后端的前端渲染不一致。
    """
    return not line.startswith(WRITTEN_MARKER)


def _start_dispatcher(q: Queue, stop: object,
                      on_line: Optional[Callable[[str], None]]) -> Optional[threading.Thread]:
    if on_line is None:
        return None

    def _loop() -> None:
        while True:
            item = q.get()
            if item is stop:
                return
            try:
                on_line(item)
            except Exception:                      # 回调炸了不能连累执行
                pass

    t = threading.Thread(target=_loop, name="sandbox-sse", daemon=True)
    t.start()
    return t


def _stop_dispatcher(q: Queue, stop: object, t: Optional[threading.Thread]) -> None:
    if t is None:
        return
    try:
        q.put(stop, timeout=5.0)
    except Exception:                              # 队列满且排不空：让它随进程走
        return
    t.join(timeout=5.0)
