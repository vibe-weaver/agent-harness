"""Web 配置链路的端到端自检。

跑法：
    python scripts/selftest_web.py

它真的会起一个 HTTP 服务、真的发请求、真的写文件，然后**还原现场**：
测试开始时把 config/models.web.yaml 与 config/secrets.web.yaml 挪到一边，
结束时原样放回，并校验你手写的 models.yaml 一个字节都没被动过。

不需要任何真实 API Key；涉及网络的检查只断言"结构正确"，不断言网络结果
（断网环境下也应全绿）。
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mrouter import catalog, config, probe, store, webserver  # noqa: E402

# 探测要发真实请求，自检里没必要等满 25 秒
probe.PROBE_TIMEOUT = 6.0

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    if ok:
        PASSED.append(name)
        print(f"  [PASS] {name}")
    else:
        FAILED.append(f"{name} :: {detail}")
        print(f"  [FAIL] {name}  -> {detail}")
    return ok


def section(title: str) -> None:
    print(f"\n--- {title} " + "-" * max(0, 58 - len(title)))


# ---------------------------------------------------------------- HTTP 客户端


# 全是访问 127.0.0.1，必须绕开环境里可能存在的 HTTP_PROXY。
# 否则请求会被转发到代理，而代理会把"连不上"变成 502，把真实的
# ConnectionRefused 掩盖成"服务还活着" —— 自检结论就不可信了。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Client:
    def __init__(self, host: str, port: int, token: str) -> None:
        self.base = f"http://{host}:{port}"
        self.token = token

    def call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        token: str | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 60.0,
    ) -> tuple[int, Any]:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        value = self.token if token is None else token
        if value:
            req.add_header("X-MR-Token", value)
        for key, val in (headers or {}).items():
            req.add_header(key, val)
        try:
            with _OPENER.open(req, timeout=timeout) as resp:
                raw = resp.read()
                return resp.status, _decode(raw)
        except urllib.error.HTTPError as exc:
            return exc.code, _decode(exc.read())


def _decode(raw: bytes) -> Any:
    text = raw.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def raw_http(host: str, port: int, payload: bytes, timeout: float = 6.0) -> str:
    """裸 socket 发请求 —— 用来伪造成 urllib 不允许伪造的头（比如 Host）。"""
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(payload)
        sock.settimeout(timeout)
        chunks: list[bytes] = []
        try:
            while True:
                data = sock.recv(65536)
                if not data:
                    break
                chunks.append(data)
        except socket.timeout:
            pass
    return b"".join(chunks).decode("utf-8", "replace")


# ---------------------------------------------------------------- 现场保护

# 自检用的基座配置夹具：开源版 models.yaml 初始是空模型池，
# 「手写层模型」相关用例需要基座里确实有模型（一图一 survivor、一视频）。
# 只在自检期间临时换上，结束后按 Sandbox 还原成用户自己的版本。
_FIXTURE_MODELS = """\
version: 1
image:
  strategy: priority_then_weight
  models:
    - id: fixture-image-a
      provider: openai
      model: fixture-image-model-a
      priority: 1
      weight: 50
      enabled: true
      supports: [text2img]
      api_key_env: MY_API_KEY
    - id: fixture-image-b
      provider: openai
      model: fixture-image-model-b
      priority: 1
      weight: 50
      enabled: true
      supports: [text2img]
      api_key_env: MY_API_KEY
video:
  strategy: priority_then_weight
  models:
    - id: fixture-video-a
      provider: openai
      model: fixture-video-model-a
      priority: 1
      weight: 50
      enabled: true
      supports: [text2video]
      api_key_env: MY_API_KEY
