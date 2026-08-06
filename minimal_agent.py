# -*- coding: utf-8 -*-
"""
最简单的 Agent 骨架（第六步：多轮对话完整版）
—— DeepSeek 版

使用业内常用依赖：
    - openai：官方 OpenAI SDK（DeepSeek 兼容 OpenAI 格式）
    - python-dotenv：从 .env 文件加载环境变量
    - rich：终端美化输出

安装依赖：
    pip install -r requirements.txt

设置环境变量（或在项目根目录创建 .env）：
    DEEPSEEK_API_KEY=你的key
    （可选）DEEPSEEK_BASE_URL=https://api.deepseek.com
    （可选）MODEL=deepseek-chat

记忆设计参考 Hermes Agent（D:\\space\\hermes-agent-main）：
    - 两个 Markdown 文件：MEMORY.md（自己学到的知识）、USER.md（用户画像）
    - 条目之间用 "\\n§\\n" 分隔（对应 tools/memory_tool.py 的 ENTRY_DELIMITER）
    - 模型在对话中主动调用 memory 工具写入（对应 Hermes 的 memory 工具）
    - 对话结束再做一次"记忆审查"提取遗漏信息（对应 Hermes 的 turn-end review）
    - AGENTS.md 项目上下文文件常驻注入（对应 Hermes 的 context files / context 层）
    - 多轮对话：同一会话内连续问答，历史消息逐轮累积并增量落库；
      --resume <session_id> 恢复历史会话（对应 Hermes CLI 会话循环 + /resume）
    - 简化掉了：文件锁、注入威胁扫描、外部漂移检测、可插拔 MemoryProvider
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI
from rich.console import Console
from rich.panel import Panel

from memory_manager import MemoryManager, SyncWorker, build_memory_context_block, load_provider
from context_compressor import compress_context, record_usage, should_compress
from approval import check_dangerous_command
from tool_dispatch import (
    execute_tool_calls_segmented,
    tool_arguments,
    tool_name,
)
from skills import build_skills_index, skills_list, skill_view
from file_tools import (
    patch_file_tool,
    read_file_tool,
    search_files_tool,
    write_file_tool,
)
from redact import redact_sensitive_text

load_dotenv()

# ---------------- 配置 ----------------
API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
MODEL = os.environ.get("MODEL", "deepseek-chat")

BASE_DIR = Path(__file__).parent

# 记忆文件（Hermes 风格：两个 md 文件，条目用 § 分隔）
MEMORY_FILE = BASE_DIR / "MEMORY.md"      # 自己学到的知识
USER_FILE = BASE_DIR / "USER.md"          # 用户画像
SESSION_DB = BASE_DIR / "sessions.db"     # 会话历史库（SQLite + FTS5 全文索引）
ENTRY_DELIMITER = "\n§\n"
CHAR_LIMIT = {"memory": 2200, "user": 1375}  # 字符上限，对齐 Hermes 默认值
# 会话历史保留天数（对齐 Hermes prune_sessions 的默认 older_than_days=90；0 或负数 = 禁用清理）
SESSION_RETENTION_DAYS = float(os.environ.get("SESSION_RETENTION_DAYS", "90") or 90)
# 记忆 nudge 间隔（用户轮次；对齐 Hermes memory.nudge_interval 默认 10；0 = 禁用周期性审查）
MEMORY_NUDGE_INTERVAL = int(os.environ.get("MEMORY_NUDGE_INTERVAL", "10") or 10)

console = Console()


# ---------------- 系统提示词 ----------------
SYSTEM_PROMPT = """你是「小助手」，一个乐于助人的 AI 助手。

## 行为规则
1. 回答要简洁、准确、友好。
2. 需要实时信息（如天气）时，必须先调用 get_weather 工具，再基于工具结果回答。
3. 不确定的信息要如实说明，不要编造。
4. 用户提到关于自己的信息（名字、偏好、习惯）时，用 memory 工具主动记下来：
   - 用户个人信息 → memory(target=user, action=add, content=...)
   - 你自己学到的知识 → memory(target=memory, action=add, content=...)
5. 当记忆占用率接近上限时，用 memory(action=replace) 合并相关旧条目，
   用 memory(action=remove) 删除不再需要的内容，保持记忆精简。
6. 回答前优先使用已注入的上下文（项目上下文、记忆、召回的向量知识）——
   这些内容已经在你的上下文里，直接据此回答，不要为了"确认"而调用搜索工具。
7. session_search 只搜"历史对话记录"（过去聊过什么），不包含记忆库和项目知识。
   只有问题确实需要回忆某次具体对话的细节时，才调用它。
8. 需要专业技能（如发版检查、话术规范）时：先用 skills_list 查看可用技能，
   再用 skill_view 加载技能内容；技能内容加载进上下文后直接据此回答。
9. 执行删除、覆盖等危险操作时，不要先问用户"是否确认"——直接调用 terminal 工具
   执行；系统会自动做危险命令审批（交互模式弹审批，或按 APPROVAL_MODE 配置处理），
   审批通过后命令才会执行。审批被拒绝时再如实告诉用户。
10. 上下文压缩后如果看到 [SKILL_PRUNED: ...] 标记，说明某个技能的内容被压缩裁剪了；
    需要用到该技能时，用标记里的 skill_view(name='...') 重新加载（每个技能一次即可）。
