"""Skill 服务 — 借鉴 DSH skill-filesystem 的设计理念。

DSH 的 skill 系统工作原理：
1. 存储：skill 以 SKILL.md 文件形式存储在目录中（每个 skill 一个子目录）
2. Frontmatter：YAML frontmatter 包含 name 和 description 字段
3. 发现：skill-filesystem provider 扫描目录，解析 frontmatter，构建 catalog
4. 使用：catalog（name + description 摘要列表）注入到 system prompt，
   模型通过调用 `skill` 工具按需加载完整的 skill 内容

本模块实现了相同的模式，但适配到本项目的 Python 后端：
- skill 存储在数据库（DshSkill 表）+ 可选的磁盘目录（dir_path）
- catalog 在每次对话时从数据库查询活跃 skill 生成
- `skill` 工具由 tool_registry 注册，执行器在此模块实现
- 支持 ZIP 上传整个 skill 目录（包含 SKILL.md + 参考文件）
"""

import io
import logging
import os
import re
import zipfile
from pathlib import Path
from typing import Optional

from ..models import DshSkill

import yaml
from sqlalchemy.orm import Session

from ..models import DshSkill

logger = logging.getLogger(__name__)

# skill 存储根目录（磁盘上的 skill 目录）
SKILLS_ROOT = Path(__file__).resolve().parent.parent.parent / "data" / "skills"

# skill name 验证正则（kebab-case，兼容下划线和大小写）
SKILL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

# 读取 skill 文件时的最大大小（防止读取超大文件）
MAX_SKILL_FILE_SIZE = 256 * 1024  # 256KB

# ZIP 导入时跳过的无关目录/文件（版本控制、IDE、垃圾文件）
_IGNORED_ZIP_DIRS = {".git", ".github", ".idea", ".vscode", "__pycache__", "node_modules"}
_IGNORED_ZIP_FILES = {".DS_Store", "Thumbs.db", ".gitignore", ".gitattributes"}

# ZIP 导入解压总大小上限（zip bomb 防护）
MAX_SKILL_IMPORT_SIZE = 100 * 1024 * 1024  # 100MB

# 整仓库导入（平铺布局兜底检测）时静默跳过的常见资源目录名——
# 它们没有 SKILL.md 也不应当作"未注册技能"报告，避免噪音
_REPO_RESOURCE_DIR_NAMES = {
    "assets", "scripts", "references", "docs", "doc", "tests", "test",
    "templates", "template", "static", "images", "img", "lib", "shared",
    "common", "examples", "dist", "build",
}


def parse_skill_md(raw: str) -> tuple[dict, str] | None:
    """解析 SKILL.md 文件的 YAML frontmatter 和正文。

    借鉴 DSH skill-filesystem 的 parseFrontmatter() 函数：
    - 第一行必须是 `---`
    - 寻找下一个 `---` 行作为 frontmatter 结束标记
    - frontmatter 是 YAML，解析为 dict
    - 正文是 frontmatter 之后的全部内容

    Returns:
        (frontmatter_dict, body) 或 None（解析失败）
    """
    raw = raw.strip()
    if not raw.startswith("---"):
        return None

    # 找到第二个 `---` 行
    lines = raw.split("\n")
    if len(lines) < 2:
        return None

    # 跳过第一个 `---`，找第二个
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break

    if end_idx is None:
        return None

    yaml_text = "\n".join(lines[1:end_idx])
    body = "\n".join(lines[end_idx + 1:]).strip()

    try:
        frontmatter = yaml.safe_load(yaml_text)
        if not isinstance(frontmatter, dict):
            return None
    except yaml.YAMLError:
        return None

    return frontmatter, body


def parse_skill_from_text(text: str, skill_name: str = "") -> tuple[str, str] | None:
    """从文本内容解析 skill 的 name 和 description。

    支持两种格式：
    1. 带 YAML frontmatter 的 SKILL.md 格式
    2. 纯 Markdown 正文（name 从目录名推断，description 取第一行）

    Returns:
        (name, description) 或 None
    """
    parsed = parse_skill_md(text)
    if parsed:
        frontmatter, body = parsed
        name = frontmatter.get("name", skill_name)
        description = frontmatter.get("description", "")
        if not description:
            # 从正文第一行推断
            first_line = body.split("\n")[0].lstrip("# ").strip() if body else ""
            description = first_line[:200] or "无描述"
        return str(name), str(description)

    # 纯 Markdown 格式 — 从文件名或第一行推断
    first_line = text.split("\n")[0].lstrip("# ").strip() if text else ""
    description = first_line[:200] or "无描述"
    return skill_name, description


def scan_skill_directory(dir_path: str) -> dict | None:
    """扫描一个 skill 目录，读取 SKILL.md 内容。

    借鉴 DSH skill-filesystem 的 discoverRoot() 函数：
    - 如果目录下有 SKILL.md，读取并解析
    - 如果目录下只有一个 .md 文件，也视为 skill 文件
    - 返回 {name, description, content, dir_path}

    Returns:
        dict 或 None（目录不存在或无有效 skill 文件）
    """
    p = Path(dir_path)
    if not p.is_dir():
        return None

    # 优先找 SKILL.md
    skill_file = p / "SKILL.md"
    if not skill_file.is_file():
        # 找第一个 .md 文件
        md_files = sorted(p.glob("*.md"))
        if md_files:
            skill_file = md_files[0]
        else:
            return None

    try:
        raw = skill_file.read_text(encoding="utf-8")
    except Exception as e:
        logger.error(f"读取 skill 文件失败 {skill_file}: {e}")
        return None

    parsed = parse_skill_md(raw)
    if parsed:
        frontmatter, body = parsed
        name = str(frontmatter.get("name", p.name))
        description = str(frontmatter.get("description", ""))
        if not description:
            first_line = body.split("\n")[0].lstrip("# ").strip() if body else ""
            description = first_line[:200] or "无描述"
        return {
            "name": name,
            "description": description,
            "content": body,
            "dir_path": str(p),
        }

    # 纯 Markdown — 无 frontmatter
    name = p.name
    first_line = raw.split("\n")[0].lstrip("# ").strip() if raw else ""
    description = first_line[:200] or "无描述"
    return {
        "name": name,
        "description": description,
        "content": raw.strip(),
        "dir_path": str(p),
    }


