# -*- coding: utf-8 -*-
"""
Skills 模块（对齐 Hermes agent/skill_utils.py + tools/skills_tool.py + prompt_builder 技能索引）

设计：索引常驻 + 内容按需加载（progressive disclosure）
    - 技能存放在 skills/<技能名>/SKILL.md，头部 YAML frontmatter 只取
      name / description / platforms / prerequisites / metadata.hermes
      （零依赖解析，不引 pyyaml；支持两级缩进嵌套与流式/块式列表）
    - 系统提示词里注入「可用技能索引」（名称 + 一句话描述）——模型一开始
      只知道有什么技能、不知道内容（对齐 Hermes 的 build_skills_system_prompt）
    - skills_list()  列出技能元信息（第 1 层：渐进披露的最小元数据）
    - skill_view()   按需加载 SKILL.md 全文或技能包内子文件（references/ 等，
      第 2-3 层），对齐 Hermes 同名工具
    - 前置条件检查（对齐 Hermes agent/skill_utils.py::extract_skill_conditions +
      agent/prompt_builder.py::_skill_should_show + tools/skills_tool.py）：
      - 索引期条件激活：frontmatter 的 metadata.hermes.requires_tools /
        fallback_for_tools（及 toolsets 两组）按当前可用工具集过滤技能，
        requires_tools 缺工具 → 隐藏；fallback_for_tools 主工具已存在 → 隐藏兜底技能
      - 加载期前置检查：prerequisites.env_vars（旧式）/ required_environment_variables
        （新式）缺失 → skill_view 返回 setup_needed + 缺失清单；
        prerequisites.commands 仅 advisory（Hermes 语义：列出但不阻塞）

简化掉的部分：
    - 技能 hub / 组织共享同步 / 插件命名空间（plugin:skill）
    - YAML 完整解析（锚点、多行字符串、块式嵌套列表等）——骨架只支持
      key: value / key: [a, b] / 缩进嵌套映射 / 块式标量列表
    - required_credential_files（Hermes 依赖 tools/credential_files 注册挂载）
    - 上下文压缩时的技能 prune/reinject（Hermes 会把已加载技能标记后裁掉）
"""

import json
import os
import re
import shutil
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
    骨架简化：支持 `key: value`（引号包裹、`[a, b]` 流式列表）与缩进嵌套映射
    （如 prerequisites.env_vars、metadata.hermes.requires_tools），
    以及 `- item` 块式标量列表；不解析锚点/多行字符串等复杂 YAML。
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
    lines: list[tuple[int, str]] = []
    for raw in yaml_content.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append((len(raw) - len(raw.lstrip(" ")), stripped))
    if lines:
        frontmatter, _ = _parse_yaml_mapping(lines, 0, lines[0][0])
    return frontmatter, body


def _parse_yaml_mapping(
    lines: list[tuple[int, str]],
    start: int,
    indent: int,
) -> tuple[dict[str, Any], int]:
    """从 lines[start:] 解析一个缩进层级为 indent 的映射，返回 (dict, 下一行下标)。

    骨架级 YAML 子集：支持 `key: value`、`key: [a, b]`、空值 key 后跟更深缩进的
    子映射或块式标量列表（`- item`）。缩进小于当前层级的行结束本映射。
    """
    result: dict[str, Any] = {}
    i = start
    while i < len(lines):
        line_indent, text = lines[i]
        if line_indent < indent:
            break
        if line_indent > indent or text.startswith("- "):
            break  # 更深缩进/列表项属于上一项的值，由递归处理
        key, _, value = text.partition(":")
        key = key.strip()
        value = value.strip()
        if not key:
            i += 1
            continue
        if value:
            result[key] = _parse_frontmatter_value(value)
            i += 1
            continue
        if i + 1 < len(lines) and lines[i + 1][0] > indent:
            child_indent = lines[i + 1][0]
            if lines[i + 1][1].startswith("- "):
                result[key], i = _parse_yaml_block_list(lines, i + 1, child_indent)
            else:
                result[key], i = _parse_yaml_mapping(lines, i + 1, child_indent)
        else:
            result[key] = None
            i += 1
    return result, i


def _parse_yaml_block_list(
    lines: list[tuple[int, str]],
    start: int,
    indent: int,
) -> tuple[list[Any], int]:
    """解析缩进层级为 indent 的块式列表，返回 (列表, 下一行下标)。

    支持两种项：
    - 标量项 `- item`（如 prerequisites.env_vars 的块式写法）
    - 映射项 `- name: X`（如 required_environment_variables 的现代写法），
      后续更深缩进的行作为该项的子字段，直到下一个同缩进 `- ` 项
    """
    items: list[Any] = []
    i = start
    while i < len(lines):
        line_indent, text = lines[i]
        if line_indent < indent:
            break
        if line_indent > indent:
            i += 1  # 列表项的悬挂内容（少见），跳过
            continue
        if not text.startswith("- "):
            break
        item = text[2:].strip()
        if not item:
            i += 1
            continue
        if re.match(r"^[A-Za-z_][A-Za-z0-9_-]*\s*:", item):
            # 映射项：- key: value（可带更深缩进的子字段）
            key, _, value = item.partition(":")
            entry: dict[str, Any] = {
                key.strip(): _parse_frontmatter_value(value.strip()) if value.strip() else None
            }
            j = i + 1
            while j < len(lines) and lines[j][0] > indent:
                child_indent = lines[j][0]
                if lines[j][1].startswith("- "):
                    break
                child_map, j = _parse_yaml_mapping(lines, j, child_indent)
                entry.update(child_map)
            items.append(entry)
            i = j
        else:
            items.append(_parse_frontmatter_value(item))
            i += 1
    return items, i


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
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    return value


