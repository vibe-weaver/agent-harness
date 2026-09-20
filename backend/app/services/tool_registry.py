"""Agent 工具注册表 — 定义 AI 可调用的工具 schema 和执行器。

方案一核心：基于 OpenAI Function Calling 实现轻量级 agent。
工具执行在 Python 进程内完成，不依赖 DSH Node.js 子进程。

工具列表：
  - read_file:    读取工作区文件内容
  - write_file:   写入/创建工作区文件
  - list_files:   列出工作区文件树
  - delete_file:  删除工作区文件或目录
  - run_python:   在独立子进程中执行 Python 代码（可验证逻辑、处理数据）

安全设计（两层，缺一不可）：
  第一层 — 脚本内的行为约束（两个后端共用同一份生成脚本）：
  - 内置函数白名单：替换 __builtins__，禁用 import、open、exec 等
  - 受控 import 白名单 + os 替身模块，工作区读写只能走 read_ws / write_ws
  - 注入 read_skill_file_ws / generate_pdf_ws / generate_docx_ws
  - 配额与输出大小限制

  第二层 — 执行载体隔离，由 SANDBOX_BACKEND 选择：
  - local：本机独立子进程 + setrlimit（内存 RLIMIT_AS、CPU、NOFILE、FSIZE）
    + 可选降权（SANDBOX_RUN_AS_USER）。跨平台，Windows 上 rlimit 静默跳过。
  - docker：把脚本文本交给 B 机的 sandbox-executor，在**全新容器**里跑
    （--network none / --read-only / --cap-drop ALL / cgroup 内存与 CPU /
    --pids-limit），工作区与技能库经 NFS 挂载进去。这一层给的是内核级隔离
    与物理隔离 —— 沙箱不再和 MySQL、.env 同机同权，也不再共享主进程的解释器
    与 site-packages。B 不可达时**不回落 local**（见 sandbox_client 模块文档）。
"""

import asyncio
import base64
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx
from sqlalchemy.orm import Session

from ..core.config import settings
from . import sandbox_client
from . import workspace_service
from .rate_limiter import rate_limiter

logger = logging.getLogger(__name__)

# ── 常量 ──
MAX_OUTPUT = 32000           # 输出最大字符数（需要足够大以读取模板文件和参考文档）
PYTHON_TIMEOUT = 120         # Python 代码执行超时秒数（pandas/图表/PDF 处理需要；Linux 有 RLIMIT_CPU 兜底）
MAX_TOOL_ROUNDS = 40         # agent loop 最大轮数（复杂 skill 工作流需要较多轮次）
# skill 参考文件单次返回上限（64K 字符，可完整容纳 template.html 40K；更大文件用 offset 分段）
MAX_SKILL_FILE_RETURN = 65536

# list_files 分页：一次返回的条目数上限（防止文件多时工具输出自己吃掉上下文预算）
_LIST_DEFAULT_LIMIT = 200
_LIST_MAX_LIMIT = 500

# local 后端的内存限制（RLIMIT_AS，单位 MB）。docker 后端的真实限额由 B 的
# --memory 决定，A 侧只有 settings.SANDBOX_DOCKER_MEMORY_MB 这个**文案用**副本。
SANDBOX_MEMORY_MB = 256
# 文件描述符上限（local 后端 RLIMIT_NOFILE；docker 后端由 --ulimit nofile 施加同值）
SANDBOX_NOFILE = 64

# 工具描述里写给模型看的内存数字，取两个后端的**较小值**。灰度期
# （SANDBOX_DOCKER_ALLOWLIST 非空）两后端并存，而 TOOLS 是模块级常量、没法按
# 用户区分。报低了模型顶多多降采样一次（浪费一点），报高了模型会撞上意料之外
# 的 OOM 然后编造结果 —— 前者明显更划算。
_DESC_MEMORY_MB = min(SANDBOX_MEMORY_MB, settings.SANDBOX_DOCKER_MEMORY_MB)

# docker 后端下容器内的固定挂载点。必须与 sandbox-executor/executor.py 的
# CTN_WS / CTN_SKILLS 逐字一致（tests/test_sandbox_client.py 有对账用例）：
# 生成脚本里写死的是这两个路径，B 侧 -v 挂的也是这两个路径，错一个字母就是
# 一整轮 FileNotFoundError。
CONTAINER_WORKSPACE = "/workspace"
CONTAINER_SKILLS = "/skills"

# 沙箱写入清单标记。生成脚本附加、post-process 解析，**不能**透给前端
# （它是一段 JSON 文件清单，用户看到只会莫名其妙）。此前它是解析处的局部变量，
# 于是实时输出回调那一侧根本挡不住它 —— 早就在往 SSE 漏了。
_WRITTEN_MARKER = "__SANDBOX_WRITTEN_FILES__:"

# ── 沙箱全局并发限制（公网场景：防止并发触发大量子进程打满 CPU/内存）──
import threading as _threading
import os as _os
# 并发自适应：按 CPU 核数调整（每个沙箱子进程可能吃满一个核），范围 2-4。
# .env 里可显式覆盖（设 0 = 沿用自适应值）。docker 后端下这个数**必须** ≤ B 的
# SANDBOX_MAX_CONCURRENT：今天默认值是从 A 的核数算出来的，恰好等于 B 的容量
# 纯属巧合，A 一扩容就会把 B 打到 429。
_cfg_slots = settings.SANDBOX_MAX_CONCURRENT
SANDBOX_MAX_CONCURRENT = (_cfg_slots if _cfg_slots > 0
                          else max(2, min(4, (_os.cpu_count() or 4) // 2)))
_sandbox_slots = _threading.BoundedSemaphore(SANDBOX_MAX_CONCURRENT)
# 排队等槽位的秒数。一轮最多并行 4 个工具调用，docker 后端还多了 0.2-0.5s 容器
# 冷启动，撞车概率比今天高；第 3 个调用等 5 秒远好于直接回"系统繁忙"——那会让
# 模型以为工具坏了并重试，白白烧掉一轮。
_SANDBOX_SLOT_TIMEOUT = 5.0


# ════════════════════════════════════════
#  安全加固（方案五）— 禁止读取的文件模式
# ════════════════════════════════════════

# 匹配这些模式的文件路径会被拒绝读取/写入，
# 防止模型被诱导读取服务器配置或密钥文件。
_BLOCKED_FILE_PATTERNS: list[re.Pattern] = [
    re.compile(r"\.env(\..*)?$", re.IGNORECASE),       # .env / .env.local / .env.production
    re.compile(r"config\.py$", re.IGNORECASE),          # config.py
    re.compile(r"settings\.py$", re.IGNORECASE),        # settings.py
    re.compile(r"credentials", re.IGNORECASE),           # credentials 文件
    re.compile(r"\.pem$", re.IGNORECASE),               # SSL 证书
    re.compile(r"\.key$", re.IGNORECASE),               # 密钥文件
    re.compile(r"id_rsa", re.IGNORECASE),                # SSH 私钥
    re.compile(r"id_ed25519$", re.IGNORECASE),           # ED25519 私钥
    re.compile(r"secret", re.IGNORECASE),                # 含 secret 的文件
    re.compile(r"gunicorn\.conf\.py$", re.IGNORECASE),  # gunicorn 配置
    re.compile(r"blog\.service$", re.IGNORECASE),      # systemd 服务文件
    re.compile(r"requirements\.txt$", re.IGNORECASE),   # 依赖列表（可能含版本漏洞信息）
]


def _is_blocked_path(path: str) -> bool:
    """检查文件路径是否匹配禁止模式。"""
    for pattern in _BLOCKED_FILE_PATTERNS:
        if pattern.search(path):
            return True
    return False


# ════════════════════════════════════════
#  工具 schema 定义 — OpenAI function calling 格式
# ════════════════════════════════════════

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "读取工作区中指定文件的内容。返回文件的文本内容。"
                "图片文件（png/jpg/jpeg/gif/webp/bmp/ico）与扫描件 PDF 会以图像形式返回，"
                "可直接用视觉识别其中的内容 —— 用户在对话里上传的图片就存档在工作区，需要回看时读它。"
                "系统提示词的 <workspace-context> 中可能已给出部分文件内容（取决于用户的注入设置）："
                "已在其中的文件无需重复读取；未在其中的一律必须调用本工具获取，"
                "严禁凭文件名或路径猜测、编造文件内容。"
                "大文件支持分段读取：若返回尾部出现续读提示，请按提示传 offset 参数继续读取，直到读完整份文件再开始处理。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "工作区中的文件路径，如 'src/main.py' 或 'config.json'",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "可选。字符偏移，从指定位置继续读取（用于分段读取大文件）。默认 0。",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "创建或覆盖工作区中的文件。如果父目录不存在会自动创建。"
                "注意：单文件大小不超过 50MB，整个工作区配额 300MB / 最多 100 个文件。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "工作区中的文件路径，如 'src/utils.py'",
                    },
                    "content": {
                        "type": "string",
                        "description": "要写入的文件完整内容",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "局部编辑工作区中的文本文件：把 old_string 替换为 new_string。"
                "修改已有文件时优先用它，不要用 write_file 重发整个文件（省 token，也不会误改无关部分）。"
                "old_string 必须与文件中的已有内容逐字一致（含空格/缩进/换行）且唯一；"
                "不唯一时请扩大上下文，或设置 replace_all=true 全部替换。"
                "未匹配到时不会改动文件，会返回错误提示供你修正。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "工作区中的文件路径，如 'src/main.py'",
                    },
                    "old_string": {
                        "type": "string",
                        "description": "要被替换的原文（须与文件内容逐字一致）",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "替换后的新内容（空字符串表示删除这段内容）",
                    },
                    "replace_all": {
                        "type": "boolean",
                        "description": "可选。true 替换所有匹配处；默认 false，要求 old_string 唯一匹配",
                    },
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "revert_file",
            "description": (
                "把工作区文件回退到更早的一版内容（撤销改错的编辑）。"
                "每次用 write_file / edit_file 覆写文件前，系统都会自动留下一份旧内容快照"
                "（每个文件保留最近 5 版），改错了可以用本工具退回。"
                "steps 表示回退到第几次编辑之前：1 = 上一次编辑前的内容（默认），2 = 再往前一次。"
                "注意：run_python 里写的文件不留快照；回退本身不产生新版本，"
                "所以反复调用 steps=1 结果相同，要退得更远就加大 steps。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要回退的工作区文件路径，如 'src/main.py'",
                    },
                    "steps": {
                        "type": "integer",
                        "description": "可选。回退几版，默认 1（上一次修改前的内容）。",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": (
                "列出工作区中的文件和目录。默认列出整个工作区。"
                "文件较多时结果会分页，按返回尾部的提示传 offset 继续查看；"
                "只想看某个子目录时传 path。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "可选。只列出该子目录（工作区相对路径，如 'src'）。默认列出全部。",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "可选。条目偏移，从指定位置继续查看（用于分页）。默认 0。",
                    },
                    "limit": {
                        "type": "integer",
                        "description": f"可选。本次最多返回的条目数，默认 {_LIST_DEFAULT_LIMIT}，上限 {_LIST_MAX_LIMIT}。",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "删除工作区中的文件或目录。删除目录时会递归删除其所有内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要删除的文件或目录路径",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rename_file",
            "description": (
                "重命名或移动工作区中的文件/目录（含目录下所有内容）。"
                "目标路径的父目录不存在会自动创建。"
                "目标已存在时会报错而不会覆盖，需要覆盖请先调用 delete_file 删除目标。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "old_path": {
                        "type": "string",
                        "description": "源文件或目录路径，如 'src/old_name.py'",
                    },
                    "new_path": {
                        "type": "string",
                        "description": "目标路径，如 'src/new_name.py' 或 'lib/new_name.py'",
                    },
                },
                "required": ["old_path", "new_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": (
                "在独立沙箱子进程中执行 Python 代码（验证逻辑、处理数据、生成文档）。\n"
                "沙箱函数：read_ws(path, binary=False) 读工作区文件（binary=True 返回原始 bytes，"
                "装配 PPT 加图、PIL 拼图需要时必用）；write_ws(path, content) 写工作区文件；"
                "read_skill_file_ws(skill_name, file_path) 读 skill 目录参考文件"
                "（如 'guizang-ppt-skill/assets/template.html'；技能包技能还支持"
                " '../../assets/x.html' 和 '@pack/assets/x.html'）；"
                "generate_pdf_ws(path, text) / generate_docx_ws(path, text) 生成 PDF/Word。\n"
                "import os 可用（受限版）：os.path.join/exists/isfile/isdir/basename/dirname/"
                "splitext/getsize/getmtime、os.getcwd、os.listdir、os.walk、os.stat 等路径与只读操作正常；"
                "os.remove/unlink/rmdir/makedirs 可用但**仅限工作区内**；"
                "os.environ 为空、os.system/popen/chmod 等不可用，"
                "工作区文件的读写内容一律用 read_ws / write_ws。\n"
                "可用 import（按用途）：\n"
                "  数据处理/文本：pandas、numpy、csv、json、re、statistics、decimal、fractions、"
                "difflib、heapq、bisect、datetime、calendar、zoneinfo、pytz、dateutil、collections、"
                "itertools、functools、random、math、hashlib、base64、uuid、textwrap、enum、contextlib；\n"
                "  压缩：zipfile、tarfile、gzip、bz2、lzma；\n"
                "  文档/办公：reportlab（PDF）、docx（Word）、openpyxl（Excel）、markdown、yaml、jinja2；\n"
                "  图表图像：matplotlib（已配中文字体）、PIL/Pillow；\n"
                "  解析/编码：lxml、xml（etree）、html（escape/parser）、"
                "charset_normalizer（探测未知编码）；\n"
                "  其他：pymupdf（PDF，fitz 同义）、packaging（版本比较）、glob、tempfile、pathlib。\n"
                "自测：可用 unittest 写测试并运行。**结果必须落到 stdout**，否则会被丢弃："
                "unittest 默认写 stderr，而沙箱只在 stdout 为空时才回退 stderr，"
                "所以务必把结果导向 StringIO 再 print，别用 unittest.main()。模板：\n"
                "  import io, unittest\n"
                "  buf = io.StringIO()\n"
                "  unittest.TextTestRunner(stream=buf, verbosity=2).run(\n"
                "      unittest.TestLoader().loadTestsFromTestCase(用例类))\n"
                "  print(buf.getvalue())\n"
                "禁止 import：sys、socket、subprocess、shutil 等系统/网络模块。\n"
                "生成图片用 write_ws(path, bytes) 写入 PNG/JPG。matplotlib 已配 Agg 后端和中文字体，"
                "直接 import matplotlib.pyplot 即可绘制中文图表。\n"
                "装配 PPT：from pptx import Presentation; from pptx.util import Inches; "
                "prs=Presentation(); slide=prs.slides.add_slide(prs.slide_layouts[6]); "
                "slide.shapes.add_picture(read_ws('图片路径.png', binary=True), Inches(0), Inches(0), Inches(10)); "
                "prs.save('output.pptx')。多张图片分别 add_picture 到不同 slide。\n"
                "拼多格科普图：from PIL import Image; img=Image.open(read_ws('路径.png', binary=True))；"
                "也可 write_ws 写入字节流。\n"
                f"超时 {PYTHON_TIMEOUT} 秒、内存 {_DESC_MEMORY_MB}MB；print() 输出作为结果返回。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "要执行的 Python 代码",
                    },
                },
                "required": ["code"],
            },
        },
    },
    # ── Skill 工具 — 借鉴 DSH tool-skill ──
    # 模型通过此工具按需加载 skill 的完整指令内容。
    # catalog（name + description 摘要）已注入 system prompt，
    # 模型看到匹配的 skill 后调用此工具获取完整指令。
    {
        "type": "function",
        "function": {
            "name": "skill",
            "description": (
                "加载指定技能的完整指令。在执行匹配某个技能描述的任务之前，"
                "请先调用此工具加载该技能的完整内容，然后遵循其指令来解决问题。"
                "传入 available_skills 列表中的确切技能名称。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "要加载的技能名称（来自 available_skills 列表）",
                    },
                },
                "required": ["name"],
            },
        },
    },
    # ── read_skill_file 工具 — 读取 skill 目录中的参考文件 ──
    # 模型加载 skill 后，可使用此工具读取 skill 目录中的参考文件
    # （如 references/examples.md、scripts/encode_gif.py、模板文件等）
    # 支持大文件分段读取：文件较长时工具会在返回尾部提示用 offset 续读剩余部分
    {
        "type": "function",
        "function": {
            "name": "read_skill_file",
            "description": (
                "读取技能目录中的参考文件内容。"
                "先用 skill 工具加载技能以查看目录中有哪些参考文件，"
                "然后使用此工具读取指定文件的完整内容。"
                "file_path 是相对于技能目录的相对路径，如 'references/examples.md'。"
                "file_path 也可以是目录路径（如 'references'），"
                "此时返回该目录下的完整文件列表（路径已拼好，可直接使用）——"
                "不确定文件名时先传目录名查看，不要臆造不存在的文件名。"
                "技能包（pack）技能还支持 '../xxx' 相对路径访问包内共享资源"
                "（如 '../../assets/template.html'、'../<兄弟技能>/references/x.md'），"
                "以及 '@pack/xxx' 前缀相对包根解析（如 '@pack/assets/template.html'）。"
                "模板类文件（如 template.html）体积较大：若返回尾部出现"
                "'如需继续读取剩余部分，请传 offset=...' 的提示，"
                "请按提示继续分段读取直到读完整份文件，再开始生成内容。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "技能名称（来自 available_skills 列表）",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "相对于技能目录的文件路径，如 'references/examples.md' 或 'scripts/encode_gif.py'",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "可选。字符偏移，从指定位置继续读取（用于分段读取大文件）。默认 0。",
                    },
                },
                "required": ["skill_name", "file_path"],
            },
        },
    },
    # ── media_generate 工具 — media-router 桥接 ──
    # media_router.py 自带 CLI 与网络调用，模块依赖（os/sys/urllib/http/threading）
    # 与沙箱 import 黑名单冲突，沙箱内永远跑不了。这个工具把 CLI 桥接给 Agent：
    # 后端 subprocess 调脚本（脱离沙箱、但参数固定、只写用户工作区），Agent 只传意图。
    # 结果 stdout（JSON）去掉绝对路径后原样回传；产物由脚本落到工作区 outputs/ 下。
    {
        "type": "function",
        "function": {
            "name": "media_generate",
            "description": (
                "通过 media-router 模型池生成图片或视频（文生图/图生图/文生视频/图生视频）。"
                "参数 action 与 media-router CLI 一致："
                "resolve=只查询可用模型（生成前先 resolve，避免白跑），"
                "generate=执行生成，report=查看模型池健康报告。"
                "模型池与 API Key 由用户在技能目录 config/models.yaml 维护。"
                "resolve 结果里 will_attempt 只有 builtin-* 时表示未配置第三方模型，"
                "此时应如实告知用户：模型池为空，需要管理员在管理后台的技能管理中"
                "上传/维护 media-router 技能的 config/models.yaml（或运行其 web 配置页）"
                "添加模型与 API Key。不要把技能目录的服务器路径告诉用户，"
                "也不要建议用户去改工作区里的文件。generate 成功后产物落在工作区 "
                "outputs/ 目录，结果中的 files[].path 是工作区相对路径，"
                "可用 read_file 之外的方式查看（图片为二进制）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["resolve", "generate", "report"],
                        "description": "resolve=查询可用模型；generate=执行生成；report=模型池健康报告",
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["image", "video"],
                        "description": "媒体类型，默认 image",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "生成提示词（action=generate 时必填）",
                    },
                    "image": {
                        "type": "string",
                        "description": "可选。图生图/图生视频的输入图片路径（工作区相对路径）",
                    },
                    "size": {
                        "type": "string",
                        "description": "可选。图片尺寸，如 1024x1024",
                    },
                    "negative_prompt": {
                        "type": "string",
                        "description": "可选。反向提示词",
                    },
                    "count": {
                        "type": "integer",
                        "description": "可选。生成数量，默认 1",
                    },
                    "duration": {
                        "type": "integer",
                        "description": "可选。视频时长（秒）",
                    },
                    "aspect_ratio": {
                        "type": "string",
                        "description": "可选。视频画幅，如 16:9",
                    },
                    "supports": {
                        "type": "string",
                        "description": "可选。resolve 时过滤能力，如 text2img",
                    },
                },
                "required": ["action"],
            },
        },
    },
    # ── 记忆工具 — 用户长期记忆（跨会话）──
    # agent 在对话中主动保存用户信息，注入 system prompt；recall 用于主动查询
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": (
                "保存一条关于用户的长期记忆（跨会话保留，下次对话仍会注入）。"
                "当用户明确表达个人信息、偏好、习惯或长期任务目标时使用，"
                "例如：'我是一名运维工程师'、'我喜欢简洁的回答'、'这个项目的技术栈是 Vue'。"
                "不要保存一次性指令、临时话题或与用户无关的信息；"
                "不确定是否值得记住时，倾向不保存。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "要记住的内容，一句话表述（不超过 200 字符）",
                    },
                    "memory_type": {
                        "type": "string",
                        "enum": ["fact", "preference", "context"],
                        "description": "记忆类型：fact=事实，preference=偏好，context=上下文/项目背景。默认 fact。",
                    },
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall",
            "description": (
                "查询该用户的长期记忆。对话开始时系统已自动注入最近的记忆，"
                "通常无需调用；当需要回忆更早或按关键词搜索特定记忆时使用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "可选。关键词，按内容模糊匹配。",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "可选。返回条数上限，默认 10，最大 20。",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": (
                "在工作区中搜索文件：按文件名关键词或文件内容关键词查找，返回匹配文件及命中行。"
                "当不知道目标文件在哪、或需要确认某关键词出现在哪些文件中时使用，"
                "避免逐个 read_file 盲目读取。只读操作，不修改任何文件。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "文件内容关键词（大小写不敏感），与 name 至少提供一个。",
                    },
                    "name": {
                        "type": "string",
                        "description": "可选。文件名/路径关键词（大小写不敏感），如 'report' 匹配 report.md、monthly_report.xlsx。",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "可选。最大返回条数，默认 20，最大 50。",
                    },
                },
                "required": [],
            },
        },
    },
]

