# -*- coding: utf-8 -*-
"""
文件工具模块（对齐 Hermes tools/file_tools.py）

提供三个核心文件工具：
    - read_file(path, offset, limit)   分页 + 行号 + 字符预算截断
    - write_file(path, content)        先过敏感路径检查再写入
    - search_files(path, pattern)      递归搜索文件名/内容（跳过敏感文件）
    - patch(path, ...)                 局部替换 / V4A 批量补丁
    - read_file 对 .docx/.xlsx/.ipynb 自动走 read_extract 抽取成文本

安全设计（对齐 Hermes 的"配对门"思路）：
    - 终端侧写文件由 approval.py 拦（rm/tee/重定向到 .env 等）
    - 文件工具侧由 _check_sensitive_path 拦：系统目录、.env、
      approval_allowlist.json（本骨架的安全策略文件）、~/.ssh、密钥文件、
      shell 启动文件、docker.sock——两侧都堵上才没有绕过路径
    - Hermes 对敏感文件读取用 agent/redact.py 做脱敏；骨架无脱敏模块，
      因此读 .env / 密钥类文件直接拒绝（更保守的简化，文档注明）

简化掉的部分（Hermes 有，骨架不做）：
    - 跨 profile 检查、文件陈旧检测（stale check）、read→modify→write 锁
    - 文档抽取（.docx/.xlsx 转文本）、设备文件/二进制扩展名精细判定
    - patch 的 LSP/语法检查接线（Hermes 有 lint/LSP 层，骨架只做 .py 的
      ast.parse 语法提示，非阻塞）
"""

import ast
import difflib
import json
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Tuple

from redact import redact_sensitive_text
from read_extract import ExtractionError, extract_document_text, is_extractable_document

# 读取字符预算（对齐 Hermes 的默认读取上限）
READ_MAX_CHARS = 20_000
# 搜索最多返回的匹配数
SEARCH_RESULT_LIMIT = 50

# 扫描时跳过的目录（与 skills.py 的排除集一致）
EXCLUDED_DIRS = frozenset({
    ".git", ".github", ".archive", ".venv", "venv", "node_modules",
    "site-packages", "__pycache__", ".pytest_cache", ".tox", ".nox",
})

# 拒绝写入的系统路径前缀（对齐 Hermes _SENSITIVE_PATH_PREFIXES，另加 Windows 系统目录）
_SYSTEM_PATH_PREFIXES = (
    "/etc/", "/boot/", "/usr/lib/systemd/",
    "/private/etc/", "/private/var/db/", "/private/var/root/",
    "C:/Windows/System32/", "C:/Windows/System/",
)
_SENSITIVE_EXACT_PATHS = {"/var/run/docker.sock", "/run/docker.sock"}

# 项目安全文件：任何路径下叫这个名字的文件都拒绝写（.env 存密钥，
# approval_allowlist.json 是审批策略，改它等于关自己的安全门）
_SENSITIVE_FILE_NAMES = {".env", "approval_allowlist.json"}

# 用户敏感文件：密钥/凭据与 shell 启动文件
_USER_SENSITIVE_DIRS = ("~/.ssh",)
_USER_SENSITIVE_FILES = frozenset({
    ".netrc", ".pgpass", ".npmrc", ".pypirc",
    ".bashrc", ".zshrc", ".profile", ".bash_profile", ".zprofile",
})


def _normalize_path(filepath: str) -> Path:
    """把用户给的路径规范成绝对路径（展开 ~、统一大小写）。"""
    expanded = Path(filepath).expanduser()
    candidate = expanded if expanded.is_absolute() else Path.cwd() / expanded
    return Path(os.path.normcase(os.path.abspath(str(candidate))))


def _check_sensitive_path(filepath: str) -> Optional[str]:
    """检查路径是否命中敏感清单（写入前调用）；命中返回错误消息，否则 None。

    对齐 Hermes _check_sensitive_path：先查系统前缀与精确路径，再查项目安全
    文件与用户密钥/启动文件。读取不经过这里——读取由 redact.py 打码兜底。
    """
    normalized = os.path.normcase(os.path.normpath(os.path.expanduser(filepath)))
    resolved = str(_normalize_path(filepath))

    for prefix in _SYSTEM_PATH_PREFIXES:
        prefix = os.path.normcase(prefix.replace("/", os.sep))
        if resolved.startswith(prefix) or normalized.startswith(prefix):
            return _sensitive_error(filepath, "系统目录")
    if resolved in _SENSITIVE_EXACT_PATHS or normalized in _SENSITIVE_EXACT_PATHS:
        return _sensitive_error(filepath, "docker.sock")

    name = Path(normalized).name
    if name in _SENSITIVE_FILE_NAMES:
        return _sensitive_error(filepath, "项目安全文件")

    expanded = os.path.expanduser(filepath)
    for sensitive_dir in _USER_SENSITIVE_DIRS:
        dir_prefix = os.path.normcase(
            os.path.normpath(os.path.expanduser(sensitive_dir)) + os.sep
        )
        if normalized.startswith(dir_prefix) or resolved.startswith(dir_prefix):
            return _sensitive_error(filepath, "用户敏感目录")
    if name in _USER_SENSITIVE_FILES:
        return _sensitive_error(filepath, "用户凭据/启动文件")
    return None


def _sensitive_error(filepath: str, kind: str) -> str:
    """构造敏感路径拒绝消息（对齐 Hermes 的措辞风格）。"""
    return (
        f"Refusing to access sensitive path ({kind}): {filepath}\n"
        "Agent cannot read or modify security-sensitive files. "
        "Manage them manually or via environment variables."
    )


def _read_text(path: Path) -> tuple[Optional[str], Optional[str]]:
    """读文本文件；二进制（含 NUL 字节）返回错误，正常返回 (内容, None)。"""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return None, f"读取失败：{exc}"
    if b"\x00" in raw[:4096]:
        return None, f"拒绝读取二进制文件：{path}"
    return raw.decode("utf-8", errors="replace"), None


