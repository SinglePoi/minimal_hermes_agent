# -*- coding: utf-8 -*-
"""
会话历史清理策略的回归测试（零依赖，直接运行）：
    python tests/test_session_cleanup.py

覆盖（对齐 Hermes SessionDB.prune_sessions 的 older_than_days 语义）：
    - 不活跃超过保留天数的旧会话被删除（messages + FTS 一起清）
    - 新会话保留、当前会话受保护
    - 孤儿消息（无 sessions 行）与空会话（无消息）也被覆盖
    - 保留天数 <=0 时禁用清理
"""

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import minimal_agent  # noqa: E402


_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    """记录一条断言结果。"""
    if cond:
        print(f"  ok: {label}")
    else:
        _failures.append(label)
        print(f"  FAIL: {label}")


def add_session(
    sid: str,
    messages: list[tuple[str, str, str]] | None = None,
    updated_at: str | None = None,
) -> None:
    """往临时库里插入一个会话（messages 为 (role, content, created_at) 列表）。"""
    conn = minimal_agent._db_conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO sessions (session_id, system_prompt, updated_at) "
            "VALUES (?, ?, COALESCE(?, datetime('now')))",
            (sid, "prompt", updated_at),
        )
        for role, content, created_at in messages or []:
            cur = conn.execute(
                "INSERT INTO messages (session_id, role, content, created_at) "
                "VALUES (?, ?, ?, ?)",
                (sid, role, content, created_at),
            )
            conn.execute(
                "INSERT INTO messages_fts (rowid, search_text) VALUES (?, ?)",
                (cur.lastrowid, content),
            )
        conn.commit()
    finally:
        conn.close()


def session_exists(sid: str) -> bool:
    """检查会话是否还存在（sessions 表或 messages 表）。"""
    conn = minimal_agent._db_conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM sessions WHERE session_id = ?", (sid,)
        ).fetchone()
        if row:
            return True
        row = conn.execute(
            "SELECT 1 FROM messages WHERE session_id = ? LIMIT 1", (sid,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def fts_has(sid: str) -> bool:
    """检查该会话是否还有全文索引残留。"""
    conn = minimal_agent._db_conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM messages_fts f JOIN messages m ON m.id = f.rowid "
            "WHERE m.session_id = ? LIMIT 1",
            (sid,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


OLD = "2020-01-01 00:00:00"
NEW = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def test_prune_old_sessions() -> None:
    """旧会话连同 messages/FTS 一起清掉；新会话保留。"""
    original_db = minimal_agent.SESSION_DB
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            minimal_agent.SESSION_DB = Path(tmpdir) / "sessions.db"
            add_session("old", [("user", "旧内容", OLD), ("assistant", "旧回答", OLD)])
            add_session("new", [("user", "新内容", NEW), ("assistant", "新回答", NEW)])

            count = minimal_agent.prune_sessions(older_than_days=90)
            check("只删除 1 个旧会话", count == 1)
            check("旧会话已删", not session_exists("old"))
            check("旧会话 FTS 已清", not fts_has("old"))
            check("新会话保留", session_exists("new"))
            check("新会话 FTS 保留", fts_has("new"))
    finally:
        minimal_agent.SESSION_DB = original_db


def test_protect_and_orphans() -> None:
    """当前会话受保护；孤儿消息（无 sessions 行）也被清理。"""
    original_db = minimal_agent.SESSION_DB
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            minimal_agent.SESSION_DB = Path(tmpdir) / "sessions.db"
            add_session("protect-me", [("user", "正在用", NEW)])
            add_session("orphan", [("user", "孤儿消息", OLD)], updated_at=OLD)
            add_session("empty-old", messages=[], updated_at=OLD)

            count = minimal_agent.prune_sessions(
                older_than_days=90, protect_session_id="protect-me"
            )
            check("保护当前会话 + 删孤儿/空会话（共 2 个）", count == 2)
            check("当前会话保留", session_exists("protect-me"))
            check("孤儿消息会话已删", not session_exists("orphan"))
            check("空旧会话已删", not session_exists("empty-old"))
    finally:
        minimal_agent.SESSION_DB = original_db


def test_disable_and_default() -> None:
    """保留天数 <=0 禁用清理；默认常量为 90（对齐 Hermes）。"""
    original_db = minimal_agent.SESSION_DB
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            minimal_agent.SESSION_DB = Path(tmpdir) / "sessions.db"
            add_session("old", [("user", "旧内容", OLD)])
            check("默认保留天数 = 90",
                  minimal_agent.SESSION_RETENTION_DAYS == 90)
            count = minimal_agent.prune_sessions(older_than_days=0)
            check("禁用清理 -> 0 且不删", count == 0 and session_exists("old"))
    finally:
        minimal_agent.SESSION_DB = original_db


def main() -> None:
    """依次运行全部测试并汇总结果。"""
    print("== 会话历史清理回归测试 ==")
    for test_fn in (
        test_prune_old_sessions,
        test_protect_and_orphans,
        test_disable_and_default,
    ):
        print(f"[{test_fn.__name__}]")
        test_fn()
    print()
    if _failures:
        print(f"共 {len(_failures)} 个用例失败：")
        for label in _failures:
            print(f"  - {label}")
        sys.exit(1)
    print("全部用例通过 ✅")


if __name__ == "__main__":
    main()