# 按名称索引
TOOL_MAP = {t["function"]["name"]: t for t in TOOL_SCHEMAS}

# 技能类工具名（无活跃技能时动态移除，避免模型空调用）
_SKILL_TOOL_NAMES = {"skill", "read_skill_file"}


def build_tool_schemas(active_skill_names: list[str] | None = None) -> list[dict]:
    """动态构建工具 schema 列表。

    - 无活跃技能时，不注册 skill / read_skill_file 工具，
      避免模型白白调用一轮得到"不存在或未启用"错误。
    - 其余工作区工具（read_file / write_file / edit_file / revert_file /
      rename_file / list_files / delete_file / search_files / run_python）始终注册。

    Args:
        active_skill_names: 当前活跃的技能名列表；None 或空表示无技能可用。

    Returns:
        过滤后的工具 schema 列表
    """
    if active_skill_names:
        return TOOL_SCHEMAS
    return [t for t in TOOL_SCHEMAS if t["function"]["name"] not in _SKILL_TOOL_NAMES]


# ════════════════════════════════════════
#  工具执行器
# ════════════════════════════════════════

def _sanitize_tool_error(msg: str, user_id: int, db: Session) -> str:
    """回传 LLM 前对错误消息脱敏（安全加固2）。

    异常字符串经常携带服务器绝对路径（工作区根、技能库根、部署目录），
    若原样进入 LLM 上下文，模型可能在回复正文中复述真实路径。
    脱敏后相对路径部分保留，LLM 仍能据此纠错；脱敏失败时返回原文不阻塞主流程。
    """
    try:
        from .sanitize import redact
        roots: list[str] = []
        try:
            roots.append(str(workspace_service._user_workspace_dir(user_id)))
        except Exception:
            pass
        try:
            from .skill_service import SKILLS_ROOT
            roots.append(str(SKILLS_ROOT))
        except Exception:
            pass
        return redact(msg, roots=roots or None, root_label="<路径>")
    except Exception:
        return msg


def execute_tool(
    tool_name: str,
    arguments: dict[str, Any],
    user_id: int,
    db: Session,
    on_tool_progress: Optional[callable] = None,
    agent_session_state: Optional[dict] = None,
) -> str:
    """执行工具调用，返回结果字符串。

    Args:
        tool_name: 工具名称
        arguments: 工具参数（已从 JSON 解析）
        user_id: 当前用户 ID（用于工作区隔离）
        db: 数据库会话
        on_tool_progress: 可选回调 (text) -> None，接收 run_python 沙箱的实时输出行
        agent_session_state: Agent 任务级共享状态字典（跨工具轮次持久化）。
            用于工具做"单任务累计"型状态共享。
            None=访客单次工具调用（无状态）。

    Returns:
        工具执行结果文本。错误时返回错误信息字符串（不以异常抛出，
        这样 LLM 能看到错误并自行修正）。
    """
    try:
        if tool_name == "read_file":
            return _tool_read_file(arguments, user_id, db)
        elif tool_name == "write_file":
            return _tool_write_file(arguments, user_id, db)
        elif tool_name == "edit_file":
            return _tool_edit_file(arguments, user_id, db)
        elif tool_name == "revert_file":
            return _tool_revert_file(arguments, user_id, db)
        elif tool_name == "list_files":
            return _tool_list_files(arguments, user_id, db)
        elif tool_name == "delete_file":
            return _tool_delete_file(arguments, user_id, db)
        elif tool_name == "rename_file":
            return _tool_rename_file(arguments, user_id, db)
        elif tool_name == "run_python":
            return _tool_run_python(arguments, user_id, db, on_tool_progress)
        elif tool_name == "skill":
            return _tool_skill(arguments, user_id, db)
        elif tool_name == "read_skill_file":
            return _tool_read_skill_file(arguments, user_id, db)
        elif tool_name == "media_generate":
            return _tool_media_generate(arguments, user_id, db, on_tool_progress)
        elif tool_name == "remember":
            return _tool_remember(arguments, user_id, db)
        elif tool_name == "recall":
            return _tool_recall(arguments, user_id, db)
        elif tool_name == "search_files":
            return _tool_search_files(arguments, user_id, db)
        else:
            return f"错误：未知工具 '{tool_name}'"
    except Exception as e:
        logger.error(f"工具执行异常 ({tool_name}): {e}", exc_info=True)
        return _sanitize_tool_error(f"工具执行出错: {e}", user_id, db)