def _extract_zip_safe(zip_bytes: bytes, dest: Path) -> int:
    """安全解压 ZIP 到 dest：过滤穿越路径/无关目录、zip bomb 防护。

    Returns:
        解出的文件条数
    """
    extracted = 0
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        total_unzipped = 0
        for member in zf.namelist():
            member_path = Path(member)
            # 安全检查：防止路径穿越（绝对路径或 .. ）
            if member_path.is_absolute() or ".." in member:
                continue
            parts = member_path.parts
            # 跳过无关目录树（任一段匹配即跳过整个子树，如 .git / .github / __pycache__）
            if any(p in _IGNORED_ZIP_DIRS for p in parts):
                continue
            # 跳过无关文件
            if member_path.name in _IGNORED_ZIP_FILES:
                continue
            # zip bomb 防护：累计解压大小上限
            info = zf.getinfo(member)
            total_unzipped += info.file_size
            if total_unzipped > MAX_SKILL_IMPORT_SIZE:
                raise ValueError("ZIP 解压内容过大（超过 100MB），已中止导入")
            # 解压
            zf.extract(member, dest)
            if not member.endswith("/"):
                extracted += 1
    return extracted


def _safe_pack_name(pack: str) -> str:
    """校验并规范化技能包名（kebab-case；空串表示独立技能）。"""
    pack = (pack or "").strip()
    if not pack:
        return ""
    safe = re.sub(r"[^a-zA-Z0-9_-]", "-", pack)
    return safe if SKILL_NAME_RE.match(safe) else ""


def prune_pack_dir_if_empty(pack: str) -> bool:
    """删除技能后清理空包目录：包根下递归已无任何文件时移除整棵空目录树。

    刻意保守——包根常放着不属于任何单个技能的共享资源（assets/ scripts/ 等），
    只要还剩一个文件就保留目录，避免删掉别的技能仍在 ../ 引用的资源。

    Returns:
        True 表示确实移除了包目录。
    """
    safe_pack = _safe_pack_name(pack)
    if not safe_pack:
        return False
    pack_root = SKILLS_ROOT / safe_pack
    if not pack_root.is_dir():
        return False
    import shutil
    try:
        if any(p.is_file() for p in pack_root.rglob("*")):
            return False
        shutil.rmtree(pack_root, ignore_errors=True)
        removed = not pack_root.exists()
        if removed:
            logger.info(f"已清理空技能包目录: {pack_root}")
        return removed
    except Exception as e:
        logger.warning(f"清理空技能包目录失败（可忽略）: {e}")
        return False


def import_skill_from_zip(zip_bytes: bytes, skill_name: str, db: Session, pack: str = "") -> dict:
    """从 ZIP 字节流导入一个 skill 目录。

    借鉴 DSH skill-filesystem 的目录结构：
    - ZIP 解压到 SKILLS_ROOT/<name>/（独立技能）
      或 SKILLS_ROOT/<pack>/skills/<name>/（技能包技能）
    - 自动检测 SKILL.md 或第一个 .md 文件
    - 解析 frontmatter 获取 name 和 description
    - 存入数据库（content = SKILL.md 正文，dir_path = 磁盘路径）

    Args:
        zip_bytes: ZIP 文件字节流
        skill_name: 期望的 skill 名称（用于目录名）
        db: 数据库会话
        pack: 所属技能包名（空=独立技能）。pack 技能目录位于
              SKILLS_ROOT/<pack>/skills/<skill_name>/——镜像多技能仓库
              的原生布局（如 ASu-skills：skills/<name>/SKILL.md + 包根
              assets/ scripts/），使 ../../assets/、../<兄弟技能>/ 等
              原生跨目录引用无需改写即可生效；@pack/ 前缀始终可用

    Returns:
        {name, description, content, dir_path, pack}
    """
    import shutil

    # 确保根目录存在
    SKILLS_ROOT.mkdir(parents=True, exist_ok=True)
    safe_pack = _safe_pack_name(pack)

    # 安全的目录名
    safe_name = re.sub(r"[^a-zA-Z0-9_-]", "-", skill_name)
    if not SKILL_NAME_RE.match(safe_name):
        safe_name = "unnamed-skill"

    # 检查是否已存在（name 全局唯一，与 pack 无关）
    existing = db.query(DshSkill).filter(DshSkill.name == safe_name).first()
    if existing:
        # 清理旧目录：优先用 DB 记录的 dir_path（pack 变更时旧目录在别处）
        for old_dir in [Path(existing.dir_path) if existing.dir_path else None,
                        SKILLS_ROOT / safe_name]:
            if old_dir and old_dir.is_dir():
                try:
                    old_dir.resolve().relative_to(SKILLS_ROOT.resolve())
                    shutil.rmtree(old_dir, ignore_errors=True)
                except ValueError:
                    pass  # 旧目录不在 SKILLS_ROOT 下（外部注册），不动

    # 解压 ZIP（pack 技能放在 SKILLS_ROOT/<pack>/skills/<name>/ 下，镜像多技能仓库布局）
    if safe_pack:
        skill_dir = SKILLS_ROOT / safe_pack / "skills" / safe_name
    else:
        skill_dir = SKILLS_ROOT / safe_name
    if skill_dir.exists():
        shutil.rmtree(skill_dir, ignore_errors=True)
    skill_dir.mkdir(parents=True, exist_ok=True)

    try:
        _extract_zip_safe(zip_bytes, skill_dir)
    except zipfile.BadZipFile:
        raise ValueError("无效的 ZIP 文件")
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"解压失败: {e}")

    # ── 规范化目录结构 ──
    # 场景 A：skill 内容被包在一个子目录里（顶层无 SKILL.md）→ 把子目录内容提升到顶层
    if not (skill_dir / "SKILL.md").is_file():
        nested_skill = None
        for entry in skill_dir.iterdir():
            if entry.is_dir() and (entry / "SKILL.md").is_file():
                nested_skill = entry
                break
        if nested_skill is not None:
            for item in nested_skill.iterdir():
                target = skill_dir / item.name
                if target.exists():
                    if target.is_dir():
                        shutil.rmtree(target)
                    else:
                        target.unlink()
                item.rename(target)
            nested_skill.rmdir()

    # 场景 B：顶层已有 SKILL.md，删除内容重复的嵌套 skill 副本
    # （历史遗留：ZIP 顶层含 .git/README 等 + 一个完整 skill 子目录时，
    #   旧逻辑 len(entries)==1 下钻不生效，会残留 guizang-ppt-skill/guizang-ppt-skill/ 副本）
    for entry in list(skill_dir.iterdir()):
        if entry.is_dir() and (entry / "SKILL.md").is_file():
            shutil.rmtree(entry, ignore_errors=True)

    # 扫描解压后的目录
    result = scan_skill_directory(str(skill_dir))
    if result is None:
        raise ValueError(f"目录中没有找到有效的 SKILL.md 或 Markdown 文件")

    result["pack"] = safe_pack
    return result


