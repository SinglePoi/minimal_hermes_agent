# -*- coding: utf-8 -*-
"""会话导出回归测试（对齐 Hermes hermes_cli/session_export_md.py + _html.py，简化版）。

覆盖：Markdown（frontmatter/标题/消息头/内容）、HTML（DOCTYPE/转义防 XSS/角色标签）、
文件导出（路径/格式/未知会话/未知格式）、REPL /export 斜杠命令。
零依赖，python tests/test_session_export.py 直接跑。
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import minimal_agent  # noqa: E402
from minimal_agent import ReplState, run_slash_command  # noqa: E402
from session_export import (  # noqa: E402
    export_session_file,
    export_session_html,
    export_session_md,
)

_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    """记录一条断言结果。"""
    if cond:
        print(f"  ok: {label}")
    else:
        _failures.append(label)
        print(f"  FAIL: {label}")


def _make_session(tmp: str, session_id: str = "s1") -> None:
    """在临时库里造一个带标题和消息的会话。"""
    minimal_agent.SESSION_DB = Path(tmp) / "sessions.db"
    conn = minimal_agent._db_conn()
    conn.execute(
        "INSERT INTO sessions (session_id, system_prompt, title) VALUES (?, ?, ?)",
        (session_id, "系统提示词", "北京行程规划"),
    )
    for role, content in (
        ("user", "帮我规划北京行程"),
        ("assistant", "好的，<script>alert(1)</script> 以下是三天行程"),
    ):
        conn.execute(
            "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
            (session_id, role, content),
        )
    conn.commit()
    conn.close()


def test_export_md() -> None:
    """Markdown：frontmatter + 标题 + 消息头 + 内容。"""
    with tempfile.TemporaryDirectory() as tmp:
        old_db = minimal_agent.SESSION_DB
        try:
            _make_session(tmp)
            text = export_session_md("s1")
            check("frontmatter 含 session_id", "session_id: 's1'" in text)
            check("frontmatter 含标题", "title: '北京行程规划'" in text)
            check("frontmatter 含消息数", "message_count: 2" in text)
            check("正文含标题", "# 北京行程规划" in text)
            check("消息头含角色与时间", "### 用户" in text and "### 助手" in text)
            check("内容保留", "帮我规划北京行程" in text)
        finally:
            minimal_agent.SESSION_DB = old_db


def test_export_html() -> None:
    """HTML：DOCTYPE + 角色标签 + 内容转义防 XSS。"""
    with tempfile.TemporaryDirectory() as tmp:
        old_db = minimal_agent.SESSION_DB
        try:
            _make_session(tmp)
            text = export_session_html("s1")
            check("HTML 有 DOCTYPE", text.startswith("<!DOCTYPE html>"))
            check("HTML 含标题", "<h1>北京行程规划</h1>" in text)
            check("HTML 含角色标签", ">用户<" in text and ">助手<" in text)
            check("HTML 转义脚本", "&lt;script&gt;" in text and "<script>alert" not in text)
        finally:
            minimal_agent.SESSION_DB = old_db


def test_export_file() -> None:
    """文件导出：默认 exports/ 目录、格式切换、错误分支。"""
    with tempfile.TemporaryDirectory() as tmp:
        old_db = minimal_agent.SESSION_DB
        old_cwd = os.getcwd()
        try:
            _make_session(tmp)
            os.chdir(tmp)
            res = export_session_file("s1")
            check("文件导出成功", res.get("success") is True)
            path = Path(res["path"])
            check("文件存在", path.exists() and path.name == "session-s1.md")
            check("文件内容为 Markdown", "# 北京行程规划" in path.read_text(encoding="utf-8"))

            res_html = export_session_file("s1", fmt="html")
            html_path = Path(res_html["path"])
            check("html 格式导出", html_path.name == "session-s1.html")

            bad = export_session_file("s1", fmt="pdf")
            check("未知格式报错", bad.get("success") is False)
            ghost = export_session_file("ghost")
            check("未知会话报错", ghost.get("success") is False)
        finally:
            os.chdir(old_cwd)
            minimal_agent.SESSION_DB = old_db


def test_repl_export_command() -> None:
    """REPL /export：写文件返回路径；缺参/未知会话提示。"""
    with tempfile.TemporaryDirectory() as tmp:
        old_db = minimal_agent.SESSION_DB
        old_cwd = os.getcwd()
        try:
            _make_session(tmp)
            os.chdir(tmp)
            state = ReplState(
                session_id="s1",
                client=None,
                messages=[],
                tools=[],
                memory_manager=None,
                review_worker=None,
            )

            handled, text = run_slash_command("/export", state)
            check("export 缺参给用法", handled is True and "用法" in text)

            handled, text = run_slash_command("/export ghost", state)
            check("export 未知会话报错", "未找到会话 ghost" in text)

            handled, text = run_slash_command("/export s1", state)
            check("export 已处理", handled is True)
            check("export 提示路径", "已导出到" in text and "session-s1.md" in text)
            check("导出文件已写盘", (Path(tmp) / "exports" / "session-s1.md").exists())

            handled, text = run_slash_command("/export s1 html", state)
            check("export html 格式", "session-s1.html" in text)
        finally:
            os.chdir(old_cwd)
            minimal_agent.SESSION_DB = old_db


def main() -> None:
    """跑全部断言。"""
    test_export_md()
    test_export_html()
    test_export_file()
    test_repl_export_command()
    if _failures:
        print(f"\n{len(_failures)} 条断言失败：")
        for label in _failures:
            print(f"  - {label}")
        raise SystemExit(1)
    print("\n全部会话导出断言通过")


if __name__ == "__main__":
    main()
