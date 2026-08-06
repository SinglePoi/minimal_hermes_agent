# -*- coding: utf-8 -*-
"""
文件工具模块（对齐 Hermes tools/file_tools.py）

提供三个核心文件工具：
    - read_file(path, offset, limit)   分页 + 行号 + 字符预算截断
    - write_file(path, content)        先过敏感路径检查再写入
    - search_files(path, pattern)      递归搜索文件名/内容（跳过敏感文件）

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
    - patch 工具（V4A 补丁格式）——后续可按需补
"""

import json
import os
import re
from pathlib import Path
from typing import Any, Optional

from redact import redact_sensitive_text

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
    """
    offset = max(1, int(offset or 1))
    limit = max(1, min(int(limit or 200), 2000))
    resolved = _normalize_path(path)
    if not resolved.is_file():
        return _error(f"文件不存在：{path}")

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
) -> str:
    """patch 工具（replace 模式）：在文件里找到 old_string 换成 new_string（对齐 Hermes patch_tool）。

    语义与 Hermes 一致：
    - old_string 必须唯一；出现多次且未传 replace_all=true 时报错，要求确认
    - 找不到 old_string：若 new_string 已存在于文件中，判定"补丁已应用"，
      返回 no_change 成功（防止模型反复重发同一补丁）；否则报错
    - 写前先过 _check_sensitive_path（改 .env 等敏感文件照样拒绝）

    简化掉的部分（Hermes 有，骨架不做）：模糊匹配（fuzzy match）、V4A 补丁头格式、
    diff/语法检查结果、CRLF 与 BOM 的完整往返处理（这里只做基础保留）。
    """
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
        if new_norm and new_norm in content_lf:
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
        return _error(f"在 {path} 中找不到 old_string")
    if count > 1 and not replace_all:
        return _error(
            f"old_string 在 {path} 里出现 {count} 次，不唯一；"
            "如确认要全部替换，请传 replace_all=true"
        )

    # 6. 替换并写回（count==1 或 replace_all=true 时都是全量替换）
    new_lf = content_lf.replace(old_norm, new_norm)
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
            "replaced": count,
        },
        ensure_ascii=False,
    )


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