11. 声称"已创建/已删除/已修改"任何文件或目录之前，必须真的通过 terminal 或文件工具
    执行过，并且看到了工具返回结果。没有真实工具返回时，不许说操作"已完成"——
    直接说明需要先执行操作，或用工具真正做一次。"""


def load_system_prompt() -> str:
    """优先读取同目录下的 SYSTEM_PROMPT.md（不改代码改人设），否则用内置的。"""
    prompt_file = BASE_DIR / "SYSTEM_PROMPT.md"
    if prompt_file.exists():
        return prompt_file.read_text(encoding="utf-8").strip()
    return SYSTEM_PROMPT


# ---------------- 记忆存储（Hermes 风格） ----------------
def _path_for(target: str) -> Path:
    """target=user → USER.md，其余 → MEMORY.md。"""
    return USER_FILE if target == "user" else MEMORY_FILE


def load_entries(target: str) -> list[str]:
    """读取一个记忆文件，按 § 分隔解析成条目列表。"""
    path = _path_for(target)
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
        return [e.strip() for e in raw.split(ENTRY_DELIMITER) if e.strip()]
    except Exception:
        return []


def save_entries(target: str, entries: list[str]) -> None:
    """把条目列表写回文件（条目用 § 连接，和 Hermes 一致）。"""
    _path_for(target).write_text(
        ENTRY_DELIMITER.join(entries) + "\n", encoding="utf-8"
    )


def memory_tool(action: str, target: str, content: str, old_text: str = "") -> str:
    """memory 工具的实现：add 新增 / replace 替换 / remove 删除。

    对应 Hermes 的 tools/memory_tool.py：mid-session 写入立即落盘（durable）。
    replace/remove 用 old_text 做子串定位（Hermes 也是"短唯一子串匹配"）。
    """
    target = "user" if target == "user" else "memory"
    entries = load_entries(target)
    added = False

    if action == "add":
        content = content.strip()
        if content and content not in entries:
            entries.append(content)
            added = True
    elif action == "replace":
        if old_text:
            entries = [content.strip() if old_text in e else e for e in entries]
    elif action == "remove":
        if old_text:
            entries = [e for e in entries if old_text not in e]

    # 字符上限保护：超出时从最旧的开始丢弃（Hermes 用 char limit 约束）
    total = 0
    kept: list[str] = []
    for entry in reversed(entries):
        if total + len(entry) + len(ENTRY_DELIMITER) > CHAR_LIMIT[target]:
            continue
        kept.insert(0, entry)
        total += len(entry) + len(ENTRY_DELIMITER)

    save_entries(target, kept)
    current = sum(len(e) + len(ENTRY_DELIMITER) for e in kept)
    limit = CHAR_LIMIT[target]
    pct = min(100, int(current / limit * 100)) if limit > 0 else 0
    return json.dumps(
        {
            "success": True,
            "target": target,
            "count": len(kept),
            "added": added,
            "usage": f"{pct}% — {current:,}/{limit:,} chars",
        },
        ensure_ascii=False,
    )


def render_memory_block(target: str, entries: list[str]) -> str:
    """渲染带占用率的记忆块，对齐 Hermes 的 MemoryStore._render_block()。

    头部展示当前占用百分比和字符数，让模型知道记忆快满、需要合并。
    """
    if not entries:
        return ""
    content = ENTRY_DELIMITER.join(entries)
    current = len(content)
    limit = CHAR_LIMIT[target]
    pct = min(100, int(current / limit * 100)) if limit > 0 else 0
    title = "用户画像" if target == "user" else "记忆（你学到的）"
    return f"## {title} [{pct}% — {current:,}/{limit:,} chars]\n{content}"


def build_system_prompt(manager: MemoryManager | None = None) -> str:
    """组装系统提示词：基础人设 + 项目上下文（AGENTS.md）+ 记忆/用户画像 + 外部 provider。

    对齐 Hermes 的 system_prompt.py 分层：stable（人设）→ context（context files）
    → volatile（记忆快照 + USER.md）。
    """
    prompt = load_system_prompt()
    context_block = load_context_files()
    if context_block:
        prompt += "\n\n" + context_block
    skills_block = build_skills_index()
    if skills_block:
        prompt += "\n\n" + skills_block
    for target in ("memory", "user"):
        block = render_memory_block(target, load_entries(target))
        if block:
            prompt += "\n\n" + block
    if manager:
        ext_block = manager.build_system_prompt()
        if ext_block:
            prompt += "\n\n" + ext_block
    return prompt


def _covered_by(existing: list[str], fact: str) -> bool:
    """新事实是否已被已有条目覆盖（子串重叠）——防止审查环节重复记入。"""
    return any(fact in e or e in fact for e in existing)


CONTEXT_FILE_MAX_CHARS = 20_000  # 对齐 Hermes 的 CONTEXT_FILE_MAX_CHARS 下限


def _truncate_context(content: str, filename: str) -> str:
    """超长截断：保留头尾 + 中间省略标记（对齐 Hermes 的 _truncate_content）。"""
    if len(content) <= CONTEXT_FILE_MAX_CHARS:
        return content
    head_chars = int(CONTEXT_FILE_MAX_CHARS * 0.6)
    tail_chars = int(CONTEXT_FILE_MAX_CHARS * 0.4)
    head = content[:head_chars]
    tail = content[-tail_chars:]
    marker = (
        f"\n\n[...已截断 {filename}：保留前 {head_chars} + 后 {tail_chars} 字符，"
        f"共 {len(content)} 字符。如需完整内容请查看文件 {filename}]\n\n"
    )
    return head + marker + tail


def load_context_files() -> str:
    """发现并加载项目上下文文件（AGENTS.md）。

    对齐 Hermes：context files 扫描 TERMINAL_CWD 下的 AGENTS.md / .cursorrules 等，
    注入系统提示词的 context 层（stable → context → volatile 的中间层）。
    这里简化：扫描当前目录和脚本目录的 AGENTS.md。
    """
    sections = []
    seen: set[Path] = set()
    for base in (Path.cwd(), BASE_DIR):
        path = base / "AGENTS.md"
        try:
            resolved = path.resolve()
        except Exception:
            continue
        if resolved in seen or not path.exists():
            continue
        seen.add(resolved)
        content = path.read_text(encoding="utf-8")
        sections.append(
            f"## 项目上下文（{path.name}@{base.name}）\n{_truncate_context(content, path.name)}"
        )
    return "\n\n".join(sections)


def _db_conn() -> sqlite3.Connection:
    """打开会话数据库并建表（messages + FTS5 全文索引 + sessions 系统提示词）。"""
    conn = sqlite3.connect(SESSION_DB)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS messages ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "session_id TEXT NOT NULL,"
        "role TEXT NOT NULL,"
        "content TEXT NOT NULL,"
        "created_at TEXT DEFAULT (datetime('now')))"
    )
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(search_text)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sessions ("
        "session_id TEXT PRIMARY KEY,"
        "system_prompt TEXT NOT NULL,"
        "updated_at TEXT DEFAULT (datetime('now')))"
    )
    # 归档标记（对齐 Hermes sessions.archived）：软标记，不删数据；
    # 旧库首次访问时补列迁移
    cols = [r[1] for r in conn.execute("PRAGMA table_info(sessions)")]
    if "archived" not in cols:
        conn.execute(
            "ALTER TABLE sessions ADD COLUMN archived INTEGER NOT NULL DEFAULT 0"
        )
        conn.commit()
    return conn


def save_session_prompt(session_id: str, prompt: str) -> None:
    """把系统提示词持久化到会话库（对齐 Hermes SessionDB.update_system_prompt）。

    同一会话重复保存走 UPSERT 覆盖（压缩重建后刷新持久化版本）。
    """
    conn = _db_conn()
    try:
        conn.execute(
            "INSERT INTO sessions (session_id, system_prompt) VALUES (?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "system_prompt=excluded.system_prompt, updated_at=datetime('now')",
            (session_id, prompt),
        )
        conn.commit()
    finally:
        conn.close()


def load_session_prompt(session_id: str) -> str | None:
    """读取持久化的系统提示词（对齐 Hermes _restore_or_build_system_prompt）。"""
    conn = _db_conn()
    try:
        row = conn.execute(
            "SELECT system_prompt FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def prune_sessions(
    older_than_days: float | None = None,
    protect_session_id: str = "",
) -> int:
    """清理不活跃超过保留天数的旧会话（对齐 Hermes SessionDB.prune_sessions）。

    规则：
    - 活跃度 = 会话最后一条消息的 created_at；无消息的会话回退到 sessions.updated_at
    - 同时删除 messages、messages_fts（全文索引）与 sessions 行，返回删除的会话数
    - protect_session_id 保护当前正在使用的会话（Hermes 只清理已结束会话）
    - older_than_days 默认取 SESSION_RETENTION_DAYS（90 天）；<=0 表示禁用
    """
    if older_than_days is None:
        older_than_days = SESSION_RETENTION_DAYS
    if older_than_days <= 0:
        return 0
    cutoff = time.strftime(
        "%Y-%m-%d %H:%M:%S",
        time.gmtime(time.time() - older_than_days * 86400),
    )
    conn = _db_conn()
    try:
        # 所有已知会话：sessions 表 ∪ messages 表里的 session_id
        ids: set[str] = {
            str(r[0]) for r in conn.execute("SELECT session_id FROM sessions")
        }
        ids |= {
            str(r[0])
            for r in conn.execute("SELECT DISTINCT session_id FROM messages")
        }
        to_delete: list[str] = []
        for sid in ids:
            if sid == protect_session_id:
                continue
            row = conn.execute(
                "SELECT MAX(created_at) FROM messages WHERE session_id = ?", (sid,)
            ).fetchone()
            last_active = row[0] if row else None
            if last_active is None:
                row = conn.execute(
                    "SELECT updated_at FROM sessions WHERE session_id = ?", (sid,)
                ).fetchone()
                last_active = row[0] if row else None
            if last_active is None:
                continue
            if str(last_active) < cutoff:
                to_delete.append(sid)

        for sid in to_delete:
            # 先清全文索引，再删消息与会话行（对齐 Hermes：messages + sessions 一起删）
            conn.execute(
                "DELETE FROM messages_fts WHERE rowid IN "
                "(SELECT id FROM messages WHERE session_id = ?)",
                (sid,),
            )
            conn.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))
        conn.commit()
        return len(to_delete)
    finally:
        conn.close()


def _search_tokens(text: str) -> list[str]:
    """检索分词：英文按单词、中文按相邻双字（2-gram）。

    对齐 Hermes 的 CJK tokenizer 思路——FTS5 默认 tokenizer 不切分中文，
    Hermes 为此加载了原生 CJK 扩展，我们用 Python 侧 bigram 模拟。
    """
    tokens = []
    for word in re.findall(r"[a-zA-Z0-9_]+", text.lower()):
        tokens.append(word)
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


def persist_messages(
    session_id: str, messages: list[dict[str, Any]], start: int = 0
) -> None:
    """把（新增的）消息写入会话库。

    对齐 Hermes：只索引 user/assistant 角色（默认 role 过滤），
    搜索文本用 bigram 预分词后进 FTS5；start 用于多轮对话增量落库，
    避免每一轮都把整段历史重复写入。
    """
    conn = _db_conn()
    try:
        for msg in messages[start:]:
            role = msg.get("role")
            content = msg.get("content")
            if role not in ("user", "assistant") or not content:
                continue
            if isinstance(content, list):  # 多模态 content 只取文本块
                content = " ".join(
                    b.get("text", "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            if role == "user":
                # 持久化时剥离注入的 <memory-context> 召回围栏，保留干净内容
                # （Hermes 用 api_content sidecar 实现同样的"干净存储"目标）
                content = re.sub(
                    r"\n*<memory-context>.*?</memory-context>", "", content, flags=re.S
                ).strip()
            if not content:
                continue
            cur = conn.execute(
                "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
                (session_id, role, content),
            )
            conn.execute(
                "INSERT INTO messages_fts (rowid, search_text) VALUES (?, ?)",
                (cur.lastrowid, " ".join(_search_tokens(content))),
            )
        conn.commit()
    finally:
        conn.close()


def load_session_history(session_id: str) -> list[dict[str, Any]]:
    """从会话库加载历史消息，作为 conversation_history（对齐 Hermes 的 /resume）。"""
    conn = _db_conn()
    try:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
        return [{"role": r[0], "content": r[1]} for r in rows]
    finally:
        conn.close()


def list_sessions(
    limit: int = 50,
    include_archived: bool = False,
    archived_only: bool = False,
) -> list[dict[str, Any]]:
    """列出会话记录（按最后活跃倒序，对齐 Hermes api_server 的 list_sessions_rich）。

    过滤语义与 Hermes 一致：
    - 默认只列未归档会话（archived 会话从列表隐藏，但仍可按 id 恢复）
    - include_archived=True：未归档 + 已归档都返回
    - archived_only=True：只返回已归档会话（归档管理视图用）
    每条包含 session_id、updated_at、archived、message_count 和最后一条用户消息的预览。
    """
    if archived_only:
        where_clause = "WHERE s.archived = 1"
    elif not include_archived:
        where_clause = "WHERE s.archived = 0"
    else:
        where_clause = ""
    conn = _db_conn()
    try:
        rows = conn.execute(
            f"""SELECT s.session_id, s.updated_at, s.archived,
                      COUNT(m.id) AS message_count,
                      (SELECT m2.content FROM messages m2
                       WHERE m2.session_id = s.session_id AND m2.role = 'user'
                       ORDER BY m2.id DESC LIMIT 1) AS preview
               FROM sessions s
               LEFT JOIN messages m ON m.session_id = s.session_id
               {where_clause}
               GROUP BY s.session_id
               ORDER BY COALESCE(MAX(m.created_at), s.updated_at) DESC
               LIMIT ?""",
            (max(1, min(int(limit), 200)),),
        ).fetchall()
        return [
            {
                "session_id": r[0],
                "updated_at": r[1] or "",
                "archived": bool(r[2]),
                "message_count": r[3] or 0,
                "preview": (r[4] or "").strip()[:80],
            }
            for r in rows
        ]
    finally:
        conn.close()


def set_session_archived(session_id: str, archived: bool) -> bool:
    """归档/取消归档会话（对齐 Hermes set_session_archived：软标记，不删数据）。

    归档会话从默认列表隐藏，但消息与系统提示词保留，--resume / 按 id 访问仍可用。
    返回是否真的更新了一行（会话不存在返回 False）。
    """
    conn = _db_conn()
    try:
        cur = conn.execute(
            "UPDATE sessions SET archived = ? WHERE session_id = ?",
            (1 if archived else 0, session_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def load_session_messages(session_id: str) -> list[dict[str, Any]]:
    """加载会话的持久化消息（含时间戳），供 Web 前端回显历史（对齐 api_server get_messages）。"""
    conn = _db_conn()
    try:
        rows = conn.execute(
            "SELECT role, content, created_at FROM messages "
            "WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
        return [
            {"role": r[0], "content": r[1], "created_at": r[2] or ""}
            for r in rows
        ]
    finally:
        conn.close()


def search_messages_db(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """FTS5 全文检索，BM25 相关性排序（对齐 Hermes 的 db.search_messages）。"""
    tokens = _search_tokens(query)
    if not tokens:
        return []
    match_expr = " ".join(tokens)  # FTS5 中空格 = AND
    conn = _db_conn()
    try:
        rows = conn.execute(
            """SELECT m.id, m.session_id, m.role, m.content
               FROM messages_fts
               JOIN messages m ON m.id = messages_fts.rowid
               WHERE messages_fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (match_expr, limit),
        ).fetchall()
        return [
            {"id": r[0], "session_id": r[1], "role": r[2], "content": r[3]}
            for r in rows
        ]
    finally:
        conn.close()


