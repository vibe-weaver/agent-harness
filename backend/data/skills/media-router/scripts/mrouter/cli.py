"""命令行入口。

对 agent 的契约：**stdout 永远是单个 JSON 对象**，日志走 stderr。
退出码：0=成功，1=失败，3=需要 agent 亲自调用内置工具（委托）。

子命令：
  config     看配置解析结果与路径
  providers  看内置支持哪些 provider
  list       看模型池
  resolve    只看路由结果（不真调用，用来排查"会选谁"）
  generate   真正执行生成
  report     手工上报某个模型的成败（用于外部调用后回写健康度）
  health     看/清健康度
  web        打开本机网页配置界面（给不写配置文件的用户准备）
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__
from .adapters import (
    Ctx,
    GenRequest,
    GenResult,
    known_providers,
    run_adapter,
)
from .caption import build_caption, caption_enabled, profile_files
from .config import ConfigError, RouterConfig, load_config
from .health import HealthStore
from .selector import plan_attempts, rank_candidates

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_DELEGATE = 3


# ---------------------------------------------------------------- 基础设施


def _reconfigure_stdout() -> None:
    """Windows 控制台默认不是 UTF-8，中文 JSON 会乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass


def _log(message: str) -> None:
    """日志走 stderr。空字符串用来打空行，别给它加前缀。"""
    if message:
        print(f"[media-router] {message}", file=sys.stderr, flush=True)
    else:
        print("", file=sys.stderr, flush=True)


def _emit(payload: dict[str, Any], pretty: bool) -> None:
    print(
        json.dumps(payload, ensure_ascii=False, indent=2 if pretty else None),
        flush=True,
    )