def _tool_read_file(args: dict, user_id: int, db: Session) -> str:
    """读取工作区文件内容

    对于文本文件，直接返回 UTF-8 文本。
    对于 PDF 文件，先用 PyPDF2 提取文本；如果提取不到（扫描件），
    自动将 PDF 页面渲染为图片，以 <image> 标签返回，让 vision 模型来"看"。
    其他二进制文件返回错误提示。
    """
    path = args.get("path", "").strip()
    if not path:
        return "错误：缺少 path 参数"

    # ── 安全加固：检查是否匹配禁止模式 ──
    if _is_blocked_path(path):
        logger.warning(f"_tool_read_file: 拒绝读取被禁止的文件路径 '{path}'")
        return f"错误：出于安全原因，无法读取 '{path}' 类型的文件"

    # 可选分段参数 offset（字符偏移，与 read_skill_file 对齐）
    offset = 0
    try:
        offset = max(0, int(args.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0

    # 使用 workspace_service 的 read_file_as_text，
    # 它会自动处理文本文件和 PDF 文件的文本提取
    text = workspace_service.read_file_as_text(user_id, path, db)
    if text is not None:
        # 分段读取 + 限制返回大小（大文件引导模型用 offset 续读）
        if offset > 0:
            if offset >= len(text):
                return f"错误：offset（{offset}）超出文件长度（{len(text)} 字符）"
            text = text[offset:]
        if len(text) > MAX_OUTPUT:
            next_offset = offset + MAX_OUTPUT
            text = (
                text[:MAX_OUTPUT]
                + f"\n\n...(文件较长，已返回 {offset}..{next_offset} 字符区间；"
                f"如需继续读取剩余部分，请调用 read_file 工具并传 offset={next_offset})"
            )
        return text

    # 文本提取失败 — 检查是否是 PDF 扫描件
    content = workspace_service.read_file(user_id, path, db)
    if content is None:
        return f"错误：文件 '{path}' 不存在或是一个目录"

    # ── 图片：以 <image> 标签返回，让 vision 模型直接"看"（图片视觉优化14）──
    # 用户在聊天里上传的图片已存档到工作区 聊天图片/（优化9），这条分支让 Agent
    # 能复读它们；工作区里任何其它图片同样适用。
    # content 上面已经读在手，无需再读盘。格式逐字对齐 llm_service._IMAGE_TAG 的
    # 硬要求（属性顺序 name→size、size="…" 后紧跟 >、内容 data: 开头且不含 <），
    # 下游 llm_service 工具轮的多模态通道即可直接接手 —— 包括模型不支持 vision
    # 时替换为占位文本的既有兜底，无需在这里判模型能力。
    if workspace_service.is_image_path(path):
        if len(content) > workspace_service.CHAT_IMAGE_MAX_BYTES:
            return (
                f"错误：图片 '{path}' 超过 "
                f"{workspace_service.CHAT_IMAGE_MAX_BYTES // (1024 * 1024)}MB，无法传给视觉模型"
            )
        mime = workspace_service.sniff_image_mime(content[:16])
        if mime is None:
            return f"错误：文件 '{path}' 扩展名是图片，但内容不是可识别的图片格式"
        b64 = base64.b64encode(content).decode()
        return f'<image name="{path}" size="{len(b64)}">data:{mime};base64,{b64}</image>'

    # PDF 扫描件降级方案：把页面渲染成图片，以 <image> 标签返回
    if path.lower().endswith(".pdf"):
        images = workspace_service.render_pdf_pages_as_images(content)
        if images:
            # 构建 <image> 标签序列 — llm_service 会把它们解析为多模态 content
            parts = [f"[PDF 扫描件 '{path}' 已渲染为 {len(images)} 页图片，请通过视觉识别内容]"]
            for i, data_url in enumerate(images):
                parts.append(f'<image name="{path}_page{i+1}.png" size="{len(data_url)}">{data_url}</image>')
            return "\n".join(parts)
        else:
            return (
                f"错误：PDF 文件 '{path}' 无法提取文本（可能是扫描件），"
                f"且 PDF 渲染组件未安装。请安装 PyMuPDF (pip install PyMuPDF) 以支持扫描件 PDF 识别。"
            )

    return f"错误：文件 '{path}' 是二进制文件，无法以文本形式读取"


def _write_workspace_text_locked(user_id: int, path: str, data: bytes, db: Session,
                                 snapshot: bool = True) -> tuple[str, str]:
    """写入工作区文本的无锁内核：配额检查 + 写盘 + DB 更新 + diff。

    调用方必须已持有该用户的写锁（write_file / edit_file / revert_file 共用，
    保证编辑的"读-改-写"整体在锁内完成，不会被并发的另一处写覆盖）。

    snapshot=True 时在覆写前留一版旧内容快照（revert_file 的回退来源）。
    revert_file 自己传 False —— 否则"回退"也会记一版，历史不再只对应"编辑前的
    版本"，steps 的语义会随回退次数漂移。

    Returns:
        (结果文案, diff 文本)；出错时返回 ("错误：...", "")。
    """
    relative_path = path.replace("\\", "/").lstrip("/")
    workspace_service._ensure_parent_dirs(user_id, relative_path, db)

    from ..models.workspace import WorkspaceFile

    # ── 统一配额检查（与 upload_file 同一入口，防止绕过单文件/数量/总大小限制）──
    existing = db.query(WorkspaceFile).filter(
        WorkspaceFile.user_id == user_id,
        WorkspaceFile.file_path == relative_path,
    ).first()
    try:
        workspace_service._check_quota(user_id, relative_path, len(data), db, existing)
    except ValueError as e:
        return _sanitize_tool_error(f"错误：{e}", user_id, db), ""

    file_path = workspace_service._safe_path(user_id, relative_path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    # 覆写前留一版快照，供 revert_file 回退（新建文件没有旧内容，内部会自动跳过）
    if snapshot:
        workspace_service.snapshot_file(user_id, relative_path)
    # 记录旧内容（用于生成 diff，前端展示改动）
    old_text: str | None = None
    try:
        old_text = workspace_service.read_file_as_text(user_id, relative_path, db)
    except Exception:
        old_text = None
    file_path.write_bytes(data)

    # 更新数据库记录
    import datetime
    import hashlib
    import mimetypes

    content_hash = hashlib.sha1(data).hexdigest()
    mime_type = mimetypes.guess_type(relative_path)[0]

    if existing:
        existing.file_size = len(data)
        existing.content_hash = content_hash
        existing.mime_type = mime_type
        existing.updated_at = datetime.datetime.utcnow()
        db.commit()
    else:
        record = WorkspaceFile(
            user_id=user_id,
            file_path=relative_path,
            file_size=len(data),
            content_hash=content_hash,
            is_directory=False,
            mime_type=mime_type,
        )
        db.add(record)
        db.commit()

    # ── 生成 diff（执行前后对比，供前端展示）──
    diff_text = ""
    if old_text is not None:
        import difflib
        new_text = data.decode("utf-8", errors="replace")
        diff_lines = list(difflib.unified_diff(
            old_text.splitlines(),
            new_text.splitlines(),
            fromfile=f"旧版 {path}",
            tofile=f"新版 {path}",
            lineterm="",
        ))
        if diff_lines:
            diff_text = "\n".join(diff_lines)

    return f"已成功写入文件 '{path}' ({len(data)} 字节)", diff_text


def _tool_write_file(args: dict, user_id: int, db: Session) -> str:
    """写入工作区文件（整文件创建或覆盖）"""
    path = args.get("path", "").strip()
    content = args.get("content", "")
    if not path:
        return "错误：缺少 path 参数"

    # ── 安全加固：检查是否匹配禁止模式 ──
    if _is_blocked_path(path):
        logger.warning(f"_tool_write_file: 拒绝写入被禁止的文件路径 '{path}'")
        return f"错误：出于安全原因，无法写入 '{path}' 类型的文件"

    data = content.encode("utf-8") if isinstance(content, str) else content

    # 统一配额检查 + 写盘 + DB 更新在同一用户写锁内（防止并发写触发唯一约束冲突）
    with workspace_service.user_write_lock(user_id):
        msg, diff_text = _write_workspace_text_locked(user_id, path, data, db)
    if diff_text:
        msg += f"\n\n<diff>\n{diff_text}\n</diff>"
    return msg


def _tool_edit_file(args: dict, user_id: int, db: Session) -> str:
    """局部编辑工作区文本文件：把 old_string 替换成 new_string。

    相比 write_file 的整文件覆写，只回传改动片段即可完成编辑，token 成本从
    O(文件大小) 降到 O(改动大小)，也避免模型重写长文件时改坏无关部分。
    """
    path = (args.get("path") or "").strip()
    old_string = args.get("old_string", "")
    new_string = args.get("new_string", "")
    replace_all = bool(args.get("replace_all", False))

    if not path:
        return "错误：缺少 path 参数"
    if not isinstance(old_string, str) or old_string == "":
        return "错误：old_string 不能为空"
    if not isinstance(new_string, str):
        return "错误：new_string 必须是字符串"
    if old_string == new_string:
        return "错误：old_string 与 new_string 相同，无需替换"
    if _is_blocked_path(path):
        logger.warning(f"_tool_edit_file: 拒绝编辑被禁止的文件路径 '{path}'")
        return f"错误：出于安全原因，无法编辑 '{path}' 类型的文件"

    relative_path = path.replace("\\", "/").lstrip("/")

    # 读-改-写整体持锁，避免与并发写互相覆盖
    with workspace_service.user_write_lock(user_id):
        try:
            old_text = workspace_service.read_file_as_text(user_id, relative_path, db)
        except Exception as e:
            return _sanitize_tool_error(f"错误：读取文件失败：{e}", user_id, db)
        if old_text is None:
            return f"错误：文件 '{path}' 不存在或不是文本文件"

        count = old_text.count(old_string)
        if count == 0:
            return (
                f"错误：在 '{path}' 中未找到 old_string。请确认它与你读取到的内容完全一致"
                "（包括空格、缩进与换行）；若文件已被改动，请重新 read_file 后再编辑。"
            )
        if count > 1 and not replace_all:
            return (
                f"错误：old_string 在 '{path}' 中匹配到 {count} 处，无法确定改哪一处。"
                "请扩大上下文让 old_string 唯一，或设置 replace_all=true 替换全部。"
            )

        new_text = old_text.replace(old_string, new_string, -1 if replace_all else 1)
        msg, diff_text = _write_workspace_text_locked(
            user_id, path, new_text.encode("utf-8"), db
        )

    if msg.startswith("错误"):
        return msg
    what = f"全部 {count} 处" if replace_all else "1 处"
    result = f"已编辑 '{path}'（替换 {what}）"
    if diff_text:
        result += f"\n\n<diff>\n{diff_text}\n</diff>"
    return result


def _tool_revert_file(args: dict, user_id: int, db: Session) -> str:
    """把文件回退到更早一版的内容（快照由 write_file / edit_file 覆写前自动留存）"""
    path = (args.get("path") or "").strip()
    if not path:
        return "错误：缺少 path 参数"
    if _is_blocked_path(path):
        logger.warning(f"_tool_revert_file: 拒绝回退被禁止的文件路径 '{path}'")
        return f"错误：出于安全原因，无法回退 '{path}' 类型的文件"
    try:
        steps = max(1, int(args.get("steps") or 1))
    except (TypeError, ValueError):
        steps = 1

    relative_path = path.replace("\\", "/").lstrip("/")

    # 选版 + 写回整体持锁，避免与并发编辑互相错位
    with workspace_service.user_write_lock(user_id):
        available = workspace_service.count_snapshots(user_id, relative_path)
        if available == 0:
            return (
                f"错误：'{path}' 没有可回退的历史版本"
                "（只有被 write_file / edit_file 覆写过的文件才会留下快照）"
            )
        if steps > available:
            return f"错误：'{path}' 只有 {available} 个历史版本，无法回退 {steps} 步"
        picked = workspace_service.pick_snapshot(user_id, relative_path, steps)
        if picked is None:
            return f"错误：读取 '{path}' 的历史版本失败"
        data, ts = picked
        # 走统一写入内核：配额/DB/diff 一致。snapshot=False —— 回退不改写历史，
        # 保证 steps 始终稳定表示"第 N 次编辑之前的内容"。
        msg, diff_text = _write_workspace_text_locked(user_id, path, data, db, snapshot=False)

    if msg.startswith("错误"):
        return msg
    result = f"已回退 '{path}' 到 {ts:%Y-%m-%d %H:%M:%S} 的内容"
    if diff_text:
        result += f"\n\n<diff>\n{diff_text}\n</diff>"
    return result


def _tool_list_files(args: dict, user_id: int, db: Session) -> str:
    """列出工作区文件树（支持 path 聚焦子树 + offset/limit 分页）"""
    tree = workspace_service.get_file_tree(user_id, db)

    sub_path = (args.get("path") or "").strip().replace("\\", "/").strip("/")

    # 定位子树：path 为空取根，否则按相对路径逐级下钻
    node = tree
    if sub_path:
        for part in sub_path.split("/"):
            nxt = next(
                (c for c in node.get("children", [])
                 if c.get("type") == "directory" and c.get("name") == part),
                None,
            )
            if nxt is None:
                return f"错误：目录 '{sub_path}' 不存在"
            node = nxt

    children = node.get("children", [])
    if not children:
        return f"目录 '{sub_path}' 为空" if sub_path else "工作区为空"

    lines: list[str] = []

    def _render(n: dict, depth: int):
        indent = "  " * depth
        name = n.get("name", "")
        if n.get("type") == "directory":
            lines.append(f"{indent}📁 {name}/")
            for child in n.get("children", []):
                _render(child, depth + 1)
        else:
            size = n.get("size", 0)
            size_str = f"{size} B" if size < 1024 else f"{size / 1024:.1f} KB"
            lines.append(f"{indent}📄 {name} ({size_str})")

    for child in children:
        _render(child, 0)

    total = len(lines)

    try:
        offset = max(0, int(args.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    try:
        limit = int(args.get("limit") or _LIST_DEFAULT_LIMIT)
    except (TypeError, ValueError):
        limit = _LIST_DEFAULT_LIMIT
    limit = max(1, min(limit, _LIST_MAX_LIMIT))

    if offset >= total:
        return f"错误：offset={offset} 超出范围（共 {total} 个条目）"

    page = lines[offset:offset + limit]
    result = "\n".join(page)
    end = offset + len(page)
    if end < total:
        result += (
            f"\n\n...(已显示第 {offset + 1}-{end} 项，共 {total} 项；"
            f"继续查看请传 offset={end})"
        )
    elif offset > 0:
        result += f"\n\n...(已显示第 {offset + 1}-{end} 项，共 {total} 项，已到末尾)"

    if len(result) > MAX_OUTPUT:
        result = result[:MAX_OUTPUT] + "\n\n...(文件列表已截断)"
    return result


def _tool_search_files(args: dict, user_id: int, db: Session) -> str:
    """在工作区中搜索文件：文件名关键词或内容关键词（只读）。

    避免模型不知道目标文件在哪时逐个 read_file 盲目读取：
    - name: 按文件名/路径关键词匹配
    - keyword: 按文件内容关键词匹配（大小写不敏感，每文件只搜前 512KB）
    """
    keyword = (args.get("keyword") or "").strip()
    name_kw = (args.get("name") or "").strip()
    if not keyword and not name_kw:
        return "错误：keyword 与 name 至少提供一个"
    try:
        max_results = max(1, min(int(args.get("max_results") or 20), 50))
    except (TypeError, ValueError):
        max_results = 20

    from ..models.workspace import WorkspaceFile
    files = (
        db.query(WorkspaceFile)
        .filter(
            WorkspaceFile.user_id == user_id,
            WorkspaceFile.is_directory == False,
        )
        .order_by(WorkspaceFile.file_path.asc())
        .all()
    )

    kw_lower = keyword.lower()
    name_lower = name_kw.lower()
    hits: list[tuple[str, str]] = []  # (path, 描述)
    matched_paths: set[str] = set()

    # 第一遍：文件名匹配（快，不读内容）
    if name_lower:
        for f in files:
            if name_lower in f.file_path.lower():
                hits.append((f.file_path, "（文件名匹配）"))
                matched_paths.add(f.file_path)
                if len(hits) >= max_results:
                    break

    # 第二遍：内容匹配（仅未命中的文件，避免重复）
    if kw_lower and len(hits) < max_results:
        for f in files:
            if f.file_path in matched_paths:
                continue
            # 图片不做内容匹配：read_file 会把整份字节读进内存（下一行才切 512KB），
            # 而 decode(errors="replace") 只会产出 "- 聊天图片/a.png: 行 3: ▒▒▒" 这类
            # 乱码命中，既浪费 IO 又污染结果。文件名匹配那一遍仍保留图片。
            if workspace_service.is_image_path(f.file_path):
                continue
            try:
                raw = workspace_service.read_file(user_id, f.file_path, db)
            except Exception:
                continue
            if not raw:
                continue
            # 只搜前 512KB，避免大文件全量读取
            head = raw[: 512 * 1024]
            try:
                text = head.decode("utf-8", errors="replace")
            except Exception:
                continue
            lines = text.splitlines()
            for i, line in enumerate(lines, 1):
                if kw_lower in line.lower():
                    snippet = line.strip()[:120]
                    hits.append((f.file_path, f"行 {i}: {snippet}"))
                    if len(hits) >= max_results:
                        break
            if len(hits) >= max_results:
                break

    if not hits:
        return "未找到匹配的文件" + (f"（关键词: {keyword}）" if keyword else "")

    lines_out = [f"找到 {len(hits)} 个匹配："]
    for path, desc in hits:
        lines_out.append(f"- {path}: {desc}")
    result = "\n".join(lines_out)
    if len(result) > MAX_OUTPUT:
        result = result[:MAX_OUTPUT] + "\n\n...(搜索结果已截断)"
    return result


def _tool_delete_file(args: dict, user_id: int, db: Session) -> str:
    """删除工作区文件或目录"""
    path = args.get("path", "").strip()
    if not path:
        return "错误：缺少 path 参数"

    success = workspace_service.delete_file(user_id, path, db)
    if success:
        return f"已删除 '{path}'"
    else:
        return f"错误：文件或目录 '{path}' 不存在"


def _tool_rename_file(args: dict, user_id: int, db: Session) -> str:
    """重命名/移动工作区文件或目录（磁盘 + DB 记录同步）"""
    old_path = (args.get("old_path") or "").strip()
    new_path = (args.get("new_path") or "").strip()
    if not old_path or not new_path:
        return "错误：缺少 old_path 或 new_path 参数"

    # 源被禁止：改名等于给它换个可读的马甲，同样拒绝
    if _is_blocked_path(old_path) or _is_blocked_path(new_path):
        logger.warning(f"_tool_rename_file: 拒绝重命名被禁止的路径 '{old_path}' -> '{new_path}'")
        return "错误：出于安全原因，无法重命名该文件"

    try:
        ok, msg = workspace_service.rename_file(user_id, old_path, new_path, db)
    except Exception as e:
        return _sanitize_tool_error(f"错误：重命名失败：{e}", user_id, db)
    return msg if ok else f"错误：{msg}"


# ════════════════════════════════════════
#  Python 沙箱执行器 — subprocess + prlimit 方案
# ════════════════════════════════════════

# 内存 / 文件描述符上限是 SANDBOX_MEMORY_MB 与 SANDBOX_NOFILE，定义在文件顶部的
# 常量区（run_python 的工具描述要在 import 期用到内存数字，那里比 TOOLS 列表更早）。

# 允许的内置函数白名单（会被序列化为子进程脚本头部）
_SAFE_BUILTINS_SRC = """
import builtins as _b
import sys as _sys

# ── 受控 import 白名单 ──
# 只允许安全的标准库和文档生成库，禁止 os/sys/socket/subprocess/shutil 等危险模块
# 安全分层说明：
# - 纯字符串/计算类（markdown/yaml/jinja2/dateutil/json 等）：任意环境安全
# - 文件 IO 类（openpyxl/zipfile/pymupdf/PIL/pandas/numpy）：可打开任意路径，
#   依赖进程降权（SANDBOX_RUN_AS_USER）兜底；numpy 额外做了模块消毒
_ALLOWED_MODULES = frozenset({
    # 标准库 - 安全模块
    'json', 'math', 're', 'datetime', 'collections', 'io', 'itertools',
    'functools', 'hashlib', 'base64', 'string', 'random', 'copy',
    'decimal', 'fractions', 'statistics', 'textwrap', 'struct',
    'uuid', 'csv', 'unicodedata', 'operator', 'time',
    # 压缩与编码：zipfile/tarfile 之外最常见的出口（文件 IO 依赖降权）
    'zipfile', 'tarfile', 'gzip', 'bz2', 'lzma',
    # 文本 / 算法 / 结构（纯计算，零风险）
    'difflib', 'heapq', 'bisect', 'enum', 'contextlib', 'calendar',
    # 日期时区（zoneinfo 读镜像里的 tzdata；pytz 是 pandas 的依赖）
    'zoneinfo', 'pytz',
    # 标记与解析：html.escape / html.parser / xml.etree 是高频需求；
    # 实体扩展（XXE / billion laughs）在本环境下无外泄出口（断网），
    # 且读取范围早被 pandas/PIL 等白名单库打开，仍以进程降权为边界
    'html', 'xml', 'lxml',
    # 编码探测：读编码不明的 CSV 时靠它，随 reportlab 已在镜像里
    'charset_normalizer',
    # 版本号比较，随 matplotlib 已在镜像里
    'packaging',
    # 文档生成
    'reportlab', 'docx',
    # PPTX 生成 —— 补齐办公三件套（Excel→openpyxl / Word→docx / PDF→reportlab / PPTX→pptx）
    'pptx',
    # 图表 / 数值 / 图像 / 数据分析（文件 IO 依赖降权；numpy 已消毒）
    'matplotlib', 'numpy', 'PIL', 'pandas',
    # 办公增强（Excel / md / 模板 / 配置 / 日期）
    'openpyxl', 'markdown', 'yaml', 'jinja2', 'dateutil',
    # 中国法定节假日 / 工作日判断（dateutil+zoneinfo 不覆盖的本土语义，纯 Python）
    'chinese_calendar',
    # LLM 友好的纯文本表格（pandas to_markdown 的等宽对齐补充，纯 Python 零依赖）
    'tabulate',
    # PDF 处理（文件 IO，依赖降权）
    'pymupdf', 'fitz',
    # 代码风格高频库（纯计算/路径/临时文件；pathlib 文件 IO 依赖降权兜底）
    'typing', 'dataclasses', 'pathlib', 'glob', 'tempfile',
    # 自测：写测试→跑测试→修的闭环靠它（纯标准库，无 C 扩展，不必动镜像）
    'unittest',
})
# 刻意**不**放行的标准库（宁缺毋滥，别顺手加）：
#   sqlite3    enable_load_extension 是"加载原生库"的原语，沙箱用不着
#   secrets / shlex / configparser / array / zlib   已被 random/csv/yaml/numpy/zipfile 覆盖

# 明确禁止的高危模块（双重保险）
_BLOCKED_MODULES = frozenset({
    'os', 'sys', 'socket', 'subprocess', 'shutil', 'signal',
    'ctypes', 'multiprocessing', 'threading', 'asyncio',
    'importlib', 'builtins', 'gc', 'marshal', 'pickle',
    'ftplib', 'smtplib', 'telnetlib', 'urllib', 'http',
    'xmlrpc', 'webbrowser', 'pdb', 'pty', 'commands',
})

# ── 受限 os 替身模块 ──
# 模型生成代码时习惯性 `import os`（路径拼接、文件判断等），直接禁止会导致
# 每次工具调用都报 ImportError、白费一轮。这里提供安全子集：
# - os.path.join/basename/dirname/splitext/abspath/normpath/sep（纯字符串运算，不碰文件系统）
# - os.path.exists/isfile/isdir/getsize/getmtime、os.listdir/walk/stat（只读，全部过 _ws_abspath 边界：
#   越界时谓词返回 False、listdir/walk/getsize/stat 抛 ValueError，堵掉用绝对路径枚举工作区外目录）
# - os.getcwd（沙箱进程 cwd 就是用户工作区）
# - os.remove/unlink/rmdir/makedirs（仅限工作区内，路径边界 _ws_abspath 校验）
# - os.environ 为空 dict、os.getenv 返回默认值（不泄露服务器环境）
# 危险入口不提供（system/popen/chmod/chown/exec* 等），
# 访问时 __getattr__ 抛出友好提示，引导模型改用 read_ws / write_ws。
# 注意边界：本替身只管 os 这一个通道。builtins 里没有 open（用户代码只能走 read_ws/write_ws），
# 但白名单第三方库（glob/pathlib/pandas/PIL 等）用的是自己模块命名空间里的 io.open/os.open，
# 不经过沙箱 builtins，仍能打开任意路径 —— 那部分依赖进程降权兜底（见 _safe_import 注释）。
def _ws_abspath(p):
    # 工作区路径边界：相对路径基于工作区解析，越界抛 ValueError。
    base = os.path.abspath(_WS_DIR)
    full = os.path.abspath(os.path.join(base, p))
    if full != base and not full.startswith(base + os.sep):
        raise ValueError("只允许操作工作区内的文件")
    return full

def _ws_abspath_opt(p):
    # 同 _ws_abspath，但越界返回 None 而不抛异常 —— 供 exists/isfile/isdir 这类
    # 谓词使用：模型常写 `if os.path.exists(p)`，路径越界时应答 False，不该整段崩掉。
    try:
        return _ws_abspath(p)
    except ValueError:
        return None

# ── 只读入口的边界包装 ──
# 裸 os.listdir / os.walk / os.path.getsize 接受绝对路径，沙箱里可以直接枚举
# 工作区外的目录结构（listdir('D:/') 之类）。这几个包装把它们收敛回 _ws_abspath。
def _ws_listdir(path='.'):
    return os.listdir(_ws_abspath(path))

def _ws_walk(top='.', topdown=True, onerror=None, followlinks=False):
    # 只借 _ws_abspath 做校验，仍把"原始 top"交给 os.walk：这样相对路径进就相对
    # 路径出，保持原生语义；若传解析后的绝对路径，遍历结果会带上工作区真实路径，
    # 等于把它泄露进工具输出。
    _ws_abspath(top)
    return os.walk(top, topdown=topdown, onerror=onerror, followlinks=followlinks)

def _ws_getsize(p):
    return os.path.getsize(_ws_abspath(p))

def _ws_stat(p):
    return os.stat(_ws_abspath(p))

def _ws_getmtime(p):
    return os.path.getmtime(_ws_abspath(p))

def _ws_exists(p):
    full = _ws_abspath_opt(p)
    return full is not None and os.path.exists(full)

def _ws_isfile(p):
    full = _ws_abspath_opt(p)
    return full is not None and os.path.isfile(full)

def _ws_isdir(p):
    full = _ws_abspath_opt(p)
    return full is not None and os.path.isdir(full)

class _SafeOSModule:
    # 受限 os 替身：路径操作与只读遍历可用，危险入口给出友好提示。
    __name__ = 'os'
    __all__ = ['path', 'getcwd', 'listdir', 'walk', 'getenv', 'environ', 'sep',
               'remove', 'unlink', 'rmdir', 'makedirs', 'stat']
    path = type('_SafeOSPath', (), {
        'join': staticmethod(os.path.join), 'exists': staticmethod(_ws_exists),
        'basename': staticmethod(os.path.basename), 'dirname': staticmethod(os.path.dirname),
        'splitext': staticmethod(os.path.splitext), 'abspath': staticmethod(os.path.abspath),
        'normpath': staticmethod(os.path.normpath), 'isfile': staticmethod(_ws_isfile),
        'isdir': staticmethod(_ws_isdir), 'getsize': staticmethod(_ws_getsize),
        'getmtime': staticmethod(_ws_getmtime),
        'sep': os.sep,
    })()
    getcwd = staticmethod(os.getcwd)
    listdir = staticmethod(_ws_listdir)
    walk = staticmethod(_ws_walk)
    getenv = staticmethod(lambda key, default=None: default)
    environ = {}
    sep = os.sep
    stat = staticmethod(_ws_stat)
    # 工作区内文件操作（路径边界校验；沙箱内可清理自己写的临时文件）
    remove = staticmethod(lambda path: os.remove(_ws_abspath(path)))
    unlink = staticmethod(lambda path: os.unlink(_ws_abspath(path)))
    rmdir = staticmethod(lambda path: os.rmdir(_ws_abspath(path)))
    makedirs = staticmethod(lambda path, exist_ok=False: os.makedirs(_ws_abspath(path), exist_ok=exist_ok))

    def __getattr__(self, name):
        raise AttributeError(
            f"os.{name} 在沙箱中不可用（安全限制）。"
            "工作区文件读写请用 read_ws / write_ws；路径操作可用 os.path；"
            "删除/建目录可用 os.remove / os.makedirs（仅限工作区内）。"
        )


_SAFE_OS_MODULE = _SafeOSModule()

def _configure_matplotlib():
    # matplotlib 惰性初始化：Agg 后端 + 中文字体（仅在用户首次 import matplotlib 时执行）
    # 用 _b.__import__ 直接加载，避免经过 _safe_import 造成递归
    try:
        _mpl = _b.__import__('matplotlib')
        _mpl.use('Agg')  # 无 GUI 后端，必须在 import pyplot 之前设置
        _mpl.rcParams['font.sans-serif'] = ['Microsoft YaHei', 'SimHei', 'SimSun', 'WenQuanYi Micro Hei', 'Noto Sans CJK SC', 'DejaVu Sans']
        _mpl.rcParams['axes.unicode_minus'] = False
        _mpl.rcParams['figure.dpi'] = 150
    except Exception:
        pass

def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    # Controlled __import__ - only allow whitelisted modules
    # 取顶层模块名（如 'reportlab.pdfgen' → 'reportlab'）
    # 说明：文件 IO 类白名单库（io.open / openpyxl / PIL / pandas 等）可打开任意路径，
    # 依赖进程降权（SANDBOX_RUN_AS_USER）兜底；numpy 额外做模块消毒（见下）。
    top = name.split('.')[0] if name else ''
    # 受限 os 替身：模型生成代码常习惯性 import os（路径拼接/文件判断），
    # 提供安全子集避免每次都因 ImportError 浪费一轮工具调用。
    # 模拟标准 __import__ 语义：`import os.path`（fromlist 为空）返回顶层 os；
    # `from os.path import join`（fromlist 非空）返回深层子模块 os.path。
    if top == 'os':
        if fromlist:
            mod = _SAFE_OS_MODULE
            for part in name.split('.')[1:]:
                mod = getattr(mod, part)
            return mod
        return _SAFE_OS_MODULE
    if top in _BLOCKED_MODULES:
        raise ImportError(f"模块 '{name}' 被禁止导入（安全限制）")
    if top not in _ALLOWED_MODULES:
        # 清单从 _ALLOWED_MODULES 现算，**不再手写副本**：此前两处各写一份，
        # 加模块时必漏一处（lxml 就是这么被漏掉的）
        raise ImportError(
            f"模块 '{name}' 不在允许列表中。可用模块: "
            + ", ".join(sorted(_ALLOWED_MODULES))
        )
    # ── fitz → pymupdf 别名 ──
    # PyMuPDF 的 fitz 是弃用 shim，import 时会把一行 warning 写到 **stdout**，
    # 而 stdout 会原样进 LLM 上下文（实测：代码只写 import fitz 时，工具结果就是
    # 那行 warning）。模型习惯写 fitz，所以这里直接指向 pymupdf：既消掉告警，
    # 也对"未来 PyMuPDF 移除该 shim"免疫。
    if top == 'fitz':
        return _b.__import__('pymupdf', globals, locals, fromlist, level)
    # ── matplotlib 惰性配置：首次 import 时初始化（不画图的代码完全不加载）──
    if top == 'matplotlib' and not _sys.modules.get('matplotlib'):
        _configure_matplotlib()
    mod = _b.__import__(name, globals, locals, fromlist, level)
    # ── numpy 消毒：移除任意文件 IO 入口（fromfile/loadtxt 等），matplotlib 不受影响 ──
    # 注意：仅对顶层 `import numpy` 消毒一次；文件读写仍可能经 pandas/PIL 等，由进程降权兜底
    if top == 'numpy' and name == 'numpy':
        for attr in ('fromfile', 'loadtxt', 'genfromtxt', 'load', 'save',
                     'savetxt', 'savez', 'savez_compressed', 'memmap'):
            if hasattr(mod, attr):
                try:
                    setattr(mod, attr, None)
                except Exception:
                    pass
        try:
            mod.ndarray.tofile = None
        except Exception:
            pass
    return mod

_SAFE_BUILTINS = {
    # 基本类型
    "bool": bool, "int": int, "float": float, "str": str, "bytes": bytes,
    "list": list, "tuple": tuple, "dict": dict, "set": set, "frozenset": frozenset,
    # bytearray 放行、memoryview 不放行：前者只是个可变字节缓冲，没有 I/O 也没有
    # 内省能力（bytes 已在白名单里，能做的事它都能做）；后者能包住任意对象的底层
    # 缓冲区，是绕过封装的通道，所以 test_safe_builtins_excludes_dangerous 明令禁止。
    "bytearray": bytearray,
    # 数学
    "abs": abs, "round": round, "min": min, "max": max, "sum": sum,
    "pow": pow, "divmod": divmod, "bin": bin, "oct": oct, "hex": hex,
    # 迭代
    "range": range, "enumerate": enumerate, "zip": zip, "map": map,
    "filter": filter, "reversed": reversed, "sorted": sorted, "iter": iter, "next": next,
    "any": any, "all": all,
    # 类型检查
    "type": type, "isinstance": isinstance, "issubclass": issubclass,
    "callable": callable, "hasattr": hasattr, "getattr": getattr, "setattr": setattr,
    "id": id, "repr": repr, "ascii": ascii, "format": format,
    # 字符串/编码
    "chr": chr, "ord": ord, "len": len,
    # 类定义支持（class 语句需要 __build_class__；super/staticmethod 等常用）
    "__build_class__": _b.__build_class__,
    "object": object, "super": super,
    "staticmethod": staticmethod, "classmethod": classmethod, "property": property,
    # 常量
    "True": True, "False": False, "None": None,
    # 异常（允许 try/except）
    "Exception": Exception, "ValueError": ValueError, "TypeError": TypeError,
    "KeyError": KeyError, "IndexError": IndexError, "ZeroDivisionError": ZeroDivisionError,
    "StopIteration": StopIteration, "AttributeError": AttributeError,
    "RuntimeError": RuntimeError, "AssertionError": AssertionError,
    "FileNotFoundError": FileNotFoundError, "ImportError": ImportError,
    "ModuleNotFoundError": ModuleNotFoundError,
    # print（输出捕获）
    "print": print,
    # 受控 import
    "__import__": _safe_import,
}
"""


def _build_sandbox_script(code: str, workspace_dir: str, skill_dirs: dict | None = None, quota_used: int = 0, skill_bounds: dict | None = None, memory_mb: int = SANDBOX_MEMORY_MB) -> str:
    """构建子进程执行的完整 Python 脚本。

    脚本结构：
    1. 内置函数白名单（禁用 import、open、exec 等）
    2. read_ws / write_ws 函数（通过文件路径访问工作区，含严格路径边界 + 配额检查）
    3. read_skill_file_ws 函数（读取 skill 目录中的参考文件）
    4. 用户代码（在受限命名空间中 exec）
    5. 异常捕获（输出友好的错误信息，不泄露服务器路径）

    Args:
        code: 用户 Python 代码
        workspace_dir: 用户工作区目录路径
        skill_dirs: {skill_name: dir_path} 映射，用于 read_skill_file_ws。
                     为 None 或空时，read_skill_file_ws 不可用。
        quota_used: 执行前工作区已用字节数（来自数据库），用于沙箱内配额检查
        skill_bounds: {skill_name: pack_root} 映射，技能包（pack）技能的
                     读取边界放宽到包根（允许 ../ 访问共享资源）；
                     无该映射的技能按自身目录为边界。
        memory_mb: 写进脚本文案的有效内存上限。docker 后端必须传
                     settings.SANDBOX_DOCKER_MEMORY_MB（B 的 --memory），否则容器里
                     真触发 MemoryError 时会向用户报一个错的数字。默认值等于 local
                     的 RLIMIT_AS，所以既有调用方与 golden 矩阵的行为不变。
    """
    # 对用户代码做基本转义，防止与脚本模板冲突
    # 用 repr() 确保代码字符串被安全引用
    code_repr = repr(code)

    # 安全地嵌入 skill 目录映射
    skill_dirs_repr = repr(skill_dirs) if skill_dirs else repr({})
    skill_bounds_repr = repr(skill_bounds) if skill_bounds else repr({})

    return f"""# -*- coding: utf-8 -*-
# Auto-generated sandbox script - DO NOT EDIT
# Generated by tool_registry._tool_run_python

import sys
import os
import io
import contextlib
import hashlib
import datetime
import mimetypes
import traceback

# ── 1. 内置函数白名单 ──
{_SAFE_BUILTINS_SRC}

# ── 2. 工作区函数 ──
_WS_DIR = {repr(workspace_dir)}

# ── 2c. Skill 目录映射（用于 read_skill_file_ws）──
_SKILL_DIRS = {skill_dirs_repr}
_SKILL_BOUNDS = {skill_bounds_repr}

# ── 配额常量（跨平台硬限制，不依赖 RLIMIT）──
_WS_MAX_FILE_SIZE = {workspace_service.MAX_FILE_SIZE}
_WS_QUOTA_LIMIT = {workspace_service.MAX_WORKSPACE_SIZE}
_WS_QUOTA_USED = {quota_used}          # 执行前工作区已用字节数（来自数据库）
_WS_WRITTEN_BYTES = 0                  # 本次执行累计写入字节数
_WS_WRITTEN_FILES = []                 # 本次执行写入的文件清单（主进程据此只同步这些文件）

# ── 2a. 纯 Python PDF 文本提取器（不依赖 import） ──
def _extract_pdf_text_simple(raw_bytes):
    \"\"\"从 PDF 字节流中提取文本（纯 Python 实现，不依赖 PyPDF2）。

    PDF 文本通常存储在 BT...ET 块中的 Tj/TJ 操作符里。
    这个简易提取器解析 PDF 内容流中的文本操作符。
    \"\"\"
    import re
    # PDF 文本可能用 FlateDecode 压缩，尝试解压
    texts = []
    try:
        # 方式1：寻找未压缩的文本对象 (Tj / TJ)
        # 匹配 (text) Tj 或 [...] TJ 模式
        content = raw_bytes
        # 尝试查找所有 stream...endstream 块
        stream_pattern = rb'stream\\r?\\n(.*?)\\r?\\nendstream'
        for match in re.finditer(stream_pattern, content, re.DOTALL):
            stream_data = match.group(1)
            # 尝试 zlib 解压（FlateDecode）
            decompressed = None
            try:
                import zlib
                decompressed = zlib.decompress(stream_data)
            except Exception:
                decompressed = stream_data
            # 从解压后的数据中提取文本
            # 匹配 (text) Tj 模式
            for text_match in re.finditer(rb'\\(([^)]*)\\)\\s*Tj', decompressed):
                raw = text_match.group(1)
                try:
                    texts.append(raw.decode('latin-1'))
                except Exception:
                    pass
            # 匹配 [(text)...] TJ 模式
            for tj_match in re.finditer(rb'\\[(.*?)\\]\\s*TJ', decompressed, re.DOTALL):
                inner = tj_match.group(1)
                for text_match in re.finditer(rb'\\(([^)]*)\\)', inner):
                    raw = text_match.group(1)
                    try:
                        texts.append(raw.decode('latin-1'))
                    except Exception:
                        pass
    except Exception:
        pass

    # 清理转义字符
    result = ' '.join(texts)
    # PDF 转义字符替换
    escapes = {{
        '\\\\n': '\\n', '\\\\r': '\\n', '\\\\t': '\\t',
        '\\\\(': '(', '\\\\)': ')', '\\\\\\\\': '\\\\',
    }}
    for esc, char in escapes.items():
        result = result.replace(esc, char)
    return result

def _is_within(base, target):
    \"\"\"严格检查 target 是否位于 base 目录内。

    使用 commonpath 做目录边界判断，修复 startswith 前缀匹配绕过
    （例如 '../11/x.txt' resolve 后以 'workspaces/1' 开头会被误判为目录内）。
    \"\"\"
    try:
        base_r = os.path.realpath(base)
        full_r = os.path.realpath(target)
        return os.path.commonpath([base_r, full_r]) == base_r
    except ValueError:
        return False

def _ws_check_write(path, size):
    \"\"\"写入前统一检查：单文件大小 + 累计配额（跨平台生效，不依赖 RLIMIT）。\"\"\"
    global _WS_WRITTEN_BYTES
    if size > _WS_MAX_FILE_SIZE:
        raise ValueError(
            f"文件超过单文件大小限制（{{_WS_MAX_FILE_SIZE // (1024 * 1024)}}MB）"
        )
    if _WS_QUOTA_USED + _WS_WRITTEN_BYTES + size > _WS_QUOTA_LIMIT:
        raise ValueError(
            f"工作区总大小超过限制（{{_WS_QUOTA_LIMIT // (1024 * 1024)}}MB），写入已拒绝"
        )
    _WS_WRITTEN_BYTES += size

def read_ws(path, binary=False):
    \"\"\"读取工作区文件。

    默认返回文本字符串；binary=True 时返回原始 bytes（图片/PPTX/PDF 等二进制装配用）。
    \"\"\"
    full = os.path.join(_WS_DIR, path.replace("\\\\", "/").lstrip("/"))
    # 防止目录穿越（严格目录边界判断）
    if not _is_within(_WS_DIR, full):
        raise ValueError("非法路径")
    if not os.path.isfile(full):
        raise FileNotFoundError(f"文件 '{{path}}' 不存在")
    with open(full, "rb") as f:
        data = f.read()
    # 二进制模式：直接返回 bytes（python-pptx 加图、PIL 拼接等需要原始字节）
    if binary:
        return data
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    # PDF 文件：尝试纯 Python 提取文本
    if path.lower().endswith(".pdf"):
        text = _extract_pdf_text_simple(data)
        if text.strip():
            return text
        raise ValueError(f"文件 '{{path}}' 是 PDF 但无法提取文本（可能是扫描件）")
    raise ValueError(f"文件 '{{path}}' 是二进制文件，请用 read_ws(path, binary=True) 读取")

def write_ws(path, content):
    \"\"\"写入工作区文件（含严格路径边界 + 配额检查）\"\"\"
    rel = path.replace("\\\\", "/").lstrip("/")
    data = content.encode("utf-8") if isinstance(content, str) else content
    # 统一配额检查（单文件大小 + 本次累计 + 工作区已用量）
    _ws_check_write(rel, len(data))
    full = os.path.join(_WS_DIR, rel)
    # 防止目录穿越（严格目录边界判断）
    if not _is_within(_WS_DIR, full):
        raise ValueError("非法路径")
    os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
    with open(full, "wb") as f:
        f.write(data)
    _WS_WRITTEN_FILES.append(rel)
    print(f"已写入: {{path}} ({{len(data)}} 字节)")

def read_skill_file_ws(skill_name, file_path):
    \"\"\"读取 skill 目录中的参考文件。

    Args:
        skill_name: skill 名称（如 'guizang-ppt-skill'）
        file_path: 相对于 skill 目录的路径（如 'assets/template.html'）。
                   技能包（pack）技能还支持 '../assets/x.html' 和
                   '@pack/assets/x.html'（边界为包根）

    Returns:
        文件文本内容（UTF-8）

    Raises:
        FileNotFoundError: skill 不存在或文件不存在
        ValueError: 非法路径
    \"\"\"
    if not _SKILL_DIRS:
        raise FileNotFoundError("没有可用的 skill 目录")
    if skill_name not in _SKILL_DIRS:
        available = ', '.join(_SKILL_DIRS.keys())
        raise FileNotFoundError(f"skill '{{skill_name}}' 不存在。可用: {{available}}")
    base = _SKILL_DIRS[skill_name]
    # 读取边界：pack 技能为包根（允许 ../），独立技能为自身目录
    boundary = _SKILL_BOUNDS.get(skill_name) or base
    # 安全拼接路径
    rel = file_path.replace("\\\\", "/").lstrip("/")
    if rel.startswith("@pack/"):
        rel = rel[len("@pack/"):]
        if not boundary or boundary == base:
            raise ValueError("该 skill 不属于技能包，不支持 @pack/ 路径")
        full = os.path.join(boundary, rel)
    else:
        full = os.path.join(base, rel)
    # 防止目录穿越（严格边界判断：boundary 为 base 或包根）
    if not _is_within(boundary, full):
        raise ValueError("非法路径")
    if not os.path.isfile(full):
        raise FileNotFoundError(f"文件 '{{file_path}}' 在 skill '{{skill_name}}' 中不存在")
    with open(full, "rb") as f:
        data = f.read()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return data.decode("latin-1")
        except Exception:
            raise ValueError(f"文件 '{{file_path}}' 是二进制文件，无法以文本形式读取")

# ── 2b. 文档生成辅助函数 ──
# 将纯文本转为 PDF/Word 二进制并写入工作区
# 用户代码可以直接调用 generate_pdf_ws(path, text) / generate_docx_ws(path, text)

# 注册中文字体（reportlab 内置 Helvetica 不支持中文）
_CN_FONT_NAME = None
_CN_FONT_BOLD = None
_MONO_FONT = None

def _get_cn_font_name():
    global _CN_FONT_NAME, _CN_FONT_BOLD, _MONO_FONT
    if _CN_FONT_NAME is not None:
        return _CN_FONT_NAME, _CN_FONT_BOLD, _MONO_FONT
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    # 正文候选 (TTC 需指定 subfontIndex)
    body_cands = [
        ('C:/Windows/Fonts/msyh.ttc', 'MSYH', 0),
        ('C:/Windows/Fonts/simhei.ttf', 'SimHei', None),
        ('C:/Windows/Fonts/simsun.ttc', 'SimSun', 0),
        ('/usr/share/fonts/truetype/wqy/wqy-microhei.ttc', 'WQYMicroHei', 0),
    ]
    bold_cands = [
        ('C:/Windows/Fonts/msyhb.ttc', 'MSYH-Bold', 0),
        ('C:/Windows/Fonts/simhei.ttf', 'SimHei-Bold', None),
        ('C:/Windows/Fonts/msyh.ttc', 'MSYH-Bold', 1),
    ]
    mono_cands = [
        ('C:/Windows/Fonts/consola.ttf', 'Consolas', None),
        ('C:/Windows/Fonts/cour.ttf', 'CourierNew', None),
        ('/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf', 'DejaMono', None),
    ]
    def _try(path, name, idx):
        try:
            if idx is not None:
                pdfmetrics.registerFont(TTFont(name, path, subfontIndex=idx))
            else:
                pdfmetrics.registerFont(TTFont(name, path))
            return True
        except Exception:
            return False
    def _scan(keys, reg_name):
        # 硬编码路径覆盖不到所有发行版：Debian/Ubuntu 是 /usr/share/fonts/truetype/<family>/，
        # RHEL/CentOS 系是 /usr/share/fonts/<family>-fonts/ 或 google-noto-cjk/，上面那两条
        # Linux 候选在 RHEL 系一条都不存在 → 正文退到 Helvetica、等宽退到 Courier，两个都是
        # Type1 WinAnsi 字体、没有 CJK 字形，中文整段丢字（A 就是 CentOS 9，一直是这个状态，
        # 而字体文件其实**装在机器上**，只是代码不认识那个路径）。
        # 与其继续往列表里堆路径（猜错一条等于没修），不如按**文件名**认字体族。
        # 只在硬编码候选全部落空时才走到这里，所以 Windows（msyh 第一条就命中）与沙箱镜像
        # （Debian 路径命中）的行为可证不变。
        found = []
        for dirpath, _dirs, files in os.walk('/usr/share/fonts'):
            for fn in files:
                low = fn.lower()
                if low.endswith(('.ttf', '.ttc')):
                    found.append((low, os.path.join(dirpath, fn)))
        def _rank(item):
            low = item[0]
            # 同族里 Regular 优先。只挡 bold/italic 是不够的：CentOS 9 的 google-noto-cjk
            # 一个目录里就并排着 Black/Bold/DemiLight/Light/Medium/Regular/Thin 七个字重，
            # 按路径字母序 Black 排第一 —— 不压字重就会拿最粗的那面当正文。
            weight = any(w in low for w in ('bold', 'italic', 'oblique', 'black', 'heavy',
                                            'medium', 'light', 'thin', 'condensed'))
            return ('regular' not in low, weight, item[1])
        # keys 是**优先级顺序**，不是集合：一台机器上常并存多个 CJK 字体族，
        # 一把 sorted() 就等于让**字母序**决定选谁（'google-droid-' 排在
        # 'google-noto-' 前面），而字母序与字形质量、轮廓格式都无关。
        for key in keys:
            for _low, p in sorted([it for it in found if key in it[0]], key=_rank):
                # .ttc 是多字体集合，reportlab 必须给 subfontIndex，0 号面就是主字体
                if _try(p, reg_name, 0 if p.lower().endswith('.ttc') else None):
                    return reg_name
        return None
    # 扫描用的字体族关键字，按**优先级**排列（对小写文件名做子串匹配）。
    # **droidsansfallback 已移除**（2026-09-08，实测后决定）。它是 stock CentOS 9 上
    # 唯一能被 reportlab 用的中文字体，但它的 ASCII 字形全是 glyph 0 —— 中文正常、
    # **数字静默变空白**。实测另一条落空路径（退回 Helvetica）恰好相反：汉字渲染成
    # .notdef（抽回文本是一串 "I"）、数字与 ASCII 正确。两者都**不抛异常** ——
    # reportlab 对 Type1 WinAnsi 字体碰 CJK 是静默丢字，不是硬报错。
    # 之所以选"中文丢字 + 出声告警"而不是"数字静默丢失"：报告里少了数字是看不出来的，
    # 而满屏 .notdef 一眼就知道坏了。装好 wqy-microhei-fonts 后两条路径都不会走到
    # （沙箱镜像与 §3.5 之后的 A 都装了 wqy，cjk_keys 第一条就命中）。
    cjk_keys = ('wqy', 'notosanssc', 'notosanscjk', 'sourcehansanssc', 'sourcehansans',
                'simhei', 'simsun', 'msyh', 'sourcehanserif', 'notoserifcjk',
                'uming', 'ukai')
    mono_keys = ('dejavusansmono', 'liberationmono', 'notosansmono', 'nimbusmono', 'freemono')
    for p, n, i in body_cands:
        if _try(p, n, i):
            _CN_FONT_NAME = n
            break
    else:
        _CN_FONT_NAME = _scan(cjk_keys, 'CNScanned')
        if _CN_FONT_NAME is None:
            # **必须走 stdout**：_merge_streams 是"stdout 优先，stdout 为空才退到
            # stderr"，而 generate_pdf_ws 末尾总会打一行"已生成 PDF"，
            # 写 stderr 的告警会被整条丢掉。
            print("警告：未找到可用的中文字体，本次生成的 PDF 里中文会丢失；"
                  "需在宿主安装 TrueType 中文字体（CentOS: dnf install wqy-microhei-fonts）")
            _CN_FONT_NAME = 'Helvetica'
    for p, n, i in bold_cands:
        if _try(p, n, i):
            _CN_FONT_BOLD = n
            break
    else:
        _CN_FONT_BOLD = _CN_FONT_NAME
    for p, n, i in mono_cands:
        if _try(p, n, i):
            _MONO_FONT = n
            break
    else:
        _MONO_FONT = _scan(mono_keys, 'MonoScanned') or 'Courier'
    return _CN_FONT_NAME, _CN_FONT_BOLD, _MONO_FONT

def generate_pdf_ws(path, text):
    # Generate professionally laid-out PDF from Markdown text
    import io as _io, re as _re, html as _html
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm as _mm
    from reportlab.lib.colors import HexColor as _HC
    from reportlab.lib.styles import ParagraphStyle as _PS
    from reportlab.lib.enums import TA_CENTER as _TC, TA_JUSTIFY as _TJ
    from reportlab.platypus import (
        SimpleDocTemplate as _SDD, Paragraph as _P, Spacer as _SP,
        Table as _TBL, TableStyle as _TS, HRFlowable as _HR,
        ListFlowable as _LF, ListItem as _LI,
    )
    from reportlab.platypus.flowables import Flowable as _FW

    cn, cn_b, mono = _get_cn_font_name()
    C_T = _HC('#1a1a2e'); C_H2 = _HC('#16213e'); C_H3 = _HC('#0f3460')
    C_BODY = _HC('#2c2c2c'); C_MUTED = _HC('#666666'); C_ACC = _HC('#0f3460')
    C_CBG = _HC('#f5f5f5'); C_QBG = _HC('#f0f4ff'); C_QBD = _HC('#0f3460')
    C_THDR = _HC('#0f3460'); C_TROW = _HC('#f8f9fa'); C_TB = _HC('#dee2e6')
    C_HR = _HC('#cccccc')

    ss = {{
        'title': _PS('T', fontName=cn_b, fontSize=20, leading=28, textColor=C_T, spaceAfter=6*_mm, alignment=_TC),
        'h2': _PS('H2', fontName=cn_b, fontSize=16, leading=22, textColor=C_H2, spaceBefore=8*_mm, spaceAfter=3*_mm),
        'h3': _PS('H3', fontName=cn_b, fontSize=13, leading=18, textColor=C_H3, spaceBefore=5*_mm, spaceAfter=2*_mm),
        'h4': _PS('H4', fontName=cn_b, fontSize=11.5, leading=16, textColor=_HC('#333'), spaceBefore=4*_mm, spaceAfter=1.5*_mm),
        'body': _PS('B', fontName=cn, fontSize=10.5, leading=16, textColor=C_BODY, spaceAfter=2*_mm, alignment=_TJ),
        'bullet': _PS('BL', fontName=cn, fontSize=10.5, leading=15, textColor=C_BODY, leftIndent=8*_mm, spaceAfter=1*_mm),
        'ordered': _PS('OL', fontName=cn, fontSize=10.5, leading=15, textColor=C_BODY, leftIndent=8*_mm, spaceAfter=1*_mm),
        'code': _PS('CB', fontName=mono, fontSize=9, leading=13, textColor=C_BODY, leftIndent=3*_mm, rightIndent=3*_mm, spaceBefore=2*_mm, spaceAfter=2*_mm),
        'quote': _PS('Q', fontName=cn, fontSize=10, leading=15, textColor=C_MUTED, leftIndent=8*_mm, rightIndent=4*_mm, spaceBefore=2*_mm, spaceAfter=2*_mm, backColor=C_QBG, borderColor=C_QBD, borderWidth=0, borderPadding=6),
        'tcell': _PS('TC', fontName=cn, fontSize=9.5, leading=13, textColor=C_BODY),
        'thdr': _PS('TH', fontName=cn_b, fontSize=9.5, leading=13, textColor=_HC('#ffffff')),
    }}

    def _pi(t):
        # 注意 \\1 / \\2 的双反斜杠：本函数整体位于 _build_sandbox_script 的**非 raw**
        # 三引号模板字符串里，写单反斜杠的 \1 是合法八进制转义，会被外层字符串先吃成
        # chr(1)，导致生成脚本里的 re.sub 替换模板变成 '<b>' + SOH + '</b>'——
        # 反向引用失效，**被捕获的文字整段丢失**，PDF 里只留下 NUL 字节。
        # 同段的 \\* \\[ \\] \\( \\) \\s \\d 等是**非法**转义，外层原样保留 —— 但
        # CPython 会为每一处报 SyntaxWarning，所以这里也一律写双反斜杠：除本段注释
        # 外生成脚本逐字节不变，警告归零。动这一段之后必须比对生成脚本（tests 的
        # golden 回归是兜底，不是替代品）。
        t = _html.escape(t)
        t = _re.sub(r'`([^`]+)`', r'<font name="' + mono + r'" color="#d63384">\\1</font>', t)
        t = _re.sub(r'\\*\\*([^*]+)\\*\\*', r'<b>\\1</b>', t)
        t = _re.sub(r'(?<!\\*)\\*([^*]+)\\*(?!\\*)', r'<i>\\1</i>', t)
        t = _re.sub(r'\\[([^\\]]+)\\]\\(([^)]+)\\)', r'<link href="\\2">\\1</link>', t)
        return t

    class _CBF(_FW):
        def __init__(self, code, style, mw):
            super().__init__(); self.code=code; self.style=style; self.mw=mw
            self.lines=code.split('\\n'); self.lh=style.leading; self.pad=6
            self.width=mw; self.height=len(self.lines)*self.lh+self.pad*2
        def wrap(self, aw, ah):
            self.width=aw; return self.width, self.height
        def draw(self):
            c=self.canv; c.setFillColor(C_CBG); c.roundRect(0,0,self.width,self.height,3,fill=1,stroke=0)
            c.setFillColor(C_ACC); c.rect(0,0,3,self.height,fill=1,stroke=0)
            c.setFillColor(C_BODY); c.setFont(self.style.fontName, self.style.fontSize)
            y=self.height-self.pad-self.style.fontSize*0.8
            for ln in self.lines:
                d = ln if len(ln)<=100 else ln[:97]+'...'
                c.drawString(self.pad+3, y, d); y -= self.lh

    pw, ph = A4; mg = 18*_mm; mw = pw - mg*2
    story = []; lines = text.split('\\n'); i = 0; n = len(lines)
    while i < n:
        s = lines[i].strip()
        if not s:
            story.append(_SP(1, 2*_mm)); i += 1; continue
        if _re.match(r'^---+\\s*$', s) or _re.match(r'^\\*\\*\\*+\\s*$', s):
            story.append(_SP(1,1*_mm)); story.append(_HR(width='100%', thickness=0.5, color=C_HR)); story.append(_SP(1,1*_mm)); i += 1; continue
        m = _re.match(r'^(#{{1,4}})\\s+(.+)$', s)
        if m:
            lv=len(m.group(1)); ct=_pi(m.group(2))
            key={{1:'title',2:'h2',3:'h3',4:'h4'}}[lv]
            story.append(_P(ct, ss[key]))
            if lv<=2: story.append(_SP(1,1*_mm))
            i += 1; continue
        if s.startswith('```'):
            cl=[]; i+=1
            while i<n and not lines[i].strip().startswith('```'):
                cl.append(lines[i]); i+=1
            i+=1
            story.append(_CBF('\\n'.join(cl), ss['code'], mw)); continue
        if s.startswith('>'):
            ql=[]
            while i<n and lines[i].strip().startswith('>'):
                q=lines[i].strip()
                ql.append(q[2:] if q.startswith('> ') else q[1:]); i+=1
            story.append(_P(_pi('\\n'.join(ql)), ss['quote'])); continue
        if '|' in s and i+1<n and _re.match(r'^[\\s|:-]+$', lines[i+1].strip()) and '|' in lines[i+1]:
            tr=[]
            while i<n and '|' in lines[i].strip():
                rl=lines[i].strip()
                if not rl: break
                cs=[c.strip() for c in rl.split('|')]
                if cs and cs[0]=='': cs=cs[1:]
                if cs and cs[-1]=='': cs=cs[:-1]
                tr.append(cs); i+=1
            if len(tr)>=2:
                hdr=tr[0]; drs=tr[2:]
                hc=[_P(_pi(c), ss['thdr']) for c in hdr]
                bc=[[_P(_pi(c), ss['tcell']) for c in r[:len(hdr)]] for r in drs]
                cc=len(hdr); cw=mw/cc
                td=[hc]+bc
                tb=_TBL(td, colWidths=[cw]*cc)
                tb.setStyle(_TS([
                    ('BACKGROUND',(0,0),(-1,0),C_THDR),
                    ('TEXTCOLOR',(0,0),(-1,0),_HC('#ffffff')),
                    ('FONTNAME',(0,0),(-1,0),cn_b),
                    ('FONTSIZE',(0,0),(-1,-1),9.5),
                    ('BOTTOMPADDING',(0,0),(-1,0),6),('TOPPADDING',(0,0),(-1,0),6),
                    ('ROWBACKGROUNDS',(0,1),(-1,-1),[_HC('#ffffff'),C_TROW]),
                    ('GRID',(0,0),(-1,-1),0.5,C_TB),
                    ('VALIGN',(0,0),(-1,-1),'MIDDLE'),
                    ('LEFTPADDING',(0,0),(-1,-1),6),('RIGHTPADDING',(0,0),(-1,-1),6),
                    ('TOPPADDING',(0,1),(-1,-1),4),('BOTTOMPADDING',(0,1),(-1,-1),4),
                ]))
                story.append(_SP(1,1*_mm)); story.append(tb); story.append(_SP(1,2*_mm))
            continue
        if _re.match(r'^[-*+]\\s+', s):
            its=[]
            while i<n and _re.match(r'^[-*+]\\s+', lines[i].strip()):
                its.append(_P(_pi(_re.sub(r'^[-*+]\\s+','',lines[i].strip())), ss['bullet'])); i+=1
            story.append(_LF([_LI(it, leftIndent=6*_mm, bulletColor=C_ACC) for it in its], bulletType='bullet', bulletFontName=cn, bulletFontSize=8, start='•', leftIndent=4*_mm))
            story.append(_SP(1,1*_mm)); continue
        if _re.match(r'^\\d+\\.\\s+', s):
            its=[]
            while i<n and _re.match(r'^\\d+\\.\\s+', lines[i].strip()):
                its.append(_P(_pi(_re.sub(r'^\\d+\\.\\s+','',lines[i].strip())), ss['ordered'])); i+=1
            story.append(_LF([_LI(it, leftIndent=6*_mm) for it in its], bulletType='1', bulletFontName=cn, bulletFontSize=10.5, leftIndent=4*_mm))
            story.append(_SP(1,1*_mm)); continue
        # 普通段落
        pl=[s]; i+=1
        while i<n:
            nl=lines[i].strip()
            if (not nl or nl.startswith('#') or nl.startswith('```') or nl.startswith('>') or
                nl.startswith('- ') or nl.startswith('* ') or nl.startswith('+ ') or
                _re.match(r'^\\d+\\.\\s+', nl) or _re.match(r'^---+\\s*$', nl)):
                break
            pl.append(nl); i+=1
        story.append(_P(_pi(' '.join(pl)), ss['body']))

    buf = _io.BytesIO()
    doc = _SDD(buf, pagesize=A4, leftMargin=mg, rightMargin=mg, topMargin=mg, bottomMargin=mg)
    def _pg(c, d):
        c.saveState(); c.setFont(cn, 8); c.setFillColor(C_MUTED)
        c.drawCentredString(pw/2, 10*_mm, '第 %d 页' % d.page)
        c.setStrokeColor(C_HR); c.setLineWidth(0.3)
        c.line(mg, ph-mg+5*_mm, pw-mg, ph-mg+5*_mm)
        c.restoreState()
    doc.build(story, onFirstPage=_pg, onLaterPages=_pg)
    pdf_data = buf.getvalue()

    # 写入工作区（严格路径边界 + 配额检查）
    rel = path.replace("\\\\", "/").lstrip("/")
    _ws_check_write(rel, len(pdf_data))
    full = os.path.join(_WS_DIR, rel)
    if not _is_within(_WS_DIR, full):
        raise ValueError("非法路径")
    os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
    with open(full, "wb") as f:
        f.write(pdf_data)
    _WS_WRITTEN_FILES.append(rel)
    print(f"已生成 PDF: {{path}} ({{len(pdf_data)}} 字节)")

def generate_docx_ws(path, text):
    # Generate Word .docx from text (supports Markdown headings/lists) and write to workspace
    from docx import Document as _Doc
    from docx.shared import Pt as _Pt
    from docx.enum.text import WD_ALIGN_PARAGRAPH as _ALIGN
    import io as _io

    doc = _Doc()

    for line in text.split('\\n'):
        trimmed = line.strip()

        if not trimmed:
            doc.add_paragraph('')
            continue

        # Markdown 标题
        heading_match = None
        for prefix, level in [('# ', 1), ('## ', 2), ('### ', 3), ('#### ', 4)]:
            if trimmed.startswith(prefix):
                heading_match = (trimmed[len(prefix):], level)
                break
        if heading_match:
            content, level = heading_match
            h = doc.add_heading(content, level=level)
            continue

        # Markdown 列表项
        if trimmed.startswith('- ') or trimmed.startswith('* '):
            doc.add_paragraph(trimmed[2:], style='List Bullet')
            continue

        # 普通段落
        doc.add_paragraph(line)

    buf = _io.BytesIO()
    doc.save(buf)
    docx_data = buf.getvalue()

    # 写入工作区（严格路径边界 + 配额检查）
    rel = path.replace("\\\\", "/").lstrip("/")
    _ws_check_write(rel, len(docx_data))
    full = os.path.join(_WS_DIR, rel)
    if not _is_within(_WS_DIR, full):
        raise ValueError("非法路径")
    os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
    with open(full, "wb") as f:
        f.write(docx_data)
    _WS_WRITTEN_FILES.append(rel)
    print(f"已生成 Word: {{path}} ({{len(docx_data)}} 字节)")

# ── 3. 用户代码执行 ──
_USER_CODE = {code_repr}

sandbox_globals = {{
    "__builtins__": _SAFE_BUILTINS,
    "__name__": "__sandbox__",
    "read_ws": read_ws,
    "write_ws": write_ws,
    "read_skill_file_ws": read_skill_file_ws,
    "generate_pdf_ws": generate_pdf_ws,
    "generate_docx_ws": generate_docx_ws,
    "__file__": None,
    "__package__": None,
    "__loader__": None,
    "__spec__": None,
}}

output_buf = io.StringIO()

def _scrub_text(s):
    # 绝对路径（含 Windows 盘符）与 "line N" 一律抹掉。
    # ★ 回显用户源码行时**必须**过这一遍：模型完全可能在代码里写死一个服务器路径
    # （比如把日志里看到的路径抄进 raise 的消息里），不过滤就等于把 error_msg
    # 已经脱敏掉的东西又从侧门放回去。tests/test_sandbox_matrix.py 的
    # exception_path_sanitized / exception_winpath_sanitized 两条钉住了这个契约。
    import re as _re
    s = _re.sub(r'(?:[A-Za-z]:)?[\\\\/][\\w./\\\\-]+\\.py', '<file>', s)
    s = _re.sub(r'[A-Za-z]:[\\\\/][^\\s\\"\\']+', '<path>', s)
    s = _re.sub(r'line \\d+', 'line <n>', s)
    return s

try:
    with contextlib.redirect_stdout(output_buf):
        exec(_USER_CODE, sandbox_globals)
    output = output_buf.getvalue()
except SyntaxError as e:
    # 显式取 msg / lineno / text，而不是直接把 str(e) 打出去：后者会带上
    # '<string>' 这个对模型毫无意义的文件名，而前者能顺手把出错的源码行一起给出。
    _sl = e.lineno
    _st = _scrub_text((e.text or "").strip())
    output = "语法错误: " + str(e.msg)
    if _sl:
        output += "（第 " + str(_sl) + " 行）"
        if _st:
            if len(_st) > 160:
                _st = _st[:160] + "…"
            output += "\\n  第 " + str(_sl) + " 行: " + _st
except MemoryError:
    # 主要是 local 后端会走到这里：RLIMIT_AS 触发时 Python 抛 MemoryError，脚本自己
    # 捕获、照样附加写入清单，于是**已经写下的文件仍会被同步**。docker 后端下超限
    # 通常是 cgroup 直接杀进程（exit 137 + oom_killed），脚本没机会执行这一行，文案
    # 由 _exit_message 依据 exit 事件生成，写入文件靠 post-process 的全量兜底扫描找回。
    # 但容器内单次 malloc 失败同样会抛 MemoryError，所以这里的数字必须跟着后端走
    # （docker = B 的 --memory），不能写死 local 的 256 —— 报错了用户只会更困惑。
    output = "错误：内存不足（超出 {memory_mb}MB 限制）"
except RecursionError:
    output = "错误：递归深度超限"
except SystemExit:
    output = "（代码调用了 exit/sys.exit，已被阻止）"
except Exception as e:
    # 只保留错误类型和消息，不泄露服务器路径（规则见 _scrub_text）
    error_type = type(e).__name__
    # 消息正文里的 line N 也会被抹掉 —— 那可能指向库内部（site-packages）的行号，
    # 与下面给出的用户代码行号不是一回事，两者并存不冲突。
    error_msg = _scrub_text(str(e))
    output = "运行时错误: " + error_type + ": " + error_msg
    # ── 出错位置：只保留用户代码自己的帧 ──
    # 用户代码经 exec(_USER_CODE, sandbox_globals) 编译成独立 code object，它的帧
    # 一律是 filename == '<string>'，且 lineno **天然相对于用户提交的那段代码**
    # （不是相对于这份包装脚本），所以不需要任何偏移换算。
    # 其余帧一律丢弃 —— 这一条同时挡住两类泄漏：容器里的 site-packages 布局，
    # 以及 local 后端下包装脚本自己的临时文件绝对路径（那是真实的服务器路径）。
    _ul = _USER_CODE.split("\\n")
    _frames = []
    for _f in traceback.extract_tb(e.__traceback__):
        if _f.filename != "<string>":
            continue
        _n = _f.lineno or 0
        _s = _ul[_n - 1].strip() if 1 <= _n <= len(_ul) else ""
        if _s:
            # ★ 源码行同样要脱敏：模型可能把服务器路径写死在代码里，
            # 不过滤就等于绕过上面 error_msg 的脱敏。
            _s = _scrub_text(_s)
            if len(_s) > 160:
                _s = _s[:160] + "…"
        _frames.append("  第 " + str(_n) + " 行 in " + str(_f.name) + (": " + _s if _s else ""))
    if _frames:
        output += ("\\n出错位置（行号从 1 开始，相对你提交的代码；只列最内层 6 帧）:\\n"
                   + "\\n".join(_frames[-6:]))

# ── 4. 附加写入清单（主进程据此只同步本次写入的文件，避免全量扫描）──
if _WS_WRITTEN_FILES:
    import json as _json
    output += "\\n__SANDBOX_WRITTEN_FILES__:" + _json.dumps(list(dict.fromkeys(_WS_WRITTEN_FILES)))

sys.stdout.write(output)
"""


def _set_resource_limits():
    """子进程启动前的资源限制回调（仅 Linux/Unix 有效）。

    通过 preexec_fn 传入 subprocess.run，在 fork 之后、exec 之前设置：
    - RLIMIT_AS: 虚拟内存上限
    - RLIMIT_CPU: CPU 时间上限（秒）
    - RLIMIT_NOFILE: 文件描述符上限
    - RLIMIT_FSIZE: 单文件写入大小上限
    """
    try:
        import resource

        # 内存限制
        mem_bytes = SANDBOX_MEMORY_MB * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))

        # CPU 时间限制（比 PYTHON_TIMEOUT 多 2 秒的余量，让超时先触发）
        resource.setrlimit(resource.RLIMIT_CPU, (PYTHON_TIMEOUT + 2, PYTHON_TIMEOUT + 2))

        # 文件描述符限制
        resource.setrlimit(resource.RLIMIT_NOFILE, (SANDBOX_NOFILE, SANDBOX_NOFILE))

        # 单文件写入限制（与工作区单文件上限一致，防止沙箱内写大文件被 SIGXFSZ 终止）
        max_file = workspace_service.MAX_FILE_SIZE
        resource.setrlimit(resource.RLIMIT_FSIZE, (max_file, max_file))
    except (ImportError, AttributeError, ValueError, OSError):
        # Windows 没有 resource 模块，或者权限不足
        pass


def _sandbox_preexec():
    """子进程 preexec 回调：资源限制 + 可选进程降权（公网根本性防护）。

    - 资源限制：内存 / CPU / FD / 单文件大小
    - 降权：设置环境变量 SANDBOX_RUN_AS_USER=<低权限用户名>（如 sandbox），
      子进程以该用户运行，服务器敏感文件（.env、源码、DB 配置）对该用户不可读，
      彻底阻断沙箱内任意文件读写（白名单消毒的兜底）。
      部署要求：创建该用户，并 chown data/workspaces 与 skill 目录使其可读写。
      未设置时保持原权限运行（向后兼容）。
    """
    _set_resource_limits()
    run_as = os.environ.get("SANDBOX_RUN_AS_USER", "")
    if not run_as:
        return
    try:
        import pwd
        pw = pwd.getpwnam(run_as)
        os.setgid(pw.pw_gid)
        os.setuid(pw.pw_uid)
    except Exception as e:
        logger.warning(f"沙箱降权失败（以原权限运行，建议修复）: {e}")


def _tool_run_python(args: dict, user_id: int, db: Session, on_tool_progress: Optional[callable] = None) -> str:
    """执行 Python 代码（带全局沙箱并发限制，公网防资源打满）。"""
    if not _sandbox_slots.acquire(timeout=_SANDBOX_SLOT_TIMEOUT):
        return "错误：系统繁忙（同时执行的代码过多），请稍后重试"
    try:
        return _tool_run_python_impl(args, user_id, db, on_tool_progress)
    finally:
        _sandbox_slots.release()


@dataclass
class _SandboxPlan:
    """一次 run_python 的执行计划。两个后端共用同一份，杜绝行为漂移。

    漂移的具体危害：docker 分支若自己拼脚本、自己算超时，两边就会各自演化，
    而回归对比（tests/test_sandbox_matrix.py 的 golden）依赖的正是"同一份脚本、
    同一个超时、同一套后处理"。
    """

    script: str        # 完整沙箱脚本（白名单 + 工作区函数 + 用户代码）
    backend: str       # "local" | "docker"
    host_ws: str       # A 上的工作区绝对路径：local 的 cwd，两个后端的脱敏根
    relpath: str       # 工作区目录名，docker 后端传给 B（B 会再校验一遍）
    timeout: float


def _sandbox_backend(user_id: int) -> str:
    """选执行后端。灰度名单语义与直觉相反：**留空 = 全量走 docker**。

    计划的灰度步骤是"先放一个测试账号 → 观察一周 → 清空名单转全量"，所以空名单
    必须表示全量；反过来定义的话最后一步要改成枚举所有 user_id，而新增用户会
    静默掉回 local（在 A 上以 web 用户权限跑代码，正是本次迁移要消除的状态）。
    """
    if (settings.SANDBOX_BACKEND or "local").strip().lower() != "docker":
        return "local"
    allow = {s.strip() for s in (settings.SANDBOX_DOCKER_ALLOWLIST or "").split(",")
             if s.strip()}
    if allow and str(user_id) not in allow:
        return "local"
    return "docker"


def _map_to_container_paths(skill_dirs: dict, skill_bounds: dict) -> tuple[dict, dict]:
    """宿主技能库路径 → 容器内 /skills 路径（NFS 把同一个目录挂到 B 上）。

    映射不上的条目**跳过而不是原样保留**：留着宿主绝对路径的话，容器里既没有
    该路径，又会把 A/B 的目录布局写进生成脚本（随后被脱敏，模型看到的是残缺
    路径）。跳过则沙箱内的 read_skill_file_ws 报"技能不存在"，模型能据此纠正。
    """
    try:
        from .skill_service import SKILLS_ROOT
        root = Path(SKILLS_ROOT).resolve()
    except Exception as e:
        logger.warning(f"技能库根不可用，docker 后端本次不映射技能目录: {e}")
        return {}, {}

    into_dirs: dict = {}
    into_bounds: dict = {}
    for target, src in ((into_dirs, skill_dirs), (into_bounds, skill_bounds)):
        for name, p in (src or {}).items():
            try:
                rel = Path(p).resolve().relative_to(root).as_posix()
                target[name] = f"{CONTAINER_SKILLS}/{rel}"
            except Exception as e:
                # ValueError = 不在技能库根下；开发机 Windows 上 resolve() 对不存在
                # 路径的语义也不同。两类都算"映射不上"，跳过。
                logger.warning(f"技能目录 {name} 无法映射到容器路径，已跳过: {e}")
    return into_dirs, into_bounds


def _prepare_sandbox(code: str, user_id: int, db: Session, backend: str) -> _SandboxPlan:
    """生成沙箱脚本与执行参数。backend 只决定脚本里写的是哪套路径。"""
    host_ws = str(workspace_service._user_workspace_dir(user_id))
    # 目录名 = QQ 号或 user_id，单段，正好落在 B 的 relpath 白名单内
    relpath = Path(host_ws).name

    # 活跃 skill 的目录映射（skill_name -> dir_path）。pack 技能额外记录包根边界
    # （skill_name -> pack_root），沙箱内的读取范围放宽到包根。
    skill_dirs: dict = {}
    skill_bounds: dict = {}
    try:
        from .skill_service import get_active_skills, SKILLS_ROOT
        for s in get_active_skills(db):
            if s.dir_path:
                skill_dirs[s.name] = s.dir_path
                if getattr(s, "pack", ""):
                    pack_root = SKILLS_ROOT / s.pack
                    if pack_root.is_dir():
                        skill_bounds[s.name] = str(pack_root)
    except Exception as e:
        logger.warning(f"获取活跃 skill 目录失败: {e}")

    # 工作区已用字节数（沙箱内配额检查的初始用量）
    quota_used = 0
    try:
        from sqlalchemy import func
        from ..models.workspace import WorkspaceFile
        quota_used = db.query(
            func.coalesce(func.sum(WorkspaceFile.file_size), 0)
        ).filter(
            WorkspaceFile.user_id == user_id,
            WorkspaceFile.is_directory == False,  # noqa: E712
        ).scalar() or 0
    except Exception as e:
        logger.warning(f"获取工作区用量失败，沙箱配额检查使用 0: {e}")

    if backend == "docker":
        # 容器内 WORKDIR 就是 /workspace（见 Dockerfile），与这里嵌进脚本的
        # _WS_DIR 一致；两个后端下脚本里的相对路径解析结果因此相同。
        script_ws = CONTAINER_WORKSPACE
        skill_dirs, skill_bounds = _map_to_container_paths(skill_dirs, skill_bounds)
    else:
        script_ws = host_ws

    script = _build_sandbox_script(
        code, script_ws, skill_dirs if skill_dirs else None, quota_used,
        skill_bounds if skill_bounds else None,
        memory_mb=_sandbox_memory_mb(backend),
    )
    return _SandboxPlan(script=script, backend=backend, host_ws=host_ws,
                        relpath=relpath, timeout=float(PYTHON_TIMEOUT))


def _merge_streams(out_lines: list[str], err_lines: list[str]) -> str:
    """stdout 优先；stdout 为空时退到 stderr（滤掉 traceback 的框架行）。

    两个后端**必须共用**这一段：docker 那边 B 只负责按流分类回传，最终拼成什么
    文本是 A 的决定。各拼各的，golden 回归对比（tests/test_sandbox_matrix.py）
    就会满屏噪声。
    """
    output = "\n".join(out_lines)
    if not output and err_lines:
        # stderr 可能包含 Python traceback，只留有用信息
        useful = [
            line for line in err_lines
            if not line.startswith("  File ")
            and not line.startswith("    ")
            and "Traceback" not in line
        ]
        output = "\n".join(useful) if useful else "\n".join(err_lines).strip()
    return output


def _sandbox_memory_mb(backend: str) -> int:
    """文案用的内存数字。真实限额：local 是 RLIMIT_AS，docker 是 B 的 --memory。"""
    return settings.SANDBOX_DOCKER_MEMORY_MB if backend == "docker" else SANDBOX_MEMORY_MB


# exit_info["error"] → 给 LLM 的文案。**每条都必须说明"代码没执行"**，否则模型会
# 以为代码跑了只是没输出，接着编造结果 —— 这正是迁移前那个缺陷的表现形式。
# 刻意不设 "timeout" 键：传输超时与沙箱超时共用 timed_out 分支的文案。
_ERROR_TEXT = {
    "not_configured": "错误：沙箱执行器未配置，本次代码未能执行（请联系管理员检查 .env）",
    "unreachable": "错误：无法连接沙箱执行器，本次代码未能执行。请稍后重试",
    "auth": "错误：沙箱执行器认证失败，本次代码未能执行（请联系管理员检查令牌配置）",
    "busy": "错误：系统繁忙（同时执行的代码过多），请稍后重试",
    "not_ready": "错误：沙箱执行器未就绪（存储挂载异常），本次代码未能执行。请稍后重试",
    "rejected": "错误：沙箱请求被拒绝（工作区不可用或代码超出大小限制），本次代码未能执行",
    "executor_error": "错误：沙箱执行器内部错误，本次代码未能执行。请稍后重试",
    "bad_stream": "错误：沙箱执行中断（未收到执行结果），本次代码未能执行。请重试",
    "internal": "错误：沙箱调用异常，本次代码未能执行",
    "interpreter_missing": "错误：找不到 Python 解释器，本次代码未能执行",
    "container_start_failed": "错误：沙箱容器启动失败（镜像或容器运行时不可用），本次代码未能执行",
}


def _exit_message(info: dict, plan: _SandboxPlan) -> Optional[str]:
    """把沙箱进程/容器的死因翻成给 LLM 的准确文案。None = 退出正常。

    迁移前 `proc.wait()` 的返回值被**完全丢弃**：OOM 或段错误时 stdout/stderr
    双空，控制流一路掉到"（代码执行完成，无输出。）"—— 对 LLM 撒谎，于是模型
    认定代码成功、在后续轮次里编造数据。本次迁移的核心修复就在这一支。
    """
    err = info.get("error")
    if err:
        return _ERROR_TEXT.get(err) or "错误：沙箱执行失败，本次代码未能执行"
    if info.get("timed_out"):
        return f"错误：代码执行超时（{int(plan.timeout)}秒限制）"
    if info.get("oom_killed"):
        return (f"错误：内存不足（超出 {_sandbox_memory_mb(plan.backend)}MB 限制，"
                "进程已被终止）。请减少一次性载入的数据量，或分批处理。")
    code = info.get("exit_code")
    if code == 0:
        return None
    if code is None:
        # 既没跑起来又没归类：宁可报错也不能说"执行完成"
        return "错误：沙箱未返回执行结果，本次代码可能未执行"
    if code in (137, -9):
        return f"错误：进程被系统强制终止（退出码 {code}，通常是内存不足）"
    return f"错误：代码执行失败（退出码 {code}）"


def _run_local_sandbox(plan: _SandboxPlan, on_tool_progress: Optional[callable] = None) -> tuple[str, dict]:
    """local 后端：本机独立子进程 + setrlimit。返回 (output, exit_info)。

    与迁移前唯一的实质差别是**不再丢弃 proc.wait() 的返回值**（见 _exit_message）。
    其余逐字保持：临时文件、clean_env、-I -X utf8、双线程读流、超时即杀并丢掉
    半截输出。行为不变是 golden 回归能直接复用的前提。
    """
    import sys
    import subprocess
    import tempfile
    import platform
    import threading as _th

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".py",
        delete=False,
        encoding="utf-8",
        prefix="sandbox_",
    ) as f:
        f.write(plan.script)
        tmp_path = f.name
    # 权限收紧：脚本含用户代码，POSIX 下仅属主可读写（防同机其他用户窥探）
    if os.name == "posix":
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass

    out_lines: list[str] = []
    err_lines: list[str] = []
    try:
        # 最小环境变量（防止子进程访问敏感信息）
        clean_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/usr/local/bin"),
            "PYTHONPATH": "",
            "HOME": "/tmp",
            "LANG": "en_US.UTF-8",
            "LC_ALL": "en_US.UTF-8",
        }
        # Windows 不支持 preexec_fn，只在 Linux/Unix 上设置资源限制
        popen_kwargs: dict = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "env": clean_env,
            "cwd": plan.host_ws,  # 工作目录设为用户工作区
        }
        if platform.system() != "Windows":
            popen_kwargs["preexec_fn"] = _sandbox_preexec

        proc = subprocess.Popen(
            # -I: isolated mode; -X utf8: force UTF-8
            [sys.executable, "-I", "-X", "utf8", tmp_path],
            **popen_kwargs,
        )

        def _read_stream(stream, lines, is_stdout):
            try:
                for raw in iter(stream.readline, b""):
                    if not raw:
                        break
                    # 手动 UTF-8 解码（Windows 上 text=True 会用 GBK）
                    # rstrip 必须连 \r 一起吃：Windows 子进程的文本模式 stdout 会把
                    # \n 翻译成 \r\n，Linux 容器只产出 \n。留着 \r 的话同一段代码两个
                    # 后端逐行都不同，回归对比全是噪声 —— test_sandbox_matrix.py 的
                    # _join_lines 早就这样归一了，golden 里也是 0 个 CR 字节。
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    lines.append(line)
                    # 标记行不透给前端：它是一段 JSON 文件清单。此前这里没有过滤，
                    # 一直在往 SSE 漏 —— 判定与 docker 后端共用 sandbox_client 那一份。
                    if is_stdout and on_tool_progress and sandbox_client.on_line_wanted(line):
                        try:
                            on_tool_progress(line)
                        except Exception:
                            pass
            except Exception:
                pass
            finally:
                try:
                    stream.close()
                except Exception:
                    pass

        t_out = _th.Thread(target=_read_stream, args=(proc.stdout, out_lines, True), daemon=True)
        t_err = _th.Thread(target=_read_stream, args=(proc.stderr, err_lines, False), daemon=True)
        t_out.start()
        t_err.start()

        try:
            rc = proc.wait(timeout=plan.timeout)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
            proc.wait()
            # 超时时丢掉已累积的输出，与 docker 后端一致（B 那边超时即杀容器、只回
            # exit 事件）。半截输出会让模型以为任务部分成功了。
            return "", sandbox_client.exit_info(timed_out=True)
        t_out.join(timeout=2)
        t_err.join(timeout=2)
        return (_merge_streams(out_lines, err_lines),
                sandbox_client.exit_info(exit_code=rc))
    except FileNotFoundError:
        return "", sandbox_client.exit_info(error="interpreter_missing")
    except Exception as e:
        # 不把异常文本回给 LLM：里面可能有宿主路径。detail 只进日志。
        logger.error(f"subprocess 执行失败: {e}", exc_info=True)
        return "", sandbox_client.exit_info(error="internal",
                                            detail=f"{type(e).__name__}: {e}")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _run_docker_sandbox(plan: _SandboxPlan, on_tool_progress: Optional[callable] = None) -> tuple[str, dict]:
    """docker 后端：脚本文本交给 B 机，在全新容器里执行。返回 (output, exit_info)。

    **绝不回落 local。** 回落等于把沙箱重新以 web 用户权限跑在 .env 和 MySQL 旁边，
    安全隔离在故障时静默消失 —— 宁可这一轮工具调用明确失败。
    """
    out_lines, err_lines, info = sandbox_client.run(
        plan.script, plan.relpath, plan.timeout, on_line=on_tool_progress)
    if info.get("request_id"):
        # 与 B 的 exec.jsonl 对账全靠这个 rid，出问题时两边日志能拼起来
        logger.info(
            f"沙箱 docker 后端 rid={info['request_id']} exit={info.get('exit_code')} "
            f"oom={info.get('oom_killed')} timeout={info.get('timed_out')} "
            f"error={info.get('error')} dropped={info.get('dropped_lines')}"
        )
    return _merge_streams(out_lines, err_lines), info


