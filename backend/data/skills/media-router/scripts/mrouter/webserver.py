"""本机 Web 配置服务。

给不熟悉命令行的用户准备的一条命令：`media_router.py web` 之后，
浏览器里点点点就能把厂商、密钥、模型、权重配完。

安全上有意做得很克制 —— 这是个"能写配置文件、能看到你的密钥"的服务，
一旦被别的东西访问到就是灾难，所以：

  * 只监听回环地址（127.0.0.1），默认拒绝绑定其他网卡
  * 校验 Host 头，防 DNS rebinding（攻击者把自己的域名解析到 127.0.0.1）
  * 校验 Origin/Referer，防跨站请求（别的网页偷偷 POST 过来）
  * 每次启动生成一次性随机 token，页面请求必须带上，放在 URL 里的只用于预览图
  * /api/file 只允许读 <skill>/outputs 里的文件，杜绝任意文件读取
  * 写入只落 config/models.web.yaml 和 config/secrets.web.yaml，
    绝不碰用户手写的 models.yaml / secrets.yaml

生命周期跟着终端走：Ctrl-C 就没了，不会留下后台进程。
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import secrets
import socket
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

from . import catalog, config, probe, store

ASSETS_DIR = config.SKILL_DIR / "assets"
PAGE_FILE = ASSETS_DIR / "config.html"
TOKEN_PLACEHOLDER = "__MR_TOKEN__"

MAX_BODY = 2 * 1024 * 1024  # 2 MiB，配置文件不可能这么大
DEFAULT_PORT = 8760
PORT_TRIES = 12

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
}


class ApiError(RuntimeError):
    """带 HTTP 状态码的业务错误。"""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------- 工具


def _within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _is_loopback(host: str) -> bool:
    if host in ("localhost", "::1"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _port_in_use(host: str, port: int) -> bool:
    """端口是否已被占用。

    **不能用 bind + SO_REUSEADDR 来探测。** SO_REUSEADDR 在 POSIX 上的含义是
    "重启时允许绑到还在 TIME_WAIT 的端口"，但在 Windows 上它的含义是
    "允许抢占" —— 即使端口已被别人监听，bind 也会成功。于是探测永远返回"空闲"，
    "端口被占就顺延"的逻辑形同虚设，最后真的会有两个服务绑在同一个端口上，
    请求发到哪个全凭运气。

    最可靠的跨平台办法是**尝试连接**：连得上就说明有人。
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.6)
    try:
        probe.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _pick_port(host: str, port: int, tries: int = PORT_TRIES) -> int:
    """端口被占了就往后顺延，别让用户看到一个看不懂的 OSError。"""
    for offset in range(tries):
        candidate = port + offset
        if _port_in_use(host, candidate):
            continue
        # 连接探测说空闲，再用一趟**不带** SO_REUSEADDR 的真实 bind 兜底
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind((host, candidate))
            return candidate
        except OSError:
            continue
        finally:
            probe.close()
    raise ApiError(
        f"{host} 上 {port}~{port + tries - 1} 这些端口都被占用了，"
        f"用 --port 换一个别的端口试试"
    )


# ---------------------------------------------------------------- 业务处理


