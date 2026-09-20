"""敏感信息脱敏 — 统一出口过滤。

覆盖两类环境：
- Windows（开发/测试）：盘符绝对路径 C:\\Users\\...、D:/...
- Linux（生产部署）：/home/ /root/ /etc/ /var/ 等高危根路径
以及常见凭据格式（sk-、AKIA、BEGIN 块、Bearer、URL 带密码等）。

设计原则：
- 只处理"系统产生的文本"（工具结果、错误消息、实时输出、工具参数回显），
  不处理用户对话正文与工作区文件内容（用户自己的数据，LLM 需要读取）。
- 模糊后统一为 <路径>/<密钥>/<URL凭据>，保留可读性但不泄露原始信息。
"""

import re

# ── Windows 盘符绝对路径：C:\Users\a\b.txt、D:/data/x.py ──
_WIN_PATH = re.compile(r'(?<![A-Za-z0-9])([A-Za-z]:[\\/][^\s"\'<>]*)')

# ── Unix 高危根路径：/home/ /root/ /etc/ /var/ /usr/ /opt/ /srv/ /tmp/ /Users/ /private/ /proc/ /sys/ ──
_UNIX_PATH = re.compile(
    r'(?<![\w])((?:/home/|/root/|/etc/|/var/|/usr/|/opt/|/srv/|/tmp/|/Users/|/private/|/proc/|/sys/)[^\s"\'<>]*)'
)

# ── 常见凭据格式 ──
_KEY_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'sk-[A-Za-z0-9_-]{8,}'), '<密钥>'),
    (re.compile(r'ghp_[A-Za-z0-9]{20,}'), '<密钥>'),
    (re.compile(r'AKIA[0-9A-Z]{16}'), '<密钥>'),
    (re.compile(r'-----BEGIN [A-Z ]+-----'), '<密钥块>'),
    (re.compile(r'(?i)bearer\s+[A-Za-z0-9._~+/-]+={0,2}'), '<密钥>'),
    # URL 带密码：mysql://user:pass@host、http://user:pass@x.com
    (re.compile(r'(?i)([a-z][a-z0-9+.-]*://[^/\s:@]+:[^@\s/]+@)'), '<URL凭据>'),
]

# ── 归一化：统一分隔符为 /、消解 . 与 ..、去尾部斜杠、去盘符大小写差异 ──
def _normalize(p: str) -> str:
    p = p.replace("\\", "/")
    # 消解 . 与 ..（简单实现，路径段级处理）
    segs: list[str] = []
    for seg in p.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if segs:
                segs.pop()
            continue
        segs.append(seg)
    out = "/".join(segs)
    if len(p) >= 2 and p[1] == ":":
        out = out[:2].lower() + out[2:]  # 盘符小写（C: 与 c: 视为同根）
    return out


def _redact_roots(text: str, roots: list[str], label: str) -> str:
    """替换已知根目录的所有常见变体（精确匹配归一化后的形式，含正斜杠/反斜杠两种）。"""
    if not roots:
        return text
    norm_roots = [r.replace("\\", "/").rstrip("/") for r in roots if r]
    # 收集根及其归一化变体（正斜杠与反斜杠两种书写形式）
    variants: set[str] = set()
    for r in norm_roots:
        variants.add(r)
        variants.add(_normalize(r))
        # 盘符小写变体
        if len(r) >= 2 and r[1] == ":":
            variants.add(r[:2].lower() + r[2:])
            variants.add(_normalize(r[:2].lower() + r[2:]))
        # 尾部再带斜杠的形式
        variants.add(r + "/")
    # 生成反斜杠书写形式（Windows 风格）
    bs_variants: set[str] = set()
    for v in variants:
        bs_variants.add(v)
        bs_variants.add(v.replace("/", "\\"))
    # 按长度降序替换，避免短根先替换导致长路径残留
    for v in sorted(bs_variants, key=len, reverse=True):
        if not v:
            continue
        text = text.replace(v, label)
    return text


# ── docker 后端：容器内固定挂载点 ──
# 不能走 _redact_roots，两个原因都是实测出来的：
# 1. 它对根做的是**无边界** text.replace，而 _normalize("/workspace") 会得到裸词
#    "workspace" —— 于是 workspace_dir、workspaces、甚至英文散文 "my workspace"
#    全被替换成 <工作区>。单段根把这个既有隐患直接激活了。
# 2. 就算去掉裸词变体，无边界替换仍会误伤：工作区里一个**名叫 skills 的子目录**
#    （/workspace/skills/x.csv）会被当成技能库路径。多段根今天不会碰到这个问题，
#    因为用户路径里出现完整的 /opt/agent-harness/后端/data/skills 子串几乎不可能。
# 所以这里要求挂载点出现在路径**起始**位置：前面不能是路径字符，后面不能接词字符。
# 尾部分隔符一起吃掉 —— _redact_roots 的最长变体带尾斜杠，local 后端今天产出的
# 就是 `<工作区>a.csv` 这种没有斜杠的形式，两边必须逐字一致才能对比。
_CONTAINER_ROOTS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'(?<![\w~/\\.-])/workspace(?![\w.-])[/\\]?'), "<工作区>"),
    (re.compile(r'(?<![\w~/\\.-])/skills(?![\w.-])[/\\]?'), "<技能库>"),
]


def redact_container_roots(text: str) -> str:
    """把容器内挂载点归一成与 local 后端一致的 <工作区> / <技能库> 标签。

    docker 后端下沙箱看到的是 /workspace 与 /skills，local 后端看到的是宿主绝对
    路径（已被 redact() 换成同样的标签）。不归一的话：① 容器内部布局原样进 LLM
    上下文；② 同一段代码两个后端产出不同文本，回归对比全是噪声。
    """
    if not text:
        return text
    for pat, label in _CONTAINER_ROOTS:
        text = pat.sub(label, text)
    return text


def redact_known_roots(text: str, roots: list[tuple[str, str]]) -> str:
    """只替换已知根目录的各种书写变体为对应标签，不做通用路径/凭据脱敏。

    与 redact() 的区别：不含盘符泛化、Unix 根泛化与凭据正则，
    用于 LLM 回复正文——正文里用户讨论的普通路径示例不应被误伤，
    只有服务器自身的根目录（工作区/技能库/部署目录）才需要模糊化。

    Args:
        text: 待处理文本
        roots: [(根目录绝对路径, 替换标签), ...]，如 [("D:/ws/1", "<工作区>")]
    """
    if not text:
        return text
    for root, label in roots:
        if root:
            text = _redact_roots(text, [root], label)
    return text


def redact(
    text: str,
    *,
    roots: list[str] | None = None,
    root_label: str = "<工作区>",
) -> str:
    """统一脱敏入口。

    roots: 已知根目录绝对路径列表（如用户工作区、技能目录），
           其所有常见变体（分隔符/大小写/归一化）都会被替换为 root_label。
    """
    if not text:
        return text
    # 1. 已知根目录变体（先做，避免正则把根目录再拆开）
    if roots:
        text = _redact_roots(text, roots, root_label)
    # 2. Windows 盘符路径
    text = _WIN_PATH.sub("<路径>", text)
    # 3. Unix 高危根路径
    text = _UNIX_PATH.sub("<路径>", text)
    # 4. 凭据
    for pat, repl in _KEY_PATTERNS:
        text = pat.sub(repl, text)
    return text


def redact_value(obj):
    """递归脱敏：用于工具参数（dict/list/str 混合结构）的整体脱敏。"""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, list):
        return [redact_value(x) for x in obj]
    if isinstance(obj, dict):
        return {k: redact_value(v) for k, v in obj.items()}
    return obj
