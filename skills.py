# -*- coding: utf-8 -*-
"""
Skills 模块（对齐 Hermes agent/skill_utils.py + tools/skills_tool.py + prompt_builder 技能索引）

设计：索引常驻 + 内容按需加载（progressive disclosure）
    - 技能存放在 skills/<技能名>/SKILL.md，头部 YAML frontmatter 只取
      name / description / platforms（零依赖解析，不引 pyyaml）
    - 系统提示词里注入「可用技能索引」（名称 + 一句话描述）——模型一开始
      只知道有什么技能、不知道内容（对齐 Hermes 的 build_skills_system_prompt）
    - skills_list()  列出技能元信息（第 1 层：渐进披露的最小元数据）
    - skill_view()   按需加载 SKILL.md 全文或技能包内子文件（references/ 等，
      第 2-3 层），对齐 Hermes 同名工具

简化掉的部分：
    - 技能 hub / 组织共享同步 / 插件命名空间（plugin:skill）/ 前置条件检查
    - YAML 完整解析（列表、嵌套 metadata）——骨架只支持简单 key: value
    - 上下文压缩时的技能 prune/reinject（Hermes 会把已加载技能标记后裁掉）
"""

import json
import re
import sys
from pathlib import Path
from typing import Any, Optional

BASE_DIR = Path(__file__).parent
SKILLS_DIR = BASE_DIR / "skills"

# 扫描时跳过的目录（对齐 Hermes agent/skill_utils.py 的 EXCLUDED_SKILL_DIRS 子集）
EXCLUDED_DIRS = frozenset({
    ".git", ".github", ".archive", ".venv", "venv", "node_modules",
    "site-packages", "__pycache__", ".pytest_cache",
})

# 技能包内的支持文件目录：不单独成技能，只能通过 skill_view 的 file_path 加载
SKILL_SUPPORT_DIRS = frozenset(("references", "templates", "assets", "scripts"))

# 平台映射（对齐 Hermes：技能 frontmatter 写平台名，代码里是 sys.platform）
PLATFORM_MAP = {"macos": "darwin", "linux": "linux", "windows": "win32"}


# =========================================================================
# frontmatter 解析（Hermes parse_frontmatter 的零依赖简化版）
# =========================================================================
def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """解析 SKILL.md 的 YAML frontmatter，返回 (元数据, 正文)。

    与 Hermes 一致的处理：
    - 剥离开头的 UTF-8 BOM（Windows 记事本/重定向保存会带上，不剥离会导致
      frontmatter 围栏失效、整个元数据被静默丢弃）
    - 只有以 `---` 开头且存在结束围栏才解析，否则整个文件视为正文
    骨架简化：只解析简单 `key: value`（支持引号包裹和 `[a, b]` 列表），
    嵌套结构（metadata 等）不展开。
    """
    frontmatter: dict[str, Any] = {}
    if content.startswith("\ufeff"):
        content = content[1:]
    body = content

    if not content.startswith("---"):
        return frontmatter, body
    end_match = re.search(r"\n---\s*\n", content[3:])
    if not end_match:
        return frontmatter, body

    yaml_content = content[3:end_match.start() + 3]
    body = content[end_match.end() + 3:]
    for line in yaml_content.strip().splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        frontmatter[key.strip()] = _parse_frontmatter_value(value.strip())
    return frontmatter, body


def _parse_frontmatter_value(value: str) -> Any:
    """把 frontmatter 的简单值解析成字符串 / 列表（引号与 [a, b] 列表）。"""
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        items = []
        for item in value[1:-1].split(","):
            item = item.strip().strip('"').strip("'")
            if item:
                items.append(item)
        return items
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value


def _skill_matches_platform(frontmatter: dict[str, Any]) -> bool:
    """按 frontmatter 的 platforms 字段过滤技能（对齐 Hermes skill_matches_platform）。

    未声明 platforms 视为全平台可用；声明了但当前系统不在列表内 → 跳过。
    """
    platforms = frontmatter.get("platforms")
    if not platforms:
        return True
    if isinstance(platforms, str):
        platforms = [platforms]
    current = sys.platform
    for platform in platforms:
        normalized = str(platform).strip().lower()
        mapped = PLATFORM_MAP.get(normalized, normalized)
        if current.startswith(mapped):
            return True
    return False


# =========================================================================
# 技能发现（Hermes iter_skill_index_files / _find_all_skills 的简化版）
# =========================================================================
def discover_skills() -> list[dict[str, Any]]:
    """扫描 skills/ 目录，返回可用技能列表（含元信息与路径）。

    规则（对齐 Hermes）：
    - 只认包含 SKILL.md 的目录为技能根，技能名 = frontmatter name 或目录名
    - 跳过 EXCLUDED_DIRS（.git、__pycache__ 等）与 SKILL_SUPPORT_DIRS
      （references/ 等只是技能包的支持文件，不是独立技能）
    - 父目录在 skills/ 下的层级视为分类（category）
    - 声明了 platforms 且与当前系统不匹配的技能不列入
    """
    if not SKILLS_DIR.exists():
        return []
    skills: list[dict[str, Any]] = []
    for skill_md in SKILLS_DIR.rglob("SKILL.md"):
        skill_root = skill_md.parent
        if any(part in EXCLUDED_DIRS or part in SKILL_SUPPORT_DIRS
               for part in skill_root.relative_to(SKILLS_DIR).parts):
            continue
        try:
            content = skill_md.read_text(encoding="utf-8")
        except OSError:
            continue
        frontmatter, _body = parse_frontmatter(content)
        if not _skill_matches_platform(frontmatter):
            continue
        name = str(frontmatter.get("name") or skill_root.name).strip()
        description = str(frontmatter.get("description", "")).strip()
        category = None
        if skill_root.parent != SKILLS_DIR:
            category = skill_root.parent.name
        skills.append({
            "name": name,
            "description": description,
            "category": category,
            "path": str(skill_root),
        })
    skills.sort(key=lambda s: (s["category"] or "", s["name"]))
    return skills