def _anchored_window(
    conn: sqlite3.Connection, session_id: str, anchor_id: int, window: int = 3
) -> list[dict[str, Any]]:
    """返回命中消息前后各 window 条，作为上下文窗口（对齐 Hermes 的 get_anchored_view）。"""
    rows = conn.execute(
        """SELECT role, content FROM messages
           WHERE session_id = ? AND id BETWEEN ? AND ?
           ORDER BY id""",
        (session_id, anchor_id - window, anchor_id + window),
    ).fetchall()
    return [{"role": r[0], "content": r[1]} for r in rows]


def session_search_tool(query: str, limit: int = 3) -> str:
    """session_search 工具：搜索历史会话，每个命中会话返回上下文窗口。"""
    hits = search_messages_db(query, limit=max(limit * 5, 5))
    conn = _db_conn()
    try:
        seen: set[str] = set()
        results = []
        for hit in hits:
            sid = hit["session_id"]
            if sid in seen:  # 每个会话只取最相关的一条命中
                continue
            seen.add(sid)
            window = _anchored_window(conn, sid, hit["id"])
            results.append({
                "session_id": sid,
                "matched": hit["content"][:100],
                "window": [
                    {"role": m["role"], "content": m["content"][:200]}
                    for m in window
                ],
            })
            if len(results) >= limit:
                break
    finally:
        conn.close()
    if not results:
        return json.dumps(
            {
                "success": True,
                "query": query,
                "count": 0,
                "results": [],
                "message": (
                    "未在历史对话记录中找到相关内容。"
                    "注意：session_search 只覆盖对话记录；"
                    "记忆、项目上下文和召回的知识请直接使用上下文中已有的信息。"
                ),
            },
            ensure_ascii=False,
        )
    return json.dumps(
        {
            "success": True,
            "query": query,
            "count": len(results),
            "results": results,
        },
        ensure_ascii=False,
    )


