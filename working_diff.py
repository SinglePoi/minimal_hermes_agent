# -*- coding: utf-8 -*-
"""工作区 git diff 收集（对齐 Hermes tools/working_diff.py）。

Hermes 的 /diff 斜杠命令让 CLI 与 gateway 共用同一套收集逻辑；骨架把同一套
逻辑做成模型可见的 working_diff 工具，回答"工作区改了什么"。

模式：
- working（默认）：未暂存改动 + 未跟踪文件（git checkout . && git clean -fd
  会丢掉的部分）
- staged：已 git add 的改动（git diff --cached）
- all：相对 HEAD 的全部改动（已暂存 + 未暂存）+ 未跟踪文件

未跟踪文件用 git diff --no-index /dev/null <file> 折进来，让新文件以新增 diff
出现而不是静默不可见（对齐 Codex CLI 的 /diff 行为）。
"""

import json
import os
import shutil
import subprocess
from typing import Any, List, Optional

_GIT_TIMEOUT = 15
_MAX_UNTRACKED_FILES = 50  # 未跟踪文件上限，防止 node_modules 爆炸拖死

VALID_MODES = ("working", "staged", "all")


def _run(args: List[str], cwd: str, timeout: int = _GIT_TIMEOUT):
    """跑一条 git 命令，返回 (returncode, stdout)；git 失败不抛异常。"""
    proc = subprocess.run(
        ["git", "-c", "core.quotePath=false", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        # Windows 默认 GBK 控制台：git diff 里的 UTF-8 中文（文件路径/内容）
        # 会解码失败；显式按 UTF-8 + errors=replace，与 run_terminal 一致
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return proc.returncode, proc.stdout


def _untracked_files(cwd: str) -> List[str]:
    """列出未跟踪文件（相对路径，遵守 .gitignore）。"""
    code, out = _run(["ls-files", "--others", "--exclude-standard"], cwd)
    if code != 0:
        return []
    return [line for line in out.splitlines() if line.strip()]


def _untracked_diff(cwd: str, files: List[str]) -> str:
    """把未跟踪文件渲染成新增 diff（git diff --no-index /dev/null <file>）。"""
    chunks: List[str] = []
    for rel in files[:_MAX_UNTRACKED_FILES]:
        try:
            # --no-index 在文件有差异时退出码为 1——这正是成功路径，
            # 忽略退出码、保留输出
            _, out = _run(["diff", "--no-index", "--", os.devnull, rel], cwd)
            if out.strip():
                chunks.append(out.rstrip("\n"))
        except (subprocess.TimeoutExpired, OSError):
            continue
    if len(files) > _MAX_UNTRACKED_FILES:
        chunks.append(
            f"... ({len(files) - _MAX_UNTRACKED_FILES} more untracked files not shown)"
        )
    return "\n".join(chunks)


def collect_working_diff(
    cwd: str,
    mode: str = "working",
    paths: Optional[List[str]] = None,
) -> dict[str, Any]:
    """收集工作区 git diff。

    返回 {"success", "stat", "diff", "untracked", "empty"}；git 不可用或不在
    git 仓库时返回 {"success": False, "error": ...}。paths 可选限制查看范围
    （原样传给 git，带空格的路径由调用方自行引用）。
    """
    if mode not in VALID_MODES:
        return {
            "success": False,
            "error": f"Unknown mode '{mode}'. Use: {', '.join(VALID_MODES)}",
        }

    if not shutil.which("git"):
        return {"success": False, "error": "git is not installed or not on PATH."}

    try:
        code, _ = _run(["rev-parse", "--is-inside-work-tree"], cwd, timeout=5)
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"success": False, "error": f"git failed: {e}"}
    if code != 0:
        return {"success": False, "error": "Not a git repository."}

    if mode == "staged":
        base_args = ["diff", "--cached"]
    elif mode == "all":
        base_args = ["diff", "HEAD"]
    else:  # working
        base_args = ["diff"]

    pathspec = ["--", *paths] if paths else []

    try:
        _, stat_out = _run([*base_args, "--stat", *pathspec], cwd)
        _, diff_out = _run([*base_args, *pathspec], cwd, timeout=_GIT_TIMEOUT * 2)

        untracked: List[str] = []
        untracked_diff = ""
        if mode in ("working", "all") and not paths:
            untracked = _untracked_files(cwd)
            if untracked:
                untracked_diff = _untracked_diff(cwd, untracked)
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "git diff timed out."}
    except OSError as e:
        return {"success": False, "error": f"git failed: {e}"}

    stat = stat_out.strip()
    diff = diff_out.strip()
    if untracked_diff:
        diff = f"{diff}\n{untracked_diff}".strip()

    result: dict[str, Any] = {
        "success": True,
        "stat": stat,
        "diff": diff,
        "untracked": untracked,
    }
    if not stat and not diff and not untracked:
        result["empty"] = True
    return result


