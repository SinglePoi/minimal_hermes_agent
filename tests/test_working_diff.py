# -*- coding: utf-8 -*-
"""working_diff 回归测试（对齐 Hermes tools/working_diff.py）。

覆盖：working/staged/all 三模式、未跟踪文件折入、空仓库、非 git 目录、
非法模式、路径过滤、工具入口 JSON 与 run_tool 分发、并行只读白名单。
零依赖（仅用 git CLI），python tests/test_working_diff.py 直接跑。
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import minimal_agent  # noqa: E402
import tool_dispatch  # noqa: E402
from working_diff import (  # noqa: E402
    collect_working_diff,
    parse_diff_files,
    summarize_files,
    working_diff_tool,
)

_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    """记录一条断言结果。"""
    if cond:
        print(f"  ok: {label}")
    else:
        _failures.append(label)
        print(f"  FAIL: {label}")


def _git(cwd: str, *args: str) -> subprocess.CompletedProcess:
    """在指定目录跑 git 命令（测试辅助）。"""
    return subprocess.run(
        ["git", "-c", "core.quotePath=false", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=15,
    )


def _make_repo() -> str:
    """建一个临时 git 仓库：提交 a.txt，然后改 a.txt + 新增 b.txt。"""
    tmp = tempfile.mkdtemp(prefix="wdiff-")
    _git(tmp, "init")
    _git(tmp, "config", "user.name", "test")
    _git(tmp, "config", "user.email", "test@example.com")
    (Path(tmp) / "a.txt").write_text("line1\nline2\n", encoding="utf-8")
    _git(tmp, "add", "a.txt")
    _git(tmp, "commit", "-m", "init")
    (Path(tmp) / "a.txt").write_text("line1\nline2 changed\n", encoding="utf-8")
    (Path(tmp) / "b.txt").write_text("brand new\n", encoding="utf-8")
    return tmp


def test_modes() -> None:
    """working/staged/all 三模式与未跟踪文件折入。"""
    repo = _make_repo()
    try:
        working = collect_working_diff(repo)
        check("working success", working.get("success") is True)
        check("working diff 含 a.txt 改动", "a.txt" in working.get("diff", ""))
        check("working stat 非空", bool(working.get("stat", "").strip()))
        check("working untracked 含 b.txt", "b.txt" in working.get("untracked", []))
        check("working diff 折入未跟踪文件", "b.txt" in working.get("diff", ""))

        # 路径过滤必须在 git add 之前测（add 后 working 模式的 a.txt 为空 diff）
        only_a = collect_working_diff(repo, paths=["a.txt"])
        check("路径过滤后无 b.txt", "b.txt" not in only_a.get("diff", ""))
        check("路径过滤保留 a.txt", "a.txt" in only_a.get("diff", ""))

        _git(repo, "add", "a.txt")
        staged = collect_working_diff(repo, mode="staged")
        check("staged diff 含 a.txt", "a.txt" in staged.get("diff", ""))
        check("staged 不含未跟踪", staged.get("untracked") == [])

        all_mode = collect_working_diff(repo, mode="all")
        check("all diff 含 a.txt", "a.txt" in all_mode.get("diff", ""))
        check("all untracked 含 b.txt", "b.txt" in all_mode.get("untracked", []))
    finally:
        import shutil

        shutil.rmtree(repo, ignore_errors=True)


def test_empty_and_errors() -> None:
    """空工作区、非 git 目录、非法模式。"""
    repo = _make_repo()
    try:
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "sync")
        empty = collect_working_diff(repo)
        check("干净工作区 empty=True", empty.get("empty") is True)

        with tempfile.TemporaryDirectory() as plain:
            not_repo = collect_working_diff(plain)
            check("非 git 目录报错", not_repo.get("success") is False)
            check("错误信息明确", "Not a git repository" in not_repo.get("error", ""))

        bad = collect_working_diff(repo, mode="bogus")
        check("非法模式报错", bad.get("success") is False)
        check("非法模式提示可用值", "Unknown mode 'bogus'" in bad.get("error", ""))
    finally:
        import shutil

        shutil.rmtree(repo, ignore_errors=True)


def test_parse_diff_files() -> None:
    """parse_diff_files：合并 diff 按文件拆分，状态/增删行数/路径正确。"""
    sample = (
        "diff --git a/a.txt b/a.txt\n"
        "index 111..222 100644\n"
        "--- a/a.txt\n"
        "+++ b/a.txt\n"
        "@@ -1,3 +1,4 @@\n"
        " line1\n"
        "-line2\n"
        "+line2 changed\n"
        "+line3\n"
        "diff --git a/b.txt b/b.txt\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/b.txt\n"
        "@@ -0,0 +1,2 @@\n"
        "+brand new\n"
        "+more\n"
        "diff --git a/old.py b/old.py\n"
        "deleted file mode 100644\n"
        "--- a/old.py\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n"
        "-gone1\n"
        "-gone2\n"
    )
    files = parse_diff_files(sample)
    check("拆出 3 个文件", len(files) == 3)

    a = next(f for f in files if f["path"] == "a.txt")
    check("a.txt 状态 modified", a["status"] == "modified")
    check("a.txt 增 2 删 1", a["additions"] == 2 and a["deletions"] == 1)
    check("a.txt chunk 完整", "diff --git a/a.txt" in a["diff"])

    b = next(f for f in files if f["path"] == "b.txt")
    check("b.txt 状态 added", b["status"] == "added")
    check("b.txt 增 2 删 0", b["additions"] == 2 and b["deletions"] == 0)

    old = next(f for f in files if f["path"] == "old.py")
    check("old.py 状态 deleted", old["status"] == "deleted")
    check("old.py 删 2 增 0", old["deletions"] == 2 and old["additions"] == 0)

    summary = summarize_files(files)
    check("汇总文件数=3", summary["files"] == 3)
    check("汇总新增行=4", summary["additions"] == 4)
    check("汇总删除行=3", summary["deletions"] == 3)
    check("状态计数 1/1/1", summary["added"] == 1 and summary["modified"] == 1 and summary["deleted"] == 1)
    check("空列表汇总归零", summarize_files([])["files"] == 0)
    check("空 diff 返回空列表", parse_diff_files("") == [])


def test_tool_and_dispatch() -> None:
    """工具入口返回 JSON；run_tool 分发；TOOLS 注册；并行只读白名单。"""
    repo = _make_repo()
    old_cwd = os.getcwd()
    try:
        raw = working_diff_tool(cwd=repo, mode="working")
        payload = json.loads(raw)
        check("工具入口 success", payload.get("success") is True)
        check("工具入口含 b.txt", "b.txt" in payload.get("diff", ""))

        names = [t["function"]["name"] for t in minimal_agent.TOOLS]
        check("TOOLS 注册 working_diff", "working_diff" in names)
        check(
            "并行只读白名单含 working_diff",
            "working_diff" in tool_dispatch._PARALLEL_SAFE_TOOLS,
        )

        os.chdir(repo)
        dispatched = json.loads(minimal_agent.run_tool("working_diff", {"mode": "working"}))
        check("run_tool 分发成功", dispatched.get("success") is True)
        check("run_tool 结果含 b.txt", "b.txt" in dispatched.get("diff", ""))
    finally:
        os.chdir(old_cwd)
        import shutil

        shutil.rmtree(repo, ignore_errors=True)


def main() -> None:
    """跑全部断言。"""
    test_modes()
    test_empty_and_errors()
    test_parse_diff_files()
    test_tool_and_dispatch()
    if _failures:
        print(f"\n{len(_failures)} 条断言失败：")
        for label in _failures:
            print(f"  - {label}")
        raise SystemExit(1)
    print("\n全部 working_diff 断言通过")


if __name__ == "__main__":
    main()
