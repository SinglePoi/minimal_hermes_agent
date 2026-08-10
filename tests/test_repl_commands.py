# -*- coding: utf-8 -*-
"""REPL 斜杠命令回归测试（/help /sessions /resume /diff，对齐 Hermes CLI）。

覆盖：help 文案、sessions 列表（空库/含标题与消息数）、diff 摘要与路径模式、
resume 缺参/未知/成功切换（会话 id、系统提示词、历史、计数重置）、未知命令。
零依赖，python tests/test_repl_commands.py 直接跑。
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import minimal_agent  # noqa: E402
from minimal_agent import ReplState, run_slash_command  # noqa: E402

_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    """记录一条断言结果。"""
    if cond:
        print(f"  ok: {label}")
    else:
        _failures.append(label)
        print(f"  FAIL: {label}")


def _make_state(session_id: str = "sess-cur", messages=None) -> ReplState:
    """构造一个最小 ReplState（斜杠命令不依赖 client/worker）。"""
    return ReplState(
        session_id=session_id,
        client=None,
        messages=list(messages) if messages is not None else [],
        tools=[],
        memory_manager=None,
        review_worker=None,
    )


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
    """建临时 git 仓库：提交 a.txt 后修改它并新增 b.txt。"""
    tmp = tempfile.mkdtemp(prefix="repl-diff-")
    _git(tmp, "init")
    _git(tmp, "config", "user.name", "test")
    _git(tmp, "config", "user.email", "test@example.com")
    (Path(tmp) / "a.txt").write_text("line1\n", encoding="utf-8")
    _git(tmp, "add", "a.txt")
    _git(tmp, "commit", "-m", "init")
    (Path(tmp) / "a.txt").write_text("line1 changed\n", encoding="utf-8")
    (Path(tmp) / "b.txt").write_text("brand new\n", encoding="utf-8")
    return tmp


def test_help() -> None:
    """/help：已处理且列出全部命令。"""
    handled, text = run_slash_command("/help", _make_state())
    check("help 已处理", handled is True)
    for kw in ("/diff", "/resume", "/sessions", "/exit"):
        check(f"help 含 {kw}", kw in text)


def test_sessions() -> None:
    """/sessions：列表含 id/标题/消息数；空库有提示。"""
    with tempfile.TemporaryDirectory() as tmp:
        old_db = minimal_agent.SESSION_DB
        minimal_agent.SESSION_DB = Path(tmp) / "sessions.db"
        try:
            conn = minimal_agent._db_conn()
            conn.execute(
                "INSERT INTO sessions (session_id, system_prompt, title) VALUES (?, ?, ?)",
                ("s1", "p", "第一个会话"),
            )
            conn.execute(
                "INSERT INTO sessions (session_id, system_prompt) VALUES (?, ?)",
                ("s2", "p"),
            )
            conn.execute(
                "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
                ("s1", "user", "你好"),
            )
            conn.commit()
            conn.close()

            handled, text = run_slash_command("/sessions", _make_state())
            check("sessions 已处理", handled is True)
            check("sessions 含 s1", "s1" in text)
            check("sessions 含标题", "第一个会话" in text)
            check("sessions 含消息数", "1 条" in text)
        finally:
            minimal_agent.SESSION_DB = old_db

    with tempfile.TemporaryDirectory() as tmp:
        old_db = minimal_agent.SESSION_DB
        minimal_agent.SESSION_DB = Path(tmp) / "sessions.db"
        try:
            minimal_agent._db_conn().close()
            handled, text = run_slash_command("/sessions", _make_state())
            check("空库提示", "暂无历史会话" in text)
        finally:
            minimal_agent.SESSION_DB = old_db


def test_diff() -> None:
    """/diff：摘要 + 文件清单；指定路径附完整 diff；模式切换。"""
    repo = _make_repo()
    old_cwd = os.getcwd()
    try:
        os.chdir(repo)
        handled, text = run_slash_command("/diff", _make_state())
        check("diff 已处理", handled is True)
        check("diff 摘要含文件数", "共 2 个文件" in text)
        check("diff 清单含 a.txt", "a.txt" in text)
        check("diff 提示路径用法", "/diff <路径>" in text)

        handled, text = run_slash_command("/diff b.txt", _make_state())
        check("diff 指定路径含内容", "brand new" in text)

        _git(repo, "add", "a.txt")
        handled, text = run_slash_command("/diff staged", _make_state())
        check("diff staged 生效", "（staged）" in text and "a.txt" in text)
    finally:
        os.chdir(old_cwd)
        shutil.rmtree(repo, ignore_errors=True)


def test_resume() -> None:
    """/resume：缺参/未知给提示；成功后切换会话并重置计数。"""
    with tempfile.TemporaryDirectory() as tmp:
        old_db = minimal_agent.SESSION_DB
        minimal_agent.SESSION_DB = Path(tmp) / "sessions.db"
        try:
            conn = minimal_agent._db_conn()
            conn.execute(
                "INSERT INTO sessions (session_id, system_prompt) VALUES (?, ?)",
                ("s1", "旧系统提示词"),
            )
            conn.execute(
                "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
                ("s1", "user", "你好"),
            )
            conn.execute(
                "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
                ("s1", "assistant", "你好呀"),
            )
            conn.commit()
            conn.close()

            state = _make_state(session_id="sess-cur")
            handled, text = run_slash_command("/resume", state)
            check("resume 缺参给用法", "用法" in text)
            check("resume 缺参不切换", state.session_id == "sess-cur")

            handled, text = run_slash_command("/resume ghost", state)
            check("resume 未知会话报错", "未找到会话 ghost" in text)
            check("resume 未知不切换", state.session_id == "sess-cur")

            handled, text = run_slash_command("/resume s1", state)
            check("resume 已处理", handled is True)
            check("resume 提示切换", "已切换到会话 s1" in text)
            check("resume 更新会话 id", state.session_id == "s1")
            check(
                "resume 载入系统提示词",
                state.messages[0]["role"] == "system"
                and "旧系统提示词" in state.messages[0]["content"],
            )
            check("resume 载入历史", len(state.messages) == 3)
            check(
                "resume 重置轮次与落库计数",
                state.turn_count == 0 and state.persisted_count == 3,
            )
        finally:
            minimal_agent.SESSION_DB = old_db


def test_unknown() -> None:
    """未知命令：不处理，留给主循环提示 /help。"""
    handled, text = run_slash_command("/foo", _make_state())
    check("未知命令未处理", handled is False and text == "")


def test_read_user_input() -> None:
    """提示符读输入：Ctrl+C / EOF 被消化为状态，不再裸崩。"""
    original = minimal_agent.console.input
    try:
        def raise_kb(*_a, **_k):
            raise KeyboardInterrupt

        def raise_eof(*_a, **_k):
            raise EOFError

        minimal_agent.console.input = raise_kb
        text, state = minimal_agent._read_user_input()
        check("Ctrl+C 返回 interrupt", state == "interrupt" and text == "")

        minimal_agent.console.input = raise_eof
        text, state = minimal_agent._read_user_input()
        check("EOF 返回 eof", state == "eof" and text == "")

        minimal_agent.console.input = lambda *_a, **_k: "  你好  "
        text, state = minimal_agent._read_user_input()
        check("正常输入去空白", state == "ok" and text == "你好")
    finally:
        minimal_agent.console.input = original


def main() -> None:
    """跑全部断言。"""
    test_help()
    test_sessions()
    test_diff()
    test_resume()
    test_unknown()
    test_read_user_input()
    if _failures:
        print(f"\n{len(_failures)} 条断言失败：")
        for label in _failures:
            print(f"  - {label}")
        raise SystemExit(1)
    print("\n全部 REPL 斜杠命令断言通过")


if __name__ == "__main__":
    main()
