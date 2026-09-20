"""配置页面的持久层。

页面上的每一次"保存"最终都落到两个文件里，分工很明确：

  config/models.web.yaml    厂商 + 模型（不含任何密钥，可以安全提交）
  config/secrets.web.yaml   密钥（已在 .gitignore 里）

为什么不直接写 models.yaml / secrets.yaml：那两个是给人手写的、带注释的。
页面只做"叠加层"，同 id 覆盖、不同 id 追加，你手写的内容和注释一个字都不会少。

所有写操作都走 `_WRITE_LOCK`，因为 Web 服务是多线程的（测试连通性要并发，
否则点一下测试整个页面就卡死）。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import catalog, config, miniyaml

OVERLAY_NAMES = ("models.web.yaml", "models.web.yml", "models.web.json")
OVERLAY_DEFAULT = "models.web.yaml"

HEADER = """\
# ============================================================================
# 这个文件由配置页面生成（media_router.py web）。
# 想手写请改同目录下的 models.yaml，那份是本文件的"底层"，注释不会被动。
# ----------------------------------------------------------------------------
# 加载顺序：models.yaml 先加载，本文件叠加在上；同 id 的条目以本文件为准。
# 密钥不在这个文件里，而在 config/secrets.web.yaml，且不含任何明文以外的信息。
# ============================================================================

"""

SECRETS_HEADER = """\
# ============================================================================
# 配置页面保存的密钥。这个文件已在 .gitignore 里，不会被提交。
# 想手写密钥请改同目录下的 secrets.yaml —— 那是本文件的"底层"。
# ============================================================================

"""

_WRITE_LOCK = threading.RLock()


class ConfigReadOnly(RuntimeError):
    """当前配置来源不允许写入。"""


# ---------------------------------------------------------------- 路径


def overlay_path() -> Path:
    """配置页面写的那份文件（沿用已存在的后缀）。"""
    for name in OVERLAY_NAMES:
        candidate = config.CONFIG_DIR / name
        if candidate.exists():
            return candidate
    return config.CONFIG_DIR / OVERLAY_DEFAULT


def readonly_reason() -> str:
    """返回不可写的原因，空串表示可写。"""
    if os.environ.get("MEDIA_ROUTER_CONFIG"):
        return (
            "当前通过环境变量 MEDIA_ROUTER_CONFIG 指定了配置文件，"
            "配置页面只会读取它，不会写入。要去掉这个变量才能在这里保存。"
        )
    return ""


def _guard_writable() -> None:
    reason = readonly_reason()
    if reason:
        raise ConfigReadOnly(reason)


# ---------------------------------------------------------------- id 生成


def _slug(text: str) -> str:
    """把模型名压成一个能用当 id 的短串。"""
    value = str(text or "").strip().lower()
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-._")
    return value[:60]


def _unique_id(base: str, taken: set[str]) -> str:
    if not base:
        base = "m"
    if base not in taken:
        return base
    index = 2
    while f"{base}-{index}" in taken:
        index += 1
    return f"{base}-{index}"


def _unique_env(base: str, taken: set[str]) -> str:
    """环境变量名只允许字母数字下划线。

    这里**不能**复用 _unique_id —— 它用连字符做分隔，
    而 ARK_API_KEY-2 这种名字在 shell / .env 里都是非法的。
    """
    if base not in taken:
        return base
    index = 2
    while f"{base}_{index}" in taken:
        index += 1
    return f"{base}_{index}"


# ---------------------------------------------------------------- 叠加层读写


def read_overlay() -> dict[str, Any]:
    """读页面生成的那份文件；不存在时返回空壳。"""
    path = overlay_path()
    if not path.exists():
        return {"version": 1}
    loaded = config.read_structured(path)
    if not isinstance(loaded, dict):
        raise config.ConfigError(f"配置文件顶层必须是映射：{path}")
    return loaded


def read_secrets_overlay() -> dict[str, Any]:
    path = config.secrets_write_path()
    if not path.exists():
        return {"keys": {}}
    try:
        loaded = config.read_structured(path)
    except Exception:  # noqa: BLE001 - 坏文件直接重建，密钥本来就该由页面托管
        return {"keys": {}}
    return loaded if isinstance(loaded, dict) else {"keys": {}}


def write_structured(path: Path, data: dict[str, Any], header: str) -> None:
    """写回结构化文件。

    一律用内置 miniyaml 序列化，而不是环境里碰巧装了的 PyYAML ——
    同一份配置在不同机器上写出来的格式必须一模一样。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    body = miniyaml.dumps(data)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(header + body, encoding="utf-8")
    tmp.replace(path)  # 原子替换，避免写一半被读到