def _postprocess_sandbox_output(output: str, info: dict, plan: _SandboxPlan,
                                user_id: int, db: Session) -> str:
    """把 (输出, 死因) 整理成回给 LLM 的最终文本。两个后端共用。

    顺序有讲究：死因文案必须在"（代码执行完成，无输出。）"兜底**之前**拼进去。
    反过来写就会在 OOM 时先补一句"执行完成"，等于把刚修好的缺陷又装回去。
    """
    msg = _exit_message(info, plan)
    if info.get("timed_out"):
        # 超时时丢掉已累积的输出（两个后端一致，理由见 _run_local_sandbox）
        output = msg or output
    elif msg:
        # 非零退出但已有输出：两边都留。报错信息常常比 stdout 更有用，
        # 而 stdout 里可能已经打印了部分结果。
        output = msg if not output.strip() else f"{output.rstrip()}\n\n{msg}"

    # ── 沙箱执行后：同步磁盘文件到数据库 ──
    # 沙箱中的 write_ws / generate_pdf_ws / generate_docx_ws 直接写磁盘，不会更新
    # 数据库记录。按写入清单只同步本次写入的文件（O(本次写入)），替代全量扫描。
    written_paths: list[str] = []
    if _WRITTEN_MARKER in output:
        prefix, _, rest = output.rpartition(_WRITTEN_MARKER)
        json_part = rest.strip().splitlines()[0] if rest.strip() else ""
        try:
            import json as _json
            parsed = _json.loads(json_part)
            if isinstance(parsed, list):
                written_paths = [str(p) for p in parsed if p]
                output = prefix.rstrip("\n")  # 从结果中移除清单标记行
        except Exception:
            written_paths = []

    if written_paths:
        # 清单驱动：只同步沙箱本次写入的文件
        try:
            changed = workspace_service.sync_workspace_to_db(user_id, db, paths=written_paths)
            if changed:
                logger.info(f"沙箱执行后同步 {len(changed)} 个文件到数据库: {changed}")
                # 在输出中追加同步信息（让 LLM 知道文件已生效）
                output += f"\n[工作区文件已更新: {', '.join(changed)}]"
        except Exception as e:
            logger.error(f"沙箱执行后同步工作区失败: {e}", exc_info=True)
    elif info.get("oom_killed") or any(m in output for m in ("已写入", "已生成")):
        # 兜底全量扫描。oom_killed 这一支是迁移新加的，因为两个后端语义有差：
        # local 下 RLIMIT_AS 触发 MemoryError，生成脚本自己捕获后**照样附加写入
        # 清单**（附加语句在 try/except 之后），已写下的文件因此能同步；docker 下
        # 超限是 cgroup 直接杀进程（exit 137 + oom_killed），脚本没机会跑到那一行，
        # 用户的半成品文件就会从工作区面板里静默消失。OOM 很罕见，O(全部文件) 的
        # 代价可以接受。
        try:
            changed = workspace_service.sync_workspace_to_db(user_id, db)
            if changed:
                output += f"\n[工作区文件已更新: {', '.join(changed)}]"
        except Exception as e:
            logger.error(f"沙箱执行后同步工作区失败: {e}", exc_info=True)

    # ── 敏感信息脱敏：统一替换服务器绝对路径与密钥 ──
    # docker 后端多一步：容器内固定挂载点归一成与 local 相同的标签。同一个逻辑
    # 路径在两个后端下必须产出**逐字相同**的文本，否则回归对比全是噪声 —— 而
    # golden_sandbox.json 覆盖不到脱敏（它存的是原始脚本输出，一个标签都没有）。
    from .sanitize import redact, redact_container_roots, redact_known_roots
    if plan.backend == "docker":
        output = redact_container_roots(output)
    # 两个标签**不能**靠连着调两次 redact() 得到：redact() 内部的 Unix 根泛化会把
    # /opt/... 一律换成 <路径>，第一次调用（技能库）就顺手把生产工作区根
    # /opt/agent-harness/workspaces/<qq> 吃掉了，第二次带 <工作区> 标签的调用永远轮不到 ——
    # 实测 '已写入 <工作区>a.csv' 退化成 '已写入 <路径>'。反过来先工作区后技能库，
    # 坏的就是技能库标签。正确做法是先用 redact_known_roots 一次带上每个根各自的
    # 标签（它不做通用泛化），再用裸 redact() 补通用路径与凭据脱敏。
    known_roots: list[tuple[str, str]] = []
    try:
        from .skill_service import SKILLS_ROOT
        known_roots.append((str(SKILLS_ROOT), "<技能库>"))
    except Exception:
        pass
    known_roots.append((plan.host_ws, "<工作区>"))
    output = redact_known_roots(output, known_roots)
    output = redact(output)

    # 限制输出大小
    if not output:
        output = "（代码执行完成，无输出。使用 print() 打印结果。）"
    if len(output) > MAX_OUTPUT:
        output = output[:MAX_OUTPUT] + f"\n\n...(输出已截断，共 {len(output)} 字符)"

    # ── 执行端（B / docker）转发预算耗尽提示 ──
    # 输出行数/字节数超过执行端预算时，B 会直接丢掉超出的行（只写审计日志）。
    # 必须让模型知道它拿到的是残缺输出，否则会基于不完整数据下结论
    # （典型错误：数据被截断却断言"这份数据只有 N 行"）。
    # 这里与上面 A 侧按**字符数**截断是两码事，文案刻意区分开。
    dropped = int(info.get("dropped_lines") or 0)
    if dropped > 0:
        output += (
            f"\n\n...(注意：本次输出过多，执行端已丢弃 {dropped} 行未返回，"
            "以上结果不完整；如需完整数据，请把结果写入文件后用 read_file 分段读取。)"
        )
    return output


