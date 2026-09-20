#!/usr/bin/env python3
"""B 机上的 run_python 执行端点。

职责刻意做窄：**收脚本文本 → `docker run` → 把 stdout/stderr 以 NDJSON 流回 A**。
不做脚本生成（那需要 A 的 DB 里的 skill_dirs / quota_used）、不连 MySQL、不持有任何
用户数据。B 被攻破时，攻击者拿到的是一台无状态、无库、无密钥、只挂着两个 NFS 目录
（工作区 rw + 技能库 ro）的机器 —— 这是"把执行搬离 A"这个决策的全部意义。

为什么自建 HTTP 端点而不是让 A 直连 B 的 Docker daemon（`DOCKER_HOST=tcp://`）或走 SSH：
docker API 与 SSH 都等于把 B 的 **root** 交给 A（docker API 能把任意宿主路径挂进容器）。
自建端点把攻击面收缩成"一个只会用固定镜像、固定参数 `docker run` 的 HTTP 接口"，
并且给了三个 SSH/remote-API 给不了的执行点：并发闸、挂载新鲜度校验、容器 reaper。

分层（**缺 fastapi 也能 import 本模块并跑全部单元测试** —— fastapi 在模块级用
try/except 守卫 import，没装时只有 `create_app()` 会明确报错，其余全部可用）：
    Config / load_config        环境变量 → 不可变配置
    validate_relpath            路径校验（B 必须自己校验，不能只信 A）
    build_docker_cmd            docker run 参数（与 tests/test_sandbox_matrix.py 的
                                docker_cmd 逐字一致，有交叉校验用例钉住）
    parse_inspect               docker inspect 输出 → (oom_killed, exit_code)
    MountMonitor                healthz 的三层挂载新鲜度校验
    execute_blocking            线程里的阻塞执行主体
    create_app                  FastAPI 路由（唯一真正需要 fastapi 的部分）

跑法（生产由 sandbox-executor.service 托管，别手跑）：
    SANDBOX_TOKEN=... SANDBOX_IMAGE=blog-sandbox:... python executor.py
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

# fastapi 只有 create_app 用得到，但**必须在模块级 import**。本文件头有
# `from __future__ import annotations`，于是路由函数的注解在运行时是**字符串**，
# FastAPI 拿 `typing.get_type_hints(endpoint)` 对着 `endpoint.__globals__`
# （= 本模块的全局命名空间）去求值它们。把 import 放进 create_app 的局部作用域，
# "Request" 就解析不出来，FastAPI 转而把 request 当成一个必填 **query** 参数 ——
# 每个 /execute 都返回 422，而且报错信息（`loc: ["query","request"]`）完全不指向
# 真正的原因。try/except 保住了"纯逻辑层不依赖 fastapi"：没装 fastapi 时本模块
# 照样能 import、单元测试照样能跑，只有 create_app 会明确报错。
try:
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, StreamingResponse
except ImportError:                      # pragma: no cover - 生产 B 机必装
    FastAPI = Request = JSONResponse = StreamingResponse = None

# ══════════════════════════════════════════════════════════════
#  常量：docker run 的硬化参数。刻意不做成配置项 —— 它们不是"按机器调"的量，
#  改任何一个都是安全语义变更，应该走代码评审而不是改 .env。
# ══════════════════════════════════════════════════════════════

CTN_WS = "/workspace"          # 容器内工作区固定挂载点（Dockerfile 的 WORKDIR）
CTN_SKILLS = "/skills"         # 容器内技能库固定挂载点（ro）
LABEL = "sandbox=1"            # reaper 与 prune timer 靠它认领容器
CONTAINER_PREFIX = "sandbox-"
CPUS = "1"                     # B 只有 2 核；并发闸限 2，所以最坏 2 个容器各吃 1 核
PIDS_LIMIT = 64                # 防 fork 炸弹（今天 A 上只有 RLIMIT_NOFILE，挡不住）
NOFILE = 64
MEMORY_SWAP_EXTRA_MB = 128     # --memory-swap = --memory + 这个值：允许溢出到 swap，
                               # 用延迟换掉 OOM kill（B 侧另配 2GB swapfile 兜底）
UID_GID_DEFAULT = "1000:1000"  # 必须与 A 上 blog 用户一致（NFS 是 root_squash）

# 写入清单标记：与 tool_registry.py:1548 生成的、:1805 解析的逐字一致
WRITTEN_MARKER = "__SANDBOX_WRITTEN_FILES__:"

# 转发预算。生产 A 侧最终只保留 MAX_OUTPUT=32000 字符，但截断发生在 marker 解析与
# stderr 兜底**之后**，所以 B 不能按 32000 截。这里给到 2MB / 5 万行：远超任何真实
# 用例，同时把 B 的内存占用钉死在 ~2MB × 并发数。
FORWARD_MAX_LINES = 50_000
FORWARD_MAX_BYTES = 2_000_000

# docker run 自身失败（镜像不存在、daemon 不可用）时的退出码
DOCKER_RUN_FAILED = 125
# 超时被杀时上报的退出码（与 coreutils timeout 的约定一致）
EXIT_TIMEOUT = 124
# 等两个读流线程收尾的上限。proc.wait() 返回时容器已经没了、管道已到 EOF，正常情况
# 毫秒级就结束，所以这个值只是兜底。**不能给小**：join 提前返回会让 `exit` 事件
# 抢在最后几行 stdout 之前发出去，而 A 侧看到 exit 就可能停止消费。
PUMP_JOIN_TIMEOUT = 30.0

# relpath 是**单段**目录名：A 的工作区布局是 /opt/blog/workspaces/<用户标识>
# （workspace_service._user_workspace_dir），没有嵌套。不含 `/` 是刻意的 ——
# 这样 `..` 穿越、绝对路径、分隔符混淆全都被一个 fullmatch 挡掉。
_REL_RE = re.compile(r"[A-Za-z0-9_.\-]{1,64}")


# ══════════════════════════════════════════════════════════════
#  配置
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Config:
    """从环境变量读出的不可变配置。缺 token/image 直接启动失败（fail fast）。"""
    token: str
    image: str
    ws_root: Path
    skills_root: Path
    sentinel_name: str = ".sandbox_nfs_ok"
    sentinel_max_age: float = 180.0     # A 侧 cron 每分钟改写；3 个周期没动就算挂载死了
    probe_ttl: float = 5.0              # 探测结果缓存秒数
    probe_timeout: float = 8.0          # 单次探测总超时（NFS 挂死时 read 会无限阻塞）
    require_nfs: bool = True            # 开发机上设 0 才能跑（见 load_config 的警告）
    max_concurrent: int = 2             # B 是 2 核 2GB，真正该限的是 B 不是 A
    memory_mb: int = 384                # cgroup 限 RSS+page cache，语义不同于 RLIMIT_AS
    tmpfs_mb: int = 32                  # tmpfs 计入 --memory cgroup，别开大
    max_script_bytes: int = 2 * 1024 * 1024
    default_timeout: float = 120.0      # 与 A 的 PYTHON_TIMEOUT 一致（有交叉钉住的测试）
    max_timeout: float = 120.0
    startup_grace: float = 10.0         # 容器创建 + python import 的墙钟余量
    host_tz: str = "Asia/Shanghai"
    uid_gid: str = UID_GID_DEFAULT
    fsize_bytes: int = 52_428_800       # 必须与 A 的 workspace_service.MAX_FILE_SIZE 一致
    log_file: Path | None = None
    listen_host: str = "0.0.0.0"        # 靠 B 的防火墙 rich rule 只放行 A，见部署手册
    listen_port: int = 8787
    # 轮换重叠窗口：旧的 SANDBOX_TOKEN_PREVIOUS 在轮换期间也被接受。
    # None = 单 token 模式（默认）。约束见 check_token 的 docstring。
    token_prev: str | None = None


def _env_str(env, key, default=None, *, required=False):
    v = env.get(key)
    v = v.strip() if isinstance(v, str) else v
    if not v:
        if required:
            raise RuntimeError(f"缺少必需的环境变量 {key}")
        return default
    return v


def _env_int(env, key, default, *, minimum=1):
    raw = _env_str(env, key)
    if raw is None:
        return default
    try:
        v = int(raw)
    except ValueError:
        raise RuntimeError(f"{key} 必须是整数，实际是 {raw!r}")
    if v < minimum:
        raise RuntimeError(f"{key} 不能小于 {minimum}，实际是 {v}")
    return v


def _env_float(env, key, default, *, minimum=0.0):
    raw = _env_str(env, key)
    if raw is None:
        return default
    try:
        v = float(raw)
    except ValueError:
        raise RuntimeError(f"{key} 必须是数字，实际是 {raw!r}")
    if v < minimum:
        raise RuntimeError(f"{key} 不能小于 {minimum}，实际是 {v}")
    return v


def _env_bool(env, key, default):
    raw = _env_str(env, key)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def load_config(env: dict | os._Environ | None = None) -> Config:
    env = os.environ if env is None else env
    # 这一个键不走 _env_str：它把空串当"未设置"，于是 `SANDBOX_LOG_FILE=` 会回落成
    # 默认路径，**关不掉**审计日志。规则是"键存在但为空 = 显式关闭；键不存在 = 用默认"。
    if "SANDBOX_LOG_FILE" in env:
        log_raw = (env.get("SANDBOX_LOG_FILE") or "").strip()
    else:
        log_raw = "/var/log/sandbox-executor/exec.jsonl"
    return Config(
        token=_env_str(env, "SANDBOX_TOKEN", required=True),
        image=_env_str(env, "SANDBOX_IMAGE", required=True),
        # 空串 = 未设置 → None（单 token）。轮换三步走见 env.example 的注释。
        token_prev=_env_str(env, "SANDBOX_TOKEN_PREVIOUS") or None,
        # resolve() 在启动时做一次：后面每次请求都要拿它做越界判定的基准，
        # 而 NFS 挂载点上反复 resolve 是有成本的。
        ws_root=Path(_env_str(env, "SANDBOX_NFS_WS_ROOT", "/mnt/blog-ws")).resolve(),
        skills_root=Path(_env_str(env, "SANDBOX_NFS_SKILLS_ROOT", "/mnt/blog-skills")).resolve(),
        sentinel_name=_env_str(env, "SANDBOX_SENTINEL_NAME", ".sandbox_nfs_ok"),
        sentinel_max_age=_env_float(env, "SANDBOX_SENTINEL_MAX_AGE", 180.0, minimum=10.0),
        probe_ttl=_env_float(env, "SANDBOX_PROBE_TTL", 5.0),
        probe_timeout=_env_float(env, "SANDBOX_PROBE_TIMEOUT", 8.0, minimum=1.0),
        require_nfs=_env_bool(env, "SANDBOX_REQUIRE_NFS", True),
        max_concurrent=_env_int(env, "SANDBOX_MAX_CONCURRENT", 2),
        memory_mb=_env_int(env, "SANDBOX_MEMORY_MB", 384, minimum=64),
        tmpfs_mb=_env_int(env, "SANDBOX_TMPFS_MB", 32, minimum=8),
        max_script_bytes=_env_int(env, "SANDBOX_MAX_SCRIPT_BYTES", 2 * 1024 * 1024,
                                  minimum=4096),
        default_timeout=_env_float(env, "SANDBOX_DEFAULT_TIMEOUT", 120.0, minimum=1.0),
        max_timeout=_env_float(env, "SANDBOX_MAX_TIMEOUT", 120.0, minimum=1.0),
        startup_grace=_env_float(env, "SANDBOX_STARTUP_GRACE", 10.0),
        host_tz=_env_str(env, "SANDBOX_HOST_TZ", "Asia/Shanghai"),
        uid_gid=_env_str(env, "SANDBOX_UID_GID", UID_GID_DEFAULT),
        fsize_bytes=_env_int(env, "SANDBOX_FSIZE_BYTES", 52_428_800, minimum=1024),
        log_file=Path(log_raw) if log_raw else None,
        listen_host=_env_str(env, "SANDBOX_LISTEN_HOST", "0.0.0.0"),
        listen_port=_env_int(env, "SANDBOX_LISTEN_PORT", 8787),
    )


# ══════════════════════════════════════════════════════════════
#  错误类型
# ══════════════════════════════════════════════════════════════

class RequestError(Exception):
    """要按指定 HTTP 状态码回给 A 的错误。

    message 会原样进 A 的日志、也可能被 A 拼进给 LLM 的文案，所以**绝不带宿主路径、
    镜像名、token、异常原文**这类信息 —— 细节只进 B 的审计日志。
    """

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


# ══════════════════════════════════════════════════════════════
#  纯逻辑层（无 I/O，可直接单测）
# ══════════════════════════════════════════════════════════════

def check_token(provided: str | None, expected: str,
                expected_prev: str | None = None) -> None:
    """常量时间比较。`expected_prev` 是轮换重叠窗口里的旧 token（可选）。

    A 被攻破时这是最后一道 —— 所以 B 必须自己校验，不能"信任内网"。
    `hmac.compare_digest` 要求两边都是 str（且仅 ASCII）或都是 bytes；
    客户端可能传来非 ASCII，先编成 bytes 再比，避免 TypeError 变成 500。

    轮换窗口的三个约束（缺一不可）：
    1. **两个 token 都必须比完再下结论** —— `ok or compare(prev)` 会短路，
       首次比较未命中与命中的耗时差异在统计上可测。两个 compare_digest
       都执行、最后合并，计时侧信道最多学到"这是不是当前 token"，学不到
       旧 token 的任何字节。
    2. prev 与 current 相等时不做特殊处理：行为与单 token 一致（无害），
       但 load_config 之后 lifespan 会打警告 —— 那通常是"把新 token 同时填进了
       两个键"，轮换实际上没生效。
    3. 401 的审计 reason 仍是 "auth"，不区分命中的是哪个窗口 —— 从外面
       不应该能探测到 B 是否处于轮换状态。
    """
    if not isinstance(provided, str) or not provided:
        raise RequestError(401, "缺少认证令牌")
    got = provided.encode("utf-8", "surrogateescape")
    cur = hmac.compare_digest(got, expected.encode("utf-8", "surrogateescape"))
    # prev 存在时**无条件比完**（见 docstring 约束 1）；不存在时没有第二个秘密可比。
    old = (hmac.compare_digest(got, expected_prev.encode("utf-8", "surrogateescape"))
           if expected_prev else False)
    if not (cur or old):
        raise RequestError(401, "认证令牌不正确")


def validate_relpath(rel: Any, ws_root: Path) -> Path:
    """把 A 传来的 workspace_relpath 变成 B 上的宿主绝对路径，越界一律拒。

    三重判定，缺一不可：
    1. `fullmatch` 单段白名单 —— 挡掉 `/`、`..` 穿越、绝对路径、分隔符混淆、超长
    2. 显式拒 `.` / `..` —— 这两个字符串**能**通过白名单（字符集含 `.`）
    3. `resolve()` 后 `relative_to(root)` —— 挡掉符号链接指向 NFS 根之外的情况
       （A 的工作区目录属主是 uid 1000，A 被写入一个恶意 symlink 时这条才拦得住）
    """
    if not isinstance(rel, str):
        raise RequestError(400, "workspace_relpath 必须是字符串")
    if not _REL_RE.fullmatch(rel):
        raise RequestError(400, "workspace_relpath 含非法字符或长度越界")
    if rel in (".", ".."):
        raise RequestError(400, "workspace_relpath 含非法字符或长度越界")
    root = ws_root.resolve()
    # resolve() 默认 strict=False：尾组件不存在时不抛错。新用户首次执行时 A 刚
    # mkdir 完，B 的 NFS 客户端可能还没看到，这里不该直接失败（下面另做存在性检查）。
    real = (root / rel).resolve()
    try:
        real.relative_to(root)
    except ValueError:
        raise RequestError(400, "workspace_relpath 越界")
    return real


def build_docker_cmd(cfg: Config, *, ws_host_path: str | Path,
                     container_name: str) -> list[str]:
    """docker run 的完整参数表。

    **与 `后端/tests/test_sandbox_matrix.py` 的 `docker_cmd()` 逐字一致**，那边有交叉
    校验用例钉住 —— 矩阵跑出来的 golden 只有在参数一致时才代表生产行为。改这里必须
    同步改那里。

    每个硬化参数各自挡一个具体的东西，删之前请先读注释：
      --pull never            镜像缺失时不要把拉取进度写进 stderr 污染错误判定；
                              同时杜绝"容器里跑的不是我审计过的那个镜像"
      --network none          真正的断网。今天 A 上只靠 _BLOCKED_MODULES 挡用户直接
                              import socket，pandas/numpy 内部的网络能力完全不受约束
      --read-only             根文件系统只读 → 用户代码写不到 site-packages、写不了
                              /etc、也没法在 B 的磁盘上留后门。可写面只剩 /workspace
                              （NFS，受配额与写入清单约束）和 /tmp（tmpfs，随容器消失）
      --tmpfs /tmp:size=32m   **必需项，不是优化**：镜像里 MPLCONFIGDIR=/tmp/mplcache、
                              XDG_CACHE_HOME=/tmp/.cache，只读根上必须有这个 tmpfs
                              否则 matplotlib 会退回 mkdtemp 并打两条 WARNING 到 stderr；
                              而生产 tool_registry.py:1775 在"用户代码无 stdout"时会把
                              stderr 顶替成工具结果喂给 LLM
      --cap-drop ALL          容器内不留任何 capability
      no-new-privileges       setuid 二进制提不了权
      --pids-limit 64         fork 炸弹
      --init                  回收容器内僵尸 + 转发信号
      --user 1000:1000        Dockerfile 里已有 USER，这里再钉一次：万一镜像重建时
                              丢了 USER 指令，容器就以 root 跑，而 NFS 是 root_squash、
                              A 侧工作区属主是 uid 1000 → 静默写不动或写到 nobody
      不加 --rm               容器退出后要先 `docker inspect` 拿 OOMKilled/ExitCode
                              才能给 A 准确的死因；加了 --rm 就拿不到了
    """
    mem = cfg.memory_mb
    # as_posix() 在 Linux（真正的运行环境）上是恒等的；在开发机上跑阶段 2.3 或
    # 跑与基线矩阵的参数一致性校验时，str(Path) 会给出反斜杠，docker 解析不了。
    ws_src = Path(ws_host_path).as_posix()
    skills_src = Path(cfg.skills_root).as_posix()
    return [
        "docker", "run", "-i",
        "--name", container_name,
        "--label", LABEL,
        "--pull", "never",
        "--network", "none",
        "--memory", f"{mem}m", "--memory-swap", f"{mem + MEMORY_SWAP_EXTRA_MB}m",
        "--cpus", CPUS,
        "--pids-limit", str(PIDS_LIMIT),
        "--security-opt", "no-new-privileges",
        "--cap-drop", "ALL",
        "--read-only",
        "--tmpfs", f"/tmp:size={cfg.tmpfs_mb}m,mode=1777",
        "--init",
        "--ulimit", f"nofile={NOFILE}:{NOFILE}",
        "--ulimit", f"fsize={cfg.fsize_bytes}",
        "-e", f"TZ={cfg.host_tz}",
        # 不用 en_US.UTF-8：debian slim 没有生成这个 locale（会打 warning）。
        # C.UTF-8 是无 locale 文件依赖的内置 locale。`python -X utf8` 已经强制了
        # UTF-8 模式，所以这里主要影响的是 docker 客户端自己的诊断输出。
        "-e", "LANG=C.UTF-8", "-e", "LC_ALL=C.UTF-8",
        # 与生产 local 后端的 clean_env（tool_registry.py:1709）保持一致：
        # --read-only 下 /home/sandbox 不可写，HOME 指向只读目录会让想写 ~/.cache
        # 的库报错。tmpfs /tmp 是可写的。
        "-e", "HOME=/tmp",
        "--user", cfg.uid_gid,
        "-v", f"{ws_src}:{CTN_WS}",
        "-v", f"{skills_src}:{CTN_SKILLS}:ro",
        cfg.image,
    ]


def parse_inspect(raw: str) -> tuple[bool, int | None]:
    """`docker inspect -f '{{.State.OOMKilled}} {{.State.ExitCode}}'` → (oom, code)。"""
    parts = (raw or "").split()
    oom = bool(parts) and parts[0].lower() == "true"
    code: int | None = None
    if len(parts) > 1:
        try:
            code = int(parts[1])
        except ValueError:
            code = None
    return oom, code


def ndjson_line(event: dict) -> bytes:
    """一个 NDJSON 事件 → 一行 UTF-8 字节。

    `ensure_ascii=False`：用户输出大量是中文，转义会让体积翻 6 倍。
    `separators` 去空格：同理。
    """
    return (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def script_fingerprint(script: str) -> tuple[int, str]:
    """(字节数, sha256 前 16 位)。审计日志记这个而不是脚本原文 —— 用户代码可能含
    隐私内容，B 的日志不该变成第二份副本；但有指纹就能对同一次执行做跨系统关联。"""
    raw = script.encode("utf-8")
    return len(raw), hashlib.sha256(raw).hexdigest()[:16]


# ══════════════════════════════════════════════════════════════
#  子进程边界（集中在一处，测试整体替换）
# ══════════════════════════════════════════════════════════════

def run_cmd(cmd: Sequence[str], *, timeout: float = 30.0) -> tuple[int, str, str]:
    """跑一条命令，返回 (returncode, stdout, stderr)。超时按失败处理（rc=124）。

    所有 docker / mountpoint / stat 调用都走这里，这样测试只需要替换一个函数。
    """
    try:
        p = subprocess.run(list(cmd), capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"
    except FileNotFoundError:
        return 127, "", f"command not found: {cmd[0]}"
    except OSError as e:
        return 1, "", f"{type(e).__name__}"
    return (p.returncode,
            p.stdout.decode("utf-8", "replace"),
            p.stderr.decode("utf-8", "replace"))


def popen_container(cmd: Sequence[str]) -> subprocess.Popen:
    """起容器。脚本走 **stdin**，不落盘、不 bind mount —— B 的磁盘上永远不出现用户
    代码，也绕开了 SELinux 对本地文件的 label 要求。"""
    return subprocess.Popen(
        list(cmd),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        # 刻意不设 cwd：容器内的工作目录由镜像的 WORKDIR /workspace 决定，
        # 宿主侧 cwd 对容器没有任何影响，但继承 executor 的 cwd 会让 docker 客户端
        # 的相对路径 bind mount 产生歧义（我们只用绝对路径，所以这条是防御性的）。
        cwd="/",
    )


def docker_rm(name: str) -> None:
    """`docker rm -f`：对运行中的容器等价于 SIGKILL 后删除，对已退出的容器只删除。

    **超时杀容器必须走这条，不能只杀 `docker run` 客户端进程** —— 客户端死了不影响
    容器内 PID 1，`--init` 也不解决这个。不 rm 就会留下孤儿容器吃内存。
    """
    run_cmd(["docker", "rm", "-f", name], timeout=30.0)


def docker_inspect(name: str) -> tuple[bool, int | None]:
    rc, out, _ = run_cmd(
        ["docker", "inspect", "-f", "{{.State.OOMKilled}} {{.State.ExitCode}}", name],
        timeout=15.0,
    )
    if rc != 0:
        # 容器已经被清掉（比如 prune timer 抢先了）→ 拿不到死因，宁可报 None
        # 也不要瞎猜成"正常退出"。A 侧对 None 有独立文案。
        return False, None
    return parse_inspect(out)


def reap_containers() -> int:
    """启动时清理上次 executor 遗留的容器。

    executor 被 systemd 重启（或 OOM 杀掉）时，它管着的容器还在跑 —— 不加 `--rm`
    又没人收尸，就会一直占内存。用 `-aq` 而不是 `-q`：不加 --rm 时**已退出**的容器
    也还留在 `docker ps -a` 里，只清运行中的会漏掉磁盘上的元数据。
    """
    rc, out, _ = run_cmd(["docker", "ps", "-aq", "--filter", f"label={LABEL}"], timeout=30.0)
    if rc != 0:
        return -1                      # -1 = 连 docker 都调不动，调用方据此判定未就绪
    n = 0
    for cid in [x for x in out.split() if x]:
        rc2, _, _ = run_cmd(["docker", "rm", "-f", cid], timeout=30.0)
        if rc2 == 0:
            n += 1
    return n


# ══════════════════════════════════════════════════════════════
#  挂载新鲜度校验 —— 整个方案里最重要的一道防线
# ══════════════════════════════════════════════════════════════

@dataclass
class ProbeResult:
    ready: bool
    detail: dict = field(default_factory=dict)
    at: float = 0.0


def sentinel_age(path: Path, now: float) -> float | None:
    """哨兵文件的年龄（秒）。文件不存在返回 None。

    优先读**内容**（A 侧 cron 每分钟写入的 unix 时间戳），内容不可解析时退回 mtime。
    两条都依赖 A 的时钟：A 比 B 快 → 年龄为负 → 通过（无害）；A 比 B 慢超过
    sentinel_max_age → 永久 503，这是"响亮失败"，靠日志里的 age 值可以立刻诊断出来。
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    try:
        return now - float(text)
    except ValueError:
        pass
    try:
        return now - path.stat().st_mtime
    except OSError:
        return None