# ---------------------------------------------------------------- 叠加层操作


class Overlay:
    """对 models.web.yaml 的读写。所有变更都在内存里改完再整体落盘。

    **必须同时认识手写层。** 页面列的是"手写层 + 叠加层"合并后的结果，
    如果只盯着叠加层，就会出现「明明看得见、点删除却报找不到」这种自相矛盾 ——
    只读这一层是不够的。

    两层的能力不一样，这点必须如实体现给用户：

      * 叠加层里的条目：随便改、随便删，写进 models.web.yaml 就行。
      * 手写层里的条目：**不能改用户手写的文件**。要删就在叠加层写一条同 id 的
        墓碑（``_deleted: true``）把它盖掉；要停用/改参数就写一条同 id 的覆盖条目。
    """

    def __init__(
        self, data: dict[str, Any] | None = None, base: dict[str, Any] | None = None
    ) -> None:
        self.data: dict[str, Any] = dict(data or {})
        self.data.setdefault("version", 1)
        self.data.setdefault("vendors", [])
        if not isinstance(self.data["vendors"], list):
            raise config.ConfigError("models.web.yaml 的 vendors 必须是列表")
        #: 手写层的原始数据，只读
        self.base: dict[str, Any] = base if base is not None else config.base_layer()

    # ---------- 查询 ----------

    @property
    def vendors(self) -> list[dict[str, Any]]:
        return self.data["vendors"]

    def find_vendor(self, vendor_id: str) -> dict[str, Any] | None:
        for item in self.vendors:
            if str(item.get("id")) == vendor_id:
                return item
        return None

    def all_models(self) -> list[tuple[str, dict[str, Any]]]:
        """叠加层里的模型（不含墓碑）。"""
        out: list[tuple[str, dict[str, Any]]] = []
        for kind, bucket in self._pools().items():
            for item in bucket:
                if isinstance(item, dict) and item.get("id") and not config.is_tombstone(item):
                    out.append((kind, item))
        return out

    def base_models(self) -> list[tuple[str, dict[str, Any]]]:
        """手写层里的模型。"""
        return config.layer_models(self.base)

    def locate_model(
        self, model_id: str
    ) -> tuple[str, dict[str, Any], str] | None:
        """在两层里找一条模型，返回 (类目, 条目, 来源)。

        来源是 ``"overlay"``（页面自己建的，能真删）或 ``"base"``（用户手写的，
        只能盖掉）。叠加层优先 —— 同 id 时以叠加层为准。
        """
        for kind, item in self.all_models():
            if str(item.get("id")) == model_id:
                return kind, item, "overlay"
        for kind, item in self.base_models():
            if str(item.get("id")) == model_id:
                return kind, item, "base"
        return None

    def find_model(self, model_id: str) -> tuple[str, dict[str, Any]] | None:
        """只在叠加层里找（写操作专用，因为只有这一层能改）。"""
        for kind, item in self.all_models():
            if str(item.get("id")) == model_id:
                return kind, item
        return None

    def model_ids(self) -> set[str]:
        """两层的 id 全集。生成新 id 时要避开手写层已经用掉的 id，
        否则新建的模型会静默顶掉用户手写的那条。"""
        return {str(item.get("id")) for _kind, item in self.all_models()} | {
            str(item.get("id")) for _kind, item in self.base_models()
        }

    def vendor_ids(self) -> set[str]:
        return {str(item.get("id")) for item in self.vendors}

    def models_of_vendor(self, vendor_id: str) -> list[str]:
        """引用该厂商的模型 id（两层都算，级联删除时要一并遮掉）。"""
        seen: list[str] = []
        for _kind, item in self.all_models() + self.base_models():
            if str(item.get("vendor") or "") == vendor_id:
                mid = str(item.get("id"))
                if mid not in seen:
                    seen.append(mid)
        return seen

    def _pools(self) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for key, value in self.data.items():
            if key in ("version", "defaults", "vendors"):
                continue
            if isinstance(value, dict) and isinstance(value.get("models"), list):
                out[key] = value["models"]
        return out

    def _ensure_pool(self, kind: str) -> list[dict[str, Any]]:
        node = self.data.get(kind)
        if not isinstance(node, dict) or not isinstance(node.get("models"), list):
            node = {"strategy": "priority_then_weight", "models": []}
            self.data[kind] = node
        return node["models"]

    def _drop_tombstone(self, model_id: str) -> None:
        """把某条模型的墓碑清掉（用户重新加回来 / 改回来时用）。"""
        for bucket in self._pools().values():
            bucket[:] = [
                m
                for m in bucket
                if not (
                    isinstance(m, dict)
                    and str(m.get("id")) == model_id
                    and config.is_tombstone(m)
                )
            ]

    def _write_tombstone(self, kind: str, model_id: str) -> None:
        """在叠加层写一条墓碑，把手写层里的同 id 条目盖掉。"""
        self._drop_tombstone(model_id)
        self._ensure_pool(kind).append(
            {"id": model_id, config.TOMBSTONE_KEY: True}
        )

    # ---------- 厂商 ----------

    def upsert_vendor(self, payload: dict[str, Any]) -> dict[str, Any]:
        catalog_key = str(payload.get("catalog_key") or "").strip()
        entry = catalog.get(catalog_key)
        if entry is None:
            raise config.ConfigError(f"未知的厂商类型：{catalog_key or '（空）'}")

        vendor_id = str(payload.get("id") or "").strip()
        created = not vendor_id
        if created:
            vendor_id = catalog.vendor_id_for(entry.key, sorted(self.vendor_ids()))

        existing = self.find_vendor(vendor_id)
        label = str(payload.get("label") or "").strip() or entry.label

        item: dict[str, Any] = {
            "id": vendor_id,
            "provider": entry.provider,
            "catalog": entry.key,
            "label": label,
        }

        # 自定义接口（generic_http）靠 options 描述请求形状，界面必须能填，
        # 否则选中「自定义接口」就是死路 —— 能选但永远跑不起来。
        raw_options = payload.get("options")
        if isinstance(raw_options, str):
            text = raw_options.strip()
            if text:
                try:
                    raw_options = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise config.ConfigError(
                        f"接口参数不是合法的 JSON：{exc.msg}（第 {exc.lineno} 行第 {exc.colno} 列）"
                    ) from exc
            else:
                raw_options = None
        if isinstance(raw_options, dict) and raw_options:
            item["options"] = raw_options

        # 接口地址：与目录默认值相同的就不写，保持文件干净、也方便以后升级默认值
        defaults = entry.endpoints or {}
        endpoints: dict[str, str] = {}
        for kind, raw in (payload.get("endpoints") or {}).items():
            text = str(raw or "").strip()
            if not text or text == defaults.get(kind):
                continue
            endpoints[str(kind)] = text
        if endpoints:
            item["endpoints"] = endpoints

        messages: list[str] = []

        if entry.keyless:
            # 内置工具通道，没有密钥
            pass
        else:
            requested = str(payload.get("api_key_env") or "").strip()
            if not requested and entry.key_fields:
                requested = entry.key_fields[0].env
            if not requested:
                requested = f"{entry.provider.upper()}_API_KEY"

            if existing and existing.get("api_key_env"):
                # 编辑时环境变量名保持不变 —— 变的会是一次性换名，把已有密钥弄丢
                env = str(existing["api_key_env"])
            else:
                used = {
                    str(v.get("api_key_env"))
                    for v in self.vendors
                    if v is not existing and v.get("api_key_env")
                }
                env = requested if requested not in used else _unique_env(
                    requested, used
                )
                if env != requested:
                    messages.append(
                        f"为避免和已有厂商共用密钥，本次密钥记为 {env}"
                    )
            item["api_key_env"] = env

        # 写回列表：保持原有位置，新条目追加在末尾
        if existing is not None:
            existing.clear()
            existing.update(item)
        else:
            self.vendors.append(item)

        key_value = str(payload.get("api_key") or "").strip()
        key_saved = False
        if key_value and not entry.keyless:
            env = str(item.get("api_key_env") or "")
            if env:
                save_secret(env, key_value)
                key_saved = True
                messages.append(f"密钥已保存到 config/{config.secrets_write_path().name}")

        return {
            "id": vendor_id,
            "created": created,
            "key_saved": key_saved,
            "message": f"厂商「{label}」{'已添加' if created else '已更新'}"
            + ("；" + "；".join(messages) if messages else ""),
        }

    def delete_vendor(self, vendor_id: str) -> dict[str, Any]:
        existing = self.find_vendor(vendor_id)
        if existing is None:
            raise config.ConfigError(f"找不到厂商：{vendor_id}")

        dropped = set(self.models_of_vendor(vendor_id))
        # 级联删除引用它的模型 —— 否则那些模型会因为没有 provider 把整份配置弄坏，
        # 而用户只会看到"页面打不开了"。宁可明确删掉并如实告知。
        for kind, bucket in self._pools().items():
            keep = [m for m in bucket if str(m.get("id")) not in dropped]
            if len(keep) != len(bucket):
                node = self.data.get(kind)
                if isinstance(node, dict):
                    node["models"] = keep
        # 手写层里如果有模型引用了这个厂商（比如用户手写了 vendor: v-xxx），
        # 也要一并盖掉，否则删完之后配置会因为引用不存在的厂商而加载失败
        for kind, item in self.base_models():
            if str(item.get("id")) in dropped:
                self._write_tombstone(kind, str(item.get("id")))

        self.vendors.remove(existing)

        label = str(existing.get("label") or vendor_id)
        message = f"厂商「{label}」已删除"
        if dropped:
            message += (
                f"，同时移除了引用它的 {len(dropped)} 个模型：" + "、".join(sorted(dropped))
            )
        return {"id": vendor_id, "removed_models": sorted(dropped), "message": message}

    # ---------- 模型 ----------

    def upsert_model(self, payload: dict[str, Any]) -> dict[str, Any]:
        kind = str(payload.get("kind") or "").strip()
        if kind not in ("image", "video"):
            raise config.ConfigError("模型类目只能是 image（图像）或 video（视频）")

        model_name = str(payload.get("model") or "").strip()
        if not model_name:
            raise config.ConfigError("请填写模型名称")

        model_id = str(payload.get("id") or "").strip()
        created = not model_id
        if created:
            model_id = _unique_id(_slug(model_name) or "model", self.model_ids())

        # 编辑时可能对着一条手写层的模型下手，所以要在两层里找
        located = self.locate_model(model_id)
        previous = self.find_model(model_id)
        previous_row = located[1] if located is not None else None

        vendor_id = str(payload.get("vendor") or "").strip()
        if vendor_id:
            if not self.find_vendor(vendor_id):
                raise config.ConfigError(
                    f"找不到厂商 {vendor_id}，请先在「厂商配置」里添加并保存它"
                )
        elif previous_row is None:
            # 没选厂商又不认识这个 id —— 新建的模型必须挂在某个厂商下，
            # 否则它不知道用哪把密钥、调哪个地址
            raise config.ConfigError(
                "新建模型要先选一个厂商。若这个模型写在 models.yaml 里，"
                "请先在列表里找到它再点「编辑」。"
            )

        try:
            priority = max(1, int(payload.get("priority", 1) or 1))
        except (TypeError, ValueError):
            priority = 1
        try:
            weight = max(0.0, float(payload.get("weight", 1) or 0))
        except (TypeError, ValueError):
            weight = 0.0

        supports_raw = payload.get("supports") or []
        if isinstance(supports_raw, str):
            supports = [s.strip() for s in supports_raw.split(",") if s.strip()]
        else:
            supports = [str(s).strip() for s in supports_raw if str(s).strip()]
        if not supports:
            supports = ["text2img"] if kind == "image" else ["text2video"]

        # 编辑时保留原有的启用状态：表单里没有这个开关，不能顺手把停用的模型开回来。
        # 手写层的条目也要读到 —— 否则改一下手写层里停用的模型就把它激活了。
        if previous_row is not None:
            enabled = bool(previous_row.get("enabled", True))
        else:
            enabled = bool(payload.get("enabled", True))

        label = str(payload.get("label") or "").strip()

        item: dict[str, Any] = {
            "id": model_id,
            "model": model_name,
            "priority": priority,
            "weight": weight,
            "enabled": enabled,
            "supports": supports,
        }
        if label and label != model_name:
            item["label"] = label

        # 生成参数（params）：尺寸/时长这类"每次生成都想固定用"的规格。
        # 合并规则：原有 params（可能来自手写层）打底，本次表单提交的键覆盖；
        # 提交空值 = 删掉那个键（用户在页面上清空了输入框）。
        # 不在表单管辖内的键原样保留，不顺手清掉用户手写的东西。
        params: dict[str, Any] = {}
        if previous_row is not None and isinstance(previous_row.get("params"), dict):
            params.update(previous_row["params"])
        params_in = payload.get("params")
        if isinstance(params_in, dict):
            for key, value in params_in.items():
                name = str(key).strip()
                if not name:
                    continue
                if value in ("", None, 0, "0"):
                    params.pop(name, None)
                elif isinstance(value, (str, int, float, bool)):
                    params[name] = value.strip() if isinstance(value, str) else value
        if params:
            item["params"] = params

        if vendor_id:
            item["vendor"] = vendor_id
        else:
            # 没选厂商 = 在编辑一条内联 provider 的模型（手写层里都是这种写法）。
            # 把它的 provider / 密钥变量 / 地址 / params 原样搬过来，
            # 只让用户在这次编辑里真正改到的东西生效（优先级、权重、名字、能力）。
            for field in ("provider", "api_key_env", "endpoint"):
                value = previous_row.get(field) if previous_row else None
                if value:
                    item[field] = value
            for field in ("params", "options"):
                value = previous_row.get(field) if previous_row else None
                if isinstance(value, dict) and value:
                    # params 例外：上面已经做过"原有打底 + 本次覆盖"的合并，
                    # 这里再照搬会把用户在表单里改的尺寸/时长冲掉。
                    if field == "params" and isinstance(payload.get("params"), dict):
                        continue
                    item[field] = dict(value)

        # 用户可能正在把一条被隐藏/被盖掉的模型改回来，把墓碑清掉
        self._drop_tombstone(model_id)

        # 类目变了就把条目从旧池挪到新池
        if previous is not None:
            old_kind, old_item = previous
            if old_kind != kind:
                node = self.data.get(old_kind)
                if isinstance(node, dict):
                    node["models"] = [
                        m for m in node.get("models", []) if str(m.get("id")) != model_id
                    ]
                previous = None
            else:
                old_item.clear()
                old_item.update(item)

        if previous is None:
            # 手写层里有同 id 的条目时，这里写入的就是一条覆盖条目 ——
            # 位置由 _dedupe_last 保证还在原处，不会跑到列表末尾
            self._ensure_pool(kind).append(item)

        verb = "已添加" if created else "已更新"
        where = "图像" if kind == "image" else "视频"
        return {
            "id": model_id,
            "created": created,
            "kind": kind,
            "message": f"{where}模型「{model_name}」{verb}",
        }

    def delete_model(self, model_id: str) -> dict[str, Any]:
        found = self.locate_model(model_id)
        if found is None:
            raise config.ConfigError(f"找不到模型：{model_id}")
        kind, item, layer = found
        name = str(item.get("model") or model_id)

        # 叠加层里的条目直接摘掉
        node = self.data.get(kind)
        if isinstance(node, dict):
            node["models"] = [
                m for m in node.get("models", []) if str(m.get("id")) != model_id
            ]

        if layer == "base":
            # 手写层里的条目不能真删 —— 那是用户自己的文件。写一条墓碑盖住它，
            # 对下游（路由、列表）的效果等同于删除，而且随时可以撤销。
            self._write_tombstone(kind, model_id)
            return {
                "id": model_id,
                "hidden": True,
                "message": (
                    f"模型「{name}」已从配置页面隐藏（它写在 config/models.yaml 里，"
                    f"页面不改动你手写的文件）。想彻底删掉请打开 models.yaml 删掉这一段；"
                    f"想恢复就在这里重新添加同 id 的模型。"
                ),
            }
        return {"id": model_id, "hidden": False, "message": f"模型「{name}」已删除"}

    def set_enabled(self, model_id: str, enabled: bool) -> dict[str, Any]:
        found = self.locate_model(model_id)
        if found is None:
            raise config.ConfigError(f"找不到模型：{model_id}")
        kind, item, layer = found
        name = str(item.get("model") or model_id)

        if layer == "overlay":
            previous = self.find_model(model_id)
            if previous is not None:
                previous[1]["enabled"] = bool(enabled)
        else:
            # 手写层里的条目：在叠加层写一条同 id 的覆盖条目，只改启用状态，
            # 其余字段照抄手写层的，保证除状态外一切不变。
            self._drop_tombstone(model_id)
            override = {
                k: v for k, v in item.items() if k != config.TOMBSTONE_KEY
            }
            override["id"] = model_id
            override["enabled"] = bool(enabled)
            pool = self._ensure_pool(kind)
            pool[:] = [m for m in pool if str(m.get("id")) != model_id]
            pool.append(override)

        return {
            "id": model_id,
            "enabled": bool(enabled),
            "message": f"模型「{name}」已{'启用' if enabled else '停用'}",
        }

    # ---------- 落盘 ----------

    def _prune_empty_pools(self) -> None:
        """空的类目不写进文件 —— models.yaml 里没有的池子，别凭空多出来一个。"""
        for kind, bucket in list(self._pools().items()):
            if not bucket:
                self.data.pop(kind, None)

    def save(self) -> Path:
        _guard_writable()
        with _WRITE_LOCK:
            self._prune_empty_pools()
            # 厂商里不写任何密钥字段，只留 api_key_env 这个名字
            path = overlay_path()
            write_structured(path, self.data, HEADER)
            return path


