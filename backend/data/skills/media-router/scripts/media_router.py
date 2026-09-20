#!/usr/bin/env python3
"""media-router 入口脚本。

用法示例：
    python media_router.py list
    python media_router.py resolve --kind image --supports text2img
    python media_router.py generate --kind image --prompt "一只戴圆框眼镜的橘猫"

只依赖 Python 3.8+ 标准库，不需要 pip 安装任何东西。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mrouter.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