def _merge_move(src: Path, dst: Path) -> None:
    """把 src 移动到 dst，已存在的目录递归合并（同名文件覆盖）。"""
    if dst.exists():
        if dst.is_dir() and src.is_dir():
            for child in list(src.iterdir()):
                _merge_move(child, dst / child.name)
            src.rmdir()
        else:
            dst.unlink()
            src.rename(dst)
    else:
        src.rename(dst)


def import_pack_resources_from_zip(zip_bytes: bytes, pack: str) -> int:
    """把共享资源 ZIP 解压到技能包根目录（SKILLS_ROOT/<pack>/）。

    多技能包的公共资源（assets/ scripts/ references/ 等）放在包根下，
    包内技能通过 ../ 或 @pack/ 前缀访问。合并语义：覆盖同名文件，
    不删除包内已有其他文件。不解析 SKILL.md、不建数据库行。

    支持整仓库 ZIP：Windows 右键压缩目录会带一层顶层目录前缀
    （如 ASu-skills/assets/...）。识别签名——解压后包根只剩唯一顶层
    目录，且满足其一：名称与包名相同；内含 skills/ 子目录（约定布局）；
    直接含至少一个带 SKILL.md 的子目录（平铺布局）——则把该目录的
    内容提升到包根（资源与技能目录一并就位，后续逐技能导入会按
    同一路径覆盖刷新）。

    Returns:
        解出的文件条数

    Raises:
        ValueError: 包名无效 / ZIP 无效
    """
    import shutil
    import tempfile

    safe_pack = _safe_pack_name(pack)
    if not safe_pack:
        raise ValueError("请提供有效的技能包名（字母/数字/连字符）")

    pack_root = SKILLS_ROOT / safe_pack
    pack_root.mkdir(parents=True, exist_ok=True)

    # 先解压到临时目录再合并：无论包根已有多少内容（如已先导入
    # 技能），都能可靠检测整仓库 ZIP 的顶层包裹目录
    tmp = Path(tempfile.mkdtemp(prefix=f"pack-{safe_pack}-", dir=SKILLS_ROOT))
    try:
        try:
            count = _extract_zip_safe(zip_bytes, tmp)
        except zipfile.BadZipFile:
            raise ValueError("无效的 ZIP 文件")
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"解压失败: {e}")

        # 整仓库 ZIP：临时目录只有唯一顶层目录（Windows 右键压缩目录
        # 产生，如 ASu-skills/assets/...）。签名满足其一 → 提升其内容，
        # 避免资源被多包一层导致引用失效：
        #   a) 名称与包名相同；b) 内含 skills/ 子目录（约定布局）；
        #   c) 直接含至少一个带 SKILL.md 的子目录（平铺布局）
        entries = list(tmp.iterdir())
        if count > 0 and len(entries) == 1 and entries[0].is_dir():
            top = entries[0]
            has_flat_skill = any(
                d.is_dir() and (d / "SKILL.md").is_file() for d in top.iterdir()
            )
            if top.name.lower() == safe_pack.lower() or (top / "skills").is_dir() or has_flat_skill:
                for item in list(top.iterdir()):
                    _merge_move(item, tmp / item.name)
                top.rmdir()

        for item in list(tmp.iterdir()):
            _merge_move(item, pack_root / item.name)
        return count
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def import_skill_repo_from_zip(zip_bytes: bytes, pack: str, db: Session, category: str = "") -> dict:
    """整仓库 ZIP 一键导入：自动拆出共享资源 + 注册全部技能（优化6）。

    管理员上传多技能仓库的完整 ZIP（如 ASu-skills），一次完成：
    1. 资源解压到包根（复用 import_pack_resources_from_zip 的
       顶层包裹提升与合并语义，skills/ 目录也一并就位）
    2. 检测技能目录并注册数据库行：
       - 优先约定布局：包根下 skills/<name>/SKILL.md
       - 平铺兜底：无 skills/ 时，包根顶层含 SKILL.md 的目录
       - 技能目录名保持仓库原样不改名（跨技能 ../<name>/ 引用依赖目录名）
    3. 逐技能执行引用健康检查（lint）

    同名技能（含已存在的独立技能）按 name 更新（与单技能上传语义一致）。
    不含任何技能的 ZIP 等价于纯资源导入（skills 为空列表）。
    category 非空时批量打到本次注册的全部技能上；留空 = 不改动已有技能的分类。

    Returns:
        {pack, files, skills: [{name, description, dir_path, warnings}],
         skipped: [未注册的目录说明]}
    """
    safe_pack = _safe_pack_name(pack)
    if not safe_pack:
        raise ValueError("请提供有效的技能包名（字母/数字/连字符）")

    file_count = import_pack_resources_from_zip(zip_bytes, safe_pack)
    pack_root = SKILLS_ROOT / safe_pack

    # ── 检测技能目录 ──
    candidates: list[Path] = []
    skills_convention = pack_root / "skills"
    if skills_convention.is_dir():
        candidates = sorted(d for d in skills_convention.iterdir() if d.is_dir())
    else:
        # 平铺兜底：顶层含 SKILL.md 的目录是技能；常见资源目录名静默跳过，
        # 不进 skipped 报告（避免 assets/docs 等噪音）
        candidates = sorted(
            d for d in pack_root.iterdir()
            if d.is_dir() and d.name.lower() not in _REPO_RESOURCE_DIR_NAMES
        )

    skills_out: list[dict] = []
    skipped: list[str] = []
    for d in candidates:
        # 严格识别：只认 SKILL.md（平铺布局下避免把 docs/ 等资源目录误判为技能）
        if not (d / "SKILL.md").is_file():
            skipped.append(f"{d.name}（无 SKILL.md，未注册）")
            continue
        scanned = scan_skill_directory(str(d))
        if scanned is None:
            skipped.append(f"{d.name}（SKILL.md 读取失败，未注册）")
            continue

        name = scanned["name"]
        if not SKILL_NAME_RE.match(name):
            # frontmatter name 非法 → 退回目录名（目录名是跨技能引用的锚点）
            name = d.name
        if not SKILL_NAME_RE.match(name):
            skipped.append(f"{d.name}（技能名含非法字符，未注册）")
            continue

        existing = db.query(DshSkill).filter(DshSkill.name == name).first()
        if existing:
            existing.description = scanned["description"]
            existing.content = scanned["content"]
            existing.dir_path = scanned["dir_path"]
            existing.pack = safe_pack
            existing.is_active = True
            # 留空 = 保持原分类（重传仓库更新正文时不该丢标签）
            if category:
                existing.category = category
        else:
            existing = DshSkill(
                name=name,
                description=scanned["description"],
                content=scanned["content"],
                dir_path=scanned["dir_path"],
                pack=safe_pack,
                category=category,
                is_active=True,
            )
            db.add(existing)
        db.commit()
        db.refresh(existing)

        warnings = lint_skill_references(existing.content, existing.dir_path, pack=safe_pack)
        skills_out.append({
            "name": existing.name,
            "description": existing.description,
            "dir_path": existing.dir_path,
            "warnings": warnings,
        })

    return {
        "pack": safe_pack,
        "files": file_count,
        "skills": skills_out,
        "skipped": skipped,
    }