def _coerce_scalar(text: str) -> Any:
    low = text.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "none", ""):
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def _parse_params(pairs: list[str] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in pairs or []:
        if "=" not in item:
            raise ConfigError(f"--param 需要 key=value 形式，收到：{item!r}")
        key, _, value = item.partition("=")
        out[key.strip()] = _coerce_scalar(value)
    return out


def _requires_key(spec: Any) -> bool:
    """这个模型是否需要 API Key。

    generic_http 是配置驱动的：只有显式声明了鉴权方式才需要密钥，
    `auth: {type: none}` 或不写 auth 都视为公开接口。
    """
    if spec.is_native:
        return False
    opts = spec.options if isinstance(spec.options, dict) else {}
    if opts.get("keyless") is True:
        return False
    auth = opts.get("auth") or {}
    kind = str((auth.get("type") if isinstance(auth, dict) else "") or "").strip().lower()
    if kind in ("none", "noauth", "no-auth", "public"):
        return False
    if spec.provider in ("generic", "generic_http", "http"):
        return bool(auth)
    return True


def _make_ctx(cfg: RouterConfig, key: str, source: str) -> Ctx:
    defaults = cfg.defaults
    return Ctx(
        api_key=key,
        key_source=source,
        timeout=float(defaults.get("timeout_seconds", 120) or 120),
        poll_interval=float(defaults.get("poll_interval_seconds", 5) or 5),
        max_poll=float(defaults.get("max_poll_seconds", 900) or 900),
        log=_log,
    )


def _infer_requires(kind: str, images: list[str], override: str | None) -> list[str]:
    if override:
        return [s.strip() for s in override.split(",") if s.strip()]
    if images:
        return ["img2video"] if kind == "video" else ["img2img"]
    return ["text2video"] if kind == "video" else ["text2img"]


def _plan(
    cfg: RouterConfig, ordered: list[Any], max_attempts: int | None
) -> tuple[list[Any], list[str]]:
    """把候选序列裁成实际要尝试的清单。

    两条重要规则：
      1. 缺密钥的模型直接跳过，且**不占用** max_attempts 预算 —— 否则用户还没配 key，
         重试次数就被空转吃光，内置兜底通道永远轮不到。
      2. native（内置工具通道）不占预算，它永远是最后那个"总能落地"的兜底。
    """
    limit = max(1, int(max_attempts or 2))
    planned: list[Any] = []
    skipped: list[str] = []
    spent = 0
    for spec in ordered:
        if spent >= limit and not spec.is_native:
            continue
        if _requires_key(spec) and not cfg.resolve_api_key(spec)[0]:
            skipped.append(spec.id)
            continue
        planned.append(spec)
        if not spec.is_native:
            spent += 1
    return planned, skipped


# ---------------------------------------------------------------- 子命令


def cmd_config(args: argparse.Namespace) -> int:
    cfg = load_config()
    payload = {"status": "ok", "version": __version__, **cfg.to_dict()}
    payload["kinds"] = cfg.kinds()
    payload["providers"] = known_providers()
    payload["caption_mode"] = caption_enabled(cfg)
    _emit(payload, args.pretty)
    return EXIT_OK


def cmd_providers(args: argparse.Namespace) -> int:
    table = {
        "native": "内置工具委托：不调 HTTP，返回调用 ImageGen / VideoGen 的指令",
        "openai": "OpenAI 风格 /v1/images/generations 与 /v1/images/edits（改 endpoint 可兼容多数网关）",
        "dashscope": "阿里云百炼 / 通义万相（异步任务 + 轮询）",
        "volcengine": "火山方舟：即梦图片同步、Seedance 视频异步",
        "kling": "可灵：AK/SK 签 JWT，图片与视频均异步",
        "replicate": "Replicate：version 或 owner/name 两种提交方式",
        "fal": "fal.ai 队列接口",
        "generic_http": "完全配置驱动：填 YAML 即可接任意 REST 接口",
    }
    _emit({"status": "ok", "providers": table}, args.pretty or True)
    return EXIT_OK


def _pool_report(cfg: RouterConfig, health: HealthStore, kind: str) -> dict[str, Any]:
    pool = cfg.pool(kind)
    snapshot = health.snapshot([m.id for m in pool.models])
    models = []
    for spec in pool.models:
        item = spec.to_dict()
        needs_key = _requires_key(spec)
        key, source = cfg.resolve_api_key(spec) if needs_key else ("", "无需密钥")
        item["needs_api_key"] = needs_key
        item["api_key_ready"] = (not needs_key) or bool(key)
        item["api_key_source"] = source if key else ""
        item.update(snapshot.get(spec.id) or {})
        models.append(item)
    return {"kind": kind, "strategy": pool.strategy, "models": models}


def cmd_list(args: argparse.Namespace) -> int:
    cfg = load_config()
    health = HealthStore(cfg.health_path)
    kinds = [args.kind] if args.kind else cfg.kinds()
    _emit(
        {
            "status": "ok",
            "config_path": str(cfg.config_path),
            "pools": [_pool_report(cfg, health, kind) for kind in kinds],
            "warnings": cfg.warnings,
        },
        args.pretty or True,
    )
    return EXIT_OK


def _resolve(cfg: RouterConfig, health: HealthStore, args: argparse.Namespace):
    pool = cfg.pool(args.kind)
    images = list(args.image or [])
    requires = _infer_requires(args.kind, images, args.supports)

    if args.model:
        spec = cfg.find_model(args.model)
        if spec.kind != args.kind:
            raise ConfigError(f"模型 {args.model} 属于 {spec.kind} 类目，与 --kind {args.kind} 不一致")
        return [spec], {
            "strategy": "forced",
            "requires": requires,
            "candidate_count": 1,
            "capability_relaxed": False,
            "health_relaxed": False,
            "excluded_cooling": [],
        }

    ordered, notes = rank_candidates(pool.models, pool.strategy, requires, health)
    if not ordered:
        raise ConfigError(f"类目 {args.kind} 下没有任何可用（enabled）模型")
    return ordered, notes


def cmd_resolve(args: argparse.Namespace) -> int:
    cfg = load_config()
    health = HealthStore(cfg.health_path)
    ordered, notes = _resolve(cfg, health, args)
    attempts, skipped = _plan(cfg, ordered, args.max_attempts)
    snapshot = health.snapshot([m.id for m in ordered])
    _emit(
        {
            "status": "ok",
            "kind": args.kind,
            "routing": notes,
            "ranked": [
                {
                    **m.to_dict(),
                    "api_key_ready": (not _requires_key(m))
                    or bool(cfg.resolve_api_key(m)[0]),
                    **(snapshot.get(m.id) or {}),
                }
                for m in ordered
            ],
            "will_attempt": [m.id for m in attempts],
            "skipped_missing_key": skipped,
            "warnings": cfg.warnings,
        },
        args.pretty or True,
    )
    return EXIT_OK


def cmd_generate(args: argparse.Namespace) -> int:
    started = time.time()
    cfg = load_config()
    health = HealthStore(cfg.health_path)
    threshold, cooldown = cfg.health_policy()

    out_dir = Path(args.output_dir).expanduser() if args.output_dir else cfg.output_dir
    if args.kind == "image" and out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)

    ordered, notes = _resolve(cfg, health, args)
    attempts, skipped_candidates = _plan(cfg, ordered, args.max_attempts)

    req = GenRequest(
        kind=args.kind,
        prompt=args.prompt or "",
        requires=notes.get("requires", []),
        images=list(args.image or []),
        size=args.size or "",
        aspect_ratio=args.aspect_ratio or "",
        duration=int(args.duration or 0),
        negative_prompt=args.negative_prompt or "",
        count=int(args.count or 1),
        output_dir=None if args.no_download else out_dir,
        extra=_parse_params(args.param),
    )
    if req.extra.get("last_image"):
        req.images = list(req.images)

    trail: list[dict[str, Any]] = []
    notes_out: list[str] = list(cfg.warnings)
    if skipped_candidates:
        notes_out.append(
            "以下候选因未配置密钥被跳过（不占用重试次数）：" + "、".join(skipped_candidates)
        )

    for spec in attempts:
        attempt_started = time.time()
        label = f"{spec.id}({spec.provider})"
        try:
            if spec.is_native:
                key, source = "", "无需密钥"
            else:
                key, source = cfg.resolve_api_key(spec)
                if _requires_key(spec) and not key:
                    raise ConfigError(source)

            _log(f"尝试 {label}，优先级={spec.priority} 权重={spec.weight}")
            result: GenResult = run_adapter(spec, req, cfg, _make_ctx(cfg, key, source))

            if result.status == "delegate":
                _log(f"{label} 走内置工具通道，交还 agent 执行")
                _emit(
                    {
                        "status": "delegate",
                        "kind": args.kind,
                        "model": {"id": spec.id, "provider": spec.provider},
                        "delegate": result.delegate,
                        "routing": notes,
                        "fallback_trail": trail,
                        "elapsed_ms": int((time.time() - started) * 1000),
                        "notes": notes_out,
                    },
                    args.pretty,
                )
                return EXIT_DELEGATE

            health.record_success(spec.id)
            files = list(result.files)
            profiles = profile_files(files, args.kind)

            caption_text = ""
            if files:
                caption_text, cap_notes = build_caption(
                    cfg, files, args.kind, goal=args.prompt or ""
                )
                notes_out.extend(cap_notes)

            _emit(
                {
                    "status": "ok",
                    "kind": args.kind,
                    "model": {
                        "id": spec.id,
                        "provider": spec.provider,
                        "model": result.meta.get("model") or spec.model or spec.id,
                    },
                    "files": profiles,
                    "urls": list(result.urls),
                    "caption": caption_text,
                    "caption_mode": caption_enabled(cfg),
                    "request": {
                        "prompt": req.prompt,
                        "negative_prompt": req.negative_prompt,
                        "size": req.size,
                        "aspect_ratio": req.aspect_ratio,
                        "duration": req.duration,
                        "count": req.count,
                        "images": req.images,
                    },
                    "meta": result.meta,
                    "routing": notes,
                    "fallback_trail": trail,
                    "elapsed_ms": int((time.time() - started) * 1000),
                    "notes": notes_out,
                    "hint": (
                        "产物已落盘。若后续步骤需要引用它，直接用 files[].path 的绝对路径。"
                        + ("caption 字段即文字描述，可直接用于文字汇报。" if caption_text else "")
                    ),
                },
                args.pretty,
            )
            return EXIT_OK

        except ConfigError as exc:
            # 配置类问题不计入熔断，否则会误伤模型
            _log(f"{label} 配置不可用：{exc}")
            trail.append(
                {
                    "model": spec.id,
                    "provider": spec.provider,
                    "error": str(exc),
                    "kind": "config",
                    "elapsed_ms": int((time.time() - attempt_started) * 1000),
                }
            )
        except Exception as exc:  # noqa: BLE001 - 任何 provider 异常都要能降级
            message = f"{type(exc).__name__}: {exc}"
            _log(f"{label} 失败：{message}")
            outcome = health.record_failure(spec.id, message, threshold, cooldown)
            entry = {
                "model": spec.id,
                "provider": spec.provider,
                "error": message,
                "kind": "runtime",
                "elapsed_ms": int((time.time() - attempt_started) * 1000),
                **outcome,
            }
            if outcome.get("circuit_open"):
                entry["note"] = f"连续失败达 {threshold} 次，进入 {cooldown:.0f} 秒冷却"
            trail.append(entry)

    _emit(
        {
            "status": "error",
            "kind": args.kind,
            "error": "所有候选模型都失败了",
            "fallback_trail": trail,
            "routing": notes,
            "attempted": [m.id for m in attempts],
            "elapsed_ms": int((time.time() - started) * 1000),
            "notes": notes_out,
            "hint": (
                "检查 config/models.yaml 的 api_key_env 是否已设置对应环境变量，"
                "或用 `list` 子命令确认 api_key_ready。"
            ),
        },
        args.pretty,
    )
    return EXIT_ERROR