"""


class Sandbox:
    """把配置页会写的两个文件先藏起来，结束后原样还回去。"""

    def __init__(self) -> None:
        self.targets = [
            config.CONFIG_DIR / "models.web.yaml",
            config.CONFIG_DIR / "secrets.web.yaml",
        ]
        self.saved: dict[Path, bytes] = {}
        self.base_config = config.CONFIG_DIR / "models.yaml"
        self.base_before = self.base_config.read_bytes() if self.base_config.exists() else b""

    def __enter__(self) -> "Sandbox":
        for path in self.targets:
            if path.exists():
                self.saved[path] = path.read_bytes()
                path.unlink()
        # 开源版自带的 models.yaml 是空模型池（用户按需自己加），
        # 而手写层相关的用例需要基座里有模型。这里临时换成一份固定夹具，
        # 结束时原样还原，不依赖也不动用户自己的配置。
        if self.base_config.exists():
            self.base_config.write_bytes(_FIXTURE_MODELS.encode("utf-8"))
        return self

    def __exit__(self, *_exc: object) -> None:
        for path in self.targets:
            if path.exists():
                path.unlink()
        for path, blob in self.saved.items():
            path.write_bytes(blob)
        if self.base_config.exists() and self.base_before:
            self.base_config.write_bytes(self.base_before)
        self.verify()

    def verify(self) -> None:
        after = self.base_config.read_bytes() if self.base_config.exists() else b""
        check(
            "自检结束后 models.yaml 未被改动",
            after == self.base_before,
            "手写的 models.yaml 被动了，这是个严重问题",
        )
        restored = all(
            path.exists() and path.read_bytes() == blob for path, blob in self.saved.items()
        )
        check("自检结束后原有配置已还原", restored, "备份没有正确还原")


# ---------------------------------------------------------------- 主流程


def check_page_js(page: Any) -> None:
    """用 node 检查页面内联 JS 的语法。

    页面是单文件 HTML，JS 写崩了只会在浏览器控制台报错，服务端一点感觉都没有 ——
    所以这里做一次纯语法检查（不需要浏览器，不用装任何依赖）。
    环境里找不到 node 就跳过，不当失败。
    """
    import re
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    if not node:
        # 常见的备选位置（相对家目录或系统路径），找不到就跳过检查
        for candidate in (
            str(Path.home() / "AppData/Roaming/npm/node.exe"),
            "/usr/local/bin/node",
            "/usr/bin/node",
        ):
            if Path(candidate).exists():
                node = candidate
                break
    if not node:
        check("页面内联 JS 语法（环境里没有 node，跳过）", True, "")
    else:
        match = re.search(r"<script>([\s\S]*?)</script>", str(page))
        if not match:
            check("页面里能找到 script 块", False, "没有 <script> 标签")
        else:
            js = match.group(1).replace(webserver.TOKEN_PLACEHOLDER, "test-token")
            with tempfile.TemporaryDirectory(prefix="media-router-js-") as workdir:
                script = Path(workdir) / "page.js"
                script.write_text(js, encoding="utf-8")
                try:
                    proc = subprocess.run(
                        [node, "--check", str(script)],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=30,
                    )
                    detail = (proc.stderr or proc.stdout or "").strip().splitlines()
                    check("页面内联 JS 语法正确", proc.returncode == 0, detail[0] if detail else "")
                except Exception as exc:  # noqa: BLE001
                    check("页面内联 JS 语法正确", False, f"{type(exc).__name__}: {exc}")

    # 顺带确认界面上那几个关键入口还在（改版时最容易悄悄漏掉）
    for snippet, why in [
        ("btn-add-vendor", "添加厂商按钮"),
        ("btn-add-model", "添加模型按钮"),
        ('data-act="del-model"', "模型删除按钮"),
        ('data-act="toggle-model"', "模型启停开关"),
        ('data-act="edit-model"', "模型编辑按钮"),
        ('data-act="edit-vendor"', "厂商编辑按钮"),
        ("/api/vendor/test", "厂商连通性测试"),
        ("/api/model/test", "模型连通性测试"),
        ("function uiConfirm(", "删除模型的确认弹窗"),
        ("modal-mask", "弹窗遮罩层"),
        # 这三条是"点了没反应"的防线，缺一个就可能又变成静默失效
        ("function guarded(", "点击处理的异常兜底"),
        ("function networkDown(", "后台断开时的提示"),
        ("scrollIntoView", "表单自动滚入视野"),
        # 自定义接口必须能在界面上配 options，否则选中它等于死路
        ('id="vf-options"', "接口参数输入框"),
        ("function collectOptions()", "接口参数校验"),
    ]:
        check(f"页面里还有{why}", snippet in str(page), f"找不到 {snippet!r}")

    # 反面检查：拉取模型功能已按要求整体移除（页面只剩手动添加）
    for snippet, why in [
        ('id="btn-fetch-models"', "拉取按钮"),
        ('id="mf-fetched"', "拉取结果面板"),
        ("btn-add-selected", "批量添加按钮"),
        ("/api/models/bulk", "批量添加接口调用"),
        ("/api/vendor/models", "拉取接口调用"),
        ("function resetFetched(", "拉取面板重置"),
        ("function renderFetched(", "拉取结果渲染"),
        ("function addSelectedModels(", "批量添加处理"),
    ]:
        check(f"页面已移除{why}", snippet not in str(page), f"还存在 {snippet!r}")

    # ---------------------------------------------------------- A 档体验改进
    # A1：用页内非阻塞错误条替代阻塞式 alert()
    check("有 showError（非阻塞错误提示）", "function showError(" in str(page), "找不到 showError 定义")
    check("错误条有对应样式 #err-toast", "#err-toast{" in str(page), "找不到 #err-toast 样式")
    check(
        "用户操作不再用阻塞式 alert（只剩注释里那一处文字）",
        str(page).count("alert(") <= 1,
        f"页面里还有 {str(page).count('alert(')} 处 alert(",
    )

    # A2：可点选的卡片（div 充当按钮）必须键盘可达
    check(
        "厂商/类目卡片是 role=button 且可聚焦",
        'role="button"' in str(page) and 'tabindex="0"' in str(page),
        "卡片缺少 role/tabindex，键盘走不到",
    )
    check(
        "卡片同步 aria-pressed 选中态",
        "aria-pressed" in str(page) and 'setAttribute("aria-pressed"' in str(page),
        "读屏读不出选中状态",
    )
    check(
        "卡片接了回车/空格键盘事件",
        'ev.key !== "Enter"' in str(page) or 'key === "Enter"' in str(page),
        "卡片没有键盘触发逻辑",
    )

    # A3：顶部路径栏默认折叠，给新手降噪
    check(
        "路径栏改成可折叠的 details",
        '<details class="pathbar' in str(page),
        "路径栏还是常驻的 div，首屏噪音大",
    )

    # A4：权重滑块与数字框并排
    check(
        "权重滑块与数字框并排（.wrow）",
        'class="wrow"' in str(page) and ".wrow{" in str(page),
        "权重控件还是上下堆叠的两个独立输入",
    )

    # 回归防线：kindList() 返回的已经是拼好的字符串，外面再 .join("") 会抛
    # "kindList(...).join is not a function"，编辑模型直接打不开（实测踩过）。
    # 用"给 model-suggestions 赋值的那段不能再出现 .join"来兜底 —— 正则对
    # kindList 里的嵌套括号不可靠，盯着赋值语句最稳。
    import re as _re
    _m = _re.search(r'model-suggestions"\)\.innerHTML\s*=\s*([^;]*);', str(page))
    check(
        "填充模型候选时没有对 kindList 结果再 .join",
        bool(_m) and ".join(" not in _m.group(1),
        _m.group(1)[:120] if _m else "找不到 model-suggestions 赋值语句",
    )


def main() -> int:
    print("media-router Web 配置自检")
    print(f"配置目录：{config.CONFIG_DIR}")

    with Sandbox():
        server = webserver.ConfigServer(host="127.0.0.1", port=8765, open_browser=False)
        url = server.start_background()
        time.sleep(0.2)
        client = Client("127.0.0.1", server.port, server.token)
        print(f"服务已启动：{url}\n")

        try:
            run_checks(client, server)
        finally:
            server.stop()

    print("\n" + "=" * 66)
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    if FAILED:
        print("\n失败明细：")
        for item in FAILED:
            print(f"  - {item}")
        return 1
    print("全部通过。")
    return 0


def check_generic_http(client: Client, check) -> None:
    """用一个本地模拟接口验证：只填一个接口地址，能不能真的生成出图。"""
    import base64
    import shutil
    import struct
    import zlib
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse

    # 一张 1x1 的真 PNG，用来验证下载与落盘
    def make_png() -> bytes:
        def chunk(tag: bytes, data: bytes) -> bytes:
            return (
                struct.pack(">I", len(data))
                + tag
                + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
            )

        raw = b"\x00" + bytes([120, 160, 200])
        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b"")
        )

    png = make_png()
    seen: dict[str, Any] = {}

    class Fake(BaseHTTPRequestHandler):
        def log_message(self, *args):  # noqa: A003
            pass

        def _json(self, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            seen["path"] = self.path
            seen["body"] = raw.decode("utf-8", "replace")
            seen["auth"] = self.headers.get("Authorization") or ""
            port = self.server.server_address[1]
            route = urlparse(self.path).path
            if route == "/broken":
                self._json({"unexpected": "shape"})  # 响应里根本没有产物地址
                return
            if route == "/nested":
                # 响应里**有两个地址**：一个在调试字段里（不是产物），一个才是真的。
                # 没配 result_path 时会取到前面那个 -> 下载不通；
                # 配了 result_path / result_url_field 才会取到对的那个。
                self._json({
                    "debug": {"preview": f"http://127.0.0.1:{port}/wrong.png"},
                    "payload": {"items": [{"link": f"http://127.0.0.1:{port}/out.png"}]},
                })
                return
            self._json({"data": {"images": [{"url": f"http://127.0.0.1:{port}/out.png"}]}})

        def do_GET(self) -> None:  # noqa: N802
            if urlparse(self.path).path == "/out.png":
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(png)))
                self.end_headers()
                self.wfile.write(png)
                return
            self.send_error(404)

    mock = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    port = mock.server_address[1]
    threading.Thread(target=mock.serve_forever, daemon=True).start()

    out_root = config.SKILL_DIR / "outputs" / "_connectivity_test"
    existed_before = out_root.exists()
    before = {p.name for p in out_root.iterdir()} if existed_before else set()

    try:
        # 只填地址、不填任何 options —— 对应用户在界面上的操作
        status, res = client.call(
            "POST",
            "/api/vendor",
            {"id": "", "catalog_key": "generic_http", "label": "自检用的自定义接口",
             "api_key_env": "MY_API_KEY", "api_key": "",
             "endpoints": {"image": f"http://127.0.0.1:{port}/generate"}},
        )
        check("建自定义接口厂商成功", status == 200 and res.get("ok"), f"{status} {res}")
        vendor = res.get("id")

        status, res = client.call(
            "POST",
            "/api/model",
            {"id": "", "vendor": vendor, "model": "my-custom-model", "kind": "image",
             "priority": 1, "weight": 1, "supports": ["text2img"]},
        )
        model_id = res.get("id")
        check("给自定义接口挂模型成功", status == 200 and res.get("ok"), f"{status} {res}")

        # 试生成：真发请求、真下载、真落盘
        status, res = client.call("POST", "/api/model/test", {"id": model_id, "deep": True}, timeout=90)
        check("试生成成功（只填了接口地址）", status == 200 and res.get("ok") is True, f"{status} {str(res)[:200]}")
        check("产物真的落盘了", bool(res.get("files")), str(res.get("files")))
        if res.get("files"):
            path = Path(res["files"][0])
            check("落盘的是有效的 PNG", path.exists() and path.read_bytes().startswith(b"\x89PNG"), str(path))
        check("请求打到了用户填的地址", seen.get("path") == "/generate", str(seen.get("path")))
        check("prompt 被带进了请求体", "自检" in str(seen.get("body")) or "prompt" in str(seen.get("body")), str(seen.get("body"))[:120])

        # 失败分支：响应结构不对时，报错要能指导用户，而不是只说"缺 submit.url"
        status, res = client.call(
            "POST", "/api/vendor",
            {"id": "", "catalog_key": "generic_http", "label": "自检-坏接口",
             "api_key_env": "MY_API_KEY", "api_key": "",
             "endpoints": {"image": f"http://127.0.0.1:{port}/broken"}},
        )
        broken_vendor = res.get("id")
        status, res = client.call(
            "POST", "/api/model",
            {"id": "", "vendor": broken_vendor, "model": "broken-model", "kind": "image",
             "priority": 1, "weight": 1, "supports": ["text2img"]},
        )
        broken_model = res.get("id")
        status, res = client.call("POST", "/api/model/test", {"id": broken_model, "deep": True}, timeout=60)
        text = str(res.get("error") or "")
        check("接口响应不对时试生成会失败", status == 200 and res.get("ok") is False, f"{status} {str(res)[:160]}")
        check(
            "报错里给出了怎么配（result_path / 示例），而不是一句没头没脑的缺配置",
            "result_path" in text and "最小可用示例" in text,
            text[:220],
        )
        check(
            "报错里说的是模型名而不是 probe-temp",
            "probe-temp" not in text and "broken-model" in text,
            text[:120],
        )

        # 界面上填的 options 要能存下来并真的生效
        status, res = client.call(
            "POST", "/api/vendor",
            {"id": broken_vendor, "catalog_key": "generic_http", "label": "自检-坏接口",
             "api_key_env": "MY_API_KEY", "api_key": "",
             "endpoints": {"image": f"http://127.0.0.1:{port}/nested"}},
        )
        check("把接口地址换成「响应里有两个地址」的接口", status == 200, f"{status} {res}")
        status, res = client.call("POST", "/api/model/test", {"id": broken_model, "deep": True}, timeout=60)
        check(
            "没配 result_path 时会取到响应里靠前的那个错误地址",
            status == 200 and res.get("ok") is False,
            str(res)[:160],
        )

        status, res = client.call(
            "POST", "/api/vendor",
            {"id": broken_vendor, "catalog_key": "generic_http", "label": "自检-坏接口",
             "api_key_env": "MY_API_KEY", "api_key": "",
             "endpoints": {"image": f"http://127.0.0.1:{port}/nested"},
             "options": '{"result_path": "payload.items", "result_url_field": "link"}'},
        )
        check("界面上填的 options 能保存", status == 200 and res.get("ok"), f"{status} {res}")
        status, boot = client.call("GET", "/api/bootstrap")
        view = next((v for v in boot["vendors"] if v["id"] == broken_vendor), {})
        check(
            "保存后的 options 能读回来",
            view.get("options", {}).get("result_path") == "payload.items",
            str(view.get("options")),
        )
        status, res = client.call("POST", "/api/model/test", {"id": broken_model, "deep": True}, timeout=90)
        check("补上 result_path 之后取到正确的地址并成功", status == 200 and res.get("ok") is True, f"{status} {str(res)[:200]}")
        check(
            "取到的确实是 out.png 而不是调试字段里的那个",
            str(res.get("urls") or "").find("out.png") >= 0 and "wrong.png" not in str(res.get("urls")),
            str(res.get("urls")),
        )

        # options 不是合法 JSON 时要给出人看得懂的报错
        status, res = client.call(
            "POST", "/api/vendor",
            {"id": "", "catalog_key": "generic_http", "label": "自检-JSON错",
             "api_key_env": "MY_API_KEY", "api_key": "", "options": "{不是 json}"},
        )
        check("options 填错 JSON 会被拒并说明", status == 400 and "JSON" in str(res.get("error", "")), f"{status} {res}")
    finally:
        mock.shutdown()
        mock.server_close()
        # 只删本次测试新增的产物，不动用户原有文件
        if out_root.exists():
            for path in list(out_root.iterdir()):
                if path.name not in before:
                    if path.is_dir():
                        shutil.rmtree(path, ignore_errors=True)
                    else:
                        path.unlink(missing_ok=True)
            if not existed_before and not any(out_root.iterdir()):
                out_root.rmdir()


def run_checks(client: Client, server: webserver.ConfigServer) -> None:
    # ---------------------------------------------------------- 页面与鉴权
    section("页面与鉴权")
    status, page = client.call("GET", "/")
    check("GET / 返回配置页面", status == 200 and "media-router" in str(page), f"status={status}")
    check(
        "页面里已经替换掉 token 占位符",
        isinstance(page, str) and webserver.TOKEN_PLACEHOLDER not in page,
        "页面里还留着 __MR_TOKEN__",
    )
    check(
        "页面里带上了真实 token",
        isinstance(page, str) and server.token in page,
        "页面里的 token 不对",
    )

    # 页面是一整块 HTML，JS 写坏了要到浏览器里才看得出来。有 node 就顺手验一下语法。
    check_page_js(page)

    status, payload = client.call("GET", "/api/bootstrap", token="")
    check("无 token 访问 API 被拒", status == 401, f"status={status}")

    status, payload = client.call("GET", "/api/bootstrap", token="wrong-token")
    check("错误 token 被拒", status == 401, f"status={status}")

    status, payload = client.call("GET", "/api/bootstrap")
    check("带 token 能读到配置", status == 200 and "catalog" in payload, f"status={status}")
    if status == 200:
        check("厂商目录有 10 个平台", len(payload["catalog"]) == 10, str(len(payload["catalog"])))
        check(
            "厂商目录带有中文说明与密钥字段提示",
            all(item["label"] and item["beginner"] for item in payload["catalog"]),
            "有目录条目缺少中文说明",
        )
        check(
            "默认没有厂商、类目含 image/video",
            payload["vendors"] == [] and "image" in payload["kinds"] and "video" in payload["kinds"],
            str(payload["kinds"]),
        )
        check("返回了密钥文件路径", bool(payload["paths"]["secrets_path"]), "secrets_path 为空")

    spoof = raw_http(
        "127.0.0.1",
        server.port,
        (
            "GET /api/bootstrap HTTP/1.1\r\n"
            "Host: evil.example.com\r\n"
            f"X-MR-Token: {server.token}\r\n"
            "Connection: close\r\n\r\n"
        ).encode(),
    )
    check("伪造 Host 被拒（防 DNS 重绑定）", " 403 " in spoof.split("\n")[0], spoof.split("\n")[0])

    status, payload = client.call(
        "GET", "/api/bootstrap", headers={"Origin": "https://evil.example.com"}
    )
    check("跨站 Origin 被拒（防 CSRF）", status == 403, f"status={status}")

    status, payload = client.call(
        "GET", "/api/bootstrap", headers={"Origin": f"http://127.0.0.1:{server.port}"}
    )
    check("同源 Origin 允许", status == 200, f"status={status}")

    status, payload = client.call("GET", "/api/nope")
    check("未知接口返回 404", status == 404, f"status={status}")

    status, payload = client.call("POST", "/api/bootstrap", {})
    check("方法不匹配返回 405", status == 405, f"status={status}")

    status, payload = client.call("POST", "/api/vendor", {}, token="")
    check("写接口同样要求 token", status == 401, f"status={status}")

    # ---------------------------------------------------------- 厂商
    section("厂商配置")

    status, res = client.call(
        "POST",
        "/api/vendor",
        {"id": "", "catalog_key": "volcengine", "label": "", "api_key_env": "ARK_API_KEY",
         "api_key": "fake-ark-key-for-test", "endpoints": {"image": "", "video": ""}},
    )
    check("添加火山方舟厂商成功", status == 200 and res.get("ok"), str(res))
    ark_id = res.get("id") if isinstance(res, dict) else None
    check("厂商 id 自动生成", ark_id == "v-volcengine", str(ark_id))
    check("密钥已写入", bool(res.get("key_saved")) if isinstance(res, dict) else False, str(res))

    secrets_path = config.secrets_write_path()
    check("密钥文件已创建", secrets_path.exists(), str(secrets_path))
    if secrets_path.exists():
        text = secrets_path.read_text(encoding="utf-8")
        check("密钥文件里有 ARK_API_KEY", "ARK_API_KEY" in text, text[:200])
        check("密钥文件顶部有说明注释", text.lstrip().startswith("#"), text[:80])

    overlay = store.overlay_path()
    check("模型池文件已创建", overlay.exists(), str(overlay))
    if overlay.exists():
        text = overlay.read_text(encoding="utf-8")
        check("模型池文件里没有明文密钥", "fake-ark-key-for-test" not in text, "密钥泄漏到 models.web.yaml 了")
        check("模型池文件只记环境变量名", "api_key_env: ARK_API_KEY" in text, text[:400])

    status, payload = client.call("GET", "/api/bootstrap")
    vendors = {v["id"]: v for v in payload["vendors"]}
    check("bootstrap 能看到新厂商", ark_id in vendors, str(list(vendors)))
    check("bootstrap 标记密钥已配置", vendors.get(ark_id, {}).get("has_key") is True, str(vendors.get(ark_id)))

    # 第二个同平台厂商：环境变量名应自动让开，不能互相覆盖
    status, res2 = client.call(
        "POST",
        "/api/vendor",
        {"id": "", "catalog_key": "volcengine", "label": "火山方舟（小号）",
         "api_key_env": "ARK_API_KEY", "api_key": "fake-second-key", "endpoints": {}},
    )
    check("添加第二个同平台厂商成功", status == 200 and res2.get("ok"), str(res2))
    second_id = res2.get("id") if isinstance(res2, dict) else None
    status, payload = client.call("GET", "/api/bootstrap")
    vendors = {v["id"]: v for v in payload["vendors"]}
    env2 = vendors.get(second_id, {}).get("api_key_env")
    check("第二个厂商的密钥变量名自动避让", env2 == "ARK_API_KEY_2", str(env2))
    if secrets_path.exists():
        text = secrets_path.read_text(encoding="utf-8")
        check(
            "两把密钥都存下来了、互不覆盖",
            "ARK_API_KEY:" in text and "ARK_API_KEY_2" in text
            and "fake-ark-key-for-test" in text and "fake-second-key" in text,
            text,
        )

    # 免密钥厂商
    status, res3 = client.call(
        "POST", "/api/vendor", {"id": "", "catalog_key": "native", "label": "", "api_key": ""}
    )
    check("添加免密钥厂商成功", status == 200 and res3.get("ok"), str(res3))
    status, payload = client.call("GET", "/api/bootstrap")
    native_view = next((v for v in payload["vendors"] if v["id"] == res3.get("id")), {})
    check("免密钥厂商不显示缺密钥告警", native_view.get("has_key") is True, str(native_view))

    status, res = client.call("POST", "/api/vendor", {"id": "", "catalog_key": "不存在的平台"})
    check("未知平台被拒", status == 400, f"status={status} {res}")

    status, res = client.call("POST", "/api/vendor/test", {"catalog_key": "native", "kind": "image"})
    check("免密钥厂商测试返回结构正确", status == 200 and "checks" in res, str(res))
    if status == 200:
        names = [c["name"] for c in res["checks"]]
        check("测试项包含厂商识别/接口可达/密钥校验", {"厂商识别", "接口可达", "密钥校验"} <= set(names), str(names))
        check("免密钥厂商的密钥校验被标记为跳过", any(c["name"] == "密钥校验" and c["status"] == "skip" for c in res["checks"]), str(res["checks"]))

    status, res = client.call(
        "POST", "/api/vendor/test", {"catalog_key": "kling", "kind": "video", "api_key": "只有一段没有冒号"}
    )
    check("可灵密钥格式错误被点出来", status == 200 and any(
        c["name"] == "密钥校验" and c["status"] == "fail" and "冒号" in c["detail"] for c in res["checks"]
    ), str(res))

    # 火山方舟：拉取列表接口已在页面上移除（改手动填模型名），
    # 但后端 /api/vendor/models 路由仍保留兼容。这里只验证它如实声明不支持。
    status, res = client.call("POST", "/api/vendor/models", {"vendor_id": ark_id, "kind": "image"})
    check(
        "火山方舟拉取列表如实返回不支持（不抛异常）",
        status == 200 and res.get("ok") is False and res.get("reason") == "unsupported",
        str(res),
    )
    check(
        "不支持时也给出了该平台常用模型名，用户不至于无从下手",
        bool(res.get("suggestions")),
        str(res.get("suggestions")),
    )
    check(
        "报错文案说明了为什么要手动填（AK/SK 签名）",
        "签名" in str(res.get("error", "")) or "控制台" in str(res.get("error", "")),
        str(res.get("error"))[:160],
    )

    # ---------------------------------------------------------- 模型
    section("模型配置")

    status, res = client.call(
        "POST",
        "/api/model",
        {"id": "", "vendor": ark_id, "model": "doubao-seedream-3-0-t2i-250415", "label": "",
         "kind": "image", "priority": 1, "weight": 50, "enabled": True, "supports": ["text2img"]},
    )
    check("添加图像模型成功", status == 200 and res.get("ok"), str(res))
    image_id = res.get("id") if isinstance(res, dict) else None
    check("模型 id 由模型名生成", image_id == "doubao-seedream-3-0-t2i-250415", str(image_id))

    status, res = client.call(
        "POST",
        "/api/model",
        {"id": "", "vendor": ark_id, "model": "doubao-seedance-1-0-pro-250528", "label": "Seedance 主力",
         "kind": "video", "priority": 1, "weight": 70, "enabled": True,
         "supports": ["text2video", "img2video"]},
    )
    check("添加视频模型成功", status == 200 and res.get("ok"), str(res))
    video_id = res.get("id") if isinstance(res, dict) else None

    status, payload = client.call("GET", "/api/bootstrap")
    image_ids = [m["id"] for m in payload["models"].get("image", [])]
    video_ids = [m["id"] for m in payload["models"].get("video", [])]
    check("bootstrap 里图像池有该模型", image_id in image_ids, str(image_ids))
    check("bootstrap 里视频池有该模型", video_id in video_ids, str(video_ids))
    video_model = next((m for m in payload["models"].get("video", []) if m["id"] == video_id), {})
    check("视频模型带上了图生视频能力", "img2video" in video_model.get("supports", []), str(video_model))
    check("模型带上了厂商中文名", video_model.get("vendor_label") == "火山方舟（即梦 / Seedance）", str(video_model.get("vendor_label")))
    check("模型带上了权重与优先级", video_model.get("weight") == 70 and video_model.get("priority") == 1, str(video_model))

    status, res = client.call(
        "POST",
        "/api/model",
        {"id": "", "vendor": ark_id, "model": "doubao-seedream-3-0-t2i-250415", "kind": "image",
         "priority": 1, "weight": 30, "supports": ["text2img"]},
    )
    check("同名模型不会撞 id", status == 200 and res.get("id") == "doubao-seedream-3-0-t2i-250415-2", str(res))

    status, res = client.call("POST", "/api/model", {"id": "", "vendor": "v-不存在", "model": "x", "kind": "image"})
    check("引用不存在的厂商被拒", status == 400, f"status={status} {res}")

    status, res = client.call("POST", "/api/model", {"id": "", "vendor": ark_id, "model": "", "kind": "image"})
    check("空模型名被拒", status == 400, f"status={status} {res}")

    status, res = client.call("POST", "/api/model", {"id": "", "vendor": ark_id, "model": "x", "kind": "audio"})
    check("非法类目被拒", status == 400, f"status={status} {res}")

    # 编辑：改权重与优先级
    status, res = client.call(
        "POST",
        "/api/model",
        {"id": video_id, "vendor": ark_id, "model": "doubao-seedance-1-0-pro-250528",
         "label": "Seedance 主力", "kind": "video", "priority": 2, "weight": 10,
         "supports": ["text2video"]},
    )
    check("编辑模型成功", status == 200 and res.get("created") is False, str(res))
    status, payload = client.call("GET", "/api/bootstrap")
    video_model = next((m for m in payload["models"].get("video", []) if m["id"] == video_id), {})
    check("编辑后权重已更新", video_model.get("weight") == 10 and video_model.get("priority") == 2, str(video_model))
    check("编辑后没有重复插入", len([m for m in payload["models"]["video"] if m["id"] == video_id]) == 1, str(payload["models"]["video"]))

    # 停用/启用
    status, res = client.call("POST", "/api/model/toggle", {"id": video_id, "enabled": False})
    check("停用模型成功", status == 200 and res.get("enabled") is False, str(res))
    status, payload = client.call("GET", "/api/bootstrap")
    video_model = next((m for m in payload["models"].get("video", []) if m["id"] == video_id), {})
    check("停用状态已持久化", video_model.get("enabled") is False, str(video_model))

    status, res = client.call(
        "POST",
        "/api/model",
        {"id": video_id, "vendor": ark_id, "model": "doubao-seedance-1-0-pro-250528",
         "kind": "video", "priority": 2, "weight": 10, "supports": ["text2video"]},
    )
    status, payload = client.call("GET", "/api/bootstrap")
    video_model = next((m for m in payload["models"].get("video", []) if m["id"] == video_id), {})
    check("编辑不会顺手把停用的模型开回来", video_model.get("enabled") is False, str(video_model))

    # 连通性测试（轻量）
    status, res = client.call("POST", "/api/model/test", {"id": image_id, "deep": False})
    check("轻量测试返回检查项", status == 200 and "checks" in res and "summary" in res, str(res)[:300])
    if status == 200:
        names = [c["name"] for c in res["checks"]]
        check(
            "轻量测试覆盖厂商/地址/可达/密钥四项",
            {"厂商识别", "接口地址", "接口可达", "密钥校验"} <= set(names),
            str(names),
        )
        check("轻量测试不会真的生成", "files" not in res, str(res.keys()))
        check(
            "密钥无效时给出明确结论",
            res.get("ok") is False and "密钥" in str(res.get("summary")),
            f"ok={res.get('ok')} summary={res.get('summary')}",
        )

    status, res = client.call("POST", "/api/model/test", {"deep": False})
    check("没有模型信息时测试被拒", status == 400, f"status={status} {res}")

    status, res = client.call("POST", "/api/model/test", {"id": "不存在的模型", "deep": False})
    check("测试不存在的模型被拒", status == 400, f"status={status} {res}")

    # ---------------------------------------------------------- 文件预览
    section("产物预览")

    outputs = config.SKILL_DIR / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    sample = outputs / "_selftest_preview.txt"
    sample.write_text("hello", encoding="utf-8")
    try:
        status, body = client.call("GET", "/api/file?path=" + urllib.parse.quote(str(sample)))
        check("能预览 outputs 里的文件", status == 200 and body == "hello", f"status={status} {body!r}")
    finally:
        sample.unlink(missing_ok=True)

    outside = config.CONFIG_DIR / "models.yaml"
    status, res = client.call("GET", "/api/file?path=" + urllib.parse.quote(str(outside)))
    check("拒绝读取 outputs 之外的文件", status == 403, f"status={status} {res}")

    status, res = client.call("GET", "/api/file?path=" + urllib.parse.quote(str(outputs / "不存在.txt")))
    check("预览不存在的文件返回 404", status == 404, f"status={status} {res}")

    status, res = client.call("GET", "/api/file?path=" + urllib.parse.quote(str(config.SKILL_DIR / ".." / ".." / "Windows" / "win.ini")))
    check("拒绝路径穿越", status in (403, 404), f"status={status} {res}")

    # ---------------------------------------------------------- 自定义接口地址
    section("自定义接口地址（高级场景）")

    status, res = client.call(
        "POST",
        "/api/vendor",
        {"id": "", "catalog_key": "generic_http", "label": "自建接口", "api_key_env": "MY_API_KEY",
         "api_key": "", "endpoints": {"image": "https://example.com/v1/generate"}},
    )
    check("添加自建接口厂商成功", status == 200 and res.get("ok"), str(res))
    custom_vendor = res.get("id")
    status, payload = client.call("GET", "/api/bootstrap")
    custom_view = next((v for v in payload["vendors"] if v["id"] == custom_vendor), {})
    check(
        "自定义地址存了下来（与默认值不同的才写）",
        custom_view.get("endpoints", {}).get("image") == "https://example.com/v1/generate",
        str(custom_view),
    )

    status, res = client.call(
        "POST",
        "/api/model",
        {"id": "", "vendor": custom_vendor, "model": "my-own-model", "kind": "image",
         "priority": 1, "weight": 1, "supports": ["text2img"]},
    )
    check("给自建接口挂模型成功", status == 200 and res.get("ok"), str(res))
    custom_model = res.get("id")

    # ---------------------------------------------------------- 生成规格（params）
    section("生成规格（params）保存与回填")

    status, res = client.call(
        "POST",
        "/api/model",
        {"id": custom_model, "vendor": custom_vendor, "model": "my-own-model", "kind": "image",
         "priority": 1, "weight": 1, "supports": ["text2img"],
         "params": {"size": "2048x2048"}},
    )
    check("带 params 保存模型成功", status == 200 and res.get("ok"), str(res))
    status, payload = client.call("GET", "/api/bootstrap")
    row = next((m for m in payload["models"].get("image", []) if m["id"] == custom_model), {})
    check("bootstrap 视图回传了 params（页面据此回填）", row.get("params", {}).get("size") == "2048x2048", str(row.get("params")))

    # 页面清空尺寸时提交的是空串 → 应把键删掉（回到平台默认）
    status, res = client.call(
        "POST",
        "/api/model",
        {"id": custom_model, "vendor": custom_vendor, "model": "my-own-model", "kind": "image",
         "priority": 1, "weight": 1, "supports": ["text2img"],
         "params": {"size": ""}},
    )
    check("清空尺寸后保存成功", status == 200 and res.get("ok"), str(res))
    status, payload = client.call("GET", "/api/bootstrap")
    row = next((m for m in payload["models"].get("image", []) if m["id"] == custom_model), {})
    check("清空后 params 里不再有 size", "size" not in (row.get("params") or {}), str(row.get("params")))

    # 视频规格：resolution + duration（duration 是整数）
    status, res = client.call(
        "POST",
        "/api/model",
        {"id": "", "vendor": custom_vendor, "model": "my-video-model", "kind": "video",
         "priority": 1, "weight": 1, "supports": ["text2video"],
         "params": {"resolution": "1080p", "duration": 5}},
    )
    check("视频模型带规格保存成功", status == 200 and res.get("ok"), str(res))
    video_spec_model = res.get("id")
    status, payload = client.call("GET", "/api/bootstrap")
    row = next((m for m in payload["models"].get("video", []) if m["id"] == video_spec_model), {})
    check(
        "视频规格原样回传",
        row.get("params", {}).get("resolution") == "1080p" and row.get("params", {}).get("duration") == 5,
        str(row.get("params")),
    )
    try:
        vspec = config.load_config().find_model(video_spec_model)
        check("运行时 ModelSpec.params 拿到规格", vspec.params.get("resolution") == "1080p" and vspec.params.get("duration") == 5, str(vspec.params))
    except config.ConfigError as exc:
        check("带 params 的配置可加载", False, str(exc))
    status, res = client.call("DELETE", f"/api/model?id={video_spec_model}")
    check("清理视频测试模型", status == 200 and res.get("ok"), str(res))

    # 页面结构：④ 生成规格的控件都在，且按 kind 切换显示
    check("页面有图像尺寸选择器", 'id="mf-size-preset"' in str(page), "config.html 缺 mf-size-preset")
    check("页面有视频清晰度/时长选择器", 'id="mf-resolution"' in str(page) and 'id="mf-duration"' in str(page), "config.html 缺视频规格控件")
    check("setKind 切换规格块显示", 'mf-spec-image' in str(page) and 'mf-spec-video' in str(page), "setKind 未接线规格块")
    check("payload 收集了 params", 'params: params' in str(page), "collectModelPayload 缺 params")

    # ---------------------------------------------------------- 配置可被真正加载
    section("写出来的配置真的能用")

    cfg = None
    try:
        cfg = config.load_config()
        check("写完后 load_config() 不报错", True)
    except config.ConfigError as exc:
        check("写完后 load_config() 不报错", False, str(exc))

    if cfg is not None:
        check("load_config() 读到了厂商", ark_id in cfg.vendors and second_id in cfg.vendors, str(list(cfg.vendors)))
        check("load_config() 读到了模型池", image_id in [m.id for m in cfg.pool("image").models], str([m.id for m in cfg.pool("image").models]))
        spec = cfg.find_model(image_id)
        check("模型从厂商继承了 provider", spec.provider == "volcengine", spec.provider)
        check("模型从厂商继承了 api_key_env", spec.api_key_env == "ARK_API_KEY", spec.api_key_env)
        check(
            "官方默认地址不写进文件（升级 skill 后能自动用上新地址）",
            spec.endpoint == "",
            spec.endpoint,
        )
        check(
            "自定义地址能被模型继承",
            cfg.find_model(custom_model).endpoint == "https://example.com/v1/generate",
            cfg.find_model(custom_model).endpoint,
        )
        check(
            "第二个厂商继承了避让后的变量名",
            cfg.vendors[second_id].api_key_env == "ARK_API_KEY_2",
            cfg.vendors[second_id].api_key_env,
        )
        key, source = cfg.resolve_api_key(spec)
        check("运行时真能取到密钥", key == "fake-ark-key-for-test", f"{key!r} / {source}")
        check("密钥来源可追溯", "secrets" in source, source)
        check("配置解析没有告警", not cfg.warnings, str(cfg.warnings))

    # ---------------------------------------------------------- 删除
    section("删除与级联")

    status, res = client.call("DELETE", f"/api/vendor?id={custom_vendor}")
    check("删除自建接口厂商（连同它的模型）", status == 200 and res.get("ok"), str(res))

    status, res = client.call("DELETE", f"/api/model?id={image_id}")
    check("删除模型成功", status == 200 and res.get("ok"), str(res))
    status, payload = client.call("GET", "/api/bootstrap")
    check("删除后列表里没有它", image_id not in [m["id"] for m in payload["models"].get("image", [])], str(payload["models"].get("image")))

    status, res = client.call("DELETE", f"/api/model?id={image_id}")
    check("删除不存在的模型返回 400", status == 400, f"status={status} {res}")

    status, res = client.call("DELETE", f"/api/vendor?id={ark_id}")
    check("删除厂商成功", status == 200 and res.get("ok"), str(res))
    check("厂商删除时连带报告了受影响的模型", video_id in (res.get("removed_models") or []), str(res))

    status, payload = client.call("GET", "/api/bootstrap")
    left = [v["id"] for v in payload["vendors"]]
    check("被删厂商已消失", ark_id not in left, str(left))
    check(
        "引用它的模型已一并清理",
        not any(m["vendor"] == ark_id for m in payload["models"].get("video", [])),
        str(payload["models"].get("video")),
    )
    check(
        "手写 models.yaml 里的视频模型没被连累",
        any(m["vendor"] == "" for m in payload["models"].get("video", [])),
        "实测里手写池的模型不该被级联删除波及",
    )

    status, res = client.call("DELETE", f"/api/vendor?id={ark_id}")
    check("删除不存在的厂商返回 400", status == 400, f"status={status} {res}")

    # 删空之后，文件里不该留下空壳类目
    if overlay.exists():
        text = overlay.read_text(encoding="utf-8")
        check("空类目不会残留成空壳", "models:" not in text, text)

    try:
        config.load_config()
        check("删空后配置依然可加载", True)
    except config.ConfigError as exc:
        # models.yaml 里本来就还有模型，所以这里必然是能加载的
        check("删空后配置依然可加载", False, str(exc))

    # ---------------------------------------------------------- 配置坏掉时也要能打开
    section("容错：手写配置写坏时还能不能进页面")

    broken = store.overlay_path()
    original = broken.read_text(encoding="utf-8") if broken.exists() else None
    try:
        broken.write_text("版本: 1\n  - 缩进错了: [没闭合\n", encoding="utf-8")
        status, payload = client.call("GET", "/api/bootstrap")
        check(
            "配置语法坏掉时页面仍能打开并给出提示",
            status == 200
            and any("解析" in w or "顶层" in w or "映射" in w for w in payload.get("warnings", [])),
            str(payload.get("warnings")),
        )
        status, res = client.call(
            "POST", "/api/vendor/test", {"catalog_key": "native", "kind": "image"}
        )
        check("配置坏掉时仍能测连通性", status == 200 and "checks" in res, f"status={status} {res}")
    finally:
        if original is None:
            broken.unlink(missing_ok=True)
        else:
            broken.write_text(original, encoding="utf-8")

    # ---------------------------------------------------------- 自定义接口真的能跑通
    # 用户报过：页面上选了「自定义接口（高级）」、填了接口地址，点「试生成」却报
    # "缺少 options.submit.url"。原因是适配器只认 options.submit.url，
    # 而页面填的地址落在 endpoint 上 —— 等于界面上填了也白填。
    # 这一组用本地模拟接口把"只填一个地址"的路径真跑一遍。
    section("自定义接口（generic_http）端到端")

    check_generic_http(client, check)

    # 拉取可用模型的页面 UI 已整体移除（用户改为手动填模型名）。
    # 后端 /api/vendor/models 与 /api/models/bulk 路由保留（无页面调用方），
    # 相关 fetch_models/bulk 的服务端行为校验也一并退役。

    # ---------------------------------------------------------- 手写层的模型
    # 这一组防的是"页面上看得见、点删除却报找不到"。页面列的是手写层 + 叠加层
    # 合并后的结果，早期的实现只盯着叠加层，于是删除/启停/编辑对手写层的模型全部失效。
    section("手写层里的模型（models.yaml）")

    base_models = config.layer_models(config.base_layer())
    base_image = [str(item.get("id")) for kind, item in base_models if kind == "image"]
    check("自带配置里有手写层的图片模型可用于测试", bool(base_image), "models.yaml 里没有 image 模型")
    if base_image:
        victim = base_image[0]
        survivor = base_image[1] if len(base_image) > 1 else None

        # --- 删除：手写层的模型不能真删，只能"隐藏" ---
        status, res = client.call("DELETE", f"/api/model?id={victim}")
        check("删除手写层模型成功（不报 400）", status == 200 and res.get("ok"), f"{status} {res}")
        check("返回里标明这是隐藏而不是删除", res.get("hidden") is True, str(res))
        check(
            "提示里说清了为什么（要去 models.yaml 才能真删）",
            "models.yaml" in str(res.get("message", "")),
            str(res.get("message")),
        )
        status, boot = client.call("GET", "/api/bootstrap")
        shown = [m["id"] for m in boot["models"].get("image", [])]
        check("隐藏后不再出现在列表里", victim not in shown, str(shown))
        overlay_text = store.overlay_path().read_text(encoding="utf-8")
        check("叠加层里写下了墓碑", f"id: {victim}" in overlay_text and "_deleted: true" in overlay_text, overlay_text[-200:])
        try:
            cfg_after = config.load_config()
            runtime_ids = [m.id for m in cfg_after.pool("image").models]
            check("运行时（load_config）也看不到它了", victim not in runtime_ids, str(runtime_ids))
        except config.ConfigError as exc:
            check("隐藏后配置仍可加载", False, str(exc))

        # --- 启停：手写层的模型也要能停 ---
        if survivor:
            status, res = client.call("POST", "/api/model/toggle", {"id": survivor, "enabled": False})
            check("手写层模型可以停用", status == 200 and res.get("enabled") is False, f"{status} {res}")
            status, boot = client.call("GET", "/api/bootstrap")
            row = next((m for m in boot["models"].get("image", []) if m["id"] == survivor), {})
            check("停用状态已持久化", row.get("enabled") is False, str(row))
            check("列表里标出了它来自手写层", row.get("from_base") is True, str(row.get("from_base")))
            try:
                spec = config.load_config().find_model(survivor)
                check("运行时也认这条停用", spec.enabled is False, str(spec.enabled))
            except config.ConfigError as exc:
                check("停用后配置仍可加载", False, str(exc))

        # --- 编辑：不改厂商，只调优先级/权重 ---
        if survivor:
            status, boot = client.call("GET", "/api/bootstrap")
            row_before = next(
                (m for m in boot["models"].get("image", []) if m["id"] == survivor), {}
            )
            status, res = client.call(
                "POST",
                "/api/model",
                {"id": survivor, "vendor": "", "model": str(row_before.get("model") or survivor),
                 "kind": "image", "priority": 4, "weight": 6, "supports": ["text2img"]},
            )
            check("手写层模型可以编辑（厂商留空）", status == 200, f"{status} {res}")
            status, boot = client.call("GET", "/api/bootstrap")
            shown = [m["id"] for m in boot["models"].get("image", [])]
            check("编辑后不会重复显示", shown.count(survivor) == 1, str(shown))
            row = next((m for m in boot["models"].get("image", []) if m["id"] == survivor), {})
            check("优先级与权重已更新", row.get("priority") == 4 and row.get("weight") == 6, str(row))
            check("保留了原来的 provider", bool(row.get("provider")), str(row))
            try:
                spec = config.load_config().find_model(survivor)
                check("运行时认这条覆盖", spec.priority == 4 and spec.weight == 6, f"{spec.priority} {spec.weight}")
                check("覆盖没有弄丢 api_key_env", bool(spec.api_key_env), spec.api_key_env)
            except config.ConfigError as exc:
                check("编辑后配置仍可加载", False, str(exc))

        # --- 恢复：重新添加同 id 的模型要把墓碑清掉 ---
        status, res = client.call(
            "POST",
            "/api/model",
            {"id": victim, "vendor": "", "model": "restored-model",
             "kind": "image", "priority": 2, "weight": 3, "supports": ["text2img"]},
        )
        check("重新添加同 id 可以恢复", status == 200, f"{status} {res}")
        status, boot = client.call("GET", "/api/bootstrap")
        shown = [m["id"] for m in boot["models"].get("image", [])]
        check("恢复后回到列表且不重复", shown.count(victim) == 1, str(shown))
        overlay_text = store.overlay_path().read_text(encoding="utf-8")
        check("墓碑已被清掉", "_deleted" not in overlay_text, overlay_text[-200:])

    # --- 新建模型仍然必须选厂商（这条不能因为上面的放宽而漏掉）---
    status, res = client.call(
        "POST", "/api/model", {"id": "", "vendor": "", "model": "brand-new", "kind": "image"}
    )
    check("新建模型时厂商留空会被拒", status == 400 and "厂商" in str(res.get("error", "")), f"{status} {res}")

    # --- 单元层面：墓碑与去重必须作用于两处加载路径 ---
    entries = [
        {"id": "a", "model": "base-a"},
        {"id": "b", "model": "base-b"},
        {"id": "a", "model": "override-a"},
        {"id": "b", "_deleted": True},
    ]
    visible = config.visible_entries(entries)
    check("visible_entries 同 id 取后者", [e["id"] for e in visible] == ["a"] and visible[0]["model"] == "override-a", str(visible))

    # --- 目录猜测：硅基流动走的是 openai 协议，但校验端点不同 ---
    check(
        "按接口地址能认出硅基流动而不是 OpenAI",
        catalog.guess_key("openai", "https://api.siliconflow.cn/v1/images/generations") == "siliconflow",
        catalog.guess_key("openai", "https://api.siliconflow.cn/v1/images/generations"),
    )
    check(
        "认不出域名时退回按 provider 匹配",
        catalog.guess_key("volcengine", "https://example.com/x") == "volcengine",
        catalog.guess_key("volcengine", "https://example.com/x"),
    )

    # ---------------------------------------------------------- 端口顺延
    # 这一组防的是"两个实例绑到同一个端口"。Windows 上 SO_REUSEADDR 的含义是
    # "允许抢占"，所以 bind 探测永远成功、顺延逻辑形同虚设 —— 必须用连接探测。
    section("端口占用与顺延")

    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(8)
    busy_port = holder.getsockname()[1]
    try:
        check("能检测出端口被占用", webserver._port_in_use("127.0.0.1", busy_port), "返回了 False")
        free_port = webserver._pick_port("127.0.0.1", busy_port, tries=4)
        check("被占用时会顺延到别的端口", free_port != busy_port, f"仍然返回了 {free_port}")
        check("顺延出去的是空闲端口", not webserver._port_in_use("127.0.0.1", free_port), str(free_port))

        # 第二个实例不能抢同一个端口
        first = webserver.ConfigServer(host="127.0.0.1", port=0, open_browser=False)
        first.start_background()
        try:
            check("端口传 0 时能读回真实端口", first.port > 0, str(first.port))
            second = webserver.ConfigServer(host="127.0.0.1", port=first.port, open_browser=False)
            second.start_background()
            try:
                check(
                    "第二个实例会自动换端口而不是抢占",
                    second.port != first.port,
                    f"两个实例都在 {first.port}",
                )
                # 两个实例都要真的能服务，才算没抢
                ok_first, _ = Client("127.0.0.1", first.port, first.token).call("GET", "/api/bootstrap")
                ok_second, _ = Client("127.0.0.1", second.port, second.token).call("GET", "/api/bootstrap")
                check(
                    "两个实例各自独立可用",
                    ok_first == 200 and ok_second == 200,
                    f"first={ok_first} second={ok_second}",
                )
                check(
                    "两个实例的 token 不同",
                    first.token != second.token,
                    "token 撞了",
                )
            finally:
                second.stop()
        except webserver.ApiError as exc:
            check("端口全占时给出可读报错", "端口" in str(exc), str(exc))
        finally:
            first.stop()
    finally:
        holder.close()

    # ---------------------------------------------------------- 停止
    section("停止与端口释放")

    temp = webserver.ConfigServer(host="127.0.0.1", port=0, open_browser=False)
    temp.start()
    temp_port = temp.port
    temp.stop()
    check("停止后端口被释放", not webserver._port_in_use("127.0.0.1", temp_port), str(temp_port))
    temp.stop()  # 幂等
    check("重复 stop() 不报错", True, "")

    # ---------------------------------------------------------- 平台目录
    section("平台目录完整性")

    directory = catalog.as_dict()
    check(
        "每个平台的类目声明合法",
        all(e["kinds"] and set(e["kinds"]) <= {"image", "video"} for e in directory),
        "有平台类目写错了",
    )
    check(
        "提供密钥字段的平台都给了获取提示或占位符",
        all((f["hint"] or f["placeholder"]) for e in directory for f in e["key_fields"]),
        "有密钥字段缺少提示",
    )
    check(
        "免密钥平台没有密钥字段",
        all(not e["key_fields"] for e in directory if e["keyless"]),
        "免密钥平台却要求填密钥",
    )
    check(
        "无法免生成校验的平台被如实标注",
        all(e["can_verify_key"] for e in directory if e["key"] in ("volcengine", "dashscope", "openai", "replicate"))
        and not next(e for e in directory if e["key"] == "fal")["can_verify_key"],
        "auth_check 标注与实际不符",
    )
    check(
        "所有 key_fields 的 env 都是合法的环境变量名",
        all(
            f["env"].replace("_", "").isalnum() and "-" not in f["env"]
            for e in directory
            for f in e["key_fields"]
        ),
        "有环境变量名带连字符或特殊字符",
    )

    # ---------------------------------------------------------- 只读模式
    # 放在最后：它会改环境变量，会影响后续所有配置读取
    section("只读模式（MEDIA_ROUTER_CONFIG 指定了配置时）")

    os.environ["MEDIA_ROUTER_CONFIG"] = str(config.CONFIG_DIR / "models.yaml")
    try:
        reason = store.readonly_reason()
        check("检测到只读模式并给出原因", bool(reason), repr(reason))
        status, res = client.call(
            "POST", "/api/vendor", {"id": "", "catalog_key": "native", "label": ""}
        )
        check("只读模式下写接口返回 409", status == 409, f"status={status} {res}")
        status, res = client.call("DELETE", "/api/vendor?id=v-native")
        check("只读模式下删除接口返回 409", status == 409, f"status={status} {res}")
        status, payload = client.call("GET", "/api/bootstrap")
        check("只读模式下页面仍可读取", status == 200 and "catalog" in payload, f"status={status}")
    finally:
        os.environ.pop("MEDIA_ROUTER_CONFIG", None)


if __name__ == "__main__":
    sys.exit(main())