# ---------------------------------------------------------------- 密钥


def save_secret(env_name: str, value: str) -> Path:
    """把密钥写进 config/secrets.web.yaml。"""
    reason = config.secrets_readonly_reason()
    if reason:
        raise ConfigReadOnly(reason)
    with _WRITE_LOCK:
        data = read_secrets_overlay()
        keys = data.setdefault("keys", {})
        if not isinstance(keys, dict):
            keys = {}
            data["keys"] = keys
        keys[str(env_name)] = str(value)
        path = config.secrets_write_path()
        write_structured(path, data, SECRETS_HEADER)
        return path


# ---------------------------------------------------------------- 页面视图


def _vendor_view(
    item: dict[str, Any], resolved_keys: dict[str, str], model_count: int
) -> dict[str, Any]:
    vendor_id = str(item.get("id") or "")
    env = str(item.get("api_key_env") or "")
    entry = catalog.get(str(item.get("catalog") or ""))
    keyless = bool(entry and entry.keyless)
    endpoints = dict(item.get("endpoints") or {})
    return {
        "id": vendor_id,
        "label": str(item.get("label") or vendor_id),
        "provider": str(item.get("provider") or ""),
        "catalog": str(item.get("catalog") or item.get("provider") or ""),
        "api_key_env": env,
        "endpoints": endpoints,
        "options": dict(item.get("options") or {}),
        "model_count": model_count,
        "keyless": keyless,
        "has_key": keyless or bool(resolved_keys.get(env) or resolved_keys.get(vendor_id)),
        # 按这个厂商**实际填的地址**算，而不是目录默认地址 ——
        # 自建接口（generic_http）的地址是用户自己填的，能推导就该亮按钮。
        "can_list_models": catalog.can_list_models_for(entry, endpoints),
        "list_note": str((entry.list_models or {}).get("unsupported") or "") if entry else "",
    }


