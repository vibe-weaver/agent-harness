"""极简 YAML 子集解析器（零依赖）。

存在的理由：这个 skill 要在多个 agent 环境里跑（WorkBuddy / Claude Code / DSH），
不能假设对方装了 PyYAML。config.py 会优先尝试真正的 PyYAML，只有在缺失时才回退到这里。

支持的语法子集（覆盖本 skill 的配置 schema）：
  - 注释：``#`` 开头，或值后面的 `` # ...``
  - 缩进嵌套的映射
  - 序列：``- item`` / ``- key: value``
  - 行内列表：``[a, b, c]``
  - 行内映射：``{a: 1, b: two}``
  - 标量：引号字符串、整数、浮点、true/false、null/~
  - 空值键（值为空时视为嵌套块或 None）

明确不支持：锚点/别名、块标量（``|`` ``>``）、多行折叠、复杂转义。
需要这些特性时请安装 PyYAML（``pip install pyyaml``），配置会自动走 PyYAML 分支。
"""

from __future__ import annotations

import re
from typing import Any


class MiniYamlError(ValueError):
    """配置语法错误。"""


_TRUE = {"true", "yes", "on"}
_FALSE = {"false", "no", "off"}
# 注意：不要在这里加 "none"。YAML 的空值只有 null/Null/NULL/~/空字符串，
# "none" 是 Python 的写法，在 YAML 里就是普通字符串。混进来会让本解析器与
# PyYAML 行为不一致（装了 pyyaml 的环境正常、没装的坏掉），排查成本极高。
_NULL = {"null", "~", ""}

# ---------------------------------------------------------------------------
# "什么算数字" 必须和 PyYAML 逐字一致
# ---------------------------------------------------------------------------
# 这两条正则是从 PyYAML 的 resolver 里照抄的。**不要**改成 Python 的
# int()/float() —— 那套规则和 YAML 1.1 不一样，会造成"同一份配置在装了 pyyaml
# 和没装 pyyaml 的机器上跑出两种结果"，而这是最难排查的一类 bug。
#
# 最典型的两个坑：
#   `1:1`   YAML 1.1 的**六十进制**整数 —— PyYAML 读成 61。而 `aspect_ratio: 1:1`
#           这种写法极其自然，裸写就等于把 61 发给接口。必须加引号写成 "1:1"。
#   `1e3`   YAML 1.1 要求指数带符号（1.0e+3 才算浮点），所以 `1e3` 是**字符串**；
#           而 Python 的 float("1e3") 是 1000.0。两边不一致。
_YAML_INT = re.compile(
    r"""^(?:[-+]?0b[0-1_]+
        |[-+]?0[0-7_]+
        |[-+]?(?:0|[1-9][0-9_]*)
        |[-+]?0x[0-9a-fA-F_]+
        |[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+)$""",
    re.X,
)
_YAML_FLOAT = re.compile(
    r"""^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?
        |\.[0-9_]+(?:[eE][-+][0-9]+)?
        |[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*
        |[-+]?\.(?:inf|Inf|INF)
        |\.(?:nan|NaN|NAN))$""",
    re.X,
)

#: 形如 `2026-01-01` 的时间戳形状 —— 见下方 _to_number 的说明。
_TIMESTAMP = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}(?:[Tt ].*)?$")


def _to_number(raw: str) -> Any:
    """按 PyYAML 的规则把裸标量转成数字；形状不符返回 None。"""
    text = raw.strip()
    if _YAML_INT.match(text):
        sign = -1 if text.startswith("-") else 1
        body = text.lstrip("+-")
        low = body.lower()
        if low.startswith("0b"):
            return sign * int(body[2:].replace("_", ""), 2)
        if low.startswith("0x"):
            return sign * int(body[2:].replace("_", ""), 16)
        if ":" in body:
            # 六十进制：`1:1` = 1*60+1 = 61
            total = 0
            for part in body.split(":"):
                total = total * 60 + int(part.replace("_", ""))
            return sign * total
        if body.startswith("0") and len(body) > 1:
            return sign * int(body[1:].replace("_", ""), 8)
        return sign * int(body.replace("_", ""))
    if _YAML_FLOAT.match(text):
        low = text.lower()
        if low.endswith(".inf"):
            return float("-inf") if text.startswith("-") else float("inf")
        if low.endswith(".nan"):
            return float("nan")
        sign = -1.0 if text.startswith("-") else 1.0
        body = text.lstrip("+-")
        if ":" in body:
            head, frac = body.split(".", 1)
            total = 0
            for part in head.split(":"):
                total = total * 60 + int(part.replace("_", ""))
            return sign * (total + float(f"0.{frac.replace('_', '')}"))
        return sign * float(body.replace("_", ""))
    return None