def read_file_tool(path: str, offset: int = 1, limit: int = 200) -> str:
    """read_file 工具：分页读取文本文件，带行号与字符预算截断（对齐 Hermes）。

    offset/limit 与 Hermes 一致（从第 1 行开始，每页默认 200 行）；
    内容超过 READ_MAX_CHARS 时截断并返回 hint。敏感内容（密钥/令牌）读取时
    自动打码（file_read 哨兵，防模型把打码值写回文件，对齐 Hermes）。
    .docx / .xlsx / .ipynb 文档自动经 read_extract 抽取成文本后再分页
    （返回带 extracted_document=True；抽取失败给明确错误，不回退成乱码）。
    """
    offset = max(1, int(offset or 1))
    limit = max(1, min(int(limit or 200), 2000))
    resolved = _normalize_path(path)
    if not resolved.is_file():
        return _error(f"文件不存在：{path}")

    # 结构化文档抽取：先于二进制判定，.docx/.xlsx/.ipynb 渲染成文本
    # （对齐 Hermes read_file_tool：is_extractable_document → extract_document_text）
    if is_extractable_document(str(resolved)):
        try:
            content = extract_document_text(str(resolved))
        except ExtractionError as exc:
            return _error(f"文档抽取失败：{exc}")
        content = redact_sensitive_text(content, file_read=True) or ""
        lines = content.splitlines()
        total_lines = len(lines)
        end_line = offset + limit - 1
        page_lines = lines[offset - 1:end_line]
        numbered = [
            f"{offset + i:>6} │ {line}"
            for i, line in enumerate(page_lines)
        ]
        page_text = "\n".join(numbered)
        truncated = total_lines > end_line

        hint = ""
        if len(page_text) > READ_MAX_CHARS:
            trimmed = page_text[:READ_MAX_CHARS]
            cut = trimmed.rfind("\n")
            if cut > 0:
                page_text = trimmed[:cut]
            truncated = True
            hint = (
                f"输出超出 {READ_MAX_CHARS:,} 字符读取预算，已截断。"
                "可用 offset 继续分段读取。"
            )
        elif truncated:
            hint = (
                f"文件共 {total_lines} 行，当前显示 {offset}-{min(end_line, total_lines)} 行。"
                f"用 offset={end_line + 1} 继续读取。"
            )

        result: dict[str, Any] = {
            "success": True,
            "path": str(resolved),
            "content": page_text,
            "total_lines": total_lines,
            "truncated": truncated,
            "extracted_document": True,
        }
        if hint:
            result["hint"] = hint
        return json.dumps(result, ensure_ascii=False)

    content, err = _read_text(resolved)
    if err:
        return _error(err)
    content = redact_sensitive_text(content, file_read=True) or ""

    lines = content.splitlines()
    total_lines = len(lines)
    end_line = offset + limit - 1
    page_lines = lines[offset - 1:end_line]
    numbered = [
        f"{offset + i:>6} │ {line}"
        for i, line in enumerate(page_lines)
    ]
    page_text = "\n".join(numbered)
    truncated = total_lines > end_line

    hint = ""
    if len(page_text) > READ_MAX_CHARS:
        # 字符预算截断：裁到预算内最后一个完整行
        trimmed = page_text[:READ_MAX_CHARS]
        cut = trimmed.rfind("\n")
        if cut > 0:
            page_text = trimmed[:cut]
        truncated = True
        hint = (
            f"输出超出 {READ_MAX_CHARS:,} 字符读取预算，已截断。"
            "可用 offset 继续分段读取。"
        )
    elif truncated:
        hint = (
            f"文件共 {total_lines} 行，当前显示 {offset}-{min(end_line, total_lines)} 行。"
            f"用 offset={end_line + 1} 继续读取。"
        )

    result: dict[str, Any] = {
        "success": True,
        "path": str(resolved),
        "content": page_text,
        "total_lines": total_lines,
        "truncated": truncated,
    }
    if hint:
        result["hint"] = hint
    return json.dumps(result, ensure_ascii=False)


def write_file_tool(path: str, content: str) -> str:
    """write_file 工具：写入文本文件（对齐 Hermes：先过敏感路径检查再写）。

    自动创建父目录；返回实际写入的绝对路径（resolved_path）。同名覆盖写入。
    """
    sensitive = _check_sensitive_path(path)
    if sensitive:
        return _error(sensitive)

    resolved = _normalize_path(path)
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        # 用字节模式写入：Windows 文本模式会把 \n 再翻译成 \r\n（CRLF 变 \r\r\n），
        # write_bytes 保证内容原样落盘
        resolved.write_bytes(content.encode("utf-8"))
    except OSError as exc:
        return _error(f"写入失败：{exc}")
    return json.dumps(
        {
            "success": True,
            "path": str(resolved),
            "resolved_path": str(resolved),
            "files_modified": [str(resolved)],
        },
        ensure_ascii=False,
    )