class ConfigApi:
    """所有 /api/* 的业务逻辑。与 HTTP 细节解耦，方便单独测。"""

    def __init__(self, host: str) -> None:
        self.host = host

    # ---------- 读 ----------

    def bootstrap(self, _query: dict[str, list[str]], _body: dict[str, Any]) -> dict[str, Any]:
        return store.bootstrap(host=self.host)

    # ---------- 厂商 ----------

    def vendor_save(
        self, _query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        if store.readonly_reason():
            raise ApiError(store.readonly_reason(), 409)

        def mutate(overlay: store.Overlay) -> dict[str, Any]:
            return overlay.upsert_vendor(body)

        result = store.save_with(mutate)
        return {"ok": True, **result}

    def vendor_delete(
        self, query: dict[str, list[str]], _body: dict[str, Any]
    ) -> dict[str, Any]:
        if store.readonly_reason():
            raise ApiError(store.readonly_reason(), 409)
        vendor_id = _one(query, "id")
        if not vendor_id:
            raise ApiError("缺少参数 id")
        result = store.save_with(lambda overlay: overlay.delete_vendor(vendor_id))
        return {"ok": True, **result}

    def vendor_test(
        self, _query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        cfg = self._config()
        kind = str(body.get("kind") or "image")
        target = self._target_from_payload(body)
        return probe.probe_target(
            cfg, target, kind=kind, model_name=str(body.get("model_name") or "")
        ).to_dict()

    def vendor_models(
        self, _query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        cfg = self._config()
        vendor_id = str(body.get("vendor_id") or "").strip()
        if not vendor_id:
            raise ApiError("缺少参数 vendor_id")
        raw = store.read_overlay()
        item = next(
            (
                v
                for v in (raw.get("vendors") or [])
                if isinstance(v, dict) and str(v.get("id")) == vendor_id
            ),
            None,
        )
        if item is None:
            raise ApiError(f"找不到厂商：{vendor_id}")
        target = self._target_from_payload(
            {
                "catalog_key": item.get("catalog") or item.get("provider"),
                "api_key_env": item.get("api_key_env"),
                "endpoints": item.get("endpoints") or {},
                "options": item.get("options") or {},
                "vendor_id": vendor_id,
                "api_key": body.get("api_key") or "",
            }
        )
        return probe.fetch_models(
            cfg, target, kind=str(body.get("kind") or "image")
        )

    def models_bulk_add(
        self, _query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        """把勾选的多个模型一次性挂到某个厂商下。

        逐个调 /api/model 也能做到，但那是 N 次往返、N 次落盘；中途失败还会
        留下半截结果。这里在一个写锁里改完再整体落盘，要么都成、要么都不动。
        """
        if store.readonly_reason():
            raise ApiError(store.readonly_reason(), 409)
        vendor_id = str(body.get("vendor") or "").strip()
        kind = str(body.get("kind") or "").strip()
        if kind not in ("image", "video"):
            raise ApiError("类目只能是 image（图像）或 video（视频）")
        names_raw = body.get("models")
        if isinstance(names_raw, str):
            names_raw = [names_raw]
        names = [str(n).strip() for n in (names_raw or []) if str(n).strip()]
        if not names:
            raise ApiError("没有选中任何模型")
        if not vendor_id:
            raise ApiError("批量添加需要先选定一个厂商")
        try:
            weight = float(body.get("weight", 50) or 0)
        except (TypeError, ValueError):
            weight = 50.0
        try:
            priority = max(1, int(body.get("priority", 1) or 1))
        except (TypeError, ValueError):
            priority = 1
        extra = bool(body.get("extra"))
        supports = ["text2img" if kind == "image" else "text2video"]
        if extra:
            supports.append("img2img" if kind == "image" else "img2video")

        def mutate(overlay: store.Overlay) -> dict[str, Any]:
            if not overlay.find_vendor(vendor_id):
                raise config.ConfigError(f"找不到厂商：{vendor_id}")
            created: list[str] = []
            skipped: list[dict[str, str]] = []
            existing = overlay.model_ids()
            taken = set(existing)
            # 同一批次里可能勾了重名的模型（拉取列表去重前、或用户手滑），
            # existing 是循环前的快照，不会随本批新增而更新 —— 必须用 added
            # 记下"这一批已经建过的 slug"，否则第二个重名会被 _unique_id
            # 变成 gpt-image-1-2，凭空多一条。
            added: set[str] = set()
            for name in names:
                base = store._slug(name) or "model"  # noqa: SLF001 - 同类协作
                if base in existing:
                    # 已经配过的不重复添加，如实告诉用户跳过了哪些
                    located = overlay.locate_model(base)
                    where = "图像" if located and located[0] == "image" else "视频"
                    skipped.append({"id": base, "model": name, "reason": f"已存在于{where}池"})
                    continue
                if base in added:
                    skipped.append({"id": base, "model": name, "reason": "本次已选择，去重"})
                    continue
                model_id = store._unique_id(base, taken)  # noqa: SLF001
                taken.add(model_id)
                added.add(base)
                overlay.upsert_model(
                    {
                        "id": model_id,
                        "vendor": vendor_id,
                        "model": name,
                        "kind": kind,
                        "priority": priority,
                        "weight": weight,
                        "supports": supports,
                    }
                )
                created.append(model_id)
            return {"created": created, "skipped": skipped}

        result = store.save_with(mutate)
        created = result["created"]
        skipped = result["skipped"]
        where = "图像" if kind == "image" else "视频"
        message = f"已添加 {len(created)} 个{where}模型" if created else "没有添加任何模型"
        if skipped:
            message += f"；跳过 {len(skipped)} 个（已存在）"
        return {"ok": True, **result, "kind": kind, "message": message}

    # ---------- 模型 ----------

    def model_save(
        self, _query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        if store.readonly_reason():
            raise ApiError(store.readonly_reason(), 409)
        result = store.save_with(lambda overlay: overlay.upsert_model(body))
        return {"ok": True, **result}

    def model_delete(
        self, query: dict[str, list[str]], _body: dict[str, Any]
    ) -> dict[str, Any]:
        if store.readonly_reason():
            raise ApiError(store.readonly_reason(), 409)
        model_id = _one(query, "id")
        if not model_id:
            raise ApiError("缺少参数 id")
        result = store.save_with(lambda overlay: overlay.delete_model(model_id))
        return {"ok": True, **result}

    def model_toggle(
        self, _query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        if store.readonly_reason():
            raise ApiError(store.readonly_reason(), 409)
        model_id = str(body.get("id") or "").strip()
        if not model_id:
            raise ApiError("缺少参数 id")
        enabled = bool(body.get("enabled", True))
        result = store.save_with(lambda overlay: overlay.set_enabled(model_id, enabled))
        return {"ok": True, **result}

    def model_test(
        self, _query: dict[str, list[str]], body: dict[str, Any]
    ) -> dict[str, Any]:
        cfg = self._config()
        entry, kind, model_name = self._resolve_model_for_test(body)

        if not body.get("deep"):
            target = self._target_from_payload(
                {
                    "catalog_key": entry.get("catalog"),
                    "api_key_env": entry.get("api_key_env"),
                    "endpoints": entry.get("endpoints") or {},
                    "provider": entry.get("provider"),
                    "options": entry.get("options") or {},
                    "vendor_id": entry.get("vendor_id") or "",
                    "api_key": body.get("api_key") or "",
                }
            )
            return probe.probe_target(
                cfg, target, kind=kind, model_name=model_name
            ).to_dict()

        # 深度测试：真跑一次生成，会消耗额度。页面已弹过确认框。
        vendors = config.build_vendors(store.read_overlay().get("vendors"))
        spec = config.build_spec_from_entry(
            kind,
            {
                # 把模型自己的 provider / api_key_env / endpoint / params 一并带上 ——
                # 手写层里的模型没有厂商条目可继承，光给 vendor 是构造不出 spec 的
                "id": entry.get("model_id") or "probe-temp",
                "vendor": entry.get("vendor_id") or "",
                "provider": entry.get("provider") or "",
                "api_key_env": entry.get("api_key_env") or "",
                "endpoint": entry.get("endpoint") or "",
                "options": entry.get("options") or {},
                "params": entry.get("params") or {},
                "model": model_name,
                "supports": body.get("supports") or [],
                "priority": body.get("priority", 1),
                "weight": body.get("weight", 1),
            },
            vendors,
        )
        result = probe.deep_probe(
            cfg,
            spec,
            prompt=str(body.get("prompt") or ""),
            timeout=_as_float(cfg.defaults.get("timeout_seconds"), 180.0),
            poll_interval=_as_float(cfg.defaults.get("poll_interval_seconds"), 5.0),
            max_poll=_as_float(cfg.defaults.get("max_poll_seconds"), 300.0),
        )
        return result

    # ---------- 内部 ----------

    @staticmethod
    def _config() -> config.RouterConfig:
        """能加载就用真配置，加载不了就用降级配置 —— 密码照样能测。"""
        try:
            return config.load_config()
        except Exception:  # noqa: BLE001 - 配置坏了也要能继续测连通性
            return config.stub_config()

    @staticmethod
    def _target_from_payload(body: dict[str, Any]) -> probe.ProbeTarget:
        catalog_key = str(body.get("catalog_key") or body.get("provider") or "").strip()
        entry = catalog.get(catalog_key)
        endpoints = {
            str(k): str(v)
            for k, v in (body.get("endpoints") or {}).items()
            if v and str(v).strip()
        }
        if entry and not endpoints:
            endpoints = dict(entry.endpoints or {})
        return probe.ProbeTarget(
            catalog_key=catalog_key,
            provider=str(body.get("provider") or (entry.provider if entry else "")),
            api_key=str(body.get("api_key") or ""),
            api_key_env=str(body.get("api_key_env") or ""),
            endpoints=endpoints,
            options=dict(body.get("options") or {}),
            vendor_id=str(body.get("vendor_id") or body.get("id") or ""),
        )

    def _resolve_model_for_test(
        self, body: dict[str, Any]
    ) -> tuple[dict[str, Any], str, str]:
        """页面上的"测试"有两种入口：刚填的表单、或列表里已保存的模型。

        两条来源都要认：叠加层（页面建的）和手写层（models.yaml 里的）。
        只查叠加层的话，用户在手写配置里的模型一点"测试"就会报"没有可测试的模型"。
        """
        overlay = store.load_overlay()

        kind = str(body.get("kind") or "")
        model_name = str(body.get("model") or "").strip()
        vendor_id = str(body.get("vendor") or "").strip()
        model_id = str(body.get("id") or "").strip()

        if model_name and vendor_id:
            entry = dict(body)  # 表单里还没保存的条目
        else:
            found = overlay.locate_model(model_id) if model_id else None
            if found is None:
                raise ApiError(
                    "没有可测试的模型：请先填写模型名称，或在列表里点某个模型的「测试」"
                )
            kind, entry, _layer = found
            vendor_id = str(entry.get("vendor") or "")
            model_name = str(entry.get("model") or model_id or "")

        if kind not in ("image", "video"):
            raise ApiError("模型类目缺失，无法测试")

        vendor = overlay.find_vendor(vendor_id) if vendor_id else None
        if vendor_id and vendor is None:
            raise ApiError(f"找不到厂商 {vendor_id}，请先在「厂商配置」里保存它")

        if vendor is not None:
            catalog_key = str(vendor.get("catalog") or vendor.get("provider") or "")
            api_key_env = str(vendor.get("api_key_env") or "")
            endpoints = dict(vendor.get("endpoints") or {})
            provider = str(vendor.get("provider") or "")
            # 厂商级 options 打底（比如自定义接口的字段映射），模型级覆盖
            options = dict(vendor.get("options") or {})
            options.update(entry.get("options") or {})
        else:
            # 手写层的模型：只写了 provider / api_key_env / endpoint，没有厂商条目
            provider = str(entry.get("provider") or "")
            api_key_env = str(entry.get("api_key_env") or "")
            catalog_key = catalog.guess_key(provider, str(entry.get("endpoint") or ""))
            endpoints = {}
            options = dict(entry.get("options") or {})

        endpoint = str(entry.get("endpoint") or "")
        if endpoint:
            endpoints[kind] = endpoint

        return (
            {
                "catalog": catalog_key,
                "api_key_env": api_key_env,
                "endpoints": endpoints,
                "endpoint": endpoint,
                "params": dict(entry.get("params") or {}),
                "options": options,
                "vendor_id": vendor_id,
                "model_id": str(entry.get("id") or model_id),
                "provider": provider,
            },
            kind,
            model_name,
        )


def _one(query: dict[str, list[str]], name: str) -> str:
    values = query.get(name) or []
    return unquote(str(values[0])).strip() if values else ""


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- HTTP


ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/api/bootstrap"): "bootstrap",
    ("POST", "/api/vendor"): "vendor_save",
    ("DELETE", "/api/vendor"): "vendor_delete",
    ("POST", "/api/vendor/test"): "vendor_test",
    ("POST", "/api/vendor/models"): "vendor_models",
    ("POST", "/api/model"): "model_save",
    ("POST", "/api/models/bulk"): "models_bulk_add",
    ("DELETE", "/api/model"): "model_delete",
    ("POST", "/api/model/toggle"): "model_toggle",
    ("POST", "/api/model/test"): "model_test",
}


class _ConfigHTTPServer(ThreadingHTTPServer):
    """配置页面的 HTTP 服务。

    **Windows 上必须关掉地址复用。** ThreadingHTTPServer 默认
    ``allow_reuse_address = 1``，那在 Windows 上的含义是 SO_REUSEADDR =
    "允许抢占" —— 两个实例能绑到同一个端口上，用户的请求发到哪一个全凭运气，
    关掉时还会留下一个僵尸端口（表现为"明明关了窗口，端口还在监听"）。
    POSIX 上则相反：留着它才能在重启时避开 TIME_WAIT。
    """

    allow_reuse_address = not sys.platform.startswith("win")
    daemon_threads = True


class ConfigServer:
    """把页面和 API 挂在回环地址上的小服务。"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = DEFAULT_PORT,
        open_browser: bool = True,
        log: Callable[[str], None] | None = None,
    ) -> None:
        if not _is_loopback(host):
            raise ApiError(
                f"出于安全考虑，配置页面只能监听本机回环地址，不接受 {host}。"
                f"确实需要请自行改代码，但请务必清楚风险。"
            )
        self.host = host
        self.port = port
        self.open_browser = open_browser
        self.token = secrets.token_urlsafe(24)
        self.log = log or (lambda _msg: None)
        self.api = ConfigApi(host=host)
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        #: 是否有线程真的在跑 serve_forever。stop() 必须知道这件事 ——
        #: 对没在服务的服务调 shutdown() 会永久阻塞（它在等一个永远不会设置的标志）。
        self._serving = threading.Event()
        self._page = ""
        self._page_lock = threading.Lock()

    # ---------- 页面 ----------

    @property
    def origin(self) -> str:
        return f"http://{self.host}:{self.port}"

    def url(self) -> str:
        # 带上 token，用户手工复制链接时也能直接用
        return f"{self.origin}/?t={self.token}"

    def _render_page(self) -> str:
        with self._page_lock:
            if not self._page:
                if not PAGE_FILE.exists():
                    raise ApiError(f"页面资源缺失：{PAGE_FILE}")
                text = PAGE_FILE.read_text(encoding="utf-8")
                self._page = text.replace(TOKEN_PLACEHOLDER, self.token)
            return self._page

    # ---------- 启动 ----------

    def start(self) -> str:
        """绑定端口并开始监听，**不阻塞**，也不在后台偷偷接受请求。

        刻意不做成"start 就自动在后台跑"：那样调用方很容易再顺手调一次
        serve_forever，于是两个线程在同一套接字上各自 accept，
        关闭时一个把 socket 关了，另一个的 select() 就报 WinError 10038。
        """
        if self.httpd is not None:
            raise ApiError("服务已经在运行了")
        self.port = _pick_port(self.host, self.port)
        self.httpd = _ConfigHTTPServer((self.host, self.port), _make_handler(self))
        # 端口传 0 表示"让系统随便给一个"，真正的端口要从绑好的 socket 里读回来，
        # 否则 self.url() 会给出 http://127.0.0.1:0/ 这种没法访问的地址
        self.port = int(self.httpd.server_address[1])
        url = self.url()
        if self.open_browser:
            threading.Timer(0.5, lambda: _open(url, self.log)).start()
        return url

    def start_background(self) -> str:
        """绑定端口并在后台线程里接受请求。给自检脚本这类调用方用。"""
        url = self.start()
        assert self.httpd is not None
        self._serving.set()  # 必须在起线程之前置位，否则 stop() 可能抢在前面
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, name="media-router-web", daemon=True
        )
        self.thread.start()
        return url

    def serve_forever(self) -> None:
        """在当前线程里接受请求，直到 stop() 或 Ctrl-C。"""
        if self.httpd is None:
            self.start()
        assert self.httpd is not None
        self._serving.set()
        self.httpd.serve_forever()

    def stop(self) -> None:
        """幂等：可以被多个线程反复调用。

        只有真的在跑 serve_forever 时才调 shutdown() —— 否则它会一直等下去。
        """
        httpd, self.httpd = self.httpd, None
        if httpd is None:
            return
        if self._serving.is_set():
            httpd.shutdown()
            self._serving.clear()
        httpd.server_close()
        self.thread = None


def _open(url: str, log: Callable[[str], None]) -> None:
    try:
        opened = webbrowser.open(url)
    except Exception as exc:  # noqa: BLE001 - 打不开浏览器不影响服务本身
        log(f"自动打开浏览器失败（{exc}），请手动复制上面的链接")
        return
    if not opened:
        log("没能自动唤出浏览器，请手动复制上面的链接")


def _make_handler(app: ConfigServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "media-router-config"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        # ---------- 日志 ----------

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            # 默认实现往 stderr 刷每个请求，对小白用户太吵；只留异常与写操作
            if str(args[1] if len(args) > 1 else "").startswith("5"):
                app.log(f"[!] {self.address_string()} {fmt % args}")

        def log_error(self, fmt: str, *args: Any) -> None:  # noqa: A003
            app.log(f"[!] {self.address_string()} {fmt % args}")

        # ---------- 响应 ----------

        def _send(
            self,
            status: int,
            body: bytes,
            content_type: str = "application/json; charset=utf-8",
            extra: dict[str, str] | None = None,
        ) -> None:
            self.send_response(status)
            if status != 204:
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if body and status != 204 and self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, payload: dict[str, Any]) -> None:
            self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

        def _error(self, status: int, message: str) -> None:
            self._json(status, {"error": message})

        # ---------- 请求 ----------

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").strip()
            if not host:
                return False
            bare = host.rsplit(":", 1)[0].strip("[]").lower()
            return bare in ("127.0.0.1", "localhost", "::1") and (
                host.endswith(f":{app.port}") or host == bare
            )

        def _origin_ok(self) -> bool:
            """有 Origin 就必须同源。没有 Origin 说明是 curl / 原生请求，放过。

            loopback 等价：127.0.0.1 / localhost / ::1 互相视为同源（与 _host_ok
            的放行范围保持一致——Host 允许 localhost，Origin 不允许会把
            用 localhost 打开页面的用户直接锁死在 403 上）。
            """
            for header in ("Origin", "Referer"):
                value = self.headers.get(header)
                if not value:
                    continue
                parsed = urlparse(value)
                netloc = parsed.netloc.lower()
                bare = netloc.rsplit(":", 1)[0].strip("[]")
                if bare not in ("127.0.0.1", "localhost", "::1"):
                    return False
                # 端口必须一致（None = 起始页 Referer 不带端口等价默认，仍需 host 匹配）
                origin_port = parsed.port
                if origin_port is not None and origin_port != app.port:
                    return False
            return True

        def _token_ok(self) -> bool:
            supplied = self.headers.get("X-MR-Token") or ""
            if not supplied:
                parsed = urlparse(self.path)
                supplied = (parse_qs(parsed.query).get("t") or [""])[0]
            return bool(supplied) and hmac.compare_digest(str(supplied), app.token)

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            if length > MAX_BODY:
                # 不读完就回包会让 keep-alive 连接上的下一个请求错位，干脆断开
                self.close_connection = True
                raise ApiError("请求体过大", 413)
            raw = self.rfile.read(length)
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ApiError(f"请求体不是合法 JSON：{exc}") from exc
            if not isinstance(parsed, dict):
                raise ApiError("请求体必须是 JSON 对象")
            return parsed

        def _serve_file(self, query: dict[str, list[str]]) -> None:
            requested = _one(query, "path")
            if not requested:
                raise ApiError("缺少参数 path")
            candidate = Path(requested)
            if not candidate.is_absolute():
                candidate = (config.SKILL_DIR / candidate).resolve()
            try:
                candidate = candidate.resolve()
            except OSError as exc:
                raise ApiError(f"路径无法解析：{exc}") from exc

            # 只允许读产物目录 —— 否则这就是个任意文件读取漏洞。
            # 产物默认落 cwd/outputs（沙箱友好），保留 skill/outputs 以兼容旧产物。
            allowed = [config.DEFAULT_OUTPUT_DIR, config.SKILL_DIR / "outputs"]
            if not any(_within(candidate, root.resolve()) for root in allowed):
                raise ApiError("只能预览 outputs 目录里的生成结果", 403)
            if not candidate.is_file():
                raise ApiError(f"文件不存在：{candidate}", 404)

            data = candidate.read_bytes()
            ctype = CONTENT_TYPES.get(candidate.suffix.lower(), "application/octet-stream")
            self._send(200, data, ctype)

        # ---------- 分发 ----------

        def _handle(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query, keep_blank_values=True)

            # 先读完请求体再判权限：带上 body 的请求如果在鉴权处就返回，
            # keep-alive 连接上残留的字节会被当成下一个请求，直接串包。
            body: dict[str, Any] = {}
            if self.command in ("POST", "PUT", "PATCH"):
                body = self._body()

            if not self._host_ok():
                self._error(403, "请求的 Host 不是本机地址，已拒绝（防 DNS 重绑定）")
                return

            # 页面自身（Origin/Referer 校验对页面导航不生效：
            # 从别的本机管理页 window.open 跳过来时，首个 GET / 的 Referer 是
            # 发起页的地址（端口不同），按 CSRF 拦会把页面自己锁死在 403。
            # API 接口才做 Origin 校验，页面访问安全由 token 保证。）
            if path in ("/", "/index.html"):
                self._send(200, app._render_page().encode("utf-8"), "text/html; charset=utf-8")
                return
            if path == "/favicon.ico":
                self._send(204, b"")
                return

            if not path.startswith("/api/"):
                self._error(404, f"没有这个地址：{path}")
                return
            if not self._origin_ok():
                self._error(403, "请求来源不是本页面，已拒绝（防跨站请求）")
                return
            if not self._token_ok():
                self._error(401, "缺少或错误的访问令牌，请从终端里那条带 token 的链接打开页面")
                return

            if path == "/api/file":
                if self.command not in ("GET", "HEAD"):
                    self._error(405, "方法不允许")
                    return
                self._serve_file(query)
                return

            if path == "/api/shutdown":
                self._json(200, {"ok": True, "message": "服务即将停止"})
                threading.Timer(0.3, app.stop).start()
                return

            name = ROUTES.get((self.command, path))
            if name is None:
                allowed = [m for m, p in ROUTES if p == path]
                if allowed:
                    self._error(405, f"这个地址只支持 {'/'.join(allowed)}")
                else:
                    self._error(404, f"没有这个接口：{path}")
                return

            result = getattr(app.api, name)(query, body)
            self._json(200, result)

        def _wrap(self) -> None:
            try:
                self._handle()
            except ApiError as exc:
                self._error(exc.status, str(exc))
            except (config.ConfigError, ValueError) as exc:
                self._error(400, str(exc))
            except store.ConfigReadOnly as exc:
                self._error(409, str(exc))
            except BrokenPipeError:
                pass  # 用户关了页面，正常
            except Exception as exc:  # noqa: BLE001 - 任何意外都要变成可读的 JSON
                app.log(f"[!] {self.command} {self.path} -> {type(exc).__name__}: {exc}")
                self._error(500, f"{type(exc).__name__}: {exc}")

        def do_GET(self) -> None:  # noqa: N802
            self._wrap()

        def do_HEAD(self) -> None:  # noqa: N802
            self._wrap()

        def do_POST(self) -> None:  # noqa: N802
            self._wrap()

        def do_DELETE(self) -> None:  # noqa: N802
            self._wrap()

        def do_PUT(self) -> None:  # noqa: N802
            self._wrap()

    return Handler


# ---------------------------------------------------------------- 终端入口


def run(
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
    stderr: Any = None,
) -> int:
    """阻塞式运行，Ctrl-C 退出。返回进程退出码。"""
    stream = stderr if stderr is not None else sys.stderr

    def log(message: str) -> None:
        if message:
            print(f"[media-router] {message}", file=stream, flush=True)
        else:
            print("", file=stream, flush=True)

    server = ConfigServer(host=host, port=port, open_browser=open_browser, log=log)
    try:
        url = server.start()
    except ApiError as exc:
        log(str(exc))
        print(
            json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False),
            flush=True,
        )
        return 1

    # 中英混排的框线永远对不齐，就别画框了，用缩进分块更耐看
    banner = [
        "",
        "配置页面已启动",
        "",
        f"  打开这个地址：{url}",
        "",
        "配置会写到哪里",
        f"  厂商与模型 -> {store.overlay_path()}",
        f"  密钥       -> {config.secrets_write_path()}",
        "  这两个文件都不会覆盖你手写的 models.yaml / secrets.yaml",
        "",
        "页面只监听本机地址，用完回到这个终端按 Ctrl-C 关闭。",
        "",
    ]
    if store.readonly_reason():
        banner.insert(3, f"  注意：{store.readonly_reason()}")
    if config.secrets_readonly_reason():
        banner.insert(4, f"  注意：{config.secrets_readonly_reason()}")
    for line in banner:
        log(line)

    print(
        json.dumps(
            {
                "status": "ok",
                "url": url,
                "host": server.host,
                "port": server.port,
                "token": server.token,
                "overlay_path": str(store.overlay_path()),
                "secrets_path": str(config.secrets_write_path()),
                "readonly": store.readonly_reason(),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("收到 Ctrl-C，已停止。")
    finally:
        server.stop()
    return 0