def _model_view(
    kind: str,
    item: dict[str, Any],
    vendors: dict[str, dict[str, Any]],
    base_ids: set[str],
) -> dict[str, Any]:
    model_id = str(item.get("id") or "")
    vendor_id = str(item.get("vendor") or "")
    vendor = vendors.get(vendor_id) or {}
    entry = catalog.get(str(vendor.get("catalog") or ""))
    model_name = str(item.get("model") or model_id)
    try:
        priority = int(item.get("priority", 1))
    except (TypeError, ValueError):
        priority = 1
    try:
        weight = float(item.get("weight", 1))
    except (TypeError, ValueError):
        weight = 1.0
    supports = item.get("supports") or []
    if isinstance(supports, str):
        supports = [s.strip() for s in supports.split(",") if s.strip()]
    params = item.get("params")
    return {
        "id": model_id,
        "kind": kind,
        "label": str(item.get("label") or model_name),
        "model": model_name,
        "vendor": vendor_id,
        "vendor_label": str(vendor.get("label") or vendor_id),
        "provider": str(item.get("provider") or vendor.get("provider") or ""),
        "catalog": str(vendor.get("catalog") or ""),
        "priority": priority,
        "weight": weight,
        "enabled": bool(item.get("enabled", True)),
        "supports": [str(s) for s in supports],
        # 生成参数（尺寸/时长等）。页面用它回填"生成规格"输入框。
        "params": dict(params) if isinstance(params, dict) else {},
        # 这条是用户手写在 models.yaml 里的，还是在配置页面里建的？
        # 页面据此告诉用户"删除"为什么是"隐藏"。
        "from_base": model_id in base_ids,
        "warnings": [],
        "suggestions": (entry.suggestions.get(kind, []) if entry else []),
    }