def get_active_skills(db: Session) -> list[DshSkill]:
    """获取所有活跃的 skill。"""
    return db.query(DshSkill).filter(DshSkill.is_active == True).all()


# ── Catalog 相关性过滤 ──
# 技能数超过阈值且用户消息非空时，只展开与消息相关的技能描述，
# 其余技能仅列名称（模型仍可按名加载，防漏）；零命中回退全量（宁多勿漏）。
CATALOG_RELEVANCE_THRESHOLD = 12
_CATALOG_MSG_SCAN_LIMIT = 2000  # 相关性匹配只扫描消息前 N 字符（长粘贴防开销）
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_LATIN_RE = re.compile(r"[A-Za-z0-9_\-]{2,}")


def _extract_keywords(text: str) -> set[str]:
    """提取消息检索关键词：CJK 二元组 + 拉丁字母/数字词。

    中文无分词依赖，用二元组覆盖（"做简历" → 帮简/简历 等），
    英文按词匹配（"resume" 命中 name=make-resume）。
    """
    kws: set[str] = {m.group(0).lower() for m in _LATIN_RE.finditer(text)}
    for seg in _CJK_RE.findall(text):
        for i in range(len(seg) - 1):
            kws.add(seg[i : i + 2])
    return kws


def _match_score(skill: DshSkill, keywords: set[str]) -> int:
    """技能 name + description 与消息关键词的命中数。"""
    if not keywords:
        return 0
    hay = f"{skill.name} {skill.description}".lower()
    return sum(1 for kw in keywords if kw in hay)


def build_skill_catalog(
    db: Session,
    loaded_skills: Optional[list[str]] = None,
    user_message: str = "",
) -> str:
    """构建 skill catalog 文本，注入到 system prompt。

    借鉴 DSH tool-skill 的 renderCatalogMessage() 函数：
    生成 <available_skills> 块，列出 skill 的 name 和 description。
    模型看到此 catalog 后，可以通过调用 `skill` 工具加载完整的 skill 内容。

    相关性过滤（技能较多时）：
    - 活跃技能数 <= CATALOG_RELEVANCE_THRESHOLD：全量展开（现状行为）
    - 超过阈值且用户消息非空：只展开命中的技能，其余仅列名称；
      已加载技能始终展开；无任何命中回退全量
    - 未提供用户消息：全量展开（首轮兜底）

    Args:
        loaded_skills: 当前会话已加载的技能名列表（会话级技能层）。
                       已加载的技能会标注状态，提示模型无需重复加载。
        user_message: 用户本次消息（相关性过滤信号；空串表示不过滤）
    """
    skills = get_active_skills(db)
    if not skills:
        return ""

    loaded = set(loaded_skills or [])

    others: list[DshSkill] = []
    msg = (user_message or "").strip()
    if msg and len(skills) > CATALOG_RELEVANCE_THRESHOLD:
        keywords = _extract_keywords(msg[:_CATALOG_MSG_SCAN_LIMIT])
        hits = [s for s in skills if _match_score(s, keywords) > 0]
        if hits:
            keep = {s.name for s in hits} | loaded
            selected = [s for s in skills if s.name in keep]
            others = [s for s in skills if s.name not in keep]
        else:
            selected = skills
    else:
        selected = skills

    lines = [
        "<system-reminder>",
        "以下技能（Skills）可在本次对话中使用。每个技能是一套可复用的任务特定指令。",
        "",
        "<available_skills>",
    ]
    for skill in selected:
        # 转义描述中的特殊字符（借鉴 DSH 的 escapeText）
        desc = skill.description.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if skill.name in loaded:
            lines.append(
                f"- `{skill.name}`: {desc}（已加载，完整指令已在 system prompt 中，"
                "请直接遵循，无需重复加载）"
            )
        else:
            lines.append(f"- `{skill.name}`: {desc}")
    if others:
        lines.append(
            "其他可用技能（与当前消息未见直接相关，仅列名称；"
            "若用户提到或任务需要，可调用 `skill` 工具按名称加载）："
        )
        lines.append("  " + "、".join(f"`{s.name}`" for s in others))
    lines.extend([
        "</available_skills>",
        "",
        "如果用户的问题明确匹配某个技能的描述，或者用户提到了某个技能名称，"
        "请在执行任务前调用 `skill` 工具加载该技能的完整指令。"
        "先加载所有适用的技能，然后遵循其完整指令来解决问题。",
        "此目录仅包含摘要；在加载技能之前，不要推断或遵循技能的指令。",
        "</system-reminder>",
    ])

    return "\n".join(lines)