def _strip_comment(line: str) -> str:
    """去掉行尾注释，但保留引号内的 ``#``。"""
    out: list[str] = []
    quote: str | None = None
    for idx, ch in enumerate(line):
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            continue
        if ch == "#" and (idx == 0 or line[idx - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out).rstrip()


def _split_top(text: str, sep: str = ",") -> list[str]:
    """按分隔符切分，忽略引号与括号内部的字符。"""
    parts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    depth = 0
    for ch in text:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            continue
        if ch in "[{(":
            depth += 1
        elif ch in "]})":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
            continue
        buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def _find_key_sep(content: str) -> int:
    """返回映射键与值之间那个 ``:`` 的下标；找不到返回 -1。

    **冒号只有在后面跟空白或位于行尾时才算键分隔符** —— 这是 YAML 的规定，
    也是 PyYAML 的行为。少了这条约束会有两类后果：

      * `https://api.example.com/v1` 里的冒号被当成键分隔符，整行被拆坏；
      * `aspect_ratio: 1:1` 的值 `1:1` 被当成嵌套映射。

    两者都会让本解析器与 PyYAML 行为分叉，所以这里必须严格按规范来
    （不许再用"值里是不是出现了 `//`"这种补丁去绕）。
    """
    quote: str | None = None
    depth = 0
    for idx, ch in enumerate(content):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            continue
        if ch in "[{(":
            depth += 1
            continue
        if ch in "]})":
            depth -= 1
            continue
        if ch == ":" and depth == 0:
            if idx + 1 >= len(content) or content[idx + 1] in " \t":
                return idx
    return -1


def _split_key(content: str) -> tuple[str, bool, str]:
    """把 ``key: value`` 拆成 (key, 是否有分隔符, value)。

    键可以带空格（YAML 允许，PyYAML 也照收），此时 dumper 会给它加引号写出来；
    这里不再用"键里有空格就不当映射"这种启发式，否则会与 PyYAML 分叉。
    """
    idx = _find_key_sep(content)
    if idx < 0:
        return content.strip(), False, ""
    raw_key = content[:idx].strip()
    rest = content[idx + 1:].strip()
    if not raw_key:
        return content.strip(), False, ""
    if raw_key[0] in ("'", '"'):
        # 显式加了引号的键
        key = _unquote(raw_key)
        return (key, True, rest) if key else (content.strip(), False, "")
    return raw_key, True, rest


def _unescape(text: str) -> str:
    """单遍反转义。

    注意不要用链式 replace：`\\\\n` 这类序列在链式替换下会被错误地吃掉反斜杠。
    """
    out: list[str] = []
    idx = 0
    mapping = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "0": "\0"}
    while idx < len(text):
        ch = text[idx]
        if ch == "\\" and idx + 1 < len(text):
            nxt = text[idx + 1]
            out.append(mapping.get(nxt, nxt))
            idx += 2
            continue
        out.append(ch)
        idx += 1
    return "".join(out)


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        inner = text[1:-1]
        if text[0] == '"':
            return _unescape(inner)
        return inner.replace("''", "'")
    return text


def _scalar(text: str) -> Any:
    raw = text.strip()
    if raw == "":
        return None
    if raw[0] in ("'", '"'):
        return _unquote(raw)
    low = raw.lower()
    if low in _NULL:
        return None
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    if raw.startswith("[") or raw.startswith("{"):
        closer = "]" if raw[0] == "[" else "}"
        if not raw.endswith(closer):
            raise MiniYamlError(
                f"行内 {'列表' if closer == ']' else '映射'}没有闭合：{raw!r}"
                f"（如果这是普通文本，加引号写：\"{raw}\"）"
            )
        inner = raw[1:-1].strip()
        if raw[0] == "[":
            return [] if not inner else [_scalar(x) for x in _split_top(inner)]
        if not inner:
            return {}
        out: dict[str, Any] = {}
        for chunk in _split_top(inner):
            key, has_sep, val = _split_key(chunk)
            if not has_sep:
                # PyYAML 到这里会抛 ConstructorError，这里也必须报错而不是
                # 猜一个空字典 —— 静默猜错比报错难查一百倍。
                # 最常见的触发是忘了给占位符加引号：prompt: {{prompt}}
                raise MiniYamlError(
                    f"行内映射格式不对：{raw!r}"
                    f"（里面的 {chunk!r} 不是 key: value；"
                    f"如果这是占位符之类的普通文本，要加引号：\"{raw}\"）"
                )
            out[key] = _scalar(val)
        return out
    number = _to_number(raw)
    if number is not None:
        return number
    return _unquote(raw)