def parse_diff_files(diff_text: str) -> List[dict[str, Any]]:
    """把合并的 unified diff 按文件拆成记录，供前端目录 + 逐文件 diff 渲染。

    每条记录含 path / status（added|modified|deleted）/ additions / deletions /
    diff（该文件的完整 chunk 文本）。未跟踪文件经 --no-index 折入后同样以
    "diff --git " 开头，解析路径与新增状态。解析失败的文件防御性跳过。
    """
    if not diff_text:
        return []
    chunks: List[str] = []
    current: List[str] = []
    for line in diff_text.splitlines():
        if line.startswith("diff --git ") and current:
            chunks.append("\n".join(current))
            current = []
        current.append(line)
    if current:
        chunks.append("\n".join(current))

    files: List[dict[str, Any]] = []
    for chunk in chunks:
        lines = chunk.splitlines()
        path = ""
        status = "modified"
        for line in lines:
            if line.startswith("+++ b/"):
                path = line[len("+++ b/"):]
            elif line.startswith("+++ /dev/null"):
                status = "deleted"
            elif line.startswith("--- /dev/null") and not path:
                status = "added"
            elif line.startswith("--- a/") and not path:
                # 删除文件没有 +++ b/ 路径，从 --- a/ 取
                path = line[len("--- a/"):]
        if not path:
            continue
        additions = sum(
            1 for ln in lines if ln.startswith("+") and not ln.startswith("+++")
        )
        deletions = sum(
            1 for ln in lines if ln.startswith("-") and not ln.startswith("---")
        )
        files.append(
            {
                "path": path,
                "status": status,
                "additions": additions,
                "deletions": deletions,
                "diff": chunk,
            }
        )
    return files


def summarize_files(files: List[dict[str, Any]]) -> dict[str, int]:
    """汇总按文件拆分的结果：文件数、增删行总数、按状态的文件数。

    供前端左侧只显示"共 N 个文件 · 新增 +X · 删除 -Y"的紧凑摘要（替代
    git diff --stat 的逐文件长表）。
    """
    return {
        "files": len(files),
        "additions": sum(f.get("additions", 0) for f in files),
        "deletions": sum(f.get("deletions", 0) for f in files),
        "added": sum(1 for f in files if f.get("status") == "added"),
        "modified": sum(1 for f in files if f.get("status") == "modified"),
        "deleted": sum(1 for f in files if f.get("status") == "deleted"),
    }


def working_diff_tool(
    cwd: Optional[str] = None,
    mode: str = "working",
    paths: Optional[List[str]] = None,
) -> str:
    """模型可见的 working_diff 工具入口：返回 JSON 字符串。

    cwd 缺省用进程当前目录（与 terminal 工具继承的 cwd 一致）。
    """
    base = cwd or os.getcwd()
    return json.dumps(collect_working_diff(base, mode=mode, paths=paths), ensure_ascii=False)