def patch_file_tool(
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
    mode: str = "replace",
    patch: str = "",
) -> str:
    """patch 工具：replace 模式局部替换 / patch 模式 V4A 批量补丁（对齐 Hermes patch_tool）。

    replace 模式（默认）：在文件里找到 old_string 换成 new_string——
    - old_string 必须唯一；出现多次且未传 replace_all=true 时报错，要求确认
    - 找不到时先用模糊匹配兜底（Hermes fuzzy_match 的策略链）；仍找不到且
      new_string 已存在于文件中，判定"补丁已应用"，返回 no_change 成功
      （防止模型反复重发同一补丁）

    patch 模式：解析 V4A 补丁（*** Update/Add/Delete/Move File: + +/- 行），
    先校验后应用（两阶段，校验失败零写入）；写前过 _check_sensitive_path
    （改 .env 等敏感文件照样拒绝），补丁头含 `..` 穿越直接拒绝。
    """
    mode = (mode or "replace").strip().lower()
    if mode == "patch":
        return _patch_v4a(patch or "")
    if mode != "replace":
        return _error(f"未知 mode：{mode}（支持 replace / patch）")

    # 1. 写操作先查"证件"：敏感路径一律拒绝
    sensitive = _check_sensitive_path(path)
    if sensitive:
        return _error(sensitive)

    # 2. 参数校验
    if not old_string:
        return _error("old_string 不能为空")
    if new_string is None:
        return _error("new_string 不能为空")

    # 3. 读文件
    resolved = _normalize_path(path)
    if not resolved.is_file():
        return _error(f"文件不存在：{path}")
    content, err = _read_text(resolved)
    if err:
        return _error(err)

    # 4. BOM / 换行符基础处理：匹配前剥 BOM，写回时按文件原有行尾归一化
    had_bom = content.startswith("\ufeff")
    if had_bom:
        content = content[1:]
    file_ending = "\r\n" if "\r\n" in content else "\n"
    old_norm = old_string.replace("\r\n", "\n")
    new_norm = new_string.replace("\r\n", "\n")
    # 匹配统一在 LF 归一化后的内容上进行，避免 CRLF 文件里多行匹配不到
    content_lf = content.replace("\r\n", "\n")

    # 5. 统计出现次数，按 Hermes 语义处理
    count = content_lf.count(old_norm)
    if count == 0:
        if is_already_applied(content_lf, old_norm, new_norm):
            return json.dumps(
                {
                    "success": True,
                    "no_change": True,
                    "message": (
                        f"文件已包含目标文本，补丁看起来已应用（{path}）。"
                        "未做任何修改，不要重发这个补丁。"
                    ),
                },
                ensure_ascii=False,
            )
        # 模糊兜底（对齐 Hermes：replace 模式同样走 fuzzy_find_and_replace 策略链）
        matched_lf, fuzzy_count, strategy, ferr = fuzzy_find_and_replace(
            content_lf, old_norm, new_norm, replace_all=replace_all
        )
        if fuzzy_count == 0:
            detail = f"（{ferr}）" if ferr and "Could not find" not in ferr else ""
            return _error(f"在 {path} 中找不到 old_string{detail}")
        new_lf = matched_lf
        matched = True
    else:
        matched = False
        new_lf = content_lf.replace(old_norm, new_norm)
    if count > 1 and not replace_all:
        return _error(
            f"old_string 在 {path} 里出现 {count} 次，不唯一；"
            "如确认要全部替换，请传 replace_all=true"
        )

    # 6. 替换并写回（count==1 或 replace_all=true 时都是全量替换）
    new_content = new_lf.replace("\n", file_ending) if file_ending == "\r\n" else new_lf
    if had_bom:
        new_content = "\ufeff" + new_content
    try:
        resolved.write_bytes(new_content.encode("utf-8"))
    except OSError as exc:
        return _error(f"写入失败：{exc}")

    return json.dumps(
        {
            "success": True,
            "path": str(resolved),
            "resolved_path": str(resolved),
            "files_modified": [str(resolved)],
            "replaced": fuzzy_count if matched else count,
            **({"strategy": strategy} if matched else {}),
        },
        ensure_ascii=False,
    )


# =========================================================================
# 模糊匹配（Hermes tools/fuzzy_match.py 的简化移植）
# =========================================================================
def _leading_whitespace(line: str) -> str:
    """返回行首空白前缀（空格/制表符）。"""
    i = 0
    while i < len(line) and line[i] in (" ", "\t"):
        i += 1
    return line[:i]


def _first_meaningful_line(text: str) -> Optional[str]:
    """返回 text 里第一个非空白行（没有则 None）。"""
    for line in text.split("\n"):
        if line.strip():
            return line
    return None


def _reindent_replacement(file_region: str, old_string: str, new_string: str) -> str:
    """按文件实际缩进重排 new_string（对齐 Hermes fuzzy_match._reindent_replacement）。

    非精确策略匹配成功后，模型给的新文本缩进可能和磁盘不一致（如模型用 2 空格、
    文件是 4 空格）；把模型基准缩进换成文件实际基准缩进，保留相对层级。
    """
    if not new_string:
        return new_string
    old_first = _first_meaningful_line(old_string)
    file_first = _first_meaningful_line(file_region)
    if old_first is None or file_first is None:
        return new_string
    old_indent = _leading_whitespace(old_first)
    file_indent = _leading_whitespace(file_first)
    if old_indent == file_indent:
        return new_string
    out_lines: list[str] = []
    for line in new_string.split("\n"):
        if not line.strip():
            out_lines.append(line)
            continue
        line_indent = _leading_whitespace(line)
        if line_indent.startswith(old_indent):
            remainder = line[len(old_indent):]
            out_lines.append(file_indent + remainder)
        else:
            out_lines.append(file_indent + line.lstrip(" \t"))
    return "\n".join(out_lines)


def is_already_applied(content: str, old_string: str, new_string: str) -> bool:
    """判断补丁是否已应用（对齐 Hermes fuzzy_match.is_already_applied）。

    保守规则：new_string 必须非平凡（去空白后 ≥8 字符）、必须精确出现在文件里；
    old_string 与 new_string 不同时，old_string 必须已消失。
    """
    if not new_string or len(new_string.strip()) < 8:
        return False
    if new_string not in content:
        return False
    if old_string == new_string:
        return True
    return old_string not in content


def _strategy_exact(content: str, pattern: str) -> list[Tuple[int, int]]:
    """精确匹配：返回所有非重叠 (start, end) 区间（对齐 Hermes）。"""
    matches: list[Tuple[int, int]] = []
    start = 0
    while True:
        pos = content.find(pattern, start)
        if pos == -1:
            break
        matches.append((pos, pos + len(pattern)))
        start = pos + len(pattern)
    return matches