def _tokenize(text: str) -> list[tuple[int, str]]:
    tokens: list[tuple[int, str]] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise MiniYamlError(f"第 {lineno} 行使用了 Tab 缩进，请改用空格")
        cleaned = _strip_comment(raw)
        if not cleaned.strip():
            continue
        indent = len(cleaned) - len(cleaned.lstrip(" "))
        tokens.append((indent, cleaned.strip()))
    return tokens


def _reject_nested_colon(rest: str) -> None:
    """裸写的值里不能再出现 ``: ``（冒号+空格）。

    `note: 重要: 别删` 这种写法在 YAML 里是非法的 —— PyYAML 直接报 ScannerError。
    本解析器也必须报错而不是自己猜一个结果，否则同一份配置在装了 PyYAML 的环境
    报错、在没装的环境能跑，行为分叉。正确写法是加引号：`note: "重要: 别删"`。
    """
    if rest[:1] in ("'", '"', "[", "{"):
        return  # 引号/行内集合里的冒号不构成歧义
    if _find_key_sep(rest) >= 0:
        raise MiniYamlError(
            f"值里出现了未加引号的「冒号+空格」：{rest!r}"
            f'（YAML 会把它当成新的键，PyYAML 会直接报错。加引号即可："{rest}"）'
        )


def _parse_map(tokens: list[tuple[int, str]], pos: int, indent: int) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    while pos < len(tokens):
        cur_indent, content = tokens[pos]
        if cur_indent < indent:
            break
        if cur_indent > indent:
            raise MiniYamlError(f"缩进异常：{content!r} 比同级多缩进了 {cur_indent - indent} 个空格")
        if content.startswith("- ") or content == "-":
            break
        key, has_sep, rest = _split_key(content)
        if not has_sep:
            raise MiniYamlError(
                f"无法解析的配置行：{content!r}"
                f"（少了冒号？值为空的写法是 `键:` 单独一行，"
                f"文本里带冒号的写法要加引号，例如 aspect_ratio: \"1:1\"）"
            )
        if rest == "":
            if pos + 1 < len(tokens) and tokens[pos + 1][0] > cur_indent:
                child, pos = _parse_block(tokens, pos + 1, tokens[pos + 1][0])
                result[key] = child
            else:
                result[key] = None
                pos += 1
            continue
        _reject_nested_colon(rest)
        result[key] = _scalar(rest)
        pos += 1
    return result, pos


def _parse_seq(tokens: list[tuple[int, str]], pos: int, indent: int) -> tuple[list[Any], int]:
    items: list[Any] = []
    while pos < len(tokens):
        cur_indent, content = tokens[pos]
        if cur_indent < indent:
            break
        if not (content.startswith("- ") or content == "-"):
            break
        body = content[2:].strip() if content.startswith("- ") else ""
        if body == "":
            if pos + 1 < len(tokens) and tokens[pos + 1][0] > cur_indent:
                child, pos = _parse_block(tokens, pos + 1, tokens[pos + 1][0])
                items.append(child)
            else:
                items.append(None)
                pos += 1
            continue
        key, has_sep, _ = _split_key(body)
        if has_sep:
            # 序列项是映射的开头，把后续更深缩进的行一起收进来
            pseudo = cur_indent + 2
            block: list[tuple[int, str]] = [(pseudo, body)]
            scan = pos + 1
            while scan < len(tokens) and tokens[scan][0] > cur_indent:
                block.append(tokens[scan])
                scan += 1
            item, _ = _parse_map(block, 0, pseudo)
            items.append(item)
            pos = scan
            continue
        items.append(_scalar(body))
        pos += 1
    return items, pos


def _parse_block(tokens: list[tuple[int, str]], pos: int, indent: int) -> tuple[Any, int]:
    if pos >= len(tokens):
        return None, pos
    first = tokens[pos][1]
    if first.startswith("- ") or first == "-":
        return _parse_seq(tokens, pos, indent)
    # 整份文档就是一个裸标量（`loads("1024x1024")`）。这也要能解析 ——
    # PyYAML 支持，回退解析器不支持的话，行为又分叉了。
    if len(tokens) == 1 and _find_key_sep(first) < 0:
        return _scalar(first), pos + 1
    return _parse_map(tokens, pos, indent)