def cmd_report(args: argparse.Namespace) -> int:
    cfg = load_config()
    health = HealthStore(cfg.health_path)
    spec = cfg.find_model(args.model)
    threshold, cooldown = cfg.health_policy()
    if args.ok:
        health.record_success(spec.id)
        outcome: dict[str, Any] = {"recorded": "success"}
    else:
        outcome = health.record_failure(
            spec.id, args.error or "agent 上报失败", threshold, cooldown
        )
        outcome["recorded"] = "failure"
    snapshot = health.snapshot([spec.id])[spec.id]
    _emit(
        {"status": "ok", "model": spec.id, **outcome, "health": snapshot},
        args.pretty or True,
    )
    return EXIT_OK


def cmd_health(args: argparse.Namespace) -> int:
    cfg = load_config()
    health = HealthStore(cfg.health_path)
    if args.reset:
        health.reset(args.model)
        health.save()
    ids: list[str] = []
    for pool in cfg.pools.values():
        ids.extend(m.id for m in pool.models)
    _emit(
        {
            "status": "ok",
            "health_path": str(cfg.health_path),
            "policy": dict(zip(("failure_threshold", "cooldown_seconds"), cfg.health_policy())),
            "models": health.snapshot(ids),
            **({"reset": args.model or "all"} if args.reset else {}),
        },
        args.pretty or True,
    )
    return EXIT_OK