# ── 会话级技能注入层 ──
# 已加载的技能内容不再作为 tool result 重放（会被 agent 上下文管理裁剪删除），
# 而是注入 system prompt 前缀，模型每轮都能看到完整指令（对齐 DSH session 层设计）。
# 受上下文预算约束：超预算时按边界截断指令正文（标签保持完整）+ 提示按需读取。
LOADED_SKILLS_BUDGET_RATIO = 0.5  # 预算：约 context_window(tokens) * 0.5 字符


def _truncate_at_boundary(text: str, limit: int) -> str:
    """按边界截断文本，避免把标题/XML 标签/步骤序号切在半截。

    边界优先级：段落（空行）> 行 > 硬切。若截断点前 40% 范围内
    找不到段落边界则退到行边界，再没有才硬切，保证保留量。
    """
    if len(text) <= limit:
        return text
    cut = text[:limit]
    min_keep = max(200, int(limit * 0.4))
    para = cut.rfind("\n\n")
    line = cut.rfind("\n")
    if para >= min_keep:
        return cut[:para].rstrip()
    if line >= min_keep:
        return cut[:line].rstrip()
    return cut.rstrip()


def render_loaded_skills(
    db: Session,
    loaded_skills: Optional[list[str]],
    context_window: int = 0,
    full_inject: bool = True,
) -> str:
    """渲染当前会话已加载技能的注入块，注入 system prompt。

    - 首轮（full_inject=True）：注入完整指令；超预算时**截断保留核心步骤**
      （不再整体跳过），并提示参考文件按需 read_skill_file 读取。
    - 后续轮次（full_inject=False）：只注入摘要 + 文件树，
      避免大技能全文反复占用上下文（"继续任务又从头加载技能文件"的根治）。

    Args:
        db: 数据库会话
        loaded_skills: 已加载技能名列表（前端持久化，按会话）
        context_window: 模型上下文窗口（token），用于计算预算
        full_inject: 是否注入完整指令（技能加载后首轮为 True，后续轮次为 False）
    """
    if not loaded_skills:
        return ""

    budget = max(8000, int((context_window or 0) * LOADED_SKILLS_BUDGET_RATIO))

    blocks: list[str] = []
    total = 0
    for name in loaded_skills:
        if not isinstance(name, str) or not name:
            continue
        skill = db.query(DshSkill).filter(
            DshSkill.name == name,
            DshSkill.is_active == True,
        ).first()
        if not skill:
            continue

        if not full_inject:
            # 后续轮次：只注入摘要 + 文件树（不重复注入全文）
            blocks.append(render_skill_summary(skill))
            continue

        block = render_skill_content(skill)
        if total + len(block) > budget:
            # 预算不足：按边界截断指令正文（XML 标签保持完整，不硬切半句），
            # 并提示被截断细节用 read_skill_file 按需读取——不诱导"重新调用 skill 工具"
            room = max(1000, budget - total)
            # 给 resources 头部/文件树/闭合标签预留约 800 字符
            block = render_skill_content(skill, max_chars=max(400, room - 800))
            if len(block) > room:
                # 极端情况：资源文件树本身就超预算 → 回退摘要块
                block = render_skill_summary(skill)
            blocks.append(block)
            total += len(block)
            continue
        blocks.append(block)
        total += len(block)

    if not blocks:
        return ""

    return "\n\n".join([
        "<system-reminder>",
        "以下技能已在此会话中加载，其完整指令已注入 system prompt。"
        "直接遵循其中的指令执行即可，无需再调用 `skill` 工具重复加载：",
        "",
        *blocks,
        "</system-reminder>",
    ])


# 摘要中核心步骤骨架的上限（字符）；够模型回顾任务结构即可，不追求完整
_CORE_STEPS_MAX_CHARS = 600
_STEP_LINE_RE = re.compile(r"^\d{1,3}[\.、\)]\s*\S")


def _extract_core_steps(content: str, max_chars: int = _CORE_STEPS_MAX_CHARS) -> str:
    """从技能正文提取"核心步骤骨架"：标题行 + 编号步骤行。

    后续轮次的摘要注入用（替代全文），让模型不重读 SKILL.md 也能
    回顾任务结构；正文无结构化行（纯散文）时回退为首段截断。
    """
    if not content:
        return ""

    kept: list[str] = []
    total = 0
    for raw in content.split("\n"):
        s = raw.strip()
        if not s:
            continue
        if not (s.startswith("#") or _STEP_LINE_RE.match(s)):
            continue
        if total + len(s) + 1 > max_chars:
            break
        kept.append(s)
        total += len(s) + 1

    if kept:
        return "\n".join(kept)

    first_para = content.strip().split("\n\n")[0]
    return _truncate_at_boundary(first_para, max_chars)


def render_skill_summary(skill: DshSkill) -> str:
    """渲染技能摘要块（后续轮次注入，避免大技能全文反复占用上下文）。

    用途 + 核心步骤骨架 + 参考文件树；模型需要细节时用 read_skill_file
    按需读取，不诱导重新调用 skill 工具。
    """
    lines = [
        f'<skill_summary name="{skill.name}">',
        f"用途：{skill.description}",
    ]
    core = _extract_core_steps(skill.content or "")
    if core:
        lines.extend(["核心步骤回顾：", core])
    if skill.dir_path:
        file_tree = list_skill_files_tree(skill.dir_path)
        if file_tree:
            lines.extend([
                "参考文件（相对路径，需要时用 read_skill_file 读取，无需重新加载技能）：",
                file_tree,
            ])
    if getattr(skill, "pack", ""):
        lines.append(
            f"所属技能包 {skill.pack}：可用 ../（如 ../../assets/xxx）或 "
            "@pack/ 前缀访问包内共享资源与兄弟技能文件。"
        )
    lines.append(
        "完整指令已在首轮注入；如需回顾，用 read_skill_file 读取该技能目录内的 SKILL.md。"
        "</skill_summary>"
    )
    return "\n".join(lines)


