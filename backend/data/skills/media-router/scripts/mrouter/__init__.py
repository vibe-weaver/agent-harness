"""media-router 内部实现包。

一个零依赖、可移植的「媒体生成调度」内核：把「多模型池 + 权重优先级 + 失败熔断 +
结果回传」这套逻辑封装成 CLI，供任意 agent（WorkBuddy / Claude Code / DSH 等）调用。
"""

__version__ = "1.0.0"

__all__ = [
    "miniyaml",
    "config",
    "health",
    "selector",
    "transport",
    "adapters",
    "caption",
    "cli",
]