def review_memories(client: OpenAI, messages: list[dict[str, Any]]) -> dict[str, list[str]]:
    """对话结束后的记忆审查：让模型补提遗漏的长期信息。

    对应 Hermes 的 turn-end review（MemoryManager.on_session_end / should_review_memory）。
    输出 JSON：{"memory": [...], "user": [...]}，失败时返回空 dict，不影响主流程。
    """
    review_prompt = (
        "根据上面的对话，补充提取值得长期记住的信息，只输出 JSON：\n"
        '{"memory": ["自己学到的知识"], "user": ["用户个人信息"]}\n'
        "要求：不要重复已记住的内容；不要一次性信息（如某次天气结果）；"
        "没有则对应为空数组。"
    )
    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=messages + [{"role": "user", "content": review_prompt}],
            temperature=0,
        )
        text = resp.choices[0].message.content or ""
        match = re.search(r"\{.*\}", text, re.S)
        data = json.loads(match.group(0)) if match else {}
        return {
            "memory": [str(x).strip() for x in data.get("memory", []) if str(x).strip()],
            "user": [str(x).strip() for x in data.get("user", []) if str(x).strip()],
        }
    except Exception as exc:
        console.print(f"[dim]（记忆审查失败，已跳过：{exc}）[/dim]")
        return {}


# ---------------- 第 1 步：调用大模型 ----------------
def create_client() -> OpenAI:
    """创建 OpenAI 兼容客户端（DeepSeek / 其他兼容接口通用）。"""
    return OpenAI(api_key=API_KEY, base_url=BASE_URL)


def call_llm(client: OpenAI, messages: list[dict[str, Any]], tools: list[dict[str, Any]]):
    """把对话消息 + 工具清单发给大模型，返回模型的回复（message 对象）。"""
    response = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        tools=tools,
    )
    # 记录真实 token 用量，供上下文压缩判断（对齐 Hermes：优先用 API 真实值）
    try:
        record_usage(response.usage.prompt_tokens)
    except Exception:
        pass
    return response.choices[0].message


class _StreamMessage:
    """从流式响应累积出的消息对象（对齐非流式 message 的接口）。"""

    def __init__(self) -> None:
        self.content = ""
        self.reasoning_content = ""
        self.tool_calls: list[Any] = []

    def model_dump(self, exclude_none=True) -> dict[str, Any]:
        """序列化供历史回填（与 OpenAI 消息结构一致）。"""
        d: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.reasoning_content:
            d["reasoning_content"] = self.reasoning_content
        if self.tool_calls:
            d["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in self.tool_calls
            ]
        return d