def cmd_web(args: argparse.Namespace) -> int:
    """启动本机网页配置界面。"""
    from . import webserver

    return webserver.run(
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
    )


#: 不带子命令时要展示的引导。顺序按"新手最可能需要的"排，web 放第一个。
GUIDE: list[tuple[str, str]] = [
    ("web", "打开本机网页配置界面 —— 第一次用就选这个，浏览器里点着配"),
    ("config", "看配置解析结果、路径、有没有告警"),
    ("list", "看模型池：优先级、权重、Key 是否就绪、有没有被熔断"),
    ("resolve", "只看会选谁，不真调用（排查首选/降级序列）"),
    ("generate", "执行生成"),
    ("providers", "看内置支持哪些平台"),
    ("report", "回写某个模型的成败（外部调用后同步健康度）"),
    ("health", "看/清熔断状态"),
]

GUIDE_EXAMPLES: list[tuple[str, str]] = [
    ("python scripts/media_router.py web", "配模型池（新手从这里开始）"),
    ("python scripts/media_router.py list --pretty", "看模型池现状"),
    ("python scripts/media_router.py generate --kind image --prompt \"一只戴圆框眼镜的橘猫\"", "生成一张图"),
    ("python scripts/media_router.py resolve --kind video --pretty", "只看视频会选哪个模型"),
]


def _guide_payload() -> dict[str, Any]:
    return {
        "status": "help",
        "message": "没有指定子命令，下面是可用命令。",
        "suggest": "第一次用请先跑 web 配好厂商和模型。",
        "commands": {name: text for name, text in GUIDE},
        "examples": [cmd for cmd, _ in GUIDE_EXAMPLES],
        "help": "python scripts/media_router.py <子命令> --help",
    }


def cmd_guide(args: argparse.Namespace) -> int:
    """没带子命令时的引导。

    小白用户很可能就是这么跑起来的，所以这里必须给出"下一步做什么"，
    而不是一句用法错误。人看的那份走 stderr，stdout 仍然只放一个 JSON。
    """
    _log("没有指定子命令。可用命令：")
    for name, text in GUIDE:
        _log(f"  {name:<10} {text}")
    _log("")
    _log("常用示例：")
    for command, note in GUIDE_EXAMPLES:
        _log(f"  {command}")
        _log(f"      {note}")
    _log("")
    _log("每个子命令的详细参数：python scripts/media_router.py <子命令> --help")
    _emit(_guide_payload(), args.pretty)
    return EXIT_OK