def _tool_run_python_impl(args: dict, user_id: int, db: Session, on_tool_progress: Optional[callable] = None) -> str:
    """执行 Python 代码，返回给 LLM 的结果文本。

    三段职责分离（迁移前是一个 240 行的函数，后端一多就会各自漂移）：
      _prepare_sandbox             生成脚本 + 执行参数，两个后端共用
      _run_local / _run_docker     只管跑完，返回 (output, exit_info)
      _postprocess_sandbox_output  死因文案 + 文件同步 + 脱敏 + 截断

    脚本层安全措施（两个后端一致）：替换 __builtins__ 的内置函数白名单、受控
    import 白名单 + os 替身模块、配额检查、输出大小限制。执行载体层的隔离见
    模块文档"第二层"。

    沙箱内可用函数：
    - print(...) — 输出会被捕获并返回
    - read_ws(path) / write_ws(path, content) — 读写工作区
    - read_skill_file_ws(skill_name, file_path) — 读技能目录中的参考文件
    - generate_pdf_ws / generate_docx_ws — 生成文档
    """
    code = (args.get("code") or "").strip()
    if not code:
        return "错误：缺少 code 参数"

    backend = _sandbox_backend(user_id)
    plan = _prepare_sandbox(code, user_id, db, backend)
    if backend == "docker":
        output, info = _run_docker_sandbox(plan, on_tool_progress)
    else:
        output, info = _run_local_sandbox(plan, on_tool_progress)
    return _postprocess_sandbox_output(output, info, plan, user_id, db)


