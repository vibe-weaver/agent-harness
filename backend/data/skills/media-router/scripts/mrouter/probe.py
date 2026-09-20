"""连通性探测。

分两级，**默认永远走轻量级**：

轻量级（不花钱、不生成任何东西）
  1. 接口可达 —— 网络/TLS/地址是否正确
  2. 密钥有效 —— 只调厂商的**只读**端点（模型列表、任务列表、账号信息）
  3. 模型名可用 —— 只有厂商提供模型列表接口才校验，否则如实标"未校验"

深度级（真跑一次生成，会消耗额度）
  用极小参数真调一次适配器，顺带验证参数、下载、落盘整条链路。
  这是唯一能 100% 确认"这个模型名真的能用"的办法。

设计取舍：轻量级**绝不**用"提交一个空任务看报错"这种取巧手段来判密钥 ——
万一平台把空提示词当合法输入，用户就白花钱了。拿不到只读校验端点的平台
（如 fal）如实标注"无法免生成校验"，而不是假装测过了。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import catalog
from .adapters import GenRequest, Ctx, kling_jwt, run_adapter
from .config import ModelSpec, RouterConfig, Vendor
from .transport import HttpError, request

PROBE_TIMEOUT = 25.0

PASS, FAIL, WARN, SKIP = "pass", "fail", "warn", "skip"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass
class ProbeTarget:
    """待探测的目标。可以来自已保存的厂商，也可以来自页面尚未保存的表单。"""

    catalog_key: str = ""
    provider: str = ""
    api_key: str = ""
    api_key_env: str = ""
    endpoints: dict[str, str] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)
    vendor_id: str = ""

    @classmethod
    def from_vendor(cls, vendor: Vendor) -> "ProbeTarget":
        return cls(
            catalog_key=vendor.catalog or vendor.provider,
            provider=vendor.provider,
            api_key_env=vendor.api_key_env,
            endpoints=dict(vendor.endpoints),
            options=dict(vendor.options),
            vendor_id=vendor.id,
        )


@dataclass
class ProbeResult:
    ok: bool
    checks: list[Check] = field(default_factory=list)
    summary: str = ""
    models: list[str] = field(default_factory=list)
    can_verify_key: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "summary": self.summary,
            "checks": [c.to_dict() for c in self.checks],
            "models": self.models,
            "can_verify_key": self.can_verify_key,
        }


# ---------------------------------------------------------------- 密钥解析


def resolve_key(
    cfg: RouterConfig, target: ProbeTarget
) -> tuple[str, str]:
    """返回 (key, 来源)。页面直接填的明文优先，其次是环境变量、secrets 文件。"""
    if target.api_key.strip():
        return target.api_key.strip(), "页面输入"

    import os

    if target.api_key_env:
        env_value = os.environ.get(target.api_key_env)
        if env_value:
            return env_value.strip(), f"环境变量 {target.api_key_env}"

    keys = (cfg.secrets or {}).get("keys") or {}
    if target.api_key_env and keys.get(target.api_key_env):
        return str(keys[target.api_key_env]).strip(), f"secrets 文件 [{target.api_key_env}]"
    if target.vendor_id and keys.get(target.vendor_id):
        return str(keys[target.vendor_id]).strip(), f"secrets 文件 [{target.vendor_id}]"
    return "", ""


def _auth_headers(target: ProbeTarget, key: str, entry: catalog.ProviderEntry) -> dict[str, str]:
    style = (entry.auth_check or {}).get("auth", "bearer")
    if style == "bearer":
        return {"Authorization": f"Bearer {key}"}
    if style == "key":
        return {"Authorization": f"Key {key}"}
    if style == "kling_jwt":
        if ":" not in key:
            raise HttpError(
                "可灵需要一对密钥，格式是「AccessKey:SecretKey」，中间用英文冒号连接"
            )
        access_key, secret_key = key.split(":", 1)
        if not access_key.strip() or not secret_key.strip():
            raise HttpError("可灵的 AccessKey 或 SecretKey 是空的，请检查冒号两侧是否都填了")
        return {"Authorization": f"Bearer {kling_jwt(access_key.strip(), secret_key.strip())}"}
    return {"Authorization": f"Bearer {key}"}


# ---------------------------------------------------------------- 轻量探测


def _check_reachable(url: str, timeout: float) -> Check:
    if not url:
        return Check("接口可达", SKIP, "未配置接口地址，将使用该厂商的默认地址")
    try:
        request("GET", url, timeout=timeout, max_retries=0)
        return Check("接口可达", PASS, "服务已响应")
    except HttpError as exc:
        if exc.status:
            return Check(
                "接口可达",
                PASS,
                f"服务已响应（HTTP {exc.status}，属正常，说明网络和地址没问题）",
            )
        return Check("接口可达", FAIL, f"连不上：{exc}")
    except Exception as exc:  # noqa: BLE001
        return Check("接口可达", FAIL, f"连不上：{exc}")


def _check_auth(
    target: ProbeTarget, entry: catalog.ProviderEntry, key: str, timeout: float
) -> tuple[Check, list[str]]:
    if entry.keyless:
        return Check("密钥校验", SKIP, "该厂商无需密钥"), []

    if not entry.auth_check:
        return (
            Check(
                "密钥校验",
                WARN,
                "该平台没有「只读」校验接口，无法在不生成的情况下确认密钥，请用「试生成」验证",
            ),
            [],
        )

    if not key:
        hint = entry.key_fields[0].hint if entry.key_fields else ""
        return Check("密钥校验", FAIL, f"还没填密钥。{hint}"), []

    url = entry.auth_check["url"]
    try:
        headers = _auth_headers(target, key, entry)
    except HttpError as exc:
        return Check("密钥校验", FAIL, str(exc)), []

    try:
        resp = request("GET", url, headers=headers, timeout=timeout, max_retries=0)
    except HttpError as exc:
        status = exc.status
        if status in (401, 403):
            return Check("密钥校验", FAIL, f"密钥无效或被拒绝（HTTP {status}）"), []
        if status == 404:
            return (
                Check("密钥校验", WARN, "校验端点返回 404（可能是区域不同），未能确认密钥"),
                [],
            )
        if status == 429:
            return Check("密钥校验", WARN, "密钥看起来有效，但当前被限流（HTTP 429）"), []
        if status:
            return Check("密钥校验", WARN, f"校验请求返回 HTTP {status}，未能确认密钥"), []
        return Check("密钥校验", FAIL, f"校验请求失败：{exc}"), []

    payload = resp.json()
    models: list[str] = []
    path = entry.auth_check.get("models_path")
    if path:
        node: Any = payload
        for part in path.split("."):
            if isinstance(node, dict):
                node = node.get(part)
            else:
                node = None
                break
        name_field = entry.auth_check.get("models_name_field", "id")
        if isinstance(node, list):
            for item in node:
                if isinstance(item, dict) and item.get(name_field):
                    models.append(str(item[name_field]))
                elif isinstance(item, str):
                    models.append(item)
    return Check("密钥校验", PASS, "密钥有效"), models


def _summarize(checks: list[Check]) -> tuple[bool, str]:
    failed = [c for c in checks if c.status == FAIL]
    warned = [c for c in checks if c.status == WARN]
    if failed:
        return False, failed[0].detail or f"{failed[0].name}未通过"
    if warned:
        return True, "基本可用，" + (warned[0].detail or "但有项目未完全确认")
    return True, "全部检查通过"


def probe_target(
    cfg: RouterConfig, target: ProbeTarget, kind: str = "image", model_name: str = ""
) -> ProbeResult:
    """轻量探测：不生成任何内容，不消耗额度。"""
    entry = catalog.get(target.catalog_key) or catalog.get(target.provider)
    if entry is None:
        return ProbeResult(
            ok=False,
            checks=[Check("厂商识别", FAIL, f"未知的厂商类型：{target.catalog_key or target.provider}")],
            summary="未知的厂商类型",
        )

    key, source = resolve_key(cfg, target)
    endpoint = target.endpoints.get(kind) or entry.endpoints.get(kind, "")

    checks = [Check("厂商识别", PASS, entry.label)]
    checks.append(
        Check(
            "接口地址",
            PASS,
            endpoint or "（使用该厂商内置默认地址）",
        )
    )

    probe_url = (entry.auth_check or {}).get("url") or endpoint
    if entry.keyless:
        checks.append(Check("接口可达", SKIP, "使用内置工具，不访问外部接口"))
    elif not probe_url:
        checks.append(
            Check("接口可达", WARN, "该厂商没有默认地址也没填地址，检测时无法确认网络可达性")
        )
    else:
        checks.append(_check_reachable(probe_url, PROBE_TIMEOUT))

    auth_check, models = _check_auth(target, entry, key, PROBE_TIMEOUT)
    checks.append(auth_check)

    if key and source:
        checks.insert(2, Check("密钥来源", PASS, source))

    if model_name.strip():
        if models:
            if model_name.strip() in models:
                checks.append(Check("模型名可用", PASS, f"在厂商模型列表中找到了 {model_name}"))
            else:
                checks.append(
                    Check(
                        "模型名可用",
                        FAIL,
                        f"厂商返回的模型列表里没有「{model_name}」，请核对名称",
                    )
                )
        elif auth_check.status == PASS:
            checks.append(
                Check(
                    "模型名可用",
                    WARN,
                    "该平台不提供模型列表，模型名需要靠「试生成」才能真正确认",
                )
            )

    ok, summary = _summarize(checks)
    return ProbeResult(
        ok=ok, checks=checks, summary=summary, models=models[:300],
        can_verify_key=bool(entry.auth_check),
    )


# ---------------------------------------------------------------- 深度探测


def deep_probe(
    cfg: RouterConfig,
    spec: ModelSpec,
    prompt: str = "",
    *,
    timeout: float = 180.0,
    poll_interval: float = 5.0,
    max_poll: float = 300.0,
) -> dict[str, Any]:
    """真跑一次生成（消耗额度）。返回结果字典，不抛异常。"""
    default_prompts = {
        "image": "一张极简的纯色背景测试图，中心有一个红色圆形",
        "video": "一个红色圆形缓慢旋转的简单动画",
    }
    text = prompt.strip() or default_prompts.get(spec.kind, "连接测试")

    # 落在配置好的产物目录下（而不是硬编码 <skill>/outputs），
    # 这样用 MEDIA_ROUTER_OUTPUT_DIR 指到临时目录就能隔离测试
    out_dir = cfg.output_dir / "_connectivity_test"
    request_obj = GenRequest(
        kind=spec.kind,
        prompt=text,
        requires=list(spec.supports) or (
            ["text2img"] if spec.kind == "image" else ["text2video"]
        ),
        count=1,
        output_dir=out_dir,
    )
    key, source = cfg.resolve_api_key(spec)
    ctx = Ctx(
        api_key=key,
        key_source=source,
        timeout=timeout,
        poll_interval=poll_interval,
        max_poll=max_poll,
        log=lambda _msg: None,
    )
    try:
        result = run_adapter(spec, request_obj, cfg, ctx)
    except Exception as exc:  # noqa: BLE001 - 探测失败要如实报告，不向上抛
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "hint": "看提示里的 HTTP 状态码：401/403 多为密钥问题，404 多为模型名或地址写错，429 是限流。",
        }

    if result.status == "delegate":
        return {
            "ok": True,
            "delegate": result.delegate,
            "note": "该模型走内置工具通道，无法在这里试生成，需要由 AI 助手调用它的内置出图能力完成。",
        }
    if result.status != "ok":
        return {"ok": False, "error": result.error or "生成失败"}

    return {
        "ok": True,
        "files": result.files,
        "urls": result.urls,
        "meta": result.meta,
    }


# ---------------------------------------------------------------- 模型列表

#: 模态标记。名字里带这个的，判断优先级最高 —— 厂商自己写在名字里的，
#: 比我们按家族名猜要准。``wanx2.1-t2v-turbo`` 就是靠它才不会被 wanx 带偏。
_VIDEO_MODE_TAGS = ("t2v", "i2v", "v2v", "flf2v", "first-last-frame")
_IMAGE_MODE_TAGS = ("t2i", "i2i")

#: 视频模型家族名
_VIDEO_FAMILY = (
    "cogvideox", "cogvideo", "seedance", "kling-v", "vidu", "hailuo", "pixverse",
    "hunyuan-video", "veo", "luma", "dream-machine", "mochi", "ltx",
    "minimax-video", "step-video", "skyreels", "wan2", "pixdance",
)

#: 图像模型家族名
_IMAGE_FAMILY = (
    "cogview", "seedream", "flux", "dall-e", "dalle", "gpt-image", "sdxl",
    "stable-diffusion", "kolors", "qwen-image", "hunyuan-image", "ideogram",
    "recraft", "midjourney", "imagen", "wanx", "pixart", "lumina",
    "playground-v", "auraflow", "sana", "hidream", "seededit",
)


def classify_by_name(name: str) -> str:
    """按名字判断一个模型是 image / video 还是 other。

    设计取向是**保守**：只有明确命中模态标记（t2i / t2v）或媒体模型家族名
    才归类，其余一律算 other。

    为什么不用"排除文本模型"的黑名单思路：黑名单靠子串匹配很容易误伤
    （比如 ``step-`` 会被 ``ep-`` 命中），而且新出的文本模型永远在名单之外，
    会被当成图像模型推荐给用户 —— 用户选了必然生成失败，还查不出原因。
    漏判的代价只是用户得手填模型名，错判的代价是一次失败和一堆困惑，
    所以宁可漏。

    这是**兜底**手段：平台自己给出类目信息时（如百炼的 capabilities=IG）
    不走这里，``fetch_models`` 会标注 ``filtered_by`` 说明用了哪种。
    """
    value = str(name or "").lower()
    if not value:
        return "other"
    if any(tag in value for tag in _VIDEO_MODE_TAGS):
        return "video"
    if any(tag in value for tag in _IMAGE_MODE_TAGS):
        return "image"
    # 家族名：视频先判，cogvideox / cogview 只差一个字母，顺序错了会串
    if any(tag in value for tag in _VIDEO_FAMILY):
        return "video"
    if any(tag in value for tag in _IMAGE_FAMILY):
        return "image"
    return "other"


def _dig_path(payload: Any, path: str) -> Any:
    """按 "a.b.c" 取值；取不到返回 None。"""
    node: Any = payload
    for part in str(path or "").split("."):
        if not part:
            continue
        if isinstance(node, dict):
            node = node.get(part)
        else:
            return None
    return node


def _extract_names(payload: Any, models_path: str, name_field: str) -> list[str]:
    """从响应里把模型名捞出来。兼容两种常见形状：

    OpenAI 风格：``{"data": [{"id": "..."}]}``
    百炼风格：  ``{"output": {"models": [{"model": "..."}]}}``
    以及干脆就是字符串数组。
    """
    node = _dig_path(payload, models_path) if models_path else payload
    out: list[str] = []
    if isinstance(node, dict):
        # 有些平台把列表又包了一层，如 {"output": {"models": {...}}}
        for key in ("models", "data", "items", "results", "list"):
            if isinstance(node.get(key), list):
                node = node[key]
                break
    if isinstance(node, list):
        for item in node:
            if isinstance(item, str):
                if item.strip():
                    out.append(item.strip())
            elif isinstance(item, dict):
                value = item.get(name_field) or item.get("id") or item.get("model")
                if value:
                    out.append(str(value))
    return out


def _extract_items(payload: Any, models_path: str) -> list[dict[str, Any]]:
    """同 _extract_names，但保留整个条目 —— 需要读 capabilities 这类字段时用。"""
    node = _dig_path(payload, models_path) if models_path else payload
    if isinstance(node, dict):
        for key in ("models", "data", "items", "results", "list"):
            if isinstance(node.get(key), list):
                node = node[key]
                break
    if isinstance(node, list):
        return [item for item in node if isinstance(item, dict)]
    return []


def _local_capability_filter(
    items: list[dict[str, Any]], name_field: str, kind: str
) -> tuple[list[str], bool]:
    """如果响应条目自带类目字段，就在本地按它过滤。

    为什么不直接信平台的过滤参数：**实测智谱会忽略所有过滤参数** —— 发
    ``?capabilities=IG`` 返回的和不发一模一样。百炼虽然支持，但同样可能在
    某些区域/版本上不生效。既然条目里往往就带着 capabilities，自己再筛一遍
    既不多花一次请求，也不会被平台的静默忽略坑到。

    返回 (模型名列表, 是否真的筛过)。筛不出类目字段时返回 ([], False)，
    由调用方退回按名字猜。
    """
    want = {"image": {"IG", "image", "text2image", "text_to_image"},
            "video": {"VG", "video", "text2video", "text_to_video"}}.get(kind, set())
    if not want:
        return [], False
    kept: list[str] = []
    saw_field = False
    for item in items:
        caps = item.get("capabilities") or item.get("modalities") or item.get("tags") or []
        if isinstance(caps, str):
            caps = [caps]
        if not isinstance(caps, list) or not caps:
            continue
        saw_field = True
        if any(str(c).strip() in want for c in caps):
            value = item.get(name_field) or item.get("model") or item.get("id")
            if value:
                kept.append(str(value))
    return kept, saw_field


def fetch_models(
    cfg: RouterConfig, target: ProbeTarget, kind: str = "image", limit: int = 200
) -> dict[str, Any]:
    """从厂商拉取模型列表，**按类目过滤后**返回，供页面勾选批量添加。

    三个不能含糊的地方（都来自实测教训）：

    1. **只认只读 GET**。列模型不花钱，但绝不用"提交空任务看报错"这类取巧办法。
    2. **必须按类目过滤**。实测智谱 /api/paas/v4/models 只返回 10 个 glm-*
       文本模型，一个图像模型都没有 —— 不过滤的话用户会看到一堆 glm-5，
       选一个去生图必然失败，而且完全查不出原因。
    3. **过滤后为空要如实说明**，并给出该类目正确的模型名，而不是回一句
       "拉取成功，0 个模型"就完事。

    列表地址的来源按优先级：目录声明 → 从厂商填的接口地址推导 → 都没有就
    如实说"这个平台没有列表接口"。
    """
    entry = catalog.get(target.catalog_key) or catalog.get(target.provider)
    suggestions = list((entry.suggestions or {}).get(kind, []) if entry else [])

    def fail(error: str, **extra: Any) -> dict[str, Any]:
        return {"ok": False, "error": error, "suggestions": suggestions, **extra}

    if entry is not None and entry.keyless:
        return fail("该厂商是内置工具通道，没有模型列表，也不需要填模型名")

    spec = dict((entry.list_models or {}) if entry else {})
    source = "catalog"

    # 目录里显式声明了"拉不了"的平台（火山方舟、可灵）：它们的地址长得像
    # OpenAI 兼容，推导会推出一个不存在的 /models，点了必报错。直接如实说明。
    if spec.get("unsupported"):
        return fail(str(spec["unsupported"]), reason="unsupported")

    # 目录没声明列表端点时，试着从厂商填的生成地址推一个（OpenAI 兼容规律）
    if not spec.get("url"):
        legacy = (entry.auth_check or {}) if entry else {}
        if legacy.get("models_path"):
            # 旧写法：校验端点顺带能列模型（OpenAI/硅基原本就这么配的）
            spec = {
                "url": legacy["url"],
                "models_path": legacy["models_path"],
                "name_field": legacy.get("models_name_field", "id"),
                "auth": legacy.get("auth", "bearer"),
            }
            source = "auth_check"
        else:
            endpoint = str(target.endpoints.get(kind) or "").strip()
            if not endpoint:
                endpoint = next(iter(target.endpoints.values()), "") if target.endpoints else ""
            derived = catalog.derive_list_url(endpoint)
            if derived:
                spec = {
                    "url": derived,
                    "models_path": "data",
                    "name_field": "id",
                    "auth": (
                        str((target.options or {}).get("auth", {}).get("type") or "bearer")
                        if isinstance((target.options or {}).get("auth"), dict)
                        else "bearer"
                    ),
                }
                source = "derived"

    if not spec.get("url"):
        return fail(
            "这个平台没有模型列表接口（或者没能从你填的接口地址推导出来），"
            "请到厂商控制台复制模型名手动填写。"
            + (f"常用模型：{('、'.join(suggestions))}" if suggestions else ""),
            reason="no_list_endpoint",
        )

    key, key_source = resolve_key(cfg, target)
    auth_style = str(spec.get("auth") or "bearer")
    if auth_style != "none":
        if not key:
            hint = ""
            if entry is not None and entry.key_fields:
                hint = f"（{entry.key_fields[0].hint or entry.key_fields[0].label}）"
            return fail(f"还没填密钥，无法拉取模型列表{hint}", reason="no_key")

    headers: dict[str, str] = {}
    params: dict[str, Any] = dict(spec.get("page_params") or {})
    if key:
        if auth_style == "key":
            headers["Authorization"] = f"Key {key}"
        elif auth_style == "kling_jwt":
            try:
                access_key, secret_key = key.split(":", 1)
                headers["Authorization"] = f"Bearer {kling_jwt(access_key.strip(), secret_key.strip())}"
            except (ValueError, HttpError) as exc:
                return fail(f"可灵密钥格式不对：{exc}", reason="bad_key_format")
        else:
            headers["Authorization"] = f"Bearer {key}"

    # 平台若支持按类目过滤（百炼的 capabilities=IG/VG），优先用它 —— 比猜名字准
    kind_param = str(spec.get("kind_param") or "")
    kind_values = (spec.get("kind_values") or {}).get(kind) if spec.get("kind_values") else None
    filtered_by = "none"
    if kind_param and kind_values:
        for value in kind_values:
            params.setdefault(kind_param, [])
            params[kind_param].append(value)
        filtered_by = kind_param

    url = str(spec["url"])
    try:
        resp = request(
            "GET", url, headers=headers, params=params or None,
            timeout=PROBE_TIMEOUT, max_retries=0,
        )
    except HttpError as exc:
        if exc.status in (401, 403):
            return fail(
                f"密钥被拒绝（HTTP {exc.status}）。"
                + (f"用的是 {key_source}。" if key_source else "")
                + "请确认这把密钥属于这个平台，且没有过期。",
                reason="auth", source_url=url,
            )
        if exc.status == 404:
            return fail(
                f"模型列表地址不存在（HTTP 404）：{url}\n"
                "这个地址是按 OpenAI 兼容规律推导的，看来该平台不遵循这个规律。"
                "请手动填写模型名。"
                + (f"常用模型：{'、'.join(suggestions)}" if suggestions else ""),
                reason="not_found", source_url=url,
            )
        if exc.status == 429:
            return fail("请求过于频繁（HTTP 429），稍等几秒再试。", reason="rate_limited", source_url=url)
        return fail(f"拉取失败：{exc}", reason="http", source_url=url)
    except Exception as exc:  # noqa: BLE001
        return fail(f"拉取失败：{type(exc).__name__}: {exc}", reason="network", source_url=url)

    try:
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        return fail(
            f"接口返回的不是 JSON（{exc}）：{(resp.text or '')[:200]}",
            reason="bad_json", source_url=url,
        )

    models_path = str(spec.get("models_path") or "")
    name_field = str(spec.get("name_field") or "id")
    names = _extract_names(payload, models_path, name_field)
    if not names:
        return fail(
            "接口通了，但响应里找不到模型名。可能是响应结构不同，"
            f"需要指定取值路径。响应前 200 字：{(resp.text or '')[:200]}",
            reason="empty_response", source_url=url,
        )

    total = len(names)

    # 就算平台支持按类目过滤，也要在本地按条目自带的 capabilities 再筛一遍 ——
    # 实测智谱会**静默忽略**所有过滤参数（发 ?capabilities=IG 和不发返回完全一样）。
    # 盲信平台过滤的话，用户会拿到一堆文本模型去生图，必然失败还查不出原因。
    kept, saw_field = _local_capability_filter(
        _extract_items(payload, models_path), name_field, kind
    )
    if saw_field:
        kept_set = set(kept)
        matched = [n for n in names if n in kept_set]
        skipped = [n for n in names if n not in kept_set]
        filtered_by = "capabilities"
    else:
        # 响应里没有类目字段，只能按名字猜 —— 结果要标明是猜的
        matched, skipped = [], []
        for name in names:
            if classify_by_name(name) == kind:
                matched.append(name)
            else:
                skipped.append(name)
        filtered_by = "name_guess"

    matched = sorted(set(matched))
    truncated = len(matched) > limit
    shown = matched[:limit]

    result: dict[str, Any] = {
        "ok": True,
        "models": shown,
        "kind": kind,
        "total": total,
        "matched": len(matched),
        "skipped": sorted(set(skipped))[:40],
        "skipped_total": len(skipped),
        "truncated": truncated,
        "source_url": url,
        "source": source,
        "filtered_by": filtered_by,
        "suggestions": suggestions,
    }

    if not matched:
        # 这是最容易被误解的一种情况：拉取"成功"了，但一个能用的都没有。
        # 必须把真实原因摆出来，否则用户会以为是程序坏了。
        note = str(spec.get("media_missing") or "")
        if not note:
            where = "图像" if kind == "image" else "视频"
            note = (
                f"接口返回了 {total} 个模型，但没有一个是{where}生成模型"
                f"（该平台可能只提供文本/语音模型）。"
            )
            if skipped:
                note += f" 返回的是：{('、'.join(sorted(set(skipped))[:8]))} 等。"
        if suggestions:
            note += f" 该类目可以直接填：{'、'.join(suggestions)}"
        result["note"] = note
        result["empty_after_filter"] = True
    elif filtered_by == "name_guess":
        result["note"] = (
            f"共 {total} 个模型，按名称推断出 {len(matched)} 个"
            f"{'图像' if kind == 'image' else '视频'}模型"
            + (f"，另有 {len(skipped)} 个是别的类型已过滤掉" if skipped else "")
            + "。（该平台没返回类目信息，分类是按名字猜的，可能不准）"
        )
    else:
        result["note"] = f"共 {len(matched)} 个{'图像' if kind == 'image' else '视频'}模型。"
    if truncated:
        result["note"] += f" 数量较多，只显示前 {limit} 个。"

    return result