def call_llm_stream(
    client: OpenAI,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    on_token: Any = None,
):
    """流式调用大模型：累积 content / 工具调用 / 推理内容，token 实时回调。

    对齐 Hermes 的流式接口语义：delta 里的 content 增量逐段给 on_token，
    tool_calls 按 index 累积出完整参数。返回 _StreamMessage
    （接口与非流式 message 一致，run_agent_turn 无需分支）。
    """
    response = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        tools=tools,
        stream=True,
    )
    msg = _StreamMessage()
    calls: dict[int, dict[str, str]] = {}
    for chunk in response:
        try:
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                record_usage(getattr(usage, "prompt_tokens", 0) or 0)
        except Exception:
            pass
        if not chunk.choices:
            continue
        delta = getattr(chunk.choices[0], "delta", None)
        if delta is None:
            continue
        text = getattr(delta, "content", None)
        if isinstance(text, str) and text:
            msg.content += text
            if on_token is not None:
                on_token(text)
        reasoning = getattr(delta, "reasoning_content", None)
        if isinstance(reasoning, str) and reasoning:
            msg.reasoning_content += reasoning
        for tc in getattr(delta, "tool_calls", None) or []:
            idx = getattr(tc, "index", 0) or 0
            slot = calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
            if getattr(tc, "id", None):
                slot["id"] = tc.id
            fn = getattr(tc, "function", None)
            if fn is not None:
                if getattr(fn, "name", None):
                    slot["name"] = fn.name
                if getattr(fn, "arguments", None):
                    slot["arguments"] += fn.arguments
    for idx in sorted(calls):
        slot = calls[idx]
        msg.tool_calls.append(
            SimpleNamespace(
                id=slot["id"] or f"call_{idx}",
                type="function",
                function=SimpleNamespace(
                    name=slot["name"], arguments=slot["arguments"]
                ),
            )
        )
    return msg


# ---------------- 工具：定义 + 执行 ----------------
def get_weather(city: str) -> str:
    """示例工具：假装查天气（离线假数据，不需要真联网）。"""
    fake = {"北京": "晴，25°C", "上海": "多云，27°C", "广州": "阵雨，30°C"}
    return fake.get(city, f"{city}：暂无数据，建议看天气预报网站")