def probe_one_mount(path: Path, *, now: float, max_age: float,
                    sentinel: Path | None = None,
                    expect_fstype: str = "nfs") -> dict:
    """单个挂载点的三层校验。任何一层不过 → ok=False。"""
    d: dict[str, Any] = {"path": str(path), "mountpoint": False,
                         "fstype": None, "sentinel_age": None, "ok": False}

    rc, _, _ = run_cmd(["mountpoint", "-q", str(path)], timeout=5.0)
    d["mountpoint"] = (rc == 0)

    # 第二层单独存在的理由：挂载掉了之后，挂载点**底下那个空目录**会露出来，
    # 而 `mountpoint -q` 在某些内核/工具组合下对 stale handle 的判定并不可靠。
    # `stat -f -c %T` 直接问文件系统类型，ext4/xfs 就说明根本没挂上 NFS。
    rc, out, _ = run_cmd(["stat", "-f", "-c", "%T", str(path)], timeout=5.0)
    d["fstype"] = (out.strip() or None) if rc == 0 else None

    ok = d["mountpoint"] and d["fstype"] == expect_fstype

    if ok and sentinel is not None:
        # 第三层：挂载"在"不等于挂载"活"。NFS 服务端挂了或网络断了，前两层可能
        # 仍然通过（尤其 hard 挂载下 stat 会阻塞而不是报错）。哨兵文件由 A 侧 cron
        # 每分钟改写，读到的时间戳陈旧 → 说明这条路径实际上已经不通了。
        age = sentinel_age(sentinel, now)
        d["sentinel_age"] = round(age, 1) if age is not None else None
        ok = age is not None and age <= max_age
    d["ok"] = ok
    return d