# ---------------------------------------------------------------- 解析器


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="media_router",
        description=(
            "媒体生成调度器：按优先级与权重从模型池里挑一个模型，生成图片或视频，"
            "失败自动按候选序列降级。stdout 输出单个 JSON，日志走 stderr。"
        ),
    )
    # 让 --pretty 既能放在子命令前，也能放在子命令后
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--pretty", action="store_true", help="JSON 缩进输出（人看的时候用）")

    parser.add_argument("--pretty", action="store_true", help="JSON 缩进输出（人看的时候用）")
    parser.add_argument("--version", action="version", version=f"media-router {__version__}")
    # 刻意不设 required：光敲 `media_router.py` 是很自然的动作，
    # 这时应该给一份引导而不是甩一句 "arguments are required: command"。
    sub = parser.add_subparsers(dest="command", required=False)
    parser.set_defaults(func=cmd_guide)

    p_config = sub.add_parser("config", parents=[common], help="查看配置解析结果与路径")
    p_config.set_defaults(func=cmd_config)

    p_providers = sub.add_parser("providers", parents=[common], help="列出支持的 provider")
    p_providers.set_defaults(func=cmd_providers)

    p_list = sub.add_parser("list", parents=[common], help="列出模型池")
    p_list.add_argument("--kind", choices=["image", "video"], help="只看某个类目")
    p_list.set_defaults(func=cmd_list)

    def add_routing_flags(p: argparse.ArgumentParser) -> None:
        p.add_argument("--kind", required=True, choices=["image", "video"], help="要生成哪类媒体")
        p.add_argument("--supports", help="能力过滤，逗号分隔，如 text2img,img2img")
        p.add_argument("--image", action="append", help="输入图片（本地路径或 URL），可重复")
        p.add_argument("--model", help="强制指定模型 id，跳过路由")

    p_resolve = sub.add_parser("resolve", parents=[common], help="只看会选谁，不真调用")
    add_routing_flags(p_resolve)
    p_resolve.add_argument("--max-attempts", type=int, default=None)
    p_resolve.set_defaults(func=cmd_resolve)

    p_gen = sub.add_parser("generate", parents=[common], help="执行生成")
    add_routing_flags(p_gen)
    p_gen.add_argument("--prompt", required=True, help="生成提示词")
    p_gen.add_argument("--negative-prompt", help="负面提示词")
    p_gen.add_argument("--size", help="图片尺寸，如 1024x1024")
    p_gen.add_argument("--aspect-ratio", help="视频比例，如 16:9 / 9:16")
    p_gen.add_argument("--duration", type=int, help="视频时长（秒）")
    p_gen.add_argument("--count", type=int, default=1, help="生成数量")
    p_gen.add_argument("--output-dir", help="产物落盘目录，默认 ./outputs（当前工作区）")
    p_gen.add_argument("--max-attempts", type=int, default=None, help="最多尝试几个候选模型")
    p_gen.add_argument("--no-download", action="store_true", help="只拿远程 URL，不下载")
    p_gen.add_argument(
        "--param", action="append", help="透传给 provider 的额外参数，key=value，可重复"
    )
    p_gen.set_defaults(func=cmd_generate)

    p_report = sub.add_parser("report", parents=[common], help="外部调用后回写某个模型的成败")
    p_report.add_argument("--model", required=True, help="模型 id")
    group = p_report.add_mutually_exclusive_group(required=True)
    group.add_argument("--ok", action="store_true", help="记一次成功")
    group.add_argument("--fail", action="store_true", help="记一次失败")
    p_report.add_argument("--error", help="失败原因")
    p_report.set_defaults(func=cmd_report)

    p_health = sub.add_parser("health", parents=[common], help="查看或清空健康度")
    p_health.add_argument("--reset", action="store_true", help="清空熔断状态")
    p_health.add_argument("--model", help="只清某个模型（配合 --reset）")
    p_health.set_defaults(func=cmd_health)

    p_web = sub.add_parser(
        "web",
        parents=[common],
        help="打开本机网页配置界面（添加厂商、填 Key、配模型与权重）",
    )
    p_web.add_argument("--port", type=int, default=8760, help="端口，默认 8760")
    p_web.add_argument(
        "--host",
        default="127.0.0.1",
        help="监听地址，默认只监听本机（改成别的地址等于把配置页面暴露到网络上，慎用）",
    )
    p_web.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    p_web.set_defaults(func=cmd_web)

    return parser


def main(argv: list[str] | None = None) -> int:
    _reconfigure_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "command", None) in ("generate", "resolve") and getattr(
        args, "max_attempts", None
    ) is None:
        try:
            cfg_for_default = load_config()
            args.max_attempts = int(cfg_for_default.defaults.get("max_attempts", 2) or 2)
        except ConfigError:
            args.max_attempts = 2

    try:
        return int(args.func(args))
    except ConfigError as exc:
        _emit({"status": "error", "error": str(exc), "kind": "config"}, args.pretty)
        return EXIT_ERROR
    except KeyboardInterrupt:
        _log("已中断。")
        return EXIT_ERROR
    except KeyboardInterrupt:
        _emit({"status": "error", "error": "被中断"}, getattr(args, "pretty", False))
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001
        _emit(
            {
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "kind": "internal",
            },
            getattr(args, "pretty", False),
        )
        return EXIT_ERROR