def loads(text: str) -> Any:
    """把 YAML 子集文本解析成 Python 对象。"""
    tokens = _tokenize(text)
    if not tokens:
        return {}
    value, _ = _parse_block(tokens, 0, tokens[0][0])
    return value


def load(path) -> Any:
    """从文件读取并解析。"""
    from pathlib import Path

    return loads(Path(path).read_text(encoding="utf-8"))


# ============================================================ 序列化

_SPECIAL_HEAD = set("-?:,[]{}#&*!|>'\"%@` ")


def _needs_quote(text: str) -> bool:
    """这个字符串必须加引号才能安全回读吗。

    判断偏向保守：条件写得宽一点、多打几个引号只是难看，少打一个引号是事故。
    凡是"某个解析器可能会读成非字符串"的形状一律加引号。
    """
    if text == "" or text.strip() != text:
        return True
    low = text.lower()
    if low in _TRUE or low in _FALSE or low in _NULL:
        return True
    # 数字形状 —— 复用与 PyYAML 完全一致的那两条正则
    if _YAML_INT.match(text) or _YAML_FLOAT.match(text):
        return True
    # 时间戳形状：PyYAML 会读成 date/datetime 对象，本解析器读成字符串。
    # 与其纠结谁对，不如一律加引号，把它永远固定成字符串。
    if _TIMESTAMP.match(text):
        return True
    if text[0] in _SPECIAL_HEAD:
        return True
    if text.endswith(":"):
        return True
    if ": " in text or " #" in text:
        return True
    # 大括号会与占位符/行内映射语法冲突，必须引起来
    if any(ch in text for ch in "{}[]"):
        return True
    if any(ch in text for ch in "\n\r\t"):
        return True
    return False


def _render_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return repr(value)
    text = str(value)
    if _needs_quote(text):
        escaped = (
            text.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
        )
        return f'"{escaped}"'
    return text


_SAFE_KEY = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$")


def _render_key(key: Any) -> str:
    """键的规则比值严：只允许 ``[A-Za-z0-9_]`` 开头、由字母数字与 ``_ . -`` 组成。

    含空格或特殊字符的键必须加引号，否则回读时会被判成散文行而报错。
    """
    text = str(key)
    if _SAFE_KEY.match(text):
        return text
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


_EMPTY = {dict: "{}", list: "[]"}


def _dump_node(node: Any, indent: int, out: list[str]) -> None:
    pad = " " * indent
    if isinstance(node, dict):
        if not node:
            out.append(f"{pad}{{}}")
            return
        for key, value in node.items():
            name = _render_key(key)
            if isinstance(value, (dict, list)) and value:
                out.append(f"{pad}{name}:")
                _dump_node(value, indent + 2, out)
            elif isinstance(value, (dict, list)):
                out.append(f"{pad}{name}: {_EMPTY[type(value)]}")
            else:
                out.append(f"{pad}{name}: {_render_scalar(value)}")
        return

    if isinstance(node, list):
        if not node:
            out.append(f"{pad}[]")
            return
        for item in node:
            if isinstance(item, dict) and item:
                first = True
                for key, value in item.items():
                    name = _render_key(key)
                    prefix = f"{pad}- {name}" if first else f"{pad}  {name}"
                    if isinstance(value, (dict, list)) and value:
                        out.append(f"{prefix}:")
                        # 列表项里键的正文从 pad+2 开始，子级再缩 2
                        _dump_node(value, indent + 4, out)
                    elif isinstance(value, (dict, list)):
                        out.append(f"{prefix}: {_EMPTY[type(value)]}")
                    else:
                        out.append(f"{prefix}: {_render_scalar(value)}")
                    first = False
            elif isinstance(item, list) and item:
                out.append(f"{pad}-")
                _dump_node(item, indent + 2, out)
            else:
                out.append(f"{pad}- {_render_scalar(item)}")
        return

    out.append(f"{pad}{_render_scalar(node)}")


def dumps(data: Any) -> str:
    """把 Python 对象序列化成 YAML 子集文本。

    保证与 :func:`loads` 往返一致 —— 需要引号的字符串一律加双引号，
    所以 `{{prompt}}` 这类占位符、`16:9`、`1024*1024` 都能安全回读。
    """
    out: list[str] = []
    _dump_node(data, 0, out)
    return "\n".join(out) + "\n"


def dump(data: Any, path) -> None:
    """序列化并写入文件。"""
    from pathlib import Path

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dumps(data), encoding="utf-8")
