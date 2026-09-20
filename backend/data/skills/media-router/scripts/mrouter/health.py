"""模型健康度与熔断。

思路：某个模型连续失败到阈值就进入冷却期，路由时被优先排除；
冷却期结束自动恢复，冷却期间只要成功一次就清空失败计数。

状态落在 state/health.json，跨进程共享，损坏时静默重建。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


class HealthStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._data: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "models": {}}
        self._load()

    # ---------- 读写 ----------

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return
        if isinstance(raw, dict) and isinstance(raw.get("models"), dict):
            self._data = raw
            self._data.setdefault("schema_version", SCHEMA_VERSION)

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self.path)
        except OSError:
            # 状态写入失败不应让整个生成流程失败
            pass

    # ---------- 查询 ----------

    def state(self, model_id: str) -> dict[str, Any]:
        return self._data["models"].get(model_id, {})

    def is_cooling(self, model_id: str, now: float | None = None) -> bool:
        entry = self.state(model_id)
        until = float(entry.get("cooldown_until") or 0)
        return until > (now if now is not None else time.time())

    def cooling_remaining(self, model_id: str, now: float | None = None) -> float:
        until = float(self.state(model_id).get("cooldown_until") or 0)
        left = until - (now if now is not None else time.time())
        return max(0.0, left)

    # ---------- 更新 ----------

    def record_success(self, model_id: str) -> None:
        entry = self._data["models"].setdefault(model_id, {})
        entry["consecutive_failures"] = 0
        entry["cooldown_until"] = 0
        entry["last_error"] = ""
        entry["success_count"] = int(entry.get("success_count") or 0) + 1
        entry["last_success_ts"] = time.time()
        self.save()

    def record_failure(
        self, model_id: str, error: str, threshold: int, cooldown_seconds: float
    ) -> dict[str, Any]:
        entry = self._data["models"].setdefault(model_id, {})
        failures = int(entry.get("consecutive_failures") or 0) + 1
        entry["consecutive_failures"] = failures
        entry["failure_count"] = int(entry.get("failure_count") or 0) + 1
        entry["last_error"] = (error or "")[:500]
        entry["last_failure_ts"] = time.time()
        tripped = False
        if failures >= max(1, threshold):
            entry["cooldown_until"] = time.time() + max(0.0, cooldown_seconds)
            tripped = True
        self.save()
        return {"consecutive_failures": failures, "circuit_open": tripped}

    def reset(self, model_id: str | None = None) -> None:
        if model_id:
            self._data["models"].pop(model_id, None)
        else:
            self._data["models"] = {}
        self.save()

    def snapshot(self, model_ids: list[str] | None = None) -> dict[str, Any]:
        models = self._data["models"]
        ids = model_ids if model_ids is not None else sorted(models)
        out: dict[str, Any] = {}
        for mid in ids:
            entry = models.get(mid) or {}
            out[mid] = {
                "cooling": self.is_cooling(mid),
                "cooldown_remaining_seconds": round(self.cooling_remaining(mid), 1),
                "consecutive_failures": int(entry.get("consecutive_failures") or 0),
                "success_count": int(entry.get("success_count") or 0),
                "failure_count": int(entry.get("failure_count") or 0),
                "last_error": entry.get("last_error") or "",
            }
        return out