def list_skill_files_tree(dir_path: str, exclude_prefix: str = "") -> str:
    """列出 skill 目录中的所有文件（递归），生成文件树文本。

    借鉴 DSH skill 加载时自动展示目录结构的设计：
    - 递归遍历目录，列出所有文件（相对路径）
    - 跳过 SKILL.md（已作为正文返回）
    - 跳过隐藏文件（.开头）和常见无关目录（node_modules, __pycache__）
    - 按字母排序

    Args:
        dir_path: 目录路径
        exclude_prefix: 相对路径前缀（如 "skills/make-resume"）；
                        列出技能包根时排除技能自身目录
                        （其文件已在 skill_resources 中展示）

    Returns:
        文件树文本，每行一个相对路径；目录为空时返回空字符串
    """
    p = Path(dir_path)
    if not p.is_dir():
        return ""

    skip_dirs = {"node_modules", "__pycache__", ".git", ".DS_Store"}
    skip_files = {"SKILL.md", ".DS_Store", "Thumbs.db"}
    exclude = exclude_prefix.replace("\\", "/").strip("/") + "/" if exclude_prefix else ""

    entries = []
    for item in sorted(p.rglob("*"), key=lambda x: str(x.relative_to(p))):
        rel = item.relative_to(p)
        # 跳过隐藏文件和目录
        parts = rel.parts
        if any(part.startswith(".") for part in parts):
            continue
        if any(part in skip_dirs for part in parts):
            continue
        rel_posix = str(rel).replace("\\", "/")
        if exclude and rel_posix.startswith(exclude):
            continue
        if item.is_file():
            if rel.name in skip_files:
                continue
            entries.append(rel_posix)

    if not entries:
        return ""

    return "\n".join(f"  - {e}" for e in entries)


def render_skill_content(skill: DshSkill, max_chars: int = 0) -> str:
    """渲染 skill 的完整内容，供模型使用。

    借鉴 DSH skill 的 renderSkillContent() 函数：
    生成 <skill_content> 块，包含 skill 的完整 Markdown 正文。
    如果 skill 有磁盘目录（dir_path），还会：
    1. 列出目录中的所有参考文件（文件树）
    2. 提示模型可以使用 read_skill_file 工具读取这些文件

    这样模型在加载 skill 后就能知道目录中有哪些可用资源，
    而不是盲目地尝试路径。

    Args:
        skill: 技能对象
        max_chars: 指令正文最大字符数；超长时按段落/行边界截断正文并附
                   提示（超预算注入时使用，XML 标签始终保持完整）。
                   0 表示不截断。
    """
    lines = [
        f'<skill_content name="{skill.name}">',
    ]

    if skill.dir_path:
        file_tree = list_skill_files_tree(skill.dir_path)
        lines.extend([
            "<skill_resources>",
            f"技能目录: {skill.dir_path}",
            "",
        ])
        if file_tree:
            lines.extend([
                "目录中的参考文件（相对路径）：",
                file_tree,
                "",
                "如需查看上述任何文件的完整内容，请使用 `read_skill_file` 工具，",
                "传入 skill_name 和 file_path（相对路径）。",
            ])
        else:
            lines.extend([
                "此技能目录中没有除 SKILL.md 之外的参考文件。",
            ])
        lines.extend([
            "</skill_resources>",
            "",
        ])
    else:
        lines.extend([
            "<skill_resources>",
            "此技能为纯文本技能，无附加资源文件。",
            "</skill_resources>",
            "",
        ])

    # ── 技能包共享资源块 ──
    # pack 技能：列出包根下的共享资源与兄弟技能文件（排除自身目录），
    # 模型据此发现 ../../assets/、../<兄弟技能>/ 这类跨目录引用的真实路径
    if getattr(skill, "pack", "") and skill.dir_path:
        pack_root = SKILLS_ROOT / skill.pack
        if pack_root.is_dir():
            try:
                rel_in_pack = Path(skill.dir_path).resolve().relative_to(pack_root.resolve())
                exclude = str(rel_in_pack).replace("\\", "/")
            except ValueError:
                exclude = Path(skill.dir_path).name  # 目录不在包根下：退回按名排除
            pack_tree = list_skill_files_tree(str(pack_root), exclude_prefix=exclude)
            if pack_tree:
                lines.extend([
                    "<pack_resources>",
                    f"所属技能包: {skill.pack}（目录 {pack_root}）",
                    "",
                    "包内共享资源与兄弟技能文件（相对于技能包根）：",
                    pack_tree,
                    "",
                    "访问方式：相对本技能目录用 ../（如 ../../assets/xxx），"
                    "或用 @pack/ 前缀相对包根（如 @pack/assets/xxx）。",
                    "</pack_resources>",
                    "",
                ])

    content = skill.content
    trunc_note = ""
    if max_chars and content and len(content) > max_chars:
        content = _truncate_at_boundary(content, max_chars)
        trunc_note = (
            f"\n[技能指令较长，已按边界截断至约 {len(content)} 字符；"
            "被截断部分的细节请用 read_skill_file 读取该技能的 SKILL.md，无需重新加载]"
        )

    lines.extend([
        "<skill_instructions>",
        content + trunc_note,
        "</skill_instructions>",
        "</skill_content>",
    ])

    return "\n".join(lines)


def execute_skill_tool(skill_name: str, db: Session) -> str:
    """执行 `skill` 工具 — 加载指定名称的 skill 完整内容。

    借鉴 DSH tool-skill 的 execute() 函数：
    1. 验证 skill 名称格式
    2. 在活跃 skill 列表中查找
    3. 返回渲染后的 <skill_content> 块
    4. 如果 skill 有磁盘目录，还会列出目录中的参考文件树

    Returns:
        skill 的完整内容文本，或错误信息
    """
    if not skill_name or not SKILL_NAME_RE.match(skill_name):
        return f"错误：无效的 skill 名称 '{skill_name}'"

    skill = db.query(DshSkill).filter(
        DshSkill.name == skill_name,
        DshSkill.is_active == True,
    ).first()

    if not skill:
        return f"错误：skill '{skill_name}' 不存在或未启用"

    return render_skill_content(skill)