# 工具清单：get_weather 查天气，memory 写记忆（Hermes 的 memory 工具）
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询指定城市的天气",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "城市名，例如：北京"},
                },
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory",
            "description": (
                "写入或更新长期记忆。target=memory 表示你自己学到的知识；"
                "target=user 表示用户的个人信息。action=add 新增，"
                "action=replace 用 old_text 定位后替换，action=remove 用 old_text 定位后删除。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["add", "replace", "remove"]},
                    "target": {"type": "string", "enum": ["memory", "user"]},
                    "content": {"type": "string", "description": "要写入的完整内容"},
                    "old_text": {"type": "string", "description": "replace/remove 时定位的原文片段"},
                },
                "required": ["action", "target", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "session_search",
            "description": (
                "搜索历史对话记录（仅覆盖过去聊过的内容，不含记忆库/项目知识/召回）。"
                "当需要回忆某次具体对话说过什么、做过什么时才使用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词，例如：产品评审会"},
                    "limit": {"type": "integer", "description": "最多返回几个会话的上下文（默认 3）"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "terminal",
            "description": (
                "在本地机器上执行一条 shell 命令（Windows 下为系统默认 shell cmd；"
                "PowerShell 专属命令请写成 powershell -Command \"...\"）。"
                "危险命令（删除、格式化、关机、SQL DROP 等）会先征求用户批准；"
                "返回 JSON，含 exit_code 与 output。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的完整命令"},
                    "timeout": {"type": "integer", "description": "最长等待秒数（默认 120）"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skills_list",
            "description": (
                "列出所有可用技能（名称 + 一句话描述，最小元数据）。"
                "需要专业技能时先用它查看有什么，再用 skill_view 加载。"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_view",
            "description": (
                "按需加载技能全文（SKILL.md 正文）或技能包内子文件（如 references/xxx.md）。"
                "参数 name 是技能名，file_path 是技能目录内的相对路径（可省略）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "技能名，如 weather-answer"},
                    "file_path": {"type": "string", "description": "技能包内相对路径（可选）"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "分页读取文本文件（带行号）。敏感文件（.env、密钥、系统配置等）会拒绝读取。"
                "参数 path 是文件路径，offset 起始行（默认 1），limit 每页行数（默认 200）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要读取的文件路径"},
                    "offset": {"type": "integer", "description": "起始行号，默认 1"},
                    "limit": {"type": "integer", "description": "每页最多行数，默认 200"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "写入文本文件（覆盖同名文件，自动创建父目录）。"
                "敏感文件（.env、approval_allowlist.json、密钥、系统目录等）会拒绝写入。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要写入的文件路径"},
                    "content": {"type": "string", "description": "文件完整内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "patch",
            "description": (
                "在文件中做局部替换：找到 old_string 换成 new_string（比整文件重写省 token）。"
                "old_string 必须唯一，除非 replace_all=true；找不到时若 new_string 已存在"
                "会判定补丁已应用。敏感文件（.env 等）拒绝修改。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要修改的文件路径"},
                    "old_string": {"type": "string", "description": "要被替换的原文（必须能唯一定位）"},
                    "new_string": {"type": "string", "description": "替换后的新文本"},
                    "replace_all": {"type": "boolean", "description": "出现多次时是否全部替换（默认 false）"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": (
                "在目录下递归搜索文件名或文件内容（大小写不敏感），返回匹配文件与命中行。"
                "敏感文件（.env 等）与排除目录（.git、__pycache__ 等）会被跳过。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "搜索起始目录"},
                    "pattern": {"type": "string", "description": "搜索关键词"},
                },
                "required": ["path", "pattern"],
            },
        },
    },
]


def run_terminal(
    command: str,
    session_key: str,
    timeout: int = 120,
    client=None,
) -> str:
    """执行本地 shell 命令：先过审批门卫，再运行（对齐 Hermes terminal_tool）。

    审批逻辑在 approval.check_dangerous_command()：危险命令需用户批准
    （once/session/always/deny），拒绝或超时返回 BLOCKED 消息且不执行。
    client 供 APPROVAL_MODE=smart 时辅助 LLM 评估用（没有也能跑，落回人工审批）。
    返回结构与 Hermes terminal_tool 一致：JSON 的 output / exit_code / error 字段，
    用户批准过则附带 approval 说明。
    """
    approval = check_dangerous_command(command, session_key, client=client)
    if not approval.get("approved"):
        console.print(
            Panel(
                f"[red]{approval.get('message', '命令未获批准')}[/red]",
                title="🚫 命令被阻止",
                border_style="red",
            )
        )
        return json.dumps(
            {
                "success": False,
                "exit_code": -1,
                "output": "",
                "error": approval.get("message", "命令未获批准"),
            },
            ensure_ascii=False,
        )

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="replace",
        )
        payload: dict[str, Any] = {
            "success": result.returncode == 0,
            "exit_code": result.returncode,
            "output": (result.stdout or "").strip(),
            "stderr": (result.stderr or "").strip(),
        }
        if approval.get("user_approved"):
            payload["approval"] = (
                f"Command required approval ({approval.get('description', 'flagged')}) "
                "and was approved by the user."
            )
        return json.dumps(payload, ensure_ascii=False)
    except subprocess.TimeoutExpired:
        return json.dumps(
            {
                "success": False,
                "exit_code": -1,
                "output": "",
                "error": f"命令超时（>{timeout}s），已终止等待",
            },
            ensure_ascii=False,
        )
    except Exception as exc:
        return json.dumps(
            {
                "success": False,
                "exit_code": -1,
                "output": "",
                "error": f"执行失败：{exc}",
            },
            ensure_ascii=False,
        )


def run_tool(
    name: str,
    args: dict[str, Any],
    manager: MemoryManager | None = None,
    session_key: str = "",
    client=None,
) -> str:
    """根据工具名找到对应函数并执行。以后加新工具就在这里加一行。"""
    if name == "get_weather":
        return get_weather(args.get("city", ""))
    if name == "memory":
        return memory_tool(
            action=args.get("action", ""),
            target=args.get("target", "memory"),
            content=args.get("content", ""),
            old_text=args.get("old_text", ""),
        )
    if name == "session_search":
        return session_search_tool(
            query=args.get("query", ""),
            limit=int(args.get("limit", 3) or 3),
        )
    if name == "terminal":
        return run_terminal(
            command=args.get("command", ""),
            session_key=session_key,
            timeout=int(args.get("timeout", 120) or 120),
            client=client,
        )
    if name == "skills_list":
        return skills_list()
    if name == "skill_view":
        return skill_view(
            name=args.get("name", ""),
            file_path=args.get("file_path", ""),
        )
    if name == "read_file":
        return read_file_tool(
            path=args.get("path", ""),
            offset=int(args.get("offset", 1) or 1),
            limit=int(args.get("limit", 200) or 200),
        )
    if name == "write_file":
        return write_file_tool(
            path=args.get("path", ""),
            content=args.get("content", ""),
        )
    if name == "patch":
        return patch_file_tool(
            path=args.get("path", ""),
            old_string=args.get("old_string", ""),
            new_string=args.get("new_string", ""),
            replace_all=bool(args.get("replace_all", False)),
        )
    if name == "search_files":
        return search_files_tool(
            path=args.get("path", ""),
            pattern=args.get("pattern", ""),
        )
    if manager is not None and manager.has_tool(name):
        # 外部 provider 自带工具（如 keyword 的 memory_search）
        return manager.handle_tool_call(name, args)
    return f"未知工具：{name}"


def get_tools(manager: MemoryManager | None = None) -> list[dict[str, Any]]:
    """核心工具 + 外部 provider 自带工具（对齐 Hermes：TOOLS + get_all_tool_schemas）。"""
    tools: list[dict[str, Any]] = list(TOOLS)
    if manager:
        tools.extend(manager.get_all_tool_schemas())
    return tools


# ---------------- 主循环：Agent Loop ----------------
def show_current_memory() -> None:
    """启动时展示当前记忆，让用户看到"它记得什么"。"""
    lines = []
    for target, title in (("user", "👤 用户画像"), ("memory", "🧠 记忆")):
        entries = load_entries(target)
        if entries:
            lines.append(f"[bold]{title}[/bold]")
            lines.extend(f"- {e}" for e in entries)
    if lines:
        console.print(Panel("\n".join(lines), title="📖 已记住的信息", border_style="yellow"))


# 工具事件分类：普通工具 / 技能加载 / 第三方来源（外部记忆检索）
_SKILL_TOOLS = {"skills_list", "skill_view"}
_SOURCE_TOOLS = {"memory_search", "vector_search"}


def _tool_event_kind(name: str) -> str:
    """按工具名归类事件类型（tool / skill / source）。"""
    if name in _SKILL_TOOLS:
        return "skill"
    if name in _SOURCE_TOOLS:
        return "source"
    return "tool"


def _tool_event_label(kind: str, name: str, args: dict) -> str:
    """生成事件显示名：skill_view 显示被加载的技能名，其余显示工具名。"""
    if kind == "skill" and name == "skill_view":
        return str(args.get("name") or name)
    return name


def _record_event(
    events: list[dict[str, Any]] | None,
    sink: Any,
    event: dict[str, Any],
) -> dict[str, Any]:
    """记录一个事件：写入列表（带自增 id）并实时回调 sink（SSE 用）。"""
    if events is not None:
        event["id"] = len(events)
        events.append(event)
    if sink is not None:
        sink(dict(event))
    return event


def run_agent_turn(
    client: OpenAI,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    manager: MemoryManager | None = None,
    session_key: str = "",
    events: list[dict[str, Any]] | None = None,
    sink: Any = None,
    on_token: Any = None,
) -> None:
    """执行一轮对话：模型 + 工具循环，直到模型给出最终回答。

    对应 Hermes 的 run_conversation()——一次用户输入驱动整个 while 循环。
    最终回答也会写回 messages（多轮对话依赖它）。
    """
    max_turns = 5  # 最大轮数：防止模型无限调工具

    for turn in range(max_turns):
        # 预检压缩：上下文超过阈值 → 中间轮次摘要化（对齐 Hermes 的 preflight compression）
        if should_compress(messages):
            # 对齐 Hermes commit_memory_session：压缩边界先把当前对话的记忆
            # 同步提取落库——原文马上要被摘要掉，先抢救信息
            if manager:
                manager.commit_memory_session(messages, client=client)
            compressed = compress_context(client, messages)
            if compressed is not messages:
                messages[:] = compressed
                console.print("[dim]🗜️ 上下文已压缩：中间轮次 → 摘要[/dim]")
                # 对齐 Hermes：压缩后重建系统提示词（刷新记忆快照/项目上下文/技能索引），
                # 并同步持久化，供 --resume 恢复最新版本
                if messages and messages[0].get("role") == "system":
                    messages[0]["content"] = build_system_prompt(manager)
                    save_session_prompt(session_key, messages[0]["content"])

        console.print(f"\n[bold blue]--- 第 {turn + 1} 轮：调用大模型 ---[/bold blue]")
        # 思考过程事件：每轮模型调用前记录；若模型暴露推理内容（如
        # DeepSeek 的 reasoning_content）则一并回显（截断 + 脱敏）
        think_event = _record_event(
            events,
            sink,
            {
                "type": "think",
                "name": f"第 {turn + 1} 轮思考",
                "args": "",
                "result": "",
            },
        )
        if on_token is not None:
            msg = call_llm_stream(client, messages, tools, on_token=on_token)
        else:
            msg = call_llm(client, messages, tools)
        reasoning = getattr(msg, "reasoning_content", None) or ""
        if reasoning:
            think_event["result"] = redact_sensitive_text(
                str(reasoning)[:300], force=True
            )
            if sink is not None:
                sink(dict(think_event))

        if msg.tool_calls:
            # 模型要求调用工具：先把 assistant 消息（含 tool_calls）放回历史
            messages.append(msg.model_dump(exclude_none=True))
            tool_calls = list(msg.tool_calls)

            # 先按顺序展示模型要调用的工具（对齐 Hermes：调用开始即展示）
            for tc in tool_calls:
                name = tool_name(tc)
                args = tool_arguments(tc) or {}
                args_display = redact_sensitive_text(
                    json.dumps(args, ensure_ascii=False), force=True
                )
                console.print(
                    f"  [yellow]🔧 模型要调用工具：[/yellow]{name}({args_display})"
                )
            # 记录工具调用事件（按模型原始顺序，参数已脱敏），供 Web 前端展示
            call_events: list[dict[str, Any]] = []
            for tc in tool_calls:
                name = tool_name(tc)
                args = tool_arguments(tc) or {}
                kind = _tool_event_kind(name)
                call_events.append(
                    _record_event(
                        events,
                        sink,
                        {
                            "type": kind,
                            "name": _tool_event_label(kind, name, args),
                            "args": redact_sensitive_text(
                                json.dumps(args, ensure_ascii=False), force=True
                            )
                            or "",
                            "result": "",
                        },
                    )
                )

            def run_one(tc) -> str:
                """执行单个工具调用（parallel 段的工作线程也会调用它）。"""
                name = tool_name(tc)
                args = tool_arguments(tc) or {}
                return run_tool(name, args, manager, session_key, client)

            def on_segment(kind: str, calls: list) -> None:
                """每段执行前提示（对齐 Hermes 并发的"running N tools concurrently"）。"""
                if kind == "parallel":
                    console.print(f"  [dim]⚡ 并行执行 {len(calls)} 个工具[/dim]")

            # 分段执行：parallel 并发 / sequential 串行，结果按原始顺序回填进 messages
            tool_start = len(messages)
            execute_tool_calls_segmented(
                tool_calls,
                messages,
                run_one,
                on_segment=on_segment,
            )

            # 按原始顺序回显工具返回（本段新增的 tool 消息正好一一对应）
            for tool_msg in messages[tool_start:]:
                console.print(f"  [green]📦 工具返回：[/green]{tool_msg['content']}")

            # 按原始顺序把工具结果回填进事件（结果截断 + 脱敏），并实时回调 sink
            for ev, tool_msg in zip(call_events, messages[tool_start:]):
                result = tool_msg.get("content", "") or ""
                ev["result"] = redact_sensitive_text(result[:300], force=True) or ""
                if sink is not None:
                    sink(dict(ev))

            continue  # 回到循环开头，把结果再发给大模型

        # 模型直接给了文字回答 → 写回历史并输出
        messages.append({"role": "assistant", "content": msg.content or ""})
        console.print()
        console.print(Panel(msg.content or "", title="🤖 助手", border_style="green"))
        return

    console.print("\n[yellow]（达到最大轮数，循环结束）[/yellow]")


def review_memory_turn(client: OpenAI, messages: list[dict[str, Any]]) -> None:
    """记忆审查：从对话中补提遗漏信息，写入 MEMORY.md / USER.md。

    对应 Hermes 的 turn-end review / 周期性 memory nudge。
    """
    review = review_memories(client, messages)
    for target in ("memory", "user"):
        existing = load_entries(target)
        new_facts = [f for f in review.get(target, []) if not _covered_by(existing, f)]
        added = []
        for fact in new_facts:
            result = json.loads(memory_tool("add", target, fact))
            if result.get("added"):
                added.append(fact)
        if added:
            title = "🧠 本次新记住（记忆）" if target == "memory" else "👤 本次新记住（用户）"
            console.print(Panel("\n".join(f"+ {f}" for f in added), title=title, border_style="cyan"))


def should_run_memory_nudge(turns_since_memory: int, interval: int) -> tuple[bool, int]:
    """判断当前用户轮次是否触发记忆 nudge（对齐 Hermes：计数达间隔触发并清零）。

    返回 (是否触发, 更新后的计数)。interval <= 0 表示禁用周期性审查。
    """
    if interval <= 0:
        return False, turns_since_memory
    turns_since_memory += 1
    if turns_since_memory >= interval:
        return True, 0
    return False, turns_since_memory


def hydrate_nudge_counter(prior_user_turns: int, interval: int) -> int:
    """恢复会话时对齐 nudge 计数（对齐 Hermes：prior_user_turns % interval）。

    让跨会话的轮次计数连续，恢复后不会立刻多触发一次。
    """
    if interval <= 0 or prior_user_turns <= 0:
        return 0
    return prior_user_turns % interval


def process_turn(
    client: OpenAI,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    manager: MemoryManager | None,
    session_id: str,
    user_input: str,
    turn_count: int,
    turns_since_memory: int,
    review_worker: SyncWorker,
    persisted_count: int,
    events: list[dict[str, Any]] | None = None,
    sink: Any = None,
    on_token: Any = None,
) -> tuple[int, int, int]:
    """处理一条用户消息（REPL 与服务器共用）：召回 → 对话 → 落库 → 同步 → nudge。

    返回 (turn_count, turns_since_memory, persisted_count) 供调用方续接状态。
    """
    turn_count += 1

    # 每轮 prefetch：外部 provider 按当前问题召回（对齐 Hermes 每轮 prefetch_all）
    user_content = user_input
    if manager:
        ext_context = manager.prefetch_all(user_input, session_id=session_id)
        if ext_context:
            user_content += "\n\n" + build_memory_context_block(ext_context)
            console.print(
                Panel(ext_context[:300], title="🔌 外部记忆召回", border_style="magenta")
            )
            _record_event(
                events,
                sink,
                {
                    "type": "source",
                    "name": "记忆召回",
                    "args": "",
                    "result": redact_sensitive_text(ext_context[:200], force=True),
                },
            )

    messages.append({"role": "user", "content": user_content})
    console.print(f"[dim]📚 会话：{session_id}[/dim]")
    run_agent_turn(
        client,
        messages,
        tools,
        manager,
        session_id,
        events=events,
        sink=sink,
        on_token=on_token,
    )

    # 增量落库（对齐 Hermes：消息逐轮写入 SessionDB，中途退出也不丢）
    persist_messages(session_id, messages, start=persisted_count)
    persisted_count = len(messages)

    # 同步本轮对话给外部 provider（对齐 Hermes 的 sync_all）
    if manager:
        last = messages[-1] if messages else {}
        assistant_text = last.get("content", "") if last.get("role") == "assistant" else ""
        manager.sync_all(user_input, assistant_text, messages=messages, client=client)

    # 记忆 nudge：按可配置间隔触发，后台异步审查（对齐 Hermes：不阻塞对话）
    should_review, turns_since_memory = should_run_memory_nudge(
        turns_since_memory, MEMORY_NUDGE_INTERVAL
    )
    if should_review:
        review_worker.submit(
            lambda msgs=list(messages), cli=client: review_memory_turn(cli, msgs)
        )

    return turn_count, turns_since_memory, persisted_count


def main():
    if not API_KEY:
        console.print(
            "[red]❌ 请先设置 DEEPSEEK_API_KEY[/red]\n"
            '  PowerShell: [cyan]$env:DEEPSEEK_API_KEY="你的key"[/cyan]\n'
            "  或在项目根目录创建 .env 文件，参考 README.md"
        )
        return

    client = create_client()

    # 命令行参数：--resume <session_id> [一次性问题]；否则进入交互多轮对话
    args = sys.argv[1:]
    resume_id = None
    one_shot = None
    if args and args[0] == "--resume":
        resume_id = args[1] if len(args) > 1 else None
        one_shot = " ".join(args[2:]) if len(args) > 2 else None
    elif args:
        one_shot = " ".join(args)
    # 一次性问题模式：答完即退出（对齐 Hermes CLI：hermes chat "问题" 单轮退出）
    one_shot_mode = one_shot is not None

    session_id = resume_id or time.strftime("session-%Y%m%d-%H%M%S")
    show_current_memory()

    # 会话历史清理：启动时清掉不活跃超过保留天数的旧会话（对齐 Hermes prune_sessions），
    # 当前正在使用的会话受保护
    pruned_count = prune_sessions(protect_session_id=session_id)
    if pruned_count:
        console.print(f"[dim]🧹 已清理 {pruned_count} 个不活跃旧会话[/dim]")

    # 外部记忆 provider（可选）：环境变量 MEMORY_PROVIDER=keyword
    # 对齐 Hermes 的 memory.provider 配置——同时只激活一个外部 provider
    memory_manager = None
    provider_name = os.environ.get("MEMORY_PROVIDER", "").strip()
    if provider_name:
        try:
            provider = load_provider(provider_name)
            if provider.is_available():
                provider.initialize(session_id=session_id)
                memory_manager = MemoryManager()
                memory_manager.add_provider(provider)
                console.print(f"[dim]🔌 外部记忆 provider：{provider.name}[/dim]")
            else:
                console.print(f"[yellow]⚠️ provider {provider_name} 不可用[/yellow]")
        except Exception as exc:
            console.print(f"[yellow]⚠️ 加载 provider {provider_name} 失败：{exc}[/yellow]")

    # 恢复历史会话（对齐 Hermes 的 /resume：加载 conversation_history）
    messages: list[dict[str, Any]] = []
    if resume_id:
        history = load_session_history(session_id)
        if history:
            console.print(f"[dim]↩️ 已恢复会话 {session_id}（{len(history)} 条历史消息）[/dim]")
            messages = history
        else:
            console.print(f"[yellow]⚠️ 未找到会话 {session_id}，将创建新会话[/yellow]")

    # 系统提示词：新会话构建一次并持久化；--resume 恢复持久化版本
    # （对齐 Hermes 的 _restore_or_build_system_prompt：先查库，没有再构建）
    system_prompt = load_session_prompt(session_id) if resume_id else None
    if system_prompt is None:
        system_prompt = build_system_prompt(memory_manager)
        save_session_prompt(session_id, system_prompt)
    messages.insert(0, {"role": "system", "content": system_prompt})

    persisted_count = len(messages)  # 已落库的消息数，增量写入从这里开始
    tools = get_tools(memory_manager)  # 核心工具 + provider 自带工具

    # 记忆 nudge：后台审查 worker + 恢复会话时对齐计数（对齐 Hermes 的 nudge_interval）
    review_worker = SyncWorker()
    turns_since_memory = (
        hydrate_nudge_counter(
            sum(1 for m in messages if m.get("role") == "user"),
            MEMORY_NUDGE_INTERVAL,
        )
        if resume_id
        else 0
    )

    turn_count = 0
    while True:
        if one_shot is not None:
            user_input = one_shot
            one_shot = None  # 一次性参数用完后进入交互模式
        else:
            try:
                user_input = console.input("\n[bold]你说（输入 退出 结束）：[/bold] ").strip()
            except EOFError:  # stdin 结束（如管道输入完毕）
                break
        if not user_input:
            continue
        if user_input in ("退出", "exit", "quit", "/exit"):
            break

        turn_count, turns_since_memory, persisted_count = process_turn(
            client,
            messages,
            tools,
            memory_manager,
            session_id,
            user_input,
            turn_count,
            turns_since_memory,
            review_worker,
            persisted_count,
        )

        if one_shot_mode:
            break  # 一次性问题已答完，退出

    # 会话结束：最后一次记忆审查（后台执行），排空后干净退出
    if turn_count > 0:
        review_worker.submit(
            lambda msgs=list(messages), cli=client: review_memory_turn(cli, msgs)
        )
    review_worker.flush(timeout=10)
    review_worker.shutdown()
    # 排空后台记忆同步（有界等待，不阻塞退出；同步卡住则放弃）
    if memory_manager:
        memory_manager.flush_pending(timeout=10)
        memory_manager.shutdown()
    console.print(f"\n[dim]会话已保存。下次用 --resume {session_id} 继续对话。[/dim]")


if __name__ == "__main__":
    main()