def extract_skill_conditions(frontmatter: dict[str, Any]) -> dict[str, list[str]]:
    """从 frontmatter 提取条件激活字段（对齐 Hermes agent/skill_utils.py 同名函数）。

    读取 metadata.hermes.requires_tools / fallback_for_tools / requires_toolsets /
    fallback_for_toolsets；metadata 或 hermes 不是 dict（畸形 YAML）时按空处理。
    """
    metadata = frontmatter.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    hermes = metadata.get("hermes")
    if not isinstance(hermes, dict):
        hermes = {}
    return {
        "fallback_for_toolsets": _normalize_prerequisite_values(hermes.get("fallback_for_toolsets")),
        "requires_toolsets": _normalize_prerequisite_values(hermes.get("requires_toolsets")),
        "fallback_for_tools": _normalize_prerequisite_values(hermes.get("fallback_for_tools")),
        "requires_tools": _normalize_prerequisite_values(hermes.get("requires_tools")),
    }


def skill_conditions_ok(
    conditions: dict[str, Any],
    available_tools: Optional[set[str]] = None,
    available_toolsets: Optional[set[str]] = None,
) -> bool:
    """判断条件激活是否放行该技能（对齐 Hermes agent/prompt_builder.py::_skill_should_show）。

    规则（与 Hermes 一致）：
    - available_tools 与 available_toolsets 都未提供 → 全部显示（向后兼容）
    - fallback_for_*：主工具/工具集已存在 → 隐藏兜底技能
    - requires_*：所需工具/工具集缺失 → 隐藏
    """
    if available_tools is None and available_toolsets is None:
        return True
    at = set(available_tools or ())
    ats = set(available_toolsets or ())
    for ts in conditions.get("fallback_for_toolsets", []):
        if ts in ats:
            return False
    for t in conditions.get("fallback_for_tools", []):
        if t in at:
            return False
    for ts in conditions.get("requires_toolsets", []):
        if ts not in ats:
            return False
    for t in conditions.get("requires_tools", []):
        if t not in at:
            return False
    return True


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
def discover_skills(
    available_tools: Optional[set[str]] = None,
    available_toolsets: Optional[set[str]] = None,
) -> list[dict[str, Any]]:
    """扫描 skills/ 目录，返回可用技能列表（含元信息、条件与路径）。

    规则（对齐 Hermes）：
    - 只认包含 SKILL.md 的目录为技能根，技能名 = frontmatter name 或目录名
    - 跳过 EXCLUDED_DIRS（.git、__pycache__ 等）与 SKILL_SUPPORT_DIRS
      （references/ 等只是技能包的支持文件，不是独立技能）
    - 父目录在 skills/ 下的层级视为分类（category）
    - 声明了 platforms 且与当前系统不匹配的技能不列入
    - metadata.hermes 条件激活不满足的技能不列入（对齐 Hermes 的
      _skill_should_show：available_tools/available_toolsets 未提供时全部显示）
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
        if not skill_conditions_ok(
            extract_skill_conditions(frontmatter),
            available_tools,
            available_toolsets,
        ):
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
            "conditions": extract_skill_conditions(frontmatter),
            "path": str(skill_root),
        })
    skills.sort(key=lambda s: (s["category"] or "", s["name"]))
    return skills


def build_skills_index(
    available_tools: Optional[set[str]] = None,
    available_toolsets: Optional[set[str]] = None,
) -> str:
    """渲染「可用技能索引」区块，注入系统提示词（对齐 Hermes build_skills_system_prompt）。

    只放名称 + 一句话描述，控制 token 占用；完整内容由 skill_view 按需加载。
    传入 available_tools 时按条件激活过滤（requires_tools 缺失隐藏、fallback 兜底隐藏）。
    """
    skills = discover_skills(available_tools, available_toolsets)
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


def _normalize_prerequisite_values(value: Any) -> list[str]:
    """把前置条件字段归一化成去重后的字符串列表（对齐 Hermes skills_tool 同名函数）。"""
    if not value:
        return []
    if isinstance(value, str):
        value = [value]
    return [str(item).strip() for item in value if str(item).strip()]


def _collect_prerequisite_values(
    frontmatter: dict[str, Any],
) -> tuple[list[str], list[str]]:
    """收集旧式前置条件：prerequisites.env_vars / prerequisites.commands（对齐 Hermes）。"""
    prereqs = frontmatter.get("prerequisites")
    if not isinstance(prereqs, dict):
        return [], []
    return (
        _normalize_prerequisite_values(prereqs.get("env_vars")),
        _normalize_prerequisite_values(prereqs.get("commands")),
    )


def _env_lookup(name: str) -> str:
    """按 Hermes _is_env_var_persisted 的语义查环境变量：os.environ 优先，.env 兜底。

    变量已出现在 os.environ 时以其为准（空值视为缺失），否则查 .env 兜底；
    .env 只做 KEY=VALUE 简单解析，不引 python-dotenv，保证零依赖可测。
    """
    if name in os.environ:
        return os.environ.get(name, "")
    try:
        env_file = BASE_DIR / ".env"
        if env_file.is_file():
            for raw in env_file.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                if key.strip() == name:
                    return val.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _get_required_environment_variables(frontmatter: dict[str, Any]) -> list[dict[str, Any]]:
    """合并新旧两种 env var 声明（对齐 Hermes skills_tool._get_required_environment_variables 简化版）。

    旧式：prerequisites.env_vars（字符串列表）；新式：required_environment_variables
    （字符串或 {name, optional, prompt, help, required_for} 字典）；同名只保留首个。
    """
    legacy_env_vars, _ = _collect_prerequisite_values(frontmatter)
    required: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name in legacy_env_vars:
        if name and name not in seen:
            seen.add(name)
            required.append({"name": name})
    raw = frontmatter.get("required_environment_variables")
    if isinstance(raw, str):
        raw = [raw]
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                entry: dict[str, Any] = {"name": item}
            elif isinstance(item, dict):
                entry = {"name": str(item.get("name") or "").strip()}
                for field in ("prompt", "help", "required_for"):
                    if item.get(field):
                        entry[field] = item[field]
                if item.get("optional"):
                    entry["optional"] = True
            else:
                continue
            name = str(entry.get("name") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            required.append(entry)
    return required


def check_skill_readiness(frontmatter: dict[str, Any]) -> dict[str, Any]:
    """加载期前置检查（对齐 Hermes skills_tool 的 readiness 语义）。

    返回结构与 Hermes skill_view 对齐：required_environment_variables /
    missing_required_environment_variables / setup_needed / readiness_status /
    setup_note；required_commands 与 missing_required_commands 只做 advisory
    （Hermes 语义：command 检查不进 setup_needed，缺失仅提示不阻塞）。
    """
    required_envs = _get_required_environment_variables(frontmatter)
    missing_envs = [
        entry["name"]
        for entry in required_envs
        if not entry.get("optional") and not _env_lookup(entry["name"])
    ]
    _legacy_envs, commands = _collect_prerequisite_values(frontmatter)
    missing_commands = [cmd for cmd in commands if shutil.which(cmd) is None]
    setup_needed = bool(missing_envs)
    setup_note = None
    if setup_needed:
        missing_items = ", ".join(f"env ${name}" for name in missing_envs)
        setup_note = f"Setup needed before using this skill: missing {missing_items}."
    return {
        "required_environment_variables": [entry["name"] for entry in required_envs],
        "required_commands": commands,
        "missing_required_environment_variables": missing_envs,
        "missing_required_commands": missing_commands,
        "setup_needed": setup_needed,
        "readiness_status": "setup_needed" if setup_needed else "available",
        "setup_note": setup_note,
    }


def skills_list(
    available_tools: Optional[set[str]] = None,
    available_toolsets: Optional[set[str]] = None,
) -> str:
    """skills_list 工具：列出可用技能（第 1 层：最小元数据，对齐 Hermes）。

    传入 available_tools 时同样按条件激活过滤（与系统提示词索引一致）。
    """
    skills = discover_skills(available_tools, available_toolsets)
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
            "hint": (
                "Use skill_view(name) to see full content, linked files, "
                "and prerequisite readiness"
            ),
        },
        ensure_ascii=False,
    )


def skill_view(name: str, file_path: str = "") -> str:
    """skill_view 工具：加载技能全文或技能包内子文件（第 2-3 层，对齐 Hermes）。

    file_path 为空时返回 SKILL.md 的正文（frontmatter 剥离）+ 可加载文件清单；
    指定时返回技能目录内的对应文件内容（references/ 等支持文件）。
    名字/路径非法（绝对路径、..、不存在）返回 success=False，绝不越界读文件。
    主视图附带前置条件 readiness（缺失 env 时 setup_needed，对齐 Hermes）；
    显式按名字加载不做条件激活过滤（对齐 Hermes：explicit load 绕过 offer-time gate）。
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
        result = {
            "success": True,
            "name": str(frontmatter.get("name") or skill_root.name),
            "description": frontmatter.get("description", ""),
            "content": body.strip(),
            "files": _list_skill_files(skill_root),
            # 前置条件 readiness（对齐 Hermes skill_view：env 缺失 → setup_needed）
            **check_skill_readiness(frontmatter),
        }
        metadata = frontmatter.get("metadata")
        if isinstance(metadata, dict):
            result["metadata"] = metadata
        return json.dumps(result, ensure_ascii=False)

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