def read_skill_file(skill_name: str, file_path: str, db: Session, offset: int = 0, user_id: Optional[int] = None) -> str:
    """读取 skill 目录中的参考文件内容。

    借鉴 DSH 中模型通过 read_file 工具读取 skill 目录参考文件的设计：
    - 验证 skill 名称和文件路径安全性
    - 以 skill 的 dir_path 为基础解析相对路径
    - 路径穿越防护（按技能类型分边界）：
      * 独立技能：不允许 ..，最终路径必须在 skill 目录内
      * 技能包（pack）技能：允许 .. 穿出技能目录，但最终路径必须
        在包根（SKILLS_ROOT/<pack>/）内——支持 ../../assets/、
        ../<兄弟技能>/ 这类多技能包引用；也支持 @pack/ 前缀
        直接相对包根解析（如 @pack/assets/template.html）
    - 限制读取文件大小（MAX_SKILL_FILE_SIZE）
    - 二进制文件（图片/模板/字体等）无法作为文本返回时，
      复制到用户工作区 .dsh_generated/（需 user_id），
      模型可继续用 read_file / run_python 处理，用户可下载。

    Args:
        skill_name: skill 名称
        file_path: 相对于 skill 目录的文件路径，如 "references/examples.md"；
                   pack 技能还支持 "../assets/x.html" 或 "@pack/assets/x.html"
        db: 数据库会话
        offset: 字符偏移，>0 时从该位置开始返回（支持大文件分段读取）
        user_id: 用户 ID（二进制文件复制到工作区时需要；None 时二进制返回错误）

    Returns:
        文件文本内容，或错误信息
    """
    if not skill_name or not SKILL_NAME_RE.match(skill_name):
        return f"错误：无效的 skill 名称 '{skill_name}'"

    if not file_path:
        return "错误：缺少 file_path 参数"

    # 安全校验：绝对路径一律禁止
    if Path(file_path).is_absolute():
        return f"错误：不允许的文件路径 '{file_path}'"

    skill = db.query(DshSkill).filter(
        DshSkill.name == skill_name,
        DshSkill.is_active == True,
    ).first()

    if not skill:
        return f"错误：skill '{skill_name}' 不存在或未启用"

    if not skill.dir_path:
        return f"错误：skill '{skill_name}' 没有磁盘目录，无法读取参考文件"

    # ── 解析路径边界 ──
    # pack 技能（且目录确实在包根下）边界放宽到包根，允许 ../ 引用共享资源
    pack_root = None
    if getattr(skill, "pack", ""):
        candidate = (SKILLS_ROOT / skill.pack).resolve()
        try:
            Path(skill.dir_path).resolve().relative_to(candidate)
            pack_root = candidate
        except ValueError:
            pack_root = None  # 目录不在包根下（外部注册）：退回目录边界

    rel = file_path.replace("\\", "/").lstrip("/")
    if rel.startswith("@pack/"):
        if not pack_root:
            return (
                f"错误：skill '{skill_name}' 不在技能包目录下，"
                "不支持 @pack/ 路径"
            )
        base = pack_root
        rel = rel[len("@pack/"):]
        if ".." in rel:
            return f"错误：不允许的文件路径 '{file_path}'"
        full = (base / rel).resolve()
    else:
        if ".." in file_path and not pack_root:
            return (
                f"错误：不允许的文件路径 '{file_path}'"
                "（独立技能不能使用 .. 引用；如需共享资源，请使用技能包）"
            )
        base = Path(skill.dir_path)
        full = (base / file_path).resolve()

    # 防止路径穿越：确保最终路径在允许边界内
    boundary = pack_root if pack_root else Path(skill.dir_path).resolve()
    try:
        full.relative_to(boundary)
    except ValueError:
        if pack_root:
            return f"错误：非法路径 '{file_path}'，不能超出技能包 '{skill.pack}' 目录范围"
        return f"错误：非法路径 '{file_path}'，不能超出 skill 目录范围"

    if full.is_dir():
        # 目录列举：返回目录内文件列表（递归，路径已拼好可直接使用），
        # 让模型看到真实存在的文件，避免臆造文件名（如 quotation-and-data.md）。
        tree_text = list_skill_files_tree(str(full))
        if tree_text:
            prefix = f"{file_path.rstrip('/')}/" if file_path else ""
            lines = []
            for ln in tree_text.splitlines():
                ln = ln.strip()
                if ln.startswith("- "):
                    ln = ln[2:]
                if ln:
                    lines.append(f"  - {prefix}{ln}")
            return (
                f"目录 '{file_path}' 下包含以下文件（可直接用 read_skill_file 按此路径读取）：\n"
                + "\n".join(lines)
            )
        return f"目录 '{file_path}' 为空（无可读文件）"

    if not full.is_file():
        return f"错误：文件 '{file_path}' 不存在"

    # 检查文件大小
    file_size = full.stat().st_size
    if file_size > MAX_SKILL_FILE_SIZE:
        return (
            f"错误：文件 '{file_path}' 大小为 {file_size} 字节，"
            f"超过最大限制 {MAX_SKILL_FILE_SIZE} 字节（256KB）"
        )

    try:
        content = full.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # 二进制文件（图片/模板/字体等）：复制到用户工作区，模型可继续处理、用户可下载
        if not user_id:
            return f"错误：文件 '{file_path}' 不是文本文件，无法读取"
        try:
            from .workspace_service import copy_binary_to_generated
            rel = copy_binary_to_generated(user_id, full.name, full.read_bytes())
            return (
                f"文件 '{file_path}' 是二进制文件（非文本），"
                f"已复制到你的工作区：{rel}\n"
                "可用 read_file 工具读取（若为文本类资源）或 run_python 处理；"
                "用户也可在工作区面板中下载该文件。"
            )
        except Exception as e:
            return f"错误：二进制文件 '{file_path}' 复制失败: {e}"
    except Exception as e:
        return f"错误：读取文件 '{file_path}' 失败: {e}"

    # 分段读取支持：offset > 0 时从指定字符位置开始返回
    if offset > 0:
        content = content[offset:]

    return content


# ── 技能健康检查（lint）──
# 上传/编辑技能时扫描 SKILL.md 正文中的相对路径引用，对照磁盘目录
# 检查可达性——入口拦截"引用不存在/越界"的坏技能（如 ASu 包的 19 处断链）。