def build_skills_index() -> str:
    """渲染「可用技能索引」区块，注入系统提示词（对齐 Hermes build_skills_system_prompt）。

    只放名称 + 一句话描述，控制 token 占用；完整内容由 skill_view 按需加载。
    """
    skills = discover_skills()
    if not skills:
        return ""
    lines = ["## 可用技能（Skills）", "需要专业技能时，先用 skills_list 查看，再用 skill_view 加载。"]
    for skill in skills:
        name = skill["name"]
        desc = skill["description"] or "（无描述）"
        prefix = f"[{skill['category']}] " if skill.get("category") else ""
        lines.append(f"- {prefix}{name}：{desc}")
    return "\n".join(lines)


# =========================================================================
# 技能查找与安全校验（Hermes _skill_lookup_path_error + _serve_skill 的简化版）
# =========================================================================
def _resolve_skill_root(name: str) -> Optional[Path]:
    """按技能名定位技能根目录；名字非法或不存在返回 None。

    安全规则（对齐 Hermes）：
    - 空名、绝对路径、含 `..` 的路径一律拒绝（防路径穿越）
    - 只允许指向 skills/ 内部、且确实包含 SKILL.md 的目录
    """
    name = (name or "").strip()
    if not name:
        return None
    candidate = Path(name)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    skill_root = (SKILLS_DIR / candidate).resolve()
    try:
        skill_root.relative_to(SKILLS_DIR.resolve())
    except ValueError:
        return None
    if not (skill_root / "SKILL.md").exists():
        return None
    return skill_root


def _list_skill_files(skill_root: Path) -> list[str]:
    """列出技能包内可加载的支持文件（相对路径），供模型发现。"""
    files = []
    for path in sorted(skill_root.rglob("*")):
        if not path.is_file() or path.name == "SKILL.md":
            continue
        rel = path.relative_to(skill_root)
        if any(part in EXCLUDED_DIRS for part in rel.parts):
            continue
        files.append(rel.as_posix())
    return files


def skills_list() -> str:
    """skills_list 工具：列出可用技能（第 1 层：最小元数据，对齐 Hermes）。"""
    skills = discover_skills()
    categories = sorted({s["category"] for s in skills if s.get("category")})
    return json.dumps(
        {
            "success": True,
            "skills": [
                {"name": s["name"], "description": s["description"],
                 "category": s.get("category")}
                for s in skills
            ],
            "categories": categories,
            "count": len(skills),
            "hint": "Use skill_view(name) to see full content and linked files",
        },
        ensure_ascii=False,
    )


def skill_view(name: str, file_path: str = "") -> str:
    """skill_view 工具：加载技能全文或技能包内子文件（第 2-3 层，对齐 Hermes）。

    file_path 为空时返回 SKILL.md 的正文（frontmatter 剥离）+ 可加载文件清单；
    指定时返回技能目录内的对应文件内容（references/ 等支持文件）。
    名字/路径非法（绝对路径、..、不存在）返回 success=False，绝不越界读文件。
    """
    skill_root = _resolve_skill_root(name)
    if skill_root is None:
        return json.dumps(
            {
                "success": False,
                "error": f"技能 '{name}' 不存在",
                "hint": "Use skills_list to see available skills",
            },
            ensure_ascii=False,
        )

    try:
        raw = (skill_root / "SKILL.md").read_text(encoding="utf-8")
    except OSError as exc:
        return json.dumps({"success": False, "error": f"读取 SKILL.md 失败：{exc}"},
                          ensure_ascii=False)
    frontmatter, body = parse_frontmatter(raw)

    if not file_path:
        return json.dumps(
            {
                "success": True,
                "name": str(frontmatter.get("name") or skill_root.name),
                "description": frontmatter.get("description", ""),
                "content": body.strip(),
                "files": _list_skill_files(skill_root),
            },
            ensure_ascii=False,
        )

    # 子文件：必须是技能目录内的相对路径，且解析后不得越界
    file_path = (file_path or "").strip().lstrip("/\\")
    target = (skill_root / file_path).resolve()
    try:
        target.relative_to(skill_root.resolve())
    except ValueError:
        return json.dumps({"success": False, "error": f"路径越界：{file_path}"},
                          ensure_ascii=False)
    if not target.is_file():
        return json.dumps({"success": False, "error": f"文件不存在：{file_path}"},
                          ensure_ascii=False)
    try:
        content = target.read_text(encoding="utf-8")
    except OSError as exc:
        return json.dumps({"success": False, "error": f"读取失败：{exc}"},
                          ensure_ascii=False)
    return json.dumps({"success": True, "name": name, "file": file_path,
                       "content": content}, ensure_ascii=False)