def _normalize_with_mapping(
    text: str,
    line_map_fn,
) -> Tuple[str, list[int]]:
    """按行归一化文本，返回 (归一化文本, 归一化字符→原始字符位置映射)。

    line_map_fn(line) 返回 (归一化行, 该行内保留字符的原始相对下标)。
    归一化后的换行符映射到原始换行符位置，保证匹配区间能映射回原文。
    """
    norm_chars: list[str] = []
    mapping: list[int] = []
    orig_cursor = 0
    for idx, line in enumerate(text.split("\n")):
        if idx > 0:
            norm_chars.append("\n")
            mapping.append(orig_cursor - 1)
        norm_line, line_map = line_map_fn(line)
        norm_chars.extend(norm_line)
        mapping.extend(orig_cursor + i for i in line_map)
        orig_cursor += len(line) + 1
    return "".join(norm_chars), mapping


def _strip_line_map(line: str) -> Tuple[str, list[int]]:
    """逐行 strip 的归一化（对齐 Hermes 的 line_trimmed 策略）。"""
    leading = len(line) - len(line.lstrip())
    stripped = line.strip()
    return stripped, list(range(leading, leading + len(stripped)))


def _lstrip_line_map(line: str) -> Tuple[str, list[int]]:
    """只去行首空白的归一化（对齐 Hermes 的 indentation_flexible 策略）。"""
    leading = len(line) - len(line.lstrip())
    return line[leading:], list(range(leading, len(line)))


def _collapse_ws_map(line: str) -> Tuple[str, list[int]]:
    """连续空格/制表符合并为单空格的归一化（对齐 Hermes whitespace_normalized）。"""
    norm: list[str] = []
    mapping: list[int] = []
    i = 0
    while i < len(line):
        ch = line[i]
        if ch in " \t":
            if norm and norm[-1] != " ":
                norm.append(" ")
                mapping.append(i)
            while i < len(line) and line[i] in " \t":
                i += 1
        else:
            norm.append(ch)
            mapping.append(i)
            i += 1
    return "".join(norm), mapping


def _unescape_nl_map(line: str) -> Tuple[str, list[int]]:
    """把字面 `\\n` 两字符还原成真实换行（对齐 Hermes escape_normalized 策略）。"""
    norm: list[str] = []
    mapping: list[int] = []
    i = 0
    while i < len(line):
        if line[i] == "\\" and i + 1 < len(line) and line[i + 1] == "n":
            norm.append("\n")
            mapping.append(i)
            i += 2
        else:
            norm.append(line[i])
            mapping.append(i)
            i += 1
    return "".join(norm), mapping


def _find_normalized_matches(content: str, pattern: str, line_map_fn) -> list[Tuple[int, int]]:
    """按逐行归一化找匹配，并把区间映射回原始 content 坐标。"""
    norm_content, c_map = _normalize_with_mapping(content, line_map_fn)
    norm_pattern, _ = _normalize_with_mapping(pattern, line_map_fn)
    if not norm_pattern:
        return []
    matches: list[Tuple[int, int]] = []
    for s, e in _strategy_exact(norm_content, norm_pattern):
        if s < len(c_map) and e - 1 < len(c_map):
            matches.append((c_map[s], c_map[e - 1] + 1))
    return matches


def _line_offset(content: str, line_idx: int) -> int:
    """返回第 line_idx 行在 content 里的起始偏移。"""
    offset = 0
    for i, line in enumerate(content.split("\n")):
        if i == line_idx:
            return offset
        offset += len(line) + 1
    return offset


def _find_context_aware_matches(content: str, pattern: str) -> list[Tuple[int, int]]:
    """相似度兜底（对齐 Hermes context_aware 的保守版）。

    要求模式首/末行是内容对应行的子串、中间行逐行相似度 ≥0.6；
    只在能精确定位区间时返回，避免模糊替换破坏文件。
    """
    clines = content.split("\n")
    plines = pattern.split("\n")
    pcount = len(plines)
    if pcount == 0:
        return []
    matches: list[Tuple[int, int]] = []
    for i in range(len(clines) - pcount + 1):
        window = clines[i:i + pcount]
        if plines[0] not in window[0] or plines[-1] not in window[-1]:
            continue
        mid_ok = True
        for k in range(1, pcount - 1):
            if difflib.SequenceMatcher(None, plines[k], window[k]).ratio() < 0.6:
                mid_ok = False
                break
        if not mid_ok:
            continue
        s = window[0].find(plines[0])
        e = window[-1].find(plines[-1]) + len(plines[-1])
        matches.append((
            _line_offset(content, i) + s,
            _line_offset(content, i + pcount - 1) + e,
        ))
    return matches


def _format_match_locations(content: str, matches: list[Tuple[int, int]], cap: int = 5) -> str:
    """把匹配位置渲染成 'L行号: 片段' 列表，帮模型下一步消歧。"""
    rows = []
    for start, _end in matches[:cap]:
        line_no = content.count("\n", 0, start) + 1
        line_start = content.rfind("\n", 0, start) + 1
        line_end = content.find("\n", line_start)
        if line_end == -1:
            line_end = len(content)
        snippet = content[line_start:line_end].strip()
        if len(snippet) > 80:
            snippet = snippet[:77] + "..."
        rows.append(f"  L{line_no}: {snippet}")
    extra = len(matches) - cap
    if extra > 0:
        rows.append(f"  ... and {extra} more")
    return "\n".join(rows)


def _apply_replacements(
    content: str,
    matches: list[Tuple[int, int]],
    new_string: str,
    old_string: Optional[str] = None,
) -> str:
    """按位置替换（从后往前保证偏移不失效）；非精确匹配时先重排缩进。"""
    sorted_matches = sorted(matches, key=lambda x: x[0], reverse=True)
    result = content
    for start, end in sorted_matches:
        if old_string is not None:
            adjusted = _reindent_replacement(content[start:end], old_string, new_string)
        else:
            adjusted = new_string
        result = result[:start] + adjusted + result[end:]
    return result