_LINT_MD_LINK_RE = re.compile(r"\[[^\]]*\]\(\s*<?([^)>\s]+)>?[^)]*\)")
_LINT_CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
_LINT_REF_EXT_RE = re.compile(
    r"\.(md|markdown|html?|css|js|mjs|cjs|py|json|ya?ml|csv|txt|png|jpe?g|svg|gif|webp|pdf|zip|ttf|woff2?)$",
    re.IGNORECASE,
)
_LINT_SKIP_PREFIXES = ("http://", "https://", "mailto:", "#", "data:")

# lint 输出的最大警告条数（防超长引用列表刷屏）
_LINT_MAX_WARNINGS = 50


def _lintable_link_target(t: str) -> bool:
    """Markdown 链接目标是否值得检查：相对路径（非 URL/锚点/纯锚）。
    含空格的目标（如 "/a → /b" 工作流链）跳过——真实文件路径几乎不含空格；
    含 <> 的目标（如 "<brand>.svg"）是占位符——这些字符在 Windows 下非法。"""
    if not t or t.startswith(_LINT_SKIP_PREFIXES):
        return False
    if " " in t or "<" in t or ">" in t:
        return False
    return True


def _lintable_code_ref(t: str) -> bool:
    """代码片段中的路径是否值得检查：含 / 或以 ./ ../ 开头，且是
    目录引用（尾 /）或带常见扩展名——排除普通代码标识符噪声。
    绝对路径（/ 开头）一律检查（运行时必然被拒绝）。
    含空格（工作流箭头链）或 <>（占位符）的目标跳过。"""
    t = t.strip()
    if not t or t.startswith(_LINT_SKIP_PREFIXES):
        return False
    if " " in t or "<" in t or ">" in t:
        return False
    if t.startswith("/"):
        return True
    has_sep = "/" in t
    starts_dot = t.startswith("./") or t.startswith("../")
    if not (has_sep or starts_dot):
        return False
    return t.endswith("/") or bool(_LINT_REF_EXT_RE.search(t))


def lint_skill_references(content: str, dir_path: str, pack: str = "") -> list[str]:
    """扫描技能正文中的相对路径引用，检查磁盘可达性。

    检查规则（与 read_skill_file 的运行时边界一致）：
    - Markdown 链接目标 [..](path) 和代码片段中的路径字面量
    - 跳过 http(s)/mailto/锚点/data URI
    - pack 技能：允许 .. 到包根内；@pack/ 前缀按包根解析
    - 独立技能：.. 引用直接告警（运行时会被拒绝）
    - 绝对路径引用告警（运行时被拒绝）

    Args:
        content: SKILL.md 正文
        dir_path: 技能磁盘目录（空=纯文本技能，跳过检查）
        pack: 所属技能包名（空=独立技能）

    Returns:
        警告列表（最多 _LINT_MAX_WARNINGS 条；空列表=健康）
    """
    if not content or not dir_path:
        return []

    base = Path(dir_path).resolve()
    pack_root = None
    safe_pack = _safe_pack_name(pack)
    if safe_pack:
        candidate = (SKILLS_ROOT / safe_pack).resolve()
        if base != candidate:
            try:
                base.relative_to(candidate)
                pack_root = candidate
            except ValueError:
                pass  # 目录不在包根下：按独立技能边界检查

    # 收集引用 → 首次出现行号
    refs: dict[str, int] = {}
    for lineno, line in enumerate(content.split("\n"), 1):
        for m in _LINT_MD_LINK_RE.finditer(line):
            t = m.group(1).strip()
            if _lintable_link_target(t):
                refs.setdefault(t, lineno)
        for m in _LINT_CODE_SPAN_RE.finditer(line):
            t = m.group(1).strip()
            if _lintable_code_ref(t):
                refs.setdefault(t, lineno)

    warnings: list[str] = []
    seen: set[str] = set()
    for ref, lineno in refs.items():
        key = ref
        if key in seen:
            continue
        seen.add(key)
        if len(warnings) >= _LINT_MAX_WARNINGS:
            warnings.append(f"... 引用过多，仅显示前 {_LINT_MAX_WARNINGS} 条")
            break

        if ref.startswith("@pack/"):
            if not pack_root:
                warnings.append(
                    f"第 {lineno} 行: '@pack/' 引用 '{ref}'，但该技能不属于技能包"
                    "（需设置 pack 且目录位于 SKILLS_ROOT/<pack>/ 下）"
                )
                continue
            target = (pack_root / ref[len("@pack/"):]).resolve()
            try:
                target.relative_to(pack_root)
            except ValueError:
                warnings.append(f"第 {lineno} 行: '@pack/' 引用 '{ref}' 超出技能包目录范围")
                continue
            if not target.exists():
                warnings.append(f"第 {lineno} 行: 引用不存在 '@pack/' 路径 '{ref}'（包内未找到该文件）")
            continue

        if ref.startswith("/"):
            # 单段无扩展名的 "/xxx"（如 /asu、/interview）通常是技能的
            # 斜杠命令写法而非文件路径，跳过避免误报
            segments = [s for s in ref.split("/") if s]
            if len(segments) <= 1 and not _LINT_REF_EXT_RE.search(ref):
                continue
            warnings.append(
                f"第 {lineno} 行: 绝对路径引用 '{ref}'——read_skill_file 不允许绝对路径，"
                "请改用相对路径"
            )
            continue

        if ".." in ref and not pack_root:
            warnings.append(
                f"第 {lineno} 行: '..' 引用 '{ref}'——独立技能不允许 .. 穿越目录；"
                "如需共享资源，请把技能放入技能包（pack）"
            )
            continue

        target = (base / ref).resolve()
        boundary = pack_root if pack_root else base
        try:
            target.relative_to(boundary)
        except ValueError:
            warnings.append(
                f"第 {lineno} 行: 引用 '{ref}' 超出"
                + (f"技能包 '{safe_pack}'" if pack_root else "技能目录")
                + "范围，运行时将无法读取"
            )
            continue
        if not target.exists():
            scope = f"技能包 '{safe_pack}' 内" if pack_root else "技能目录内"
            warnings.append(f"第 {lineno} 行: 引用不存在 '{ref}'（{scope}未找到该文件/目录）")

    return warnings