def probe_mounts(cfg: Config, *, now: float | None = None) -> ProbeResult:
    """探测工作区（三层）与技能库（前两层）。

    技能库不放哨兵：它是 **ro** 导出，A 侧 cron 不该往里面写东西。挂载失效的后果
    是技能文件静默缺失（沙箱报"文件不存在"），比工作区失效轻，但仍然是**执行前
    可检测**的，所以照样 fail closed。

    `require_nfs=False` 时整段跳过 —— 那是开发机上把 ws_root 指向本地目录跑
    阶段 2.3 全链路验证的开关，生产绝不能开（启动时会打警告）。
    """
    now = time.time() if now is None else now
    if not cfg.require_nfs:
        return ProbeResult(True, {"mode": "dev-nfs-check-disabled",
                                  "ws_root": str(cfg.ws_root)}, now)

    ws = probe_one_mount(cfg.ws_root, now=now, max_age=cfg.sentinel_max_age,
                         sentinel=cfg.ws_root / cfg.sentinel_name)
    skills = probe_one_mount(cfg.skills_root, now=now, max_age=cfg.sentinel_max_age)

    ready = ws["ok"] and skills["ok"]
    return ProbeResult(ready, {"ws": ws, "skills": skills}, now)


class MountMonitor:
    """带缓存与单飞（single-flight）的挂载探测。

    为什么必须单飞：NFS 挂成 `hard` 时，对已死挂载点的 `read()`/`stat()` 会**无限
    阻塞且不可中断**。探测跑在线程里、外面套 `wait_for`，超时能返回 503，但那个线程
    杀不掉。若 healthz 每 10 秒来一次就叠一个线程，几分钟后 B 就没了。所以同一时刻
    只允许一个探测在飞，其余请求直接拿上次的缓存结果。

    初始缓存是 **not ready**：探测没成功过之前一律拒绝执行（fail closed）。
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._last = ProbeResult(False, {"reason": "尚未完成首次探测"}, 0.0)

    @property
    def last(self) -> ProbeResult:
        return self._last

    def _probe_sync(self) -> ProbeResult:
        if not self._lock.acquire(blocking=False):
            return self._last              # 已有探测在飞（可能正卡在死挂载上）
        try:
            self._last = probe_mounts(self.cfg)
            return self._last
        finally:
            self._lock.release()

    async def check(self) -> ProbeResult:
        fresh = (time.time() - self._last.at) < self.cfg.probe_ttl
        if fresh:
            return self._last
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._probe_sync), timeout=self.cfg.probe_timeout)
        except (asyncio.TimeoutError, TimeoutError):
            # 探测本身卡死 = 挂载卡死（hard NFS 上 read 是不可中断的，wait_for 只能
            # 放弃等待，那个线程杀不掉）。两件事分开看：
            #   - 返回值不写进 _last：_last 只应该反映"真实探测到过什么"，
            #     把超时结果存进去会让下一次请求误以为这是一次有效观测
            #   - 不叠线程靠的是 _probe_sync 里的单飞锁：下一次 check() 发现锁被
            #       那个卡死的线程占着，会立刻拿 _last 返回，而不是再起一个同样会卡的
            return ProbeResult(False, {"reason": "探测超时（挂载可能已卡死）",
                                       "last": self._last.detail}, time.time())


# ══════════════════════════════════════════════════════════════
#  审计日志
# ══════════════════════════════════════════════════════════════

class AuditLog:
    """JSONL 审计。写不动只降级到 stderr，绝不让执行失败。

    记录的字段是为阶段 4 的灰度观测服务的：exit_code 分布（重点看 137/OOM 比例，
    用来收紧 --memory）、p95 duration、truncated 比例。**不记脚本原文**（见
    script_fingerprint 的理由）。
    """

    def __init__(self, path: Path | None):
        self.path = path
        self._lock = threading.Lock()
        self._broken = False
        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                print(f"[executor] 审计日志目录创建失败 {path.parent}: {e}", file=sys.stderr)
                self._broken = True

    def write(self, rec: dict) -> None:
        if self.path is None or self._broken:
            return
        rec = dict(rec)
        rec.setdefault("ts", round(time.time(), 3))
        line = json.dumps(rec, ensure_ascii=False, sort_keys=True)
        with self._lock:
            try:
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError as e:
                # 只报一次：日志盘写满时每条都打会把 journal 也刷爆
                self._broken = True
                print(f"[executor] 审计日志写入失败，已停用: {e}", file=sys.stderr)


# ══════════════════════════════════════════════════════════════
#  执行主体（跑在线程里，全阻塞）
# ══════════════════════════════════════════════════════════════

class _Budget:
    """转发预算。用尽后**继续读、不再转发**。

    "继续读"是必须的：如果停止 drain，容器写满管道后会阻塞在 write() 上，一直挂到
    超时被 SIGKILL —— 用户看到的是"执行超时"，而真实原因是"输出太长"，两个完全不同
    的问题被混成一个，排查时会走错方向。
    """

    def __init__(self, max_lines: int = FORWARD_MAX_LINES,
                 max_bytes: int = FORWARD_MAX_BYTES):
        self.max_lines = max_lines
        self.max_bytes = max_bytes
        self.lines = 0
        self.bytes = 0
        self.dropped = 0
        self._notified = False

    def feed(self, kind: str, line: str, emit: Callable[[dict], None]) -> None:
        # 写入清单标记行永远转发：它是 stdout 的**最后一行**（tool_registry.py:1548
        # 把 marker 追加到 output 末尾后一次性 write），恰恰是超预算时最先被丢的那行。
        # 丢了 A 侧就拿不到本次写入清单，只能退回 O(全部文件) 的全量磁盘扫描。
        if line.startswith(WRITTEN_MARKER):
            emit({"type": kind, "line": line})
            return
        if self.lines < self.max_lines and self.bytes + len(line) + 1 <= self.max_bytes:
            self.lines += 1
            self.bytes += len(line) + 1
            emit({"type": kind, "line": line})
        else:
            self.dropped += 1
            if not self._notified:
                self._notified = True
                emit({"type": "truncated", "reason": "output_budget",
                      "max_lines": self.max_lines, "max_bytes": self.max_bytes})


def _pump(stream, kind: str, budget: _Budget, emit: Callable[[dict], None],
          cancel: threading.Event) -> None:
    """逐行读一路输出并转发。逻辑搬自生产 tool_registry.py:1735-1753 的 _read_stream。

    rstrip("\\r\\n") 而不是生产的 rstrip("\\n")：容器里天然是 \\n，但 docker 客户端
    在 Windows 宿主上会翻译成 \\r\\n，开发机跑阶段 2.3 时会带上 \\r。抹平它，否则
    每条多行输出的 golden 都会 diff 出噪声（tests/test_sandbox_matrix.py:131 同理）。
    """
    try:
        for raw in iter(stream.readline, b""):
            if cancel.is_set():
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            budget.feed(kind, line, emit)
    except Exception:
        # 管道被关闭（容器被 rm -f）会抛 ValueError/OSError，这是超时路径的**正常**
        # 结局，不是故障。
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _wait_deadline(proc: subprocess.Popen, seconds: float, cancel: threading.Event,
                   poll: float = 0.2) -> bool:
    """等容器退出。返回 True 表示"因为超时或取消而放弃等待"。

    不用 `proc.wait(timeout=seconds)` 一次等到底：那样 cancel（客户端断开）要等到
    超时才生效，容器白跑几十秒。分片轮询让取消能在 0.2s 内响应。
    """
    end = time.monotonic() + seconds
    while True:
        try:
            proc.wait(timeout=poll)
            return False
        except subprocess.TimeoutExpired:
            pass
        if cancel.is_set():
            return True
        if time.monotonic() >= end:
            return True


def execute_blocking(cfg: Config, *, script: str, ws_path: Path, container_name: str,
                     timeout: float, emit: Callable[[dict], None],
                     cancel: threading.Event) -> dict:
    """起容器、喂脚本、流式转发、拿死因、清容器。返回 exit_info（供审计与 A 侧映射文案）。

    这个函数**只在线程里调用**，全程阻塞。所有对外的信号都通过 emit 出去。
    """
    started = time.monotonic()
    cmd = build_docker_cmd(cfg, ws_host_path=ws_path, container_name=container_name)

    info: dict[str, Any] = {
        "container": container_name,
        "exit_code": None,
        "oom_killed": False,
        "timed_out": False,
        "cancelled": False,
        "stdout_lines": 0,
        "stderr_lines": 0,
        "dropped_lines": 0,
        "duration_ms": 0,
        "docker_error": None,
    }

    try:
        proc = popen_container(cmd)
    except OSError as e:
        # B 上没装 docker、docker.sock 权限不对、daemon 没起来。
        #
        # **必须补发一个 exit 事件**，让"每次 execute_blocking 恰好发一个 exit 事件"
        # 成为不变量。只发 error 不发 exit 的话，A 侧得写两条分支；而更坏的情况是
        # A 的逻辑本来就是"累积 stdout、靠 exit 事件映射文案" —— 这条路径会产出
        # 空输出且没有 exit，正好掉进本次迁移要修的那个坑：对 LLM 谎报
        # "（代码执行完成，无输出。）"，模型于是以为代码跑成功了继续往下编。
        #
        # 不调 docker_rm：Popen 都没成功，容器根本没被创建。
        info["docker_error"] = f"{type(e).__name__}: {e}"
        info["exit_code"] = DOCKER_RUN_FAILED
        emit({"type": "error", "code": "container_start_failed",
              "message": "无法启动容器执行器"})
        emit({"type": "exit", "code": DOCKER_RUN_FAILED, "oom_killed": False,
              "timed_out": False})
        info["duration_ms"] = int((time.monotonic() - started) * 1000)
        return info

    budget = _Budget()
    try:
        # 脚本走 stdin。写完立刻关闭 —— ENTRYPOINT 是 `python -I -X utf8 -`，
        # 不关 stdin 的话 Python 会一直等 EOF 而不退出。
        try:
            proc.stdin.write(script.encode("utf-8"))
            proc.stdin.close()
        except (OSError, ValueError) as e:
            # BrokenPipeError（容器还没起来就死了：镜像缺失、参数被 daemon 拒绝）是
            # OSError 的子类；ValueError 覆盖"写入已关闭的文件"。两者都继续往下走：
            # 退出码与 stderr 会把原因带出来，这里不 return 是为了保证 finally 清容器。
            info["docker_error"] = f"stdin: {type(e).__name__}"

        t_out = threading.Thread(target=_pump,
                                 args=(proc.stdout, "stdout", budget, emit, cancel),
                                 daemon=True, name=f"{container_name}-out")
        t_err = threading.Thread(target=_pump,
                                 args=(proc.stderr, "stderr", budget, emit, cancel),
                                 daemon=True, name=f"{container_name}-err")
        t_out.start()
        t_err.start()

        gave_up = _wait_deadline(proc, timeout + cfg.startup_grace, cancel)
        if gave_up:
            info["timed_out"] = not cancel.is_set()
            info["cancelled"] = cancel.is_set()
            # 必须先 rm 容器再等客户端：杀 `docker run` 客户端进程不影响容器内 PID 1
            docker_rm(container_name)
            try:
                proc.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass

        t_out.join(timeout=PUMP_JOIN_TIMEOUT)
        t_err.join(timeout=PUMP_JOIN_TIMEOUT)

        oom, inspected = docker_inspect(container_name)
        info["oom_killed"] = oom
        code = proc.returncode if proc.returncode is not None else inspected
        if info["timed_out"]:
            code = EXIT_TIMEOUT
        if code == DOCKER_RUN_FAILED:
            # docker run 自身失败（镜像不在、daemon 拒绝）。原始 stderr 只进审计日志，
            # 回给 A 的是通用文案 —— 里面可能带镜像 tag 与宿主路径。
            info["docker_error"] = info["docker_error"] or "docker run exited 125"
            emit({"type": "error", "code": "container_start_failed",
                  "message": "沙箱镜像或容器运行时不可用"})
        info["exit_code"] = code
        info["stdout_lines"] = budget.lines
        info["dropped_lines"] = budget.dropped

        ev: dict[str, Any] = {"type": "exit", "code": code, "oom_killed": oom,
                              "timed_out": info["timed_out"]}
        if budget.dropped:
            ev["dropped_lines"] = budget.dropped
        emit(ev)
    finally:
        # 正常路径也要 rm：不加 --rm，容器元数据会一直留在 B 上（--read-only 下
        # 可写层几乎为 0，但 `docker ps -a` 会无限增长，且 prune timer 只管 10 分钟
        # 以前的）。超时路径已经 rm 过一次，重复调用无害。
        docker_rm(container_name)
        info["duration_ms"] = int((time.monotonic() - started) * 1000)
    return info


# ══════════════════════════════════════════════════════════════
#  HTTP 层（只有这里 import fastapi）
# ══════════════════════════════════════════════════════════════

def clamp_timeout(raw: Any, cfg: Config) -> float:
    """A 传来的 timeout 只做**收窄**，不做信任：非法值退回默认，超上限按上限截。

    上限必须存在 —— 否则 A 被攻破后可以用一个巨大的 timeout 长期占住 B 的 2 个并发槽。
    """
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return cfg.default_timeout
    if v != v or v <= 0:            # NaN 与负数
        return cfg.default_timeout
    return min(v, cfg.max_timeout)


def validate_script(raw: Any, cfg: Config) -> str:
    if not isinstance(raw, str):
        raise RequestError(400, "script 必须是字符串")
    if not raw.strip():
        raise RequestError(400, "script 不能为空")
    if len(raw.encode("utf-8")) > cfg.max_script_bytes:
        raise RequestError(413, "script 超过大小上限")
    return raw


class _State:
    """每个 app 实例一份：并发计数、挂载监视器、审计日志。"""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.monitor = MountMonitor(cfg)
        self.audit = AuditLog(cfg.log_file)
        self.active = 0
        self._active_lock = threading.Lock()   # 只在 worker 线程里改时用
        self.served = 0

    def acquire(self) -> None:
        if self.active >= self.cfg.max_concurrent:
            raise RequestError(429, "沙箱并发已满，请稍后重试")
        self.active += 1

    def release(self) -> None:
        self.active = max(0, self.active - 1)


def create_app(cfg: Config | None = None):
    if FastAPI is None:
        raise RuntimeError("未安装 fastapi，无法创建 HTTP 应用"
                           "（配置/校验/docker 参数/挂载探测等纯逻辑层不受影响）")

    cfg = cfg or load_config()
    state = _State(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 镜像版本由 B 单一持有，A 不传 tag。启动时把它打进日志，A 侧通过 /healthz
        # 读到 image 后与自己的期望比对、不一致就告警 —— 换镜像不用动 A。
        print(f"[executor] image={cfg.image} ws_root={cfg.ws_root} "
              f"skills_root={cfg.skills_root} max_concurrent={cfg.max_concurrent} "
              f"memory={cfg.memory_mb}m tmpfs={cfg.tmpfs_mb}m", file=sys.stderr)
        if cfg.token_prev:
            if cfg.token_prev == cfg.token:
                print("[executor] 警告：SANDBOX_TOKEN_PREVIOUS 与 SANDBOX_TOKEN 相同，"
                      "轮换没有生效 —— 通常是把新 token 同时填进了两个键。",
                      file=sys.stderr)
            else:
                # 轮换没走完的可见痕迹：旧 token 仍被接受。A 侧切完并确认无 401 后，
                # 删掉 PREVIOUS 键再重启一次，这行日志应该消失。
                print("[executor] 轮换窗口开启：旧 token 仍被接受"
                      "（SANDBOX_TOKEN_PREVIOUS 已配置）", file=sys.stderr)
        if not cfg.require_nfs:
            print("[executor] 警告：SANDBOX_REQUIRE_NFS=0，挂载新鲜度校验已关闭。"
                  "仅供开发机全链路验证，生产环境绝不可开。", file=sys.stderr)
        n = await asyncio.to_thread(reap_containers)
        if n < 0:
            print("[executor] 警告：启动时 docker 不可用，未能清理遗留容器", file=sys.stderr)
        elif n:
            print(f"[executor] 启动清理：移除 {n} 个遗留沙箱容器", file=sys.stderr)
        yield
        print("[executor] 关闭中，清理在跑的沙箱容器", file=sys.stderr)
        await asyncio.to_thread(reap_containers)

    # docs/redoc/openapi 全关：这是个只被 A 调用的内网端点，暴露交互式 API 文档
    # 等于给攻进内网的人一份现成的调用说明。
    app = FastAPI(title="blog-sandbox-executor", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    # 挂到 app.state 上：并发计数与挂载监视器是进程内单例，测试与将来的运维端点
    # 都需要能拿到它，而不是去闭包里掏。
    app.state.sandbox = state

    @app.exception_handler(RequestError)
    async def _on_request_error(request: Request, exc: RequestError):
        return JSONResponse({"error": exc.message}, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def _on_unexpected(request: Request, exc: Exception):
        # 不把异常原文回给 A：里面可能有宿主路径。细节进 journal。
        print(f"[executor] 未处理异常: {type(exc).__name__}: {exc}", file=sys.stderr)
        return JSONResponse({"error": "执行器内部错误"}, status_code=500)

    @app.get("/healthz")
    async def healthz():
        probe = await state.monitor.check()
        rc, ver, _ = await asyncio.to_thread(
            run_cmd, ["docker", "version", "--format", "{{.Server.Version}}"], timeout=5.0)
        body = {
            "ready": probe.ready,
            "image": cfg.image,
            "docker_version": ver.strip() if rc == 0 else None,
            "mounts": probe.detail,
            "active": state.active,
            "capacity": cfg.max_concurrent,
            "served": state.served,
        }
        # 503 而不是 200+ready:false —— A 侧的 fail-closed 判定靠状态码，
        # 靠解析 JSON 字段的代码更容易在重构时被漏掉。
        return JSONResponse(body, status_code=200 if probe.ready else 503)

    @app.post("/execute")
    async def execute(request: Request):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        # 认证在**最前面**：未认证的调用者不该能从响应里学到挂载是否健康、
        # 并发是否已满这些内部状态。
        try:
            check_token(request.headers.get("x-sandbox-token"), cfg.token,
                        cfg.token_prev)
        except RequestError:
            # 401 也要留痕。阶段 4 轮换令牌时，两边不一致的表现就是 A 侧一片 401 ——
            # B 的日志里若什么都没有，就没法区分「令牌不匹配」与「A 根本连不上 B」，
            # 而这两件事的排查方向完全相反（一个改配置，一个查网络/防火墙）。
            state.audit.write({"event": "refused", "request_id": rid,
                               "reason": "auth", "status": 401})
            raise

        probe = await state.monitor.check()
        if not probe.ready:
            # 绝不放行：挂载失效时 bind mount 会挂到挂载点底下那个**空目录**，
            # 沙箱在空目录里跑 → 用户看到"文件全没了"，而且是静默的。
            state.audit.write({"event": "refused", "request_id": rid,
                               "reason": "mount_not_ready", "status": 503,
                               "detail": probe.detail})
            raise RequestError(503, "沙箱执行器未就绪：存储挂载异常")

        try:
            state.acquire()      # 429 要在读 body 之前：不给要拒绝的请求缓冲 2MB
        except RequestError:
            # 灰度期（阶段 4）判断 B 是否饱和就靠这条：429 的比例升高说明
            # max_concurrent 或内存上限该调了，而不是靠 A 侧"系统繁忙"的口头反馈。
            # 这里绝不能 release —— 槽位没拿到过。
            state.audit.write({"event": "refused", "request_id": rid,
                               "reason": "busy", "status": 429})
            raise

        rel = None
        try:
            body = await _read_body(request, cfg.max_script_bytes)
            payload = _parse_json(body)
            script = validate_script(payload.get("script"), cfg)
            rel = payload.get("workspace_relpath")
            ws_path = _resolve_existing_ws(rel, cfg)
            timeout = clamp_timeout(payload.get("timeout"), cfg)
        except BaseException as e:
            state.release()
            # detail 只在 RequestError 时记：那些文案是回给 A 的，已经过校验不含宿主
            # 路径（有测试钉住）。其余异常只记类型名 —— str(e) 里可能带宿主路径。
            rec: dict[str, Any] = {"event": "refused", "request_id": rid,
                                   "reason": type(e).__name__}
            if isinstance(e, RequestError):
                rec["detail"] = e.message[:120]
                # status 必须单独记：reason 对 400/404/413 是同一个字符串
                # "RequestError"，而这三种在灰度期含义完全不同 —— 404 说明 A 认为
                # 存在的工作区 B 看不到（NFS 滞后或 TTL 清理竞态），413 说明 A 侧
                # 脚本超了上限（A 的 bug），400 是请求本身畸形。靠 detail 文案
                # 区分太脆，改一个字就对不上了。
                rec["status"] = e.status_code
            if rel is not None:
                # relpath 是调用方可控的，截断后落盘（JSON 转义保证不破坏行格式）
                rec["relpath"] = str(rel)[:80]
            state.audit.write(rec)
            raise

        container_name = f"{CONTAINER_PREFIX}{uuid.uuid4().hex[:16]}"
        nbytes, fp = script_fingerprint(script)
        state.served += 1

        async def gen():
            loop = asyncio.get_running_loop()
            q: asyncio.Queue = asyncio.Queue()
            done = object()
            cancel = threading.Event()

            def emit(ev: dict) -> None:
                try:
                    loop.call_soon_threadsafe(q.put_nowait, ev)
                except RuntimeError:
                    pass        # 事件循环已关闭（进程在退出），丢掉即可

            def worker() -> None:
                info: dict[str, Any] = {}
                try:
                    info = execute_blocking(
                        cfg, script=script, ws_path=ws_path,
                        container_name=container_name, timeout=timeout,
                        emit=emit, cancel=cancel)
                except BaseException as e:      # noqa: BLE001 —— 必须兜住，否则线程静默死掉
                    info = {"exit_code": None, "docker_error": f"{type(e).__name__}: {e}"}
                    emit({"type": "error", "code": "internal",
                          "message": "执行器内部错误"})
                finally:
                    state.audit.write({
                        "event": "execute", "request_id": rid,
                        "workspace": ws_path.name, "script_bytes": nbytes,
                        "script_sha256_16": fp, "timeout": timeout,
                        **{k: info.get(k) for k in
                           ("exit_code", "oom_killed", "timed_out", "cancelled",
                            "stdout_lines", "dropped_lines", "duration_ms",
                            "docker_error")},
                    })
                    emit({"type": "done"})
                    try:
                        loop.call_soon_threadsafe(q.put_nowait, done)
                    except RuntimeError:
                        pass

            threading.Thread(target=worker, daemon=True,
                             name=f"exec-{rid[:8]}").start()
            try:
                while True:
                    item = await q.get()
                    if item is done:
                        break
                    yield ndjson_line(item)
            except (asyncio.CancelledError, GeneratorExit):
                # 客户端断开：让 worker 尽早杀容器，别白跑到超时
                cancel.set()
                raise
            finally:
                cancel.set()
                state.release()

        try:
            return StreamingResponse(
                gen(), media_type="application/x-ndjson",
                headers={"X-Request-Id": rid, "Cache-Control": "no-store",
                         "X-Accel-Buffering": "no"})
        except BaseException:
            state.release()
            raise

    return app


async def _read_body(request, cap: int) -> bytes:
    cl = request.headers.get("content-length")
    if cl:
        try:
            if int(cl) > cap:
                raise RequestError(413, "请求体超过大小上限")
        except ValueError:
            pass
    body = await request.body()
    # content-length 可以被谎报（或用 chunked 干脆不发），所以读完后必须再判一次
    if len(body) > cap:
        raise RequestError(413, "请求体超过大小上限")
    return body


def _parse_json(body: bytes) -> dict:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RequestError(400, "请求体不是合法的 UTF-8 JSON")
    if not isinstance(payload, dict):
        raise RequestError(400, "请求体必须是 JSON 对象")
    return payload


def _resolve_existing_ws(rel: Any, cfg: Config) -> Path:
    """校验 relpath 并确认目录**确实存在**。

    存在性检查不是可选的：`docker run -v <不存在的路径>:/workspace` 会让 daemon
    在宿主上**以 root 身份创建**这个目录（然后被 NFS root_squash 映射成 nobody）。
    于是攻击者可以用 relpath 在 NFS 根下造任意目录，而正常用户看到的是"工作区是空的"
    —— 正是本方案最想避免的那类静默失败。

    失败后重试一次：A 侧 mkdir 与 B 侧 NFS 客户端之间可能有短暂的负向 dentry 缓存
    （挂载参数 lookupcache=pos 已经禁掉它，这条是双保险，成本 0.2 秒）。
    """
    ws_path = validate_relpath(rel, cfg.ws_root)
    if ws_path.is_dir():
        return ws_path
    time.sleep(0.2)
    if ws_path.is_dir():
        return ws_path
    raise RequestError(404, "工作区目录不存在")


def main() -> None:
    import uvicorn

    cfg = load_config()
    app = create_app(cfg)
    # 单 worker、单进程：并发闸与挂载单飞都是**进程内**状态，多 worker 会让
    # max_concurrent 变成 max_concurrent × workers，把 B 的 2GB 内存直接打爆。
    uvicorn.run(app, host=cfg.listen_host, port=cfg.listen_port,
                log_level="info", access_log=False)


if __name__ == "__main__":
    main()