def fuzzy_find_and_replace(
    content: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> Tuple[str, int, Optional[str], Optional[str]]:
    """用策略链找并替换文本（对齐 Hermes fuzzy_match.fuzzy_find_and_replace 简化版）。

    策略顺序：exact → line_trimmed → whitespace_normalized → indentation_flexible
    → escape_normalized → context_aware（相似度兜底，保守）。相似度策略在
    replace_all 且多命中时拒绝执行，防止误替换近似区域。
    返回 (新内容, 替换次数, 策略名, 错误信息)。
    """
    if not old_string:
        return content, 0, None, "old_string cannot be empty"
    if not old_string.strip():
        return content, 0, None, (
            "old_string is only whitespace — provide non-blank text to match"
        )
    if old_string == new_string:
        return content, 0, None, "old_string and new_string are identical"

    strategies: list[Tuple[str, Any]] = [
        ("exact", lambda c, p: _strategy_exact(c, p)),
        ("line_trimmed", lambda c, p: _find_normalized_matches(c, p, _strip_line_map)),
        ("whitespace_normalized", lambda c, p: _find_normalized_matches(c, p, _collapse_ws_map)),
        ("indentation_flexible", lambda c, p: _find_normalized_matches(c, p, _lstrip_line_map)),
        ("escape_normalized", lambda c, p: _find_normalized_matches(c, p, _unescape_nl_map)),
        ("context_aware", lambda c, p: _find_context_aware_matches(c, p)),
    ]
    similarity_strategies = {"context_aware"}

    for strategy_name, strategy_fn in strategies:
        matches = strategy_fn(content, old_string)
        if not matches:
            continue
        if len(matches) > 1 and not replace_all:
            return content, 0, None, (
                f"Found {len(matches)} matches for old_string. "
                "Provide more context to make it unique, or use replace_all=True. "
                f"Matches:\n{_format_match_locations(content, matches)}"
            )
        if replace_all and len(matches) > 1 and strategy_name in similarity_strategies:
            return content, 0, None, (
                f"Found {len(matches)} approximate matches via the "
                f"'{strategy_name}' strategy; replace_all only applies to exact "
                "matches. Provide the precise text (whitespace included)."
            )
        new_content = _apply_replacements(
            content,
            matches,
            new_string,
            old_string=old_string if strategy_name != "exact" else None,
        )
        return new_content, len(matches), strategy_name, None

    return content, 0, None, "Could not find a match for old_string in the file"


# =========================================================================
# V4A 补丁（Hermes tools/patch_parser.py 的简化移植）
# =========================================================================
class _V4AOpType(Enum):
    """V4A 补丁操作类型。"""
    ADD = "add"
    UPDATE = "update"
    DELETE = "delete"
    MOVE = "move"


@dataclass
class _V4AHunkLine:
    """补丁块里的一行：prefix 为 ' ' / '-' / '+'，content 为行内容。"""
    prefix: str
    content: str


@dataclass
class _V4AHunk:
    """一个补丁块：可带 @@ 上下文提示，含若干行。"""
    context_hint: Optional[str] = None
    lines: list[_V4AHunkLine] = field(default_factory=list)


@dataclass
class _V4AOp:
    """V4A 补丁的单个文件操作：Update/Add/Delete/Move。"""
    operation: _V4AOpType
    file_path: str
    new_path: Optional[str] = None
    hunks: list[_V4AHunk] = field(default_factory=list)


def parse_v4a_patch(patch_content: str) -> Tuple[list[_V4AOp], Optional[str]]:
    """解析 V4A 补丁文本（对齐 Hermes tools/patch_parser.py::parse_v4a_patch）。

    支持 *** Begin Patch / *** End Patch 定界、*** Update/Add/Delete/Move File: 头、
    @@ 上下文提示与 + / - / 空格前缀行；CRLF 补丁逐行剥 \r 再解析。
    返回 (操作列表, 错误信息)；空补丁返回 ([], None)。
    """
    lines = [ln[:-1] if ln.endswith("\r") else ln for ln in patch_content.split("\n")]
    operations: list[_V4AOp] = []
    begin_marker = re.compile(r"^\*\*\*\s*Begin\s+Patch\s*$")
    end_marker = re.compile(r"^\*\*\*\s*End\s+Patch\s*$")
    start_idx: Optional[int] = None
    end_idx: Optional[int] = None
    for i, line in enumerate(lines):
        if begin_marker.match(line):
            start_idx = i
        elif end_marker.match(line):
            end_idx = i
            break
    if start_idx is None:
        start_idx = -1
    if end_idx is None:
        end_idx = len(lines)

    i = start_idx + 1
    current_op: Optional[_V4AOp] = None
    current_hunk: Optional[_V4AHunk] = None

    def _flush_current() -> None:
        """把当前操作（含未关闭的 hunk）收进 operations。"""
        nonlocal current_op, current_hunk
        if current_op is None:
            return
        if current_hunk and current_hunk.lines:
            current_op.hunks.append(current_hunk)
        operations.append(current_op)
        current_op = None
        current_hunk = None

    while i < end_idx:
        line = lines[i]
        update_match = re.match(r"\*\*\*\s*Update\s+File:\s*(.+)", line)
        add_match = re.match(r"\*\*\*\s*Add\s+File:\s*(.+)", line)
        delete_match = re.match(r"\*\*\*\s*Delete\s+File:\s*(.+)", line)
        move_match = re.match(r"\*\*\*\s*Move\s+File:\s*(.+?)\s*->\s*(.+)", line)

        if update_match:
            _flush_current()
            current_op = _V4AOp(
                operation=_V4AOpType.UPDATE, file_path=update_match.group(1).strip()
            )
            current_hunk = None
        elif add_match:
            _flush_current()
            current_op = _V4AOp(
                operation=_V4AOpType.ADD, file_path=add_match.group(1).strip()
            )
            current_hunk = _V4AHunk()
        elif delete_match:
            _flush_current()
            operations.append(_V4AOp(
                operation=_V4AOpType.DELETE, file_path=delete_match.group(1).strip()
            ))
            current_op = None
            current_hunk = None
        elif move_match:
            _flush_current()
            operations.append(_V4AOp(
                operation=_V4AOpType.MOVE,
                file_path=move_match.group(1).strip(),
                new_path=move_match.group(2).strip(),
            ))
            current_op = None
            current_hunk = None
        elif line.startswith("@@"):
            if current_op:
                if current_hunk and current_hunk.lines:
                    current_op.hunks.append(current_hunk)
                hint_match = re.match(r"@@\s*(.+?)\s*@@", line)
                current_hunk = _V4AHunk(
                    context_hint=hint_match.group(1) if hint_match else None
                )
        elif current_op and line:
            if current_hunk is None:
                current_hunk = _V4AHunk()
            if line.startswith("+"):
                current_hunk.lines.append(_V4AHunkLine("+", line[1:]))
            elif line.startswith("-"):
                current_hunk.lines.append(_V4AHunkLine("-", line[1:]))
            elif line.startswith(" "):
                current_hunk.lines.append(_V4AHunkLine(" ", line[1:]))
            elif line.startswith("\\"):
                pass  # "\ No newline at end of file" 标记跳过
            else:
                current_hunk.lines.append(_V4AHunkLine(" ", line))
        i += 1

    _flush_current()

    if not operations:
        return operations, None

    parse_errors: list[str] = []
    for op in operations:
        if not op.file_path:
            parse_errors.append("Operation with empty file path")
        if op.operation == _V4AOpType.UPDATE and not op.hunks:
            parse_errors.append(f"UPDATE {op.file_path!r}: no hunks found")
        if op.operation == _V4AOpType.MOVE and not op.new_path:
            parse_errors.append(
                f"MOVE {op.file_path!r}: missing destination path (expected 'src -> dst')"
            )
    if parse_errors:
        return [], "Parse error: " + "; ".join(parse_errors)
    return operations, None


def _v4a_key(path: str) -> str:
    """V4A 路径的规范化键（绝对路径 + 大小写统一）。"""
    return str(_normalize_path(path))


def _v4a_load(path: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """读取文件并归一化换行：返回 (LF 内容, BOM, 行尾) 或 (None, None, 错误)。"""
    resolved = _normalize_path(path)
    if not resolved.is_file():
        return None, None, f"文件不存在：{path}"
    content, err = _read_text(resolved)
    if err:
        return None, None, err
    had_bom = content.startswith("\ufeff")
    if had_bom:
        content = content[1:]
    file_ending = "\r\n" if "\r\n" in content else "\n"
    return content.replace("\r\n", "\n"), had_bom, file_ending


def _v4a_save(path: str, lf_content: str, had_bom: bool, file_ending: str) -> Optional[str]:
    """按原 BOM/行尾写回文件；写失败返回错误消息。"""
    resolved = _normalize_path(path)
    content = lf_content.replace("\n", file_ending) if file_ending == "\r\n" else lf_content
    if had_bom:
        content = "\ufeff" + content
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_bytes(content.encode("utf-8"))
    except OSError as exc:
        return f"写入失败：{exc}"
    return None


def _v4a_hunk_change_lines(hunk: _V4AHunk) -> Tuple[list[str], list[str], list[str]]:
    """把 hunk 拆成 (搜索行, 删除行, 新增行)（对齐 Hermes 的 hunk 拆解）。"""
    search_lines = [ln.content for ln in hunk.lines if ln.prefix in {" ", "-"}]
    removed_lines = [ln.content for ln in hunk.lines if ln.prefix == "-"]
    added_lines = [ln.content for ln in hunk.lines if ln.prefix == "+"]
    return search_lines, removed_lines, added_lines


def _v4a_validate(operations: list[_V4AOp]) -> Tuple[list[str], dict[str, int]]:
    """校验所有操作且不写盘；返回 (错误列表, mtime 快照)。

    用虚拟 overlay 模拟 MOVE→UPDATE 的链式状态；UPDATE 的 hunk 按顺序逐个
    模拟（后一个 hunk 基于前一个的结果）；已应用 hunk 跳过不报错。
    """
    errors: list[str] = []
    mtimes: dict[str, int] = {}
    pending_content: dict[str, str] = {}
    removed_paths: set[str] = set()
    real_change_count = 0

    def _read(path: str) -> Tuple[Optional[str], Optional[str]]:
        key = _v4a_key(path)
        if key in removed_paths and key not in pending_content:
            return None, "file not found"
        if key in pending_content:
            return pending_content[key], None
        content, _had_bom, file_ending = _v4a_load(path)
        if content is None:
            return None, file_ending
        resolved = _normalize_path(path)
        mtimes[str(resolved)] = resolved.stat().st_mtime_ns
        return content, None

    for op in operations:
        if op.operation == _V4AOpType.UPDATE:
            content, read_err = _read(op.file_path)
            if read_err:
                errors.append(f"{op.file_path}: {read_err}")
                continue
            simulated = content or ""
            for hunk_index, hunk in enumerate(op.hunks, start=1):
                search_lines, removed_lines, added_lines = _v4a_hunk_change_lines(hunk)
                if not removed_lines and not added_lines:
                    continue  # 惯性锚点 hunk（无实际改动），忽略
                real_change_count += 1
                if not search_lines:
                    # 纯新增 hunk：校验 @@ 上下文提示唯一
                    if hunk.context_hint:
                        occurrences = simulated.count(hunk.context_hint)
                        if occurrences == 0:
                            errors.append(
                                f"{op.file_path}: addition-only hunk context hint "
                                f"'{hunk.context_hint}' not found"
                            )
                        elif occurrences > 1:
                            errors.append(
                                f"{op.file_path}: addition-only hunk context hint "
                                f"'{hunk.context_hint}' is ambiguous "
                                f"({occurrences} occurrences)"
                            )
                    continue
                search_pattern = "\n".join(search_lines)
                replacement = "\n".join(
                    [ln.content for ln in hunk.lines if ln.prefix in {" ", "+"}]
                )
                new_simulated, count, _strategy, match_error = fuzzy_find_and_replace(
                    simulated, search_pattern, replacement, replace_all=False
                )
                if count == 0:
                    if is_already_applied(simulated, search_pattern, replacement):
                        continue
                    label = f"'{hunk.context_hint}'" if hunk.context_hint else "(no hint)"
                    msg = f"{op.file_path}: hunk {hunk_index} {label} not found"
                    if match_error and "Could not find" not in match_error:
                        msg += f" — {match_error}"
                    errors.append(msg)
                else:
                    simulated = new_simulated
            pending_content[_v4a_key(op.file_path)] = simulated

        elif op.operation == _V4AOpType.DELETE:
            _content, read_err = _read(op.file_path)
            if read_err:
                errors.append(f"{op.file_path}: file not found for deletion")
            else:
                removed_paths.add(_v4a_key(op.file_path))
                pending_content.pop(_v4a_key(op.file_path), None)
                real_change_count += 1

        elif op.operation == _V4AOpType.MOVE:
            if not op.new_path:
                errors.append(f"{op.file_path}: MOVE operation missing destination path")
                continue
            src_content, src_err = _read(op.file_path)
            if src_err:
                errors.append(f"{op.file_path}: source file not found for move")
            dst_content, dst_err = _read(op.new_path)
            if not dst_err:
                errors.append(
                    f"{op.new_path}: destination already exists — move would overwrite"
                )
            if not src_err and dst_err:
                pending_content[_v4a_key(op.new_path)] = src_content or ""
                pending_content.pop(_v4a_key(op.file_path), None)
                removed_paths.add(_v4a_key(op.file_path))
                real_change_count += 1

    if not errors and real_change_count == 0:
        errors.append("Patch contains no changes (only context lines were provided)")
    return errors, mtimes


def _v4a_apply(operations: list[_V4AOp], mtimes: dict[str, int]) -> dict[str, Any]:
    """应用 V4A 操作（校验已通过）。返回结果字典。

    陈旧检测（简化版，对齐 Hermes file_state 思路）：每个文件写入前对比 mtime
    快照，若校验后被外部改动则失败；本补丁自己写过的文件会刷新快照，不会误报。
    """
    files_modified: list[str] = []
    files_created: list[str] = []
    files_deleted: list[str] = []
    diffs: list[str] = []
    errors: list[str] = []

    def _stale_ok(resolved: Path, path_label: str) -> bool:
        try:
            current = resolved.stat().st_mtime_ns
        except OSError:
            errors.append(f"{path_label}: 文件状态读取失败")
            return False
        if mtimes.get(str(resolved)) is not None and mtimes[str(resolved)] != current:
            errors.append(
                f"{path_label}: 文件在补丁校验后被外部修改（陈旧检测），请重新读取后再打补丁"
            )
            return False
        return True

    def _refresh(resolved: Path) -> None:
        try:
            mtimes[str(resolved)] = resolved.stat().st_mtime_ns
        except OSError:
            pass

    for op in operations:
        if op.operation == _V4AOpType.ADD:
            resolved = _normalize_path(op.file_path)
            if resolved.exists():
                errors.append(f"Failed to add {op.file_path}: file already exists")
                continue
            content_lines = [
                ln.content for hunk in op.hunks for ln in hunk.lines if ln.prefix == "+"
            ]
            content = "\n".join(content_lines)
            write_err = _v4a_save(op.file_path, content, False, "\n")
            if write_err:
                errors.append(f"Failed to add {op.file_path}: {write_err}")
                continue
            files_created.append(op.file_path)
            diffs.append(
                f"--- /dev/null\n+++ {op.file_path}\n"
                + "\n".join(f"+{line}" for line in content_lines)
            )
            _refresh(resolved)

        elif op.operation == _V4AOpType.UPDATE:
            resolved = _normalize_path(op.file_path)
            if not _stale_ok(resolved, op.file_path):
                continue
            content, had_bom, file_ending = _v4a_load(op.file_path)
            if content is None:
                errors.append(f"Failed to update {op.file_path}: {file_ending}")
                continue
            simulated = content or ""
            for hunk in op.hunks:
                search_lines, removed_lines, added_lines = _v4a_hunk_change_lines(hunk)
                if not removed_lines and not added_lines:
                    continue
                if not search_lines:
                    continue  # 纯新增 hunk：已应用处由校验保证上下文唯一
                search_pattern = "\n".join(search_lines)
                replacement = "\n".join(
                    [ln.content for ln in hunk.lines if ln.prefix in {" ", "+"}]
                )
                simulated, count, _strategy, _err = fuzzy_find_and_replace(
                    simulated, search_pattern, replacement, replace_all=False
                )
                if count == 0:
                    continue  # 校验阶段判定为已应用
            write_err = _v4a_save(op.file_path, simulated, had_bom, file_ending)
            if write_err:
                errors.append(f"Failed to update {op.file_path}: {write_err}")
                continue
            files_modified.append(op.file_path)
            before = content or ""
            after = simulated
            diff_lines = list(difflib.unified_diff(
                before.splitlines(), after.splitlines(),
                fromfile=op.file_path, tofile=op.file_path, lineterm="",
            ))
            diffs.append("\n".join(diff_lines))
            _refresh(resolved)

        elif op.operation == _V4AOpType.DELETE:
            resolved = _normalize_path(op.file_path)
            if not _stale_ok(resolved, op.file_path):
                continue
            content, _had_bom, file_ending = _v4a_load(op.file_path)
            if content is None:
                errors.append(f"Failed to delete {op.file_path}: {file_ending}")
                continue
            try:
                resolved.unlink()
            except OSError as exc:
                errors.append(f"Failed to delete {op.file_path}: {exc}")
                continue
            files_deleted.append(op.file_path)
            diff_lines = list(difflib.unified_diff(
                (content or "").splitlines(), [],
                fromfile=op.file_path, tofile="/dev/null", lineterm="",
            ))
            diffs.append("\n".join(diff_lines))

        elif op.operation == _V4AOpType.MOVE:
            src_resolved = _normalize_path(op.file_path)
            dst_resolved = _normalize_path(op.new_path or "")
            if not _stale_ok(src_resolved, op.file_path):
                continue
            if dst_resolved.exists():
                errors.append(f"Failed to move {op.file_path}: destination already exists")
                continue
            content, had_bom, file_ending = _v4a_load(op.file_path)
            if content is None:
                errors.append(f"Failed to move {op.file_path}: {file_ending}")
                continue
            write_err = _v4a_save(op.new_path or "", content or "", had_bom, file_ending)
            if write_err:
                errors.append(f"Failed to move {op.file_path}: {write_err}")
                continue
            try:
                src_resolved.unlink()
            except OSError as exc:
                errors.append(f"Failed to move {op.file_path}: 删除源失败 {exc}")
                continue
            files_modified.append(f"{op.file_path} -> {op.new_path}")
            diffs.append(f"--- {op.file_path}\n+++ {op.new_path}\n(moved)")
            _refresh(dst_resolved)

    result: dict[str, Any] = {
        "success": not errors,
        "files_modified": files_modified,
        "files_created": files_created,
        "files_deleted": files_deleted,
        "diff": "\n".join(diffs),
    }
    if errors:
        result["error"] = (
            "Apply phase failed (state may be inconsistent — run `git diff` to assess):\n"
            + "\n".join(f"  - {e}" for e in errors)
        )
    return result


def _v4a_syntax_note(path: str, content: str) -> Optional[str]:
    """对 .py 文件做 ast.parse 语法提示（非阻塞，仅信息）。"""
    if not path.endswith(".py"):
        return None
    try:
        ast.parse(content)
    except SyntaxError as exc:
        return f"{path}: 语法提示——{exc.msg}（line {exc.lineno}）"
    return None


def _patch_v4a(patch_content: str) -> str:
    """V4A 补丁入口：解析 → 路径安全 → 校验 → 应用 → 语法提示。"""
    if not patch_content.strip():
        return _error("patch 模式需要提供 V4A 补丁内容（mode=patch, patch=...）")

    operations, parse_err = parse_v4a_patch(patch_content)
    if parse_err:
        return _error(parse_err)
    if not operations:
        return _error("补丁为空（没有可执行的操作）")

    # 路径安全：V4A 头来自补丁内容（更易被注入），拒绝 `..` 穿越与敏感路径；
    # Move 的两个端点都要查（对齐 Hermes patch_tool 的 V4A 检查）
    path_headers: list[str] = []
    for op in operations:
        path_headers.append(op.file_path)
        if op.operation == _V4AOpType.MOVE and op.new_path:
            path_headers.append(op.new_path)
    for header in path_headers:
        candidate = Path(header)
        if ".." in candidate.parts:
            return _error(
                f"V4A patch header contains '..' traversal: {header!r}. "
                "Use the agent's cwd-relative path (no '..') or an absolute path."
            )
        sensitive = _check_sensitive_path(header)
        if sensitive:
            return _error(sensitive)

    validation_errors, mtimes = _v4a_validate(operations)
    if validation_errors:
        return _error(
            "Patch validation failed (no files were modified):\n"
            + "\n".join(f"  - {e}" for e in validation_errors)
        )

    result = _v4a_apply(operations, mtimes)
    if not result["success"]:
        return json.dumps(result, ensure_ascii=False)

    # 语法提示（信息性，不阻塞）
    syntax_notes: list[str] = []
    for path in result["files_modified"] + result["files_created"]:
        if isinstance(path, str) and path.endswith(".py"):
            content, _err = _read_text(_normalize_path(path))
            if content is not None:
                note = _v4a_syntax_note(path, content)
                if note:
                    syntax_notes.append(note)
    if syntax_notes:
        result["syntax"] = syntax_notes
    return json.dumps(result, ensure_ascii=False)


def search_files_tool(path: str, pattern: str) -> str:
    """search_files 工具：递归搜索文件名或内容（大小写不敏感，对齐 Hermes）。

    跳过排除目录与敏感文件（.env / approval_allowlist.json 等）；
    结果上限 SEARCH_RESULT_LIMIT 条，超出时提示继续收窄。
    """
    pattern = (pattern or "").strip()
    if not pattern:
        return _error("pattern 不能为空")

    root = _normalize_path(path)
    if not root.exists():
        return _error(f"路径不存在：{path}")

    needle = pattern.lower()
    matches: list[dict[str, Any]] = []
    for candidate in root.rglob("*"):
        if not candidate.is_file():
            continue
        try:
            rel = candidate.relative_to(root)
        except ValueError:
            continue
        if any(part in EXCLUDED_DIRS for part in rel.parts):
            continue
        if candidate.name in _SENSITIVE_FILE_NAMES or candidate.name in _USER_SENSITIVE_FILES:
            continue

        name_hit = needle in candidate.name.lower()
        # 内容命中：逐行找，最多记 3 个命中行
        hit_lines: list[dict[str, Any]] = []
        content, err = _read_text(candidate)
        if err is None:
            hit_lines = [
                {"line": i + 1, "text": line.strip()[:200]}
                for i, line in enumerate(content.splitlines())
                if needle in line.lower()
            ]
        if name_hit or hit_lines:
            record: dict[str, Any] = {"path": str(rel)}
            if name_hit and hit_lines:
                record["type"] = "name+content"
                record["lines"] = hit_lines[:3]
            elif name_hit:
                record["type"] = "file"
            else:
                record["type"] = "content"
                record["lines"] = hit_lines[:3]
            matches.append(record)
            if len(matches) >= SEARCH_RESULT_LIMIT:
                break

    truncated = len(matches) >= SEARCH_RESULT_LIMIT
    result: dict[str, Any] = {
        "success": True,
        "pattern": pattern,
        "count": len(matches),
        "matches": matches,
    }
    if truncated:
        result["truncated"] = True
        result["hint"] = (
            f"匹配过多，仅返回前 {SEARCH_RESULT_LIMIT} 条；"
            "请把 path 收窄或换更精确的 pattern。"
        )
    return json.dumps(result, ensure_ascii=False)


def _error(message: str) -> str:
    """构造统一的失败返回。"""
    return json.dumps({"success": False, "error": message}, ensure_ascii=False)