def _resolved_keys() -> dict[str, str]:
    """当前能拿到的密钥：两份密钥文件 + 环境变量（环境变量优先，与运行时一致）。"""
    out: dict[str, str] = {}
    for path in config.secrets_paths():
        try:
            loaded = config.read_structured(path)
        except Exception:  # noqa: BLE001 - 坏文件不该让页面打不开
            continue
        if isinstance(loaded, dict):
            for key, value in (loaded.get("keys") or {}).items():
                if value and str(value).strip():
                    out[str(key)] = str(value).strip()
    for env_name in list(out) + [f.env for entry in catalog.CATALOG for f in entry.key_fields]:
        value = os.environ.get(env_name)
        if value and value.strip():
            out[env_name] = value.strip()
    return out


def bootstrap(host: str = "127.0.0.1") -> dict[str, Any]:
    """页面首屏要的全部数据。配置写坏时也要能返回。"""
    warnings: list[str] = []
    data: dict[str, Any] = {"version": 1}
    paths: list[Path] = []

    try:
        data, paths, _overlay = config.load_raw(allow_missing=True)
    except config.ConfigError as exc:
        warnings.append(f"{exc}（配置页面仍可打开，保存后会修正）")
    except Exception as exc:  # noqa: BLE001 - 语法错误不能让页面打不开
        warnings.append(
            f"配置文件解析失败：{type(exc).__name__}: {exc}"
            f"（仍可在这里修改，保存后会修正该文件）"
        )

    # 顶层再校验一遍，把"能读但有毛病"的问题也提前告诉用户
    try:
        config.load_config()
    except config.ConfigError as exc:
        warnings.append(str(exc))
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"{type(exc).__name__}: {exc}")

    raw_vendors = data.get("vendors") or []
    if not isinstance(raw_vendors, list):
        raw_vendors = []
        warnings.append("vendors 必须是列表，已按空处理")
    vendor_index = {
        str(item.get("id")): item for item in raw_vendors if isinstance(item, dict)
    }

    resolved = _resolved_keys()
    counts: dict[str, int] = {}
    for key, value in data.items():
        if key in ("version", "defaults", "vendors"):
            continue
        if isinstance(value, dict) and isinstance(value.get("models"), list):
            for item in config.visible_entries(value["models"]):
                if isinstance(item, dict):
                    vid = str(item.get("vendor") or "")
                    counts[vid] = counts.get(vid, 0) + 1

    vendors = [
        _vendor_view(item, resolved, counts.get(str(item.get("id")), 0))
        for item in raw_vendors
        if isinstance(item, dict) and item.get("id")
    ]

    models: dict[str, list[dict[str, Any]]] = {}
    base_ids = {str(item.get("id")) for _kind, item in config.layer_models(config.base_layer())}
    for key, value in data.items():
        if key in ("version", "defaults", "vendors"):
            continue
        if isinstance(value, dict) and isinstance(value.get("models"), list):
            # 和运行时的加载逻辑保持一致：同 id 只留最后一条、墓碑丢掉。
            # 少了这一步，页面会把同一条模型显示两遍（手写层一遍、覆盖层一遍），
            # 而且会把已经"删掉"的模型又列出来。
            entries = config.visible_entries(value["models"])
            models[key] = [
                _model_view(key, item, vendor_index, base_ids)
                for item in entries
                if isinstance(item, dict) and item.get("id")
            ]

    kinds = list(models)
    for extra in ("image", "video"):
        if extra not in kinds:
            kinds.append(extra)

    # 引用了不存在的厂商的模型，等于运行时一定会失败，提前点出来
    for kind, bucket in models.items():
        for entry in bucket:
            if entry["vendor"] and entry["vendor"] not in vendor_index:
                warnings.append(
                    f"模型 {entry['id']} 引用了不存在的厂商 {entry['vendor']}，"
                    f"请到「厂商配置」里补上，或把它删掉"
                )

    # 同一句话不重复说（load_raw 与 load_config 可能报同一个错）
    deduped: list[str] = []
    for text in warnings:
        if text and text not in deduped:
            deduped.append(text)

    base_config = paths[0] if paths else config.CONFIG_DIR / "models.yaml"
    target = overlay_path()
    return {
        "catalog": catalog.as_dict(),
        "vendors": vendors,
        "models": models,
        "kinds": kinds,
        "warnings": deduped,
        "readonly": readonly_reason(),
        "secrets_readonly": config.secrets_readonly_reason(),
        "paths": {
            "skill_dir": str(config.SKILL_DIR),
            "config_path": str(base_config),
            "overlay_path": str(target) if target.exists() else "",
            "overlay_target": str(target),
            "secrets_path": str(config.secrets_write_path()),
            "secrets_base": str(config.locate_secrets() or ""),
            "host": host,
        },
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


# ---------------------------------------------------------------- 供服务端调用


def load_overlay() -> Overlay:
    return Overlay(read_overlay())


def save_with(mutate: Callable[[Overlay], Any]) -> Any:
    """加载 → 在写锁里改 → 落盘。服务端所有写接口都走这里。"""
    with _WRITE_LOCK:
        overlay = Overlay(read_overlay())
        result = mutate(overlay)
        overlay._prune_empty_pools()  # noqa: SLF001 - 同类协作
        write_structured(overlay_path(), overlay.data, HEADER)
        return result