def _tool_media_generate(args: dict, user_id: int, db: Session,
                         on_tool_progress: Optional[callable] = None) -> str:
    """media-router 桥接工具：后端 subprocess 调 CLI，Agent 只传意图。

    - CLI 脚本由沙箱外执行（media_router.py 的模块依赖与沙箱 import 黑名单
      冲突，沙箱内永远跑不了；这里换来的代价是参数硬编码、cwd 锁定用户工作区，
      脚本产物只能落到工作区 outputs/ 下）
    - 工具描述与 _tool_media_generate_impl 对应：action ∈ resolve/generate/report
    - stdout 单 JSON 原样回传（绝对路径重写为工作区相对路径）；
      失败时 stderr 尾部若干行并入结果便于模型纠错
    - 并发安全：走 run_python 的全局沙箱并发槽，与其他重工具共用限流
    """
    if not _sandbox_slots.acquire(timeout=_SANDBOX_SLOT_TIMEOUT):
        return "错误：系统繁忙（同时执行的代码过多），请稍后重试"
    try:
        return _tool_media_generate_impl(args, user_id, db, on_tool_progress)
    finally:
        _sandbox_slots.release()


def _tool_media_generate_impl(args: dict, user_id: int, db: Session,
                              on_tool_progress: Optional[callable] = None) -> str:
    import subprocess

    action = (args.get("action") or "").strip()
    if action not in ("resolve", "generate", "report"):
        return "错误：action 必须是 resolve / generate / report 之一"

    # 技能存在性检查 + 定位 CLI
    from .skill_service import get_active_skills
    skill_dir: Optional[str] = None
    for s in get_active_skills(db):
        if s.name == "media-router" and s.dir_path:
            skill_dir = s.dir_path
            break
    if not skill_dir:
        return ("错误：media-router 技能未安装或未启用，无法调用模型池。"
                "请让用户在管理后台安装/启用该技能后重试。")
    script = Path(skill_dir) / "scripts" / "media_router.py"
    if not script.is_file():
        return "错误：media-router 技能目录中未找到 scripts/media_router.py"

    # 工作区路径（cwd，决定 outputs/ 落盘位置）
    try:
        ws_dir = workspace_service._user_workspace_dir(user_id)
    except Exception as e:
        return _sanitize_tool_error(f"错误：无法定位用户工作区: {e}", user_id, db)

    # 拼参数（白名单式，用户输入不透传任意 flag）
    kind = (args.get("kind") or "image").strip()
    if kind not in ("image", "video"):
        return "错误：kind 必须是 image 或 video"
    cmd = [sys.executable, "-X", "utf8", str(script), action, "--kind", kind]

    if action == "resolve":
        supports = (args.get("supports") or "").strip()
        if supports and not re.fullmatch(r"[\w,-]+", supports):
            return "错误：supports 含非法字符"
        cmd += ["--supports", supports, "--pretty"]
    elif action == "generate":
        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return "错误：action=generate 时 prompt 必填"
        if len(prompt) > 4000:
            return "错误：prompt 过长（上限 4000 字符）"
        cmd += ["--prompt", prompt]
        for key, flag, pattern in (
            ("image", "--image", None),
            ("size", "--size", r"\d{2,5}x\d{2,5}"),
            ("negative_prompt", "--negative-prompt", None),
            ("aspect_ratio", "--aspect-ratio", r"\d{1,2}:\d{1,2}"),
        ):
            val = (args.get(key) or "").strip()
            if not val:
                continue
            if pattern and not re.fullmatch(pattern, val):
                return f"错误：{key} 格式不合法（{val!r}）"
            if key == "image":
                # 图生图/图生视频输入必须落在用户工作区内（防任意读）
                val = str(Path(ws_dir) / val).replace("\\", "/") \
                    if not val.startswith(("/", "http://", "https://")) else val
                if val.startswith(str(ws_dir)):
                    real = Path(val).resolve()
                    if not str(real).startswith(str(Path(ws_dir).resolve())):
                        return "错误：image 参数越出工作区"
                    if not real.is_file():
                        return f"错误：输入图片不存在（{args.get(key)}）"
            cmd += [flag, val]
        for key, flag in (("count", "--count"), ("duration", "--duration")):
            try:
                val = int(args.get(key) or 0)
            except (TypeError, ValueError):
                return f"错误：{key} 必须是整数"
            if val > 0:
                if key == "count" and val > 4:
                    return "错误：count 上限 4"
                if key == "duration" and val > 60:
                    return "错误：duration 上限 60 秒"
                cmd += [flag, str(val)]

    # 执行（cwd=工作区；视频生成可能数分钟，超时取 10 分钟）
    timeout = 600 if action == "generate" else 60
    env = {k: v for k, v in os.environ.items()}
    env.setdefault("PYTHONIOENCODING", "utf-8")
    logger.info(f"media_generate: user={user_id} action={action} kind={kind}")
    try:
        proc = subprocess.run(
            cmd, cwd=str(ws_dir), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        return f"错误：media-router {action} 执行超时（>{timeout}s）。视频生成较慢，可稍后重试或减少时长。"
    except Exception as e:
        return _sanitize_tool_error(f"错误：执行 media-router 失败: {e}", user_id, db)

    ws_prefix = str(Path(ws_dir).resolve()).replace("\\", "/")
    out = (proc.stdout or "").strip()
    if out:
        # 绝对路径 → 工作区相对路径（模型侧只见 outputs/xxx，不暴露服务器路径）
        out = out.replace(ws_prefix, "").replace(
            (ws_prefix + "/").replace("//", "/"), "")
        out = re.sub(r'"(?:[A-Za-z]:)?/[^"\n]*?/outputs/', '"/outputs/', out)
        out = out.replace('"/outputs/', '"outputs/') if '"outputs/' not in out else out
        if len(out) > MAX_OUTPUT:
            out = out[:MAX_OUTPUT] + f"\n...(输出已截断，共 {len(out)} 字符)"

    err_tail = "\n".join((proc.stderr or "").strip().splitlines()[-8:])
    status_line = f"[exit_code={proc.returncode}]" + (f" {err_tail}" if err_tail and proc.returncode != 0 else "")

    if not out:
        return (
            f"media-router {action} 无 stdout 输出。{status_line}\n"
            "提示：exit_code=3 表示模型池无可用第三方模型（未配置 API Key），"
            "exit_code=1 表示全部候选模型失败。"
        )
    return out + (f"\n{status_line}" if status_line and proc.returncode != 0 else "")


def _tool_skill(args: dict, user_id: int, db: Session) -> str:
    """执行 skill 工具 — 加载指定名称的 skill 完整指令内容。

    借鉴 DSH tool-skill 的 execute() 函数：
    1. 验证 skill 名称
    2. 查询数据库中活跃的 skill
    3. 返回渲染后的 <skill_content> 块（包含 SKILL.md 正文 + 目录文件树）
    """
    from .skill_service import execute_skill_tool

    name = args.get("name", "").strip()
    result = execute_skill_tool(name, db)

    # 限制返回大小
    if len(result) > MAX_OUTPUT:
        result = result[:MAX_OUTPUT] + f"\n\n...(skill 内容已截断，共 {len(result)} 字符)"
    return result


def _tool_read_skill_file(args: dict, user_id: int, db: Session) -> str:
    """读取 skill 目录中的参考文件内容。

    当 skill 有磁盘目录（dir_path）时，模型可使用此工具读取目录中的参考文件，
    如 references/examples.md、scripts/encode_gif.py 等。

    支持大文件分段读取：
    - 可选参数 offset（字符偏移），从指定位置继续读取
    - 单次返回上限 MAX_SKILL_FILE_RETURN（64K 字符），超出时在尾部引导模型用 offset 续读
    - 模板类文件（template.html 约 40K）单次即可完整返回

    安全设计：
    - 路径必须为相对路径（不允许绝对路径）
    - 不允许 .. 路径穿越
    - 最终路径必须在 skill 目录内
    - 文件大小限制 256KB
    """
    from .skill_service import read_skill_file

    skill_name = args.get("skill_name", "").strip()
    file_path = args.get("file_path", "").strip()

    # 可选分段参数 offset（字符偏移）
    offset = 0
    try:
        offset = max(0, int(args.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0

    result = read_skill_file(skill_name, file_path, db, offset=offset, user_id=user_id)

    # 错误信息或空内容直接返回
    if not result or result.startswith("错误："):
        return result

    # 单次返回上限：超过则截断并引导模型用 offset 续读剩余部分
    if len(result) > MAX_SKILL_FILE_RETURN:
        result = result[:MAX_SKILL_FILE_RETURN]
        next_offset = offset + MAX_SKILL_FILE_RETURN
        result += (
            f"\n\n[文件内容较长，已返回 {offset}..{next_offset} 字符区间；"
            f"如需继续读取剩余部分，请调用 read_skill_file 工具并传 offset={next_offset}]"
        )
    return result


def _tool_remember(args: dict, user_id: int, db: Session) -> str:
    """保存用户长期记忆（跨会话）。"""
    if not user_id:
        return "错误：未登录用户无法保存记忆"
    content = args.get("content", "").strip()
    if not content:
        return "错误：缺少 content 参数"
    memory_type = args.get("memory_type") or "fact"
    try:
        from .memory_service import add_memory
        record = add_memory(user_id, content, db, memory_type=memory_type)
        return f"已记住：{record.content}"
    except ValueError as e:
        return _sanitize_tool_error(f"错误：{e}", user_id, db)


def _tool_recall(args: dict, user_id: int, db: Session) -> str:
    """查询用户长期记忆。"""
    if not user_id:
        return "错误：未登录用户无法查询记忆"
    keyword = args.get("keyword") or ""
    try:
        limit = max(1, min(int(args.get("limit") or 10), 20))
    except (TypeError, ValueError):
        limit = 10
    try:
        from .memory_service import recall_memories
        memories = recall_memories(user_id, db, keyword=keyword.strip() or None, limit=limit)
    except Exception as e:
        logger.error(f"recall 记忆查询失败: {e}")
        return "错误：记忆查询失败"
    if not memories:
        return "没有找到相关的记忆" if keyword.strip() else "暂无长期记忆"
    lines = []
    for i, m in enumerate(memories, 1):
        tag = {"fact": "事实", "preference": "偏好", "context": "上下文"}.get(m.memory_type, "记忆")
        lines.append(f"{i}. [{tag}] {m.content}")
    return "\n".join(lines)

