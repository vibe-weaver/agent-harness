#!/usr/bin/env python3
"""端到端自检 —— 不用任何 API Key 就能验证整条链路。

做法：本地起一个模拟的「异步任务型」图片接口（提交拿 task_id → 轮询 → 返回图片 URL），
再临时写一份指向它的配置，然后真的跑一次 generate，断言：

  1. 排在前面的模型故意 404，路由是否正确降级到第二个模型
  2. 提交 → 轮询 → 下载 → 落盘 是否走通
  3. 落盘文件的扩展名是否按真实文件头纠正为 .png
  4. 图片宽高是否被正确读出来（元数据回传）
  5. 失败模型是否被记入健康度，成功模型是否清零

运行：python scripts/selftest.py
退出码 0 = 全部通过。
"""

from __future__ import annotations

import contextlib
import io
import json
import struct
import sys
import tempfile
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mrouter.cli import build_parser  # noqa: E402
from mrouter.cli import main as cli_main  # noqa: E402
from mrouter.config import load_config  # noqa: E402
from mrouter.health import HealthStore  # noqa: E402

TARGET_W, TARGET_H = 64, 32
POLL_CALLS: dict[str, int] = {}


def make_png(width: int, height: int, rgb: tuple[int, int, int] = (110, 150, 210)) -> bytes:
    """手搓一张纯色 PNG，用来验证真实文件头解析。"""
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class Handler(BaseHTTPRequestHandler):
    server_port = 0

    def log_message(self, *args):  # noqa: D102 - 静音
        pass

    def _raw(self, status: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status: int = 200) -> None:
        self._raw(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/fail":
            self._raw(404, b'{"error":"model unavailable"}')
            return
        if self.path == "/submit":
            self._json({"data": {"task_id": "selftest-task"}})
            return
        self._raw(404, b'{"error":"no route"}')

    def do_GET(self):  # noqa: N802
        if self.path == "/img.png":
            self._raw(200, make_png(TARGET_W, TARGET_H), "image/png")
            return
        if self.path.startswith("/tasks/"):
            seen = POLL_CALLS.get(self.path, 0)
            POLL_CALLS[self.path] = seen + 1
            if seen == 0:
                self._json({"data": {"status": "running", "images": []}})
            else:
                self._json(
                    {
                        "data": {
                            "status": "succeeded",
                            "images": [
                                {"url": f"http://127.0.0.1:{self.server_port}/img.png"}
                            ],
                        }
                    }
                )
            return
        self._raw(404, b'{"error":"no route"}')


CONFIG_TEMPLATE = """\
version: 1
defaults:
  timeout_seconds: 15
  poll_interval_seconds: 1
  max_poll_seconds: 30
  max_attempts: 3
  output_dir: __OUT__
  state_dir: __STATE__
  health:
    failure_threshold: 3
    cooldown_seconds: 60
  caption:
    mode: metadata

image:
  strategy: fallback_chain
  models:
    - id: mock-broken
      provider: generic_http
      model: mock-broken
      priority: 1
      weight: 100
      supports: [text2img]
      options:
        auth:
          type: none
        submit:
          url: http://127.0.0.1:__PORT__/fail
          method: POST
          body:
            prompt: "{{prompt}}"
        poll:
          url: http://127.0.0.1:__PORT__/tasks/{{task_id}}

    - id: mock-working
      provider: generic_http
      model: mock-working
      priority: 2
      weight: 100
      supports: [text2img, img2img]
      options:
        auth:
          type: none
        submit:
          url: http://127.0.0.1:__PORT__/submit
          method: POST
          body:
            model: "{{model}}"
            prompt: "{{prompt}}"
          task_id_path: data.task_id
        poll:
          url: http://127.0.0.1:__PORT__/tasks/{{task_id}}
          method: GET
          interval: 1
          status_path: data.status
          success: [succeeded]
          failure: [failed]
          result_path: data.images
          result_url_field: url
"""


def main() -> int:
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, bool(ok), detail))

    tmp = Path(tempfile.mkdtemp(prefix="media-router-selftest-"))
    out_dir = tmp / "outputs"
    state_dir = tmp / "state"
    config_path = tmp / "models.yaml"

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    Handler.server_port = port
    config_path.write_text(
        CONFIG_TEMPLATE.replace("__PORT__", str(port))
        .replace("__OUT__", out_dir.as_posix())
        .replace("__STATE__", state_dir.as_posix()),
        encoding="utf-8",
    )

    import os

    os.environ["MEDIA_ROUTER_CONFIG"] = str(config_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    print(f"[selftest] 模拟接口已启动: http://127.0.0.1:{port}")
    print(f"[selftest] 临时配置: {config_path}")

    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            code = cli_main(
                [
                    "generate",
                    "--kind",
                    "image",
                    "--prompt",
                    "自检用图：纯色色块",
                    "--output-dir",
                    out_dir.as_posix(),
                ]
            )
    except Exception as exc:  # noqa: BLE001
        print(f"[selftest][debug] cli_main 抛出异常: {exc!r}")

    raw = buffer.getvalue().strip()
    payload = json.loads(raw) if raw else {}

    if payload.get("status") != "ok":
        print("\n[selftest][debug] 首轮返回：")
        print(json.dumps(payload, ensure_ascii=False, indent=2)[:3000])

    print("\n=== 自检结果 ===")
    check("generate 退出码为 0", code == 0, f"实际 {code}")
    check("status == ok", payload.get("status") == "ok", str(payload.get("status")))
    check(
        "失败模型降级成功（选中 mock-working）",
        (payload.get("model") or {}).get("id") == "mock-working",
        str((payload.get("model") or {}).get("id")),
    )

    trail = payload.get("fallback_trail") or []
    check("降级轨迹记录了 mock-broken", any(t.get("model") == "mock-broken" for t in trail), str(trail))
    check(
        "失败被归类为 runtime（计入熔断）",
        any(t.get("kind") == "runtime" for t in trail),
        str([t.get("kind") for t in trail]),
    )

    files = payload.get("files") or []
    check("产出 1 个文件", len(files) == 1, f"实际 {len(files)}")
    if files:
        f = files[0]
        path = Path(f.get("path", ""))
        check("文件已落盘", path.exists(), str(path))
        check("扩展名按真实文件头纠正为 .png", path.suffix == ".png", path.suffix)
        check(
            "图片宽高解析正确",
            f.get("width") == TARGET_W and f.get("height") == TARGET_H,
            f"{f.get('width')}x{f.get('height')}（应为 {TARGET_W}x{TARGET_H}）",
        )
        check("文件大小已统计", (f.get("bytes") or 0) > 0, str(f.get("bytes")))
        check("落地文件内容为合法 PNG", path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n")

    check("轮询确实发生了多次（异步任务生效）", len(POLL_CALLS) > 0, str(POLL_CALLS))

    health = HealthStore(state_dir / "health.json")
    broken = health.state("mock-broken")
    working = health.state("mock-working")
    check(
        "失败模型计数 +1",
        broken.get("failure_count") == 1,
        str(broken.get("failure_count")),
    )
    check(
        "成功模型成功计数 +1",
        working.get("success_count") == 1,
        str(working.get("success_count")),
    )
    check(
        "失败模型未被误熔断（仅 1 次 < 阈值 3）",
        not health.is_cooling("mock-broken"),
        f"cooling={health.is_cooling('mock-broken')}",
    )

    # 第二轮：验证同一份配置能稳定复现（不依赖一次性状态）
    print("\n[selftest] 第二轮验证 —— 确认可重复运行")
    buffer2 = io.StringIO()
    try:
        thread2 = threading.Thread(target=server.serve_forever, daemon=True)
        thread2.start()
        with contextlib.redirect_stdout(buffer2):
            code2 = cli_main(
                [
                    "generate",
                    "--kind",
                    "image",
                    "--prompt",
                    "第二轮",
                    "--output-dir",
                    out_dir.as_posix(),
                ]
            )
    finally:
        with contextlib.suppress(Exception):
            server.shutdown()
    payload2 = json.loads(buffer2.getvalue().strip() or "{}")
    check("第二轮同样成功", code2 == 0 and payload2.get("status") == "ok", str(payload2.get("status")))
    check(
        "两轮产出文件不同名（不互相覆盖）",
        bool(payload2.get("files"))
        and payload2["files"][0]["path"] != (files[0]["path"] if files else ""),
        "",
    )

    # ---------------------------------------------------------- 解析器一致性
    # 这一组是防"装了 PyYAML 和没装行为分叉"的。分叉的后果是：同一份配置在
    # 两台机器上跑出两个结果，而报错信息完全不指向真正的原因。
    print("\n[selftest] 解析器一致性")
    check_parser_agreement(check)

    print("\n[selftest] 配置原文体检（lint）")
    check_config_lint(check)

    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = "PASS" if ok else "FAIL"
        suffix = f"  -> {detail}" if detail and not ok else ""
        print(f"  [{mark}] {name}{suffix}")
    print(f"\n{passed}/{len(results)} 项通过")
    if passed != len(results):
        print(f"临时目录保留以便排查: {tmp}")
        return 1
    print("全部通过。")
    return 0


#: 这些值的"用户本意"都是字符串。放进映射值的位置（配置里的真实上下文）
#: 之后，两个解析器必须给出同样的结果，否则就是行为分叉。
TRICKY_VALUES = [
    # 比值 / 时间 —— YAML 1.1 的六十进制陷阱，最险的一类
    "1:1", "16:9", "9:16", "4:3", "4:5", "1:2:3", "12:30:45", "16:99", "1.5:30",
    # 数字的各种写法
    "0", "007", "017", "0o17", "0x10", "0b101", "1_000", "+5", "-5",
    "1.5", "-1.5", ".5", "5.", "1.", "1e3", "1.0e+3", "1.5e-3", ".inf", "-.inf", ".nan",
    # 尺寸
    "1024x1024", "1024*1024", "1280*720",
    # 布尔 / 空值的各种写法（none 必须是字符串，不能是 null）
    "true", "True", "TRUE", "false", "yes", "no", "on", "off", "null", "~", "None", "none",
    # 常见字符串
    "https://api.example.com/v1", "https://api.example.com/v1/tasks/x",
    "black-forest-labs/FLUX.1-schnell", "kling-v2-master",
    "带中文的值", "hello world", "a#b", "a #b",
    # 空值后面的注释形式
    "16:9 ",
]


def check_parser_agreement(check) -> None:
    """只要 PyYAML 在，就逐项比对两个解析器；解析器自己的往返测试总是跑。"""
    from mrouter import miniyaml

    mismatched: list[str] = []
    for value in TRICKY_VALUES:
        text = f"k: {value}"
        try:
            mine = miniyaml.loads(text)["k"]
        except Exception as exc:  # noqa: BLE001
            mine = f"<{type(exc).__name__}: {exc}>"
        try:
            import yaml  # type: ignore

            theirs = yaml.safe_load(text)["k"]
        except ImportError:
            theirs = mine
        except Exception as exc:  # noqa: BLE001
            theirs = f"<{type(exc).__name__}: {exc}>"
        same = type(mine) is type(theirs) and (
            mine == theirs
            or (isinstance(mine, float) and isinstance(theirs, float) and mine != mine and theirs != theirs)
        )
        if not same:
            mismatched.append(f"{value!r}: PyYAML={theirs!r} miniyaml={mine!r}")
    check(
        f"两个解析器对 {len(TRICKY_VALUES)} 个易错值判断一致",
        not mismatched,
        "; ".join(mismatched[:5]),
    )

    # 裸写 `1:1` 必须被两个解析器都读成六十进制 61（这是 YAML 1.1 的规定），
    # 正因为如此，配置里必须加引号 —— 而引号之后必须读回字符串。
    bare = miniyaml.loads("k: 1:1")["k"]
    quoted = miniyaml.loads('k: "1:1"')["k"]
    check(
        "裸写 1:1 会被读成 61（所以才必须加引号）",
        bare == 61 and quoted == "1:1",
        f"裸写={bare!r} 加引号={quoted!r}",
    )

    # 序列化往返：写成文本再读回来，必须一模一样
    round_trip = [
        "1:1", "16:9", "1024x1024", "{{prompt}}", "{{model}}", "a: b", "a #b", "a#b",
        "true", "no", "none", "Null", " 前后有空格 ", "带#号 # 的值", "2026-01-01",
        "1e3", "0x10", "-", "?", "*", "", "多行\n文本", "tab\t键", '带"引号"', "带'单引号'",
        "第 3 行: 有冒号", "https://a.b/c?d=1&e=2",
    ]
    bad: list[str] = []
    for value in round_trip:
        try:
            back = miniyaml.loads(miniyaml.dumps({"k": value}))["k"]
        except Exception as exc:  # noqa: BLE001
            bad.append(f"{value!r} -> {type(exc).__name__}: {exc}")
            continue
        if back != value or type(back) is not str:
            bad.append(f"{value!r} -> {back!r} ({type(back).__name__})")
    check(f"序列化往返不失真（{len(round_trip)} 个刁钻值）", not bad, "; ".join(bad[:5]))

    # 嵌套结构往返
    nested = {
        "version": 1,
        "image": {"strategy": "priority_then_weight", "models": [
            {"id": "a-1", "model": "x/y-2.0", "priority": 1, "weight": 50,
             "enabled": True, "supports": ["text2img"], "params": {"aspect_ratio": "1:1",
             "size": "1024*1024", "note": None, "flag": False}},
        ]},
        "defaults": {"caption": {"prompt": "用一两句中文描述这张图片：主体、风格。"}},
    }
    reloaded = miniyaml.loads(miniyaml.dumps(nested))
    check("嵌套结构往返一致", reloaded == nested, f"{reloaded!r}"[:200])

    # 写坏的配置要给可读的报错，而不是猜一个结果
    for text, why in [
        ("k: {{prompt}}", "占位符没加引号"),
        ("k: [a, b", "行内列表没闭合"),
        ("k: 重要: 别删", "值里有裸写的「冒号+空格」"),
    ]:
        try:
            miniyaml.loads(text)
            ok = False
            detail = f"{text!r} 没有报错"
        except miniyaml.MiniYamlError as exc:
            ok = True
            detail = str(exc)
        except Exception as exc:  # noqa: BLE001
            ok = False
            detail = f"{type(exc).__name__}: {exc}"
        check(f"写坏的配置会明确报错（{why}）", ok, detail)

    # 文档级裸标量：PyYAML 支持，回退解析器也必须支持
    check(
        "文档级裸标量能解析",
        miniyaml.loads("1024x1024") == "1024x1024" and miniyaml.loads("16:9") == 969,
        f"{miniyaml.loads('1024x1024')!r} / {miniyaml.loads('16:9')!r}",
    )
    check(
        "单行文本不会被硬当成配置报错",
        miniyaml.loads("这不是配置") == "这不是配置" and miniyaml.loads("a:b") == "a:b",
        f"{miniyaml.loads('这不是配置')!r} / {miniyaml.loads('a:b')!r}",
    )
    check(
        "冒号后没空格不算键分隔符（与 PyYAML 一致）",
        miniyaml.loads("url: https://a.b/c")["url"] == "https://a.b/c"
        and miniyaml.loads("k: 10:30 开会")["k"] == "10:30 开会",
        f"{miniyaml.loads('url: https://a.b/c')!r}",
    )

    # 数组/映射往返
    check("空列表与空映射往返", miniyaml.loads(miniyaml.dumps({"a": [], "b": {}})) == {"a": [], "b": {}})

    # 整份真实配置：两条解析路径必须给出**完全相同**的结构。
    # 这是最有价值的一条 —— 当初 `aspect_ratio: 1:1` 被 PyYAML 读成 61、
    # 被回退解析器读成 "1:1" 的分叉就是这么抓出来的。
    config_dir = Path(__file__).resolve().parent.parent / "config"
    for name in ("models.yaml", "secrets.example.yaml"):
        path = config_dir / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        try:
            mine = miniyaml.loads(text)
        except Exception as exc:  # noqa: BLE001
            check(f"{name} 能被回退解析器解析", False, f"{type(exc).__name__}: {exc}")
            continue
        check(f"{name} 能被回退解析器解析", True)
        # 往返：序列化再读回来必须一致（网页配置写文件走的就是这条路）
        check(
            f"{name} 序列化往返不失真",
            miniyaml.loads(miniyaml.dumps(mine)) == mine,
            "dumps 之后读不回来了",
        )
        try:
            import yaml  # type: ignore

            theirs = yaml.safe_load(text)
        except ImportError:
            continue  # 没装 PyYAML，没有第二条路径可比
        a = json.dumps(theirs, sort_keys=True, default=str)
        b = json.dumps(mine, sort_keys=True, default=str)
        check(
            f"{name} 两条解析路径结果逐字节相同",
            a == b,
            f"PyYAML 与回退解析器结果不同（装了 PyYAML 的机器会读出另一个结果）",
        )

    # 值域层面：比值类参数必须是字符串。少了这条，有人把配置里的引号删掉
    # 也依然"两条路径一致"（一致地错成 61）。
    shipped = config_dir / "models.yaml"
    if shipped.exists():
        data = miniyaml.loads(shipped.read_text(encoding="utf-8"))
        ratios: list[str] = []
        for pool in data.values():
            if not isinstance(pool, dict):
                continue
            for model in pool.get("models") or []:
                for key, value in (model.get("params") or {}).items():
                    ratios.append(f"{model.get('id')}.{key}={value!r}")
                    if key in ("aspect_ratio", "ratio", "fps") and not isinstance(value, str):
                        ratios.append(f"!! {model.get('id')}.{key} 不是字符串")
        bad_types = [r for r in ratios if r.startswith("!!")]
        check("比值类参数读出来是字符串而不是数字", not bad_types, "; ".join(bad_types))


def check_config_lint(check) -> None:
    """原文体检：能被解析、但结果不是用户想要的那种写法必须被挑出来。"""
    from mrouter import config

    cases = [
        ("params:\n  aspect_ratio: 1:1\n", "裸写比值", True),
        ("params:\n  ratio: 16:9\n", "裸写比值（视频）", True),
        ('params:\n  aspect_ratio: "1:1"\n', "加引号的比值（应当放过）", False),
        ("note: 2026-01-01\n", "裸写日期", True),
        ('note: "2026-01-01"\n', "加引号的日期（应当放过）", False),
        ("prompt: {{prompt}}\n", "没加引号的占位符", True),
        ('prompt: "{{prompt}}"  # 说明\n', "加引号的占位符（应当放过）", False),
        ("params: {size:1024x1024}\n", "行内映射冒号后没空格", True),
        ("params: {size: 1024x1024}\n", "行内映射写法正确（应当放过）", False),
        ("note: 重要: 别删\n", "值里有裸写的「冒号+空格」", True),
        ('note: "重要: 别删"\n', "加了引号（应当放过）", False),
        ("keys:\n  ARK_API_KEY: ab:cd\n", "密钥值里带冒号但不含空格（应当放过）", False),
        ("endpoint: https://ark.cn-beijing.volces.com/api/v3\n", "URL（应当放过）", False),
        ("# 注释里的 1:1 不该被误报\nsize: 1024x1024\n", "注释与正常值（应当放过）", False),
        ("size: 1024*1024\ntimeout_seconds: 120\n", "正常值（应当放过）", False),
    ]
    for text, why, should_warn in cases:
        messages = config.lint_text(text)
        got = bool(messages)
        check(
            f"lint {'能查出' if should_warn else '不误报'}：{why}",
            got == should_warn,
            f"期望{'有' if should_warn else '无'}告警，实际 {messages}",
        )

    # 自带的默认配置本身必须是干净的 —— 这是最容易忘记的一条。
    # （这里显式读文件，不能走 lint_files()，因为它会跟随 MEDIA_ROUTER_CONFIG
    #   指向本次自检的临时配置。）
    shipped = config.CONFIG_DIR / "models.yaml"
    shipped_lints = config.lint_text(shipped.read_text(encoding="utf-8")) if shipped.exists() else []
    check("自带的 models.yaml 通过体检", not shipped_lints, "; ".join(shipped_lints[:3]))

    shipped_secrets = config.CONFIG_DIR / "secrets.example.yaml"
    if shipped_secrets.exists():
        secret_lints = config.lint_text(shipped_secrets.read_text(encoding="utf-8"))
        check("自带的 secrets.example.yaml 通过体检", not secret_lints, "; ".join(secret_lints[:3]))

    # 解析失败必须报成"配置错误"（kind=config），而不是丢一个原始异常让 CLI
    # 标成 internal —— 用户看到 internal 会以为是程序坏了，其实是自己写错了。
    import os  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    original = os.environ.get("MEDIA_ROUTER_CONFIG")
    with tempfile.TemporaryDirectory(prefix="media-router-lint-") as workdir:
        broken = Path(workdir) / "broken.yaml"
        # 同时埋两个坑：一个只是"读出来不是你想的值"（1:1），一个是真语法错（占位符没引号）。
        # 报错信息里应该把两个都指出来。
        broken.write_text(
            "image:\n"
            "  models:\n"
            "    - id: a\n"
            "      provider: volcengine\n"
            "      params:\n"
            "        r: 1:1\n"
            "        prompt: {{prompt}}\n",
            encoding="utf-8",
        )
        os.environ["MEDIA_ROUTER_CONFIG"] = str(broken)
        try:
            try:
                config.load_config()
                check("写坏的配置会抛 ConfigError", False, "居然没报错")
            except config.ConfigError as exc:
                text = str(exc)
                check(
                    "写坏的配置会抛 ConfigError",
                    "占位符" in text and "1:1" in text,
                    text[:160],
                )
            except Exception as exc:  # noqa: BLE001
                check("写坏的配置会抛 ConfigError", False, f"{type(exc).__name__}: {exc}")
            lints = config.lint_files()
            check("解析失败时体检结果也能拿到", any("1:1" in m for m in lints), str(lints)[:120])
        finally:
            if original is None:
                os.environ.pop("MEDIA_ROUTER_CONFIG", None)
            else:
                os.environ["MEDIA_ROUTER_CONFIG"] = original

    # ---------------------------------------------------------- 命令行入口
    # 光敲脚本名是最自然的动作，必须给引导而不是 "arguments are required"
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli_main([])
    payload = json.loads(out.getvalue().strip() or "{}")
    check(
        "不带子命令时给出引导而不是报错",
        code == 0 and payload.get("status") == "help" and "web" in payload.get("commands", {}),
        f"code={code} payload={str(payload)[:120]}",
    )
    check(
        "引导走 stderr、stdout 仍然只有 JSON",
        "web" in err.getvalue() and out.getvalue().count("\n") == 1,
        f"stderr={err.getvalue()[:60]!r}",
    )

    # 每个子命令都要有 --help 且能跑起来（漏注册的会在这里露馅）
    available = set(build_parser()._subparsers._group_actions[0].choices)  # noqa: SLF001
    check(
        "八个子命令都注册了 --help",
        available
        == {"config", "providers", "list", "resolve", "generate", "report", "health", "web"},
        str(sorted(available)),
    )
    for name in sorted(available):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                build_parser().parse_args([name, "--help"])
                ok = True
                detail = ""
            except SystemExit as exc:
                ok = exc.code == 0
                detail = f"退出码 {exc.code}"
            except Exception as exc:  # noqa: BLE001
                ok = False
                detail = f"{type(exc).__name__}: {exc}"
        check(f"`{name} --help` 正常", ok, detail)



if __name__ == "__main__":
    raise SystemExit(main())
