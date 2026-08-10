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
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

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
import web_tools
from title_generator import auto_title_session, maybe_auto_title
from ansi_strip import strip_ansi
from tool_output_limits import truncate_output
from working_diff import (
    collect_working_diff,
    parse_diff_files,
    summarize_files,
    working_diff_tool,
)
from retry_utils import call_with_retry
from todo_tool import (
    TODO_SCHEMA,
    get_todo_store,
    hydrate_todo_store,
    render_todo_lines,
    todo_tool,
)
from file_tools import (
    patch_file_tool,
    read_file_tool,
    search_files_tool,
    write_file_tool,
)
from redact import redact_sensitive_text

# 配置源：.env 优先于系统环境变量（override=True，2026-08-07 按用户要求调整）
load_dotenv(override=True)

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


def _env_int(name: str, default: int, min_value: int = 0) -> int:
    """读取环境变量整数配置（非法值回退默认，避免启动崩溃）。"""
    try:
        return max(min_value, int(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        return default


# turn 级预算（对齐 Hermes 的 max_iterations / iteration_budget，简化版）：
# - MAX_AGENT_TURNS：单次提问内"调模型"的最大轮数（默认 5）
# - TURN_TOKEN_BUDGET：单次提问累计 prompt token 预算（0 = 不限制）
MAX_AGENT_TURNS = _env_int("MAX_AGENT_TURNS", 5, 1)
TURN_TOKEN_BUDGET = _env_int("TURN_TOKEN_BUDGET", 0, 0)

# Windows 下控制台默认 GBK/cp936，rich 渲染 emoji 等 UTF-8 字符会抛
# UnicodeEncodeError（'gbk' codec can't encode）崩溃；启动早期强制 stdout/stderr
# 走 UTF-8，避免发版冒烟与日常 REPL 在中文控制台直接炸。
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        # 非文本流或旧版本 Python 无 reconfigure，交给 rich 的 legacy 渲染兜底
        pass

console = Console()


# ---------------- 系统提示词 ----------------
SYSTEM_PROMPT = """你是「小助手」，一个乐于助人的 AI 助手。

## 行为规则
1. 回答要简洁、准确、友好。
2. 需要实时/最新信息（天气、新闻、资料等）时，先用 web_search 工具查询，
   再基于工具结果回答。
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
    直接说明需要先执行操作，或用工具真正做一次。
12. 需要当前日期/时间时，用 get_current_time 工具；不要调用终端命令 date/time
    （Windows cmd 下它们是交互式命令，会等待输入并卡住）。需要联网查最新信息
    （新闻、资料、事实核查等）时用 web_search / web_fetch，不要用 terminal 模拟联网。
13. 用 skill_view 加载技能后，如果返回里带 setup_needed 或 missing_required_*
    （技能缺少所需环境变量/命令，未就绪），要如实告诉用户缺什么、怎么补齐；
    不要假装技能可用，也不要编造技能内容。"""


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
    → volatile（记忆快照 + USER.md）；技能索引按当前可用工具集做条件过滤
    （对齐 Hermes build_skills_system_prompt(available_tools=...)）。
    """
    prompt = load_system_prompt()
    context_block = load_context_files()
    if context_block:
        prompt += "\n\n" + context_block
    skills_block = build_skills_index(available_tools=available_tool_names(manager))
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
    """打开会话数据库并建表（messages + FTS5 全文索引 + sessions 系统提示词 + events）。"""
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
    # 过程事件表（思考/工具/技能/来源）：对话进行时实时展示 + 落库，切换会话后
    # 按 user_message_id 分组还原活动托盘（挂在对应用户消息之后）
    conn.execute(
        "CREATE TABLE IF NOT EXISTS events ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "session_id TEXT NOT NULL,"
        "user_message_id INTEGER NOT NULL,"
        "type TEXT NOT NULL,"
        "name TEXT NOT NULL DEFAULT '',"
        "args TEXT NOT NULL DEFAULT '',"
        "result TEXT NOT NULL DEFAULT '',"
        "created_at TEXT DEFAULT (datetime('now')))"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_session ON events (session_id, user_message_id)"
    )
    # 每轮耗时（毫秒）：events 表迁移补列，供前端"已耗时 Xs"展示
    ev_cols = [r[1] for r in conn.execute("PRAGMA table_info(events)")]
    if "duration_ms" not in ev_cols:
        conn.execute("ALTER TABLE events ADD COLUMN duration_ms INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    # 归档标记（对齐 Hermes sessions.archived）：软标记，不删数据；
    # 旧库首次访问时补列迁移
    cols = [r[1] for r in conn.execute("PRAGMA table_info(sessions)")]
    if "archived" not in cols:
        conn.execute(
            "ALTER TABLE sessions ADD COLUMN archived INTEGER NOT NULL DEFAULT 0"
        )
    if "title" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN title TEXT")
    if "archived" not in cols or "title" not in cols:
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


def delete_session(session_id: str) -> bool:
    """硬删一个会话及其全部消息（对齐 Hermes SessionDB.delete_session）。

    在单个事务里删除 messages_fts（全文索引）、messages 与 sessions 行；
    返回是否真的存在并删除。供 HTTP DELETE /sessions/<id> 与前端删除入口使用。
    """
    conn = _db_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        exists = bool(row and row[0])
        # 先清全文索引，再删消息与会话行（与 prune_sessions 保持一致）
        conn.execute(
            "DELETE FROM messages_fts WHERE rowid IN "
            "(SELECT id FROM messages WHERE session_id = ?)",
            (session_id,),
        )
        conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        conn.commit()
        return exists
    finally:
        conn.close()


def set_session_title(session_id: str, title: str) -> bool:
    """设置会话标题（对齐 Hermes api_server 的 PATCH /api/sessions 标题更新）。

    title 为空串表示清除标题（回退到自动标题/预览）；返回会话是否存在。
    """
    conn = _db_conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return False
        conn.execute(
            "UPDATE sessions SET title = ?, updated_at = datetime('now') "
            "WHERE session_id = ?",
            (title, session_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def get_session_title(session_id: str) -> str:
    """读取会话当前标题（无标题或会话不存在返回空串）。"""
    conn = _db_conn()
    try:
        row = conn.execute(
            "SELECT title FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return (row[0] or "") if row else ""
    finally:
        conn.close()


def set_auto_title_if_empty(session_id: str, title: str) -> bool:
    """仅当会话当前没有标题时写入（LLM 后台生成 vs 人工改名的竞态保护）。

    对齐 Hermes 的 set_auto_title_if_empty：谓词 + 写入在同一个 UPDATE 里完成，
    人工改名（PATCH）与后台自动生成并发时，先落库的赢，自动生成绝不覆盖。
    返回是否真的写入。
    """
    conn = _db_conn()
    try:
        cur = conn.execute(
            "UPDATE sessions SET title = ?, updated_at = datetime('now') "
            "WHERE session_id = ? AND (title IS NULL OR trim(title) = '')",
            (title, session_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def fork_session(source_id: str, new_id: str, title: str = "") -> bool:
    """复制会话历史开新会话（对齐 Hermes api_server 的 POST /api/sessions/<id>/fork）。

    复制 system_prompt、标题与全部消息（含 FTS 全文索引）；新会话 archived=0。
    源会话不存在或新 id 已占用返回 False。简化：无 parent_session_id 血缘列，
    删除源会话不影响分支（与 Hermes"分支子会话独立"语义一致）。
    """
    conn = _db_conn()
    try:
        src = conn.execute(
            "SELECT system_prompt, title FROM sessions WHERE session_id = ?",
            (source_id,),
        ).fetchone()
        if src is None:
            return False
        if conn.execute(
            "SELECT 1 FROM sessions WHERE session_id = ?", (new_id,)
        ).fetchone():
            return False
        system_prompt, src_title = src
        fork_title = title.strip() if title.strip() else (
            f"{src_title.strip()} fork" if (src_title or "").strip() else "fork"
        )
        conn.execute(
            "INSERT INTO sessions (session_id, system_prompt, updated_at, archived, title) "
            "VALUES (?, ?, datetime('now'), 0, ?)",
            (new_id, system_prompt, fork_title),
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, content, created_at) "
            "SELECT ?, role, content, created_at FROM messages WHERE session_id = ?",
            (new_id, source_id),
        )
        for mid, content in conn.execute(
            "SELECT id, content FROM messages WHERE session_id = ?", (new_id,)
        ):
            conn.execute(
                "INSERT INTO messages_fts (rowid, search_text) VALUES (?, ?)",
                (mid, " ".join(_search_tokens(content))),
            )
        conn.commit()
        return True
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
) -> Optional[int]:
    """把（新增的）消息写入会话库。

    对齐 Hermes：只索引 user/assistant 角色（默认 role 过滤），
    搜索文本用 bigram 预分词后进 FTS5；start 用于多轮对话增量落库，
    避免每一轮都把整段历史重复写入。
    返回本轮最后一条用户消息的 rowid（供过程事件挂靠）；无则返回 None。
    """
    conn = _db_conn()
    last_user_id: Optional[int] = None
    try:
        for msg in messages[start:]:
            role = msg.get("role")
            content = msg.get("content")
            # 内部指令（如轮次收尾的"已达上限"）不落库，避免重放时伪装成用户提问
            if msg.get("_finalize"):
                continue
            if role not in ("user", "assistant") or not content:
                continue
            # 中间轮旁白（带 tool_calls 的 assistant 消息）属于"过程"：落库跳过，
            # 重放时只通过 events 托盘展示，避免在消息区重复出现
            if role == "assistant" and msg.get("tool_calls"):
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
            if role == "user":
                last_user_id = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO messages_fts (rowid, search_text) VALUES (?, ?)",
                (cur.lastrowid, " ".join(_search_tokens(content))),
            )
            # 标题不在落库时生成：首轮交换完成后由 title_generator 后台线程用
            # LLM 生成（对齐 Hermes agent/title_generator.py），失败回退截断，
            # 通过 set_auto_title_if_empty 原子写入，避免覆盖人工改名
        conn.commit()
        return last_user_id
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
    每条包含 session_id、updated_at、archived、title、message_count 和最后一条用户消息的预览。
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
            f"""SELECT s.session_id, s.updated_at, s.archived, s.title,
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
                "title": r[3] or "",
                "message_count": r[4] or 0,
                "preview": (r[5] or "").strip()[:80],
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
            "SELECT id, role, content, created_at FROM messages "
            "WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
        return [
            {"id": r[0], "role": r[1], "content": r[2], "created_at": r[3] or ""}
            for r in rows
        ]
    finally:
        conn.close()


def persist_events(
    session_id: str,
    user_message_id: Optional[int],
    events: list[dict[str, Any]],
    duration_ms: int = 0,
) -> None:
    """把一轮的过程事件（思考/工具/技能/来源）写入 events 表。

    实时事件只在对话进行时展示，落库后切换会话可按 user_message_id 分组还原活动托盘；
    只存展示所需字段（type/name/args/result + duration_ms 本轮耗时），单字段截断防膨胀。
    """
    if not events or not user_message_id:
        return
    conn = _db_conn()
    try:
        for ev in events:
            # 无推理内容的思考事件不落库（前端同样不展示）
            if ev.get("type") == "think" and not str(ev.get("result") or "").strip():
                continue
            conn.execute(
                "INSERT INTO events "
                "(session_id, user_message_id, type, name, args, result, duration_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    int(user_message_id),
                    str(ev.get("type") or "tool")[:32],
                    str(ev.get("name") or "")[:200],
                    str(ev.get("args") or "")[:2000],
                    str(ev.get("result") or "")[:2000],
                    int(duration_ms),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def load_session_events(session_id: str) -> list[dict[str, Any]]:
    """读取会话的过程事件（按 user_message_id, id 排序），供前端还原活动托盘。"""
    conn = _db_conn()
    try:
        rows = conn.execute(
            "SELECT user_message_id, type, name, args, result, duration_ms FROM events "
            "WHERE session_id=? ORDER BY user_message_id, id",
            (session_id,),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "user_message_id": r[0],
            "type": r[1],
            "name": r[2],
            "args": r[3],
            "result": r[4],
            "duration_ms": r[5],
        }
        for r in rows
    ]


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
        resp = call_with_retry(
            lambda: client.chat.completions.create(
                model=MODEL,
                messages=messages + [{"role": "user", "content": review_prompt}],
                temperature=0,
            ),
            what="记忆审查",
            on_retry=_on_llm_retry,
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


def _on_llm_retry(what: str, attempt: int, delay: float, exc: Exception) -> None:
    """调模型失败后的可见提示：告诉用户"线路忙，等一下再试"。"""
    console.print(
        f"[dim]（{what}失败，{delay:.1f}s 后重试第 {attempt} 次：{exc}）[/dim]"
    )


def _handle_model_call_failure(exc: Exception, messages: list[dict[str, Any]]) -> None:
    """模型调用重试耗尽 / 不可重试失败：转成助手错误消息，不裸崩。

    大白话：重试 3 次还是失败（或参数错误这类重试也没用的错），就不再往上抛
    Traceback——告诉用户"这次没调通"，本轮到此为止，下一条消息可以继续。
    """
    error_text = f"（模型调用失败：{exc}）"
    console.print(
        Panel(f"[red]{error_text}[/red]", title="🤖 助手", border_style="red")
    )
    messages.append({"role": "assistant", "content": error_text})


def call_llm(client: OpenAI, messages: list[dict[str, Any]], tools: list[dict[str, Any]]):
    """把对话消息 + 工具清单发给大模型，返回 (message, prompt_tokens)。

    prompt_tokens 供 turn 级 token 预算统计真实用量（对齐 Hermes 优先用 API 真实值）。
    """
    response = call_with_retry(
        lambda: client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=tools,
        ),
        what="模型调用",
        on_retry=_on_llm_retry,
    )
    try:
        prompt_tokens = int(response.usage.prompt_tokens or 0)
    except Exception:
        prompt_tokens = 0
    if prompt_tokens:
        # 记录真实 token 用量，供上下文压缩判断（对齐 Hermes：优先用 API 真实值）
        record_usage(prompt_tokens)
    return response.choices[0].message, prompt_tokens


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
    （接口与非流式 message 一致）+ 流内累积的 prompt_tokens（供 turn budget 统计）。
    """
    response = call_with_retry(
        lambda: client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=tools,
            stream=True,
        ),
        what="流式模型调用",
        on_retry=_on_llm_retry,
    )
    msg = _StreamMessage()
    calls: dict[int, dict[str, str]] = {}
    stream_tokens = 0
    for chunk in response:
        try:
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                tokens = getattr(usage, "prompt_tokens", 0) or 0
                if tokens:
                    stream_tokens = int(tokens)
                    record_usage(stream_tokens)
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
    return msg, stream_tokens


# ---------------- 工具：定义 + 执行 ----------------
_WEEKDAYS_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def get_current_time() -> str:
    """返回当前本地日期时间与星期（模型问"今天几号/几点"时用，避免误调交互式 date 命令）。"""
    now = time.localtime()
    return time.strftime("%Y-%m-%d %H:%M:%S", now) + " " + _WEEKDAYS_CN[now.tm_wday]


# 工具清单：get_current_time 查时间，memory 写记忆，web_search 联网（Hermes 工具体系）
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": (
                "获取当前本地日期时间与星期。模型需要知道今天几号、现在几点、星期几时用它，"
                "不要调用终端命令 date/time（Windows 下会交互式等待输入而卡住）。"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
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
            "name": "working_diff",
            "description": (
                "查看当前工作区的 git 改动：mode=working（默认）返回未暂存改动+未跟踪文件，"
                "staged 返回已 git add 的改动，all 返回相对 HEAD 的全部改动+未跟踪文件。"
                "当需要回答'工作区改了什么/这个项目最近改了哪些文件'时使用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["working", "staged", "all"],
                        "description": "diff 模式，默认 working",
                    },
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "只查看这些路径的改动（可选）",
                    },
                },
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
                    "name": {"type": "string", "description": "技能名，如 release-check"},
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
                ".docx / .xlsx / .ipynb 文档会自动抽取成文本再分页返回。"
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
                "修改文件。mode=replace（默认）：找到 old_string 换成 new_string，"
                "支持模糊匹配兜底，old_string 必须唯一（除非 replace_all=true）。"
                "mode=patch：V4A 补丁格式批量操作（*** Update/Add/Delete/Move File:），"
                "先校验后应用。敏感文件（.env 等）与 .. 穿越路径一律拒绝。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "description": "replace（默认）或 patch（V4A 补丁）"},
                    "path": {"type": "string", "description": "replace 模式要修改的文件路径"},
                    "old_string": {"type": "string", "description": "replace 模式：要被替换的原文（必须能唯一定位）"},
                    "new_string": {"type": "string", "description": "replace 模式：替换后的新文本"},
                    "replace_all": {"type": "boolean", "description": "出现多次时是否全部替换（默认 false）"},
                    "patch": {"type": "string", "description": "patch 模式：V4A 补丁内容（*** Begin Patch ... *** End Patch）"},
                },
                "required": ["mode"],
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
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "联网搜索：按关键词搜索互联网，返回标题/链接/摘要。"
                "需要最新信息或本地知识库查不到时使用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词，例如：2026 年 AI 最新进展"},
                    "limit": {"type": "integer", "description": "返回结果条数（1-10，默认 5）"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": (
                "抓取网页正文：输入 http/https 网址，返回页面可读文本（自动去标签、截断）。"
                "用于阅读搜索到的链接或指定网页；仅允许公网地址。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "完整的 http/https 网页地址"},
                    "max_chars": {"type": "integer", "description": "返回最大字符数（默认 4000）"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": TODO_SCHEMA,
    },
]


def _kill_process_tree(proc) -> None:
    """杀掉子进程（Windows 用 taskkill /T 杀整棵树）。

    shell=True 时 ping 这类命令是 cmd 的孙进程：只 kill cmd 的话孙进程仍占着
    stdout 管道，communicate() 会一直阻塞；taskkill /T 连子孙一起杀（对齐
    Hermes 中断时终止整棵进程树的语义）。
    """
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        else:
            proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


_ENV_DUMP_COMMANDS = frozenset({"env", "printenv", "set", "export", "declare"})


def _is_env_dump_command(command: str) -> bool:
    """判断命令是否是环境变量倾倒（env/printenv/set/export/declare 开头）。

    对齐 Hermes redact.is_env_dump_command：按管道/分号/&& 拆段后看每段首词，
    命中说明输出大概率是 KEY=value，脱敏时走赋值规则（code_file=False）。
    """
    if not command:
        return False
    for seg in re.split(r"[|;&]+", command):
        seg = seg.strip()
        if not seg:
            continue
        tokens = seg.split(maxsplit=1)
        first = tokens[0] if tokens else seg
        if first in _ENV_DUMP_COMMANDS:
            return True
    return False


def _clean_terminal_output(raw: str, command: str = "") -> str:
    """清洗终端输出：截断 → 剥 ANSI → 脱敏（对齐 Hermes terminal_tool）。

    顺序与 Hermes 一致：先截断（头 40% + 尾 60% + 省略标记，上限 50000 字符），
    再 strip_ansi（防模型把转义序列抄进文件写入），最后 redact 脱敏。env 类
    命令输出就是 KEY=value，走赋值规则（code_file=False）；其他输出按代码/
    数据文件处理（code_file=True），避免源码/配置常量误伤。
    """
    text = truncate_output(raw or "")
    text = strip_ansi(text)
    return redact_sensitive_text(text, code_file=not _is_env_dump_command(command)) or ""


def _run_terminal_interruptible(
    command: str,
    timeout: int,
    interrupt_event: Any,
) -> str:
    """带中断的终端执行：事件置位立即 kill 子进程（对齐 Hermes 线程级中断信号）。

    用 Popen + 0.5s 轮询 communicate；返回结构与 run_terminal 一致（JSON），
    中断时带 status=cancelled。
    """
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            errors="replace",
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
    deadline = time.time() + timeout
    while True:
        if interrupt_event.is_set():
            _kill_process_tree(proc)
            try:
                out, err = proc.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                out, err = "", ""
            return json.dumps(
                {
                    "success": False,
                    "exit_code": -1,
                    "output": _clean_terminal_output(out, command).strip(),
                    "error": "命令被用户中断",
                    "status": "cancelled",
                },
                ensure_ascii=False,
            )
        try:
            out, err = proc.communicate(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            if time.time() > deadline:
                _kill_process_tree(proc)
                try:
                    out, err = proc.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    out, err = "", ""
                return json.dumps(
                    {
                        "success": False,
                        "exit_code": -1,
                        "output": "",
                        "error": f"命令超时（>{timeout}s），已终止等待",
                    },
                    ensure_ascii=False,
                )
    return json.dumps(
        {
            "success": proc.returncode == 0,
            "exit_code": proc.returncode,
            "output": _clean_terminal_output(out, command).strip(),
            "stderr": _clean_terminal_output(err, command).strip(),
        },
        ensure_ascii=False,
    )


def run_terminal(
    command: str,
    session_key: str,
    timeout: int = 120,
    client=None,
    interrupt_event: Any = None,
) -> str:
    """执行本地 shell 命令：先过审批门卫，再运行（对齐 Hermes terminal_tool）。

    审批逻辑在 approval.check_dangerous_command()：危险命令需用户批准
    （once/session/always/deny），拒绝或超时返回 BLOCKED 消息且不执行。
    client 供 APPROVAL_MODE=smart 时辅助 LLM 评估用（没有也能跑，落回人工审批）。
    interrupt_event 置位时立即杀掉子进程并返回 cancelled（对齐 Hermes 线程级中断信号）。
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
        if interrupt_event is not None:
            return _run_terminal_interruptible(command, timeout, interrupt_event)
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            # stdin 置空：防止交互式命令（如 Windows cmd 内置的 date/time）在
            # 服务终端里等待输入造成"无限等待"（模型误调裸 date 曾卡住整轮）
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
            errors="replace",
        )
        payload: dict[str, Any] = {
            "success": result.returncode == 0,
            "exit_code": result.returncode,
            "output": _clean_terminal_output(result.stdout, command).strip(),
            "stderr": _clean_terminal_output(result.stderr, command).strip(),
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
    interrupt_event: Any = None,
    available_tools: set[str] | None = None,
) -> str:
    """根据工具名找到对应函数并执行。以后加新工具就在这里加一行。

    available_tools 传给 skills_list 做条件激活过滤（对齐 Hermes：技能列表
    按当前可用工具集展示）；None 时显示全部（向后兼容）。
    """
    if name == "get_current_time":
        return get_current_time()
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
            interrupt_event=interrupt_event,
        )
    if name == "working_diff":
        return working_diff_tool(
            mode=args.get("mode", "working"),
            paths=args.get("paths") or None,
        )
    if name == "skills_list":
        return skills_list(available_tools=available_tools)
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
            mode=args.get("mode", "replace"),
            patch=args.get("patch", ""),
        )
    if name == "search_files":
        return search_files_tool(
            path=args.get("path", ""),
            pattern=args.get("pattern", ""),
        )
    if name == "web_search":
        return web_tools.web_search(
            query=args.get("query", ""),
            limit=int(args.get("limit", 5) or 5),
        )
    if name == "web_fetch":
        return web_tools.web_fetch(
            url=args.get("url", ""),
            max_chars=int(args.get("max_chars", 4000) or 4000),
        )
    if name == "todo":
        # 会话级内存任务清单（对齐 Hermes：每个会话一个 TodoStore）
        return todo_tool(
            todos=args.get("todos"),
            merge=bool(args.get("merge", False)),
            store=get_todo_store(session_key),
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


def available_tool_names(manager: MemoryManager | None = None) -> set[str]:
    """汇总核心工具 + provider 自带工具的名字集合（供技能条件过滤）。

    对齐 Hermes：build_skills_system_prompt(available_tools=...) 用当前可用工具集
    过滤 requires_tools / fallback_for_tools 条件。manager 为桩对象（无
    get_all_tool_schemas）时只统计核心 TOOLS，保证压缩重建提示词等路径不崩。
    """
    names = {tool_name(t) for t in TOOLS}
    if manager is not None and hasattr(manager, "get_all_tool_schemas"):
        names.update(tool_name(t) for t in manager.get_all_tool_schemas())
    return names


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


def _finalize_turn_summary(
    client: OpenAI,
    messages: list[dict[str, Any]],
) -> None:
    """轮数/token 预算耗尽时收尾：请求模型基于已有信息给出最终回答（对齐 Hermes
    handle_max_iterations 的"Requesting summary"行为）。

    不带工具再调一次模型（工具被禁用，防止无限递归）；失败时退回占位消息。
    内部指令消息带 `_finalize` 标记：落库时跳过、前端重放时跳过，避免被当成
    用户提问展示（Hermes 同样用 user 角色追加，但骨架补了持久化/展示隔离）。
    """
    messages.append(
        {
            "role": "user",
            "_finalize": True,
            "content": (
                "已经达到本轮执行上限（轮数或 token 预算）。"
                "请基于已有信息直接给出最终回答，不要再调用任何工具。"
            ),
        }
    )
    try:
        response = call_with_retry(
            lambda: client.chat.completions.create(model=MODEL, messages=messages),
            what="收尾调用",
            on_retry=_on_llm_retry,
        )
        reply = response.choices[0].message.content or ""
    except Exception as exc:
        reply = ""
        console.print(f"[dim]（收尾调用失败：{exc}）[/dim]")
    if reply:
        messages.append({"role": "assistant", "content": reply})
        console.print()
        console.print(Panel(reply, title="🤖 助手", border_style="green"))


def run_agent_turn(
    client: OpenAI,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    manager: MemoryManager | None = None,
    session_key: str = "",
    events: list[dict[str, Any]] | None = None,
    sink: Any = None,
    on_token: Any = None,
    interrupt_event: Any = None,
) -> None:
    """执行一轮对话：模型 + 工具循环，直到模型给出最终回答。

    对应 Hermes 的 run_conversation()——一次用户输入驱动整个 while 循环。
    最终回答也会写回 messages（多轮对话依赖它）。
    turn 级预算：MAX_AGENT_TURNS 限轮数、TURN_TOKEN_BUDGET 限累计 token（0 不限制），
    触顶后请求模型基于已有信息收尾（对齐 Hermes 的 max_iterations / iteration_budget）。
    interrupt_event 置位（如客户端断开）时停止本轮：跳过未执行的工具并回填
    cancelled 结果，再以"已中断"收尾（对齐 Hermes 的 agent.interrupt()）。
    """
    max_turns = max(1, int(MAX_AGENT_TURNS))
    token_budget = max(0, int(TURN_TOKEN_BUDGET))
    api_call_count = 0  # 累计模型调用次数（对齐 Hermes api_call_count）
    token_used = 0  # 累计 prompt token（真实值；流式仅在 chunk 带 usage 时计入）

    for turn in range(max_turns):
        # 中断检查：每轮模型调用前看是否被用户/客户端中断
        if interrupt_event is not None and interrupt_event.is_set():
            console.print("[yellow]（用户中断，本轮停止）[/yellow]")
            messages.append({"role": "assistant", "content": "（已中断，本轮停止）"})
            return
        # token 预算预检：触顶即收尾，不再调模型（对齐 Hermes 的 budget 收尾）
        if token_budget and token_used >= token_budget:
            console.print(
                f"[yellow]（token 预算已达上限 {token_budget}，请求模型收尾）[/yellow]"
            )
            _finalize_turn_summary(client, messages)
            return
        # 预检压缩：上下文超过阈值 → 中间轮次摘要化（对齐 Hermes 的 preflight compression）
        if should_compress(messages):
            # 对齐 Hermes commit_memory_session：压缩边界先把当前对话的记忆
            # 同步提取落库——原文马上要被摘要掉，先抢救信息
            if manager:
                manager.commit_memory_session(messages, client=client)
            # 对齐 Hermes：压缩时把未完成的任务清单随摘要一起保留（todo 重注入）
            todo_block = get_todo_store(session_key).format_for_injection() or ""
            compressed = compress_context(client, messages, todo_block=todo_block)
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
            # 方案 B（对齐 Codex 交互）：流式也先不把 token 推给气泡——中间轮的
            # 旁白属于"过程"，走 note 事件进活动托盘；只有最终回答才一次性交给气泡
            try:
                msg, prompt_tokens = call_llm_stream(client, messages, tools)
            except Exception as exc:
                _handle_model_call_failure(exc, messages)
                return
        else:
            try:
                msg, prompt_tokens = call_llm(client, messages, tools)
            except Exception as exc:
                _handle_model_call_failure(exc, messages)
                return
        api_call_count += 1
        token_used += prompt_tokens
        reasoning = getattr(msg, "reasoning_content", None) or ""
        if reasoning:
            think_event["result"] = redact_sensitive_text(
                str(reasoning)[:300], force=True
            )
            if sink is not None:
                sink(dict(think_event))

        content = msg.content or ""
        if msg.tool_calls:
            # 中间轮：旁白作为"过程说明"（note）事件进托盘，不展示成消息气泡
            if content.strip():
                _record_event(
                    events,
                    sink,
                    {
                        "type": "note",
                        "name": "过程说明",
                        "args": "",
                        "result": redact_sensitive_text(content[:1000], force=True),
                    },
                )
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
                return run_tool(
                    name, args, manager, session_key, client,
                    interrupt_event=interrupt_event,
                    available_tools={tool_name(t) for t in tools},
                )

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
                interrupt_event=interrupt_event,
            )

            # 按原始顺序回显工具返回（本段新增的 tool 消息正好一一对应）
            for tool_msg in messages[tool_start:]:
                console.print(f"  [green]📦 工具返回：[/green]{tool_msg['content']}")

            # todo 可视化：本轮动过任务清单就打印当前面板，方便人工跟踪进度
            if any(tool_name(tc) == "todo" for tc in tool_calls):
                todo_lines = render_todo_lines(get_todo_store(session_key))
                if todo_lines:
                    console.print(
                        Panel(
                            "\n".join(todo_lines),
                            title="📋 当前任务清单",
                            border_style="cyan",
                        )
                    )

            # 按原始顺序把工具结果回填进事件（结果截断 + 脱敏），并实时回调 sink
            for ev, tool_msg in zip(call_events, messages[tool_start:]):
                result = tool_msg.get("content", "") or ""
                ev["result"] = redact_sensitive_text(result[:300], force=True) or ""
                if sink is not None:
                    sink(dict(ev))

            # todo 可视化：模型动过任务清单就发一条 todo 事件（完整清单），
            # 网页据此渲染常驻任务清单卡片（与 REPL 的 📋 面板对应）
            if any(tool_name(tc) == "todo" for tc in tool_calls):
                _record_event(
                    events,
                    sink,
                    {
                        "type": "todo",
                        "name": "任务清单",
                        "args": "",
                        "result": json.dumps(
                            get_todo_store(session_key).read(), ensure_ascii=False
                        ),
                    },
                )

            continue  # 回到循环开头，把结果再发给大模型

        # 模型直接给了文字回答 → 流式下一次性把内容交给气泡，写回历史并输出
        if on_token is not None and content:
            on_token(content)
        messages.append({"role": "assistant", "content": content})
        console.print()
        console.print(Panel(content, title="🤖 助手", border_style="green"))
        return

    console.print(f"\n[yellow]（达到最大轮数 {max_turns}，请求模型收尾）[/yellow]")
    _finalize_turn_summary(client, messages)


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
    interrupt_event: Any = None,
) -> tuple[int, int, int]:
    """处理一条用户消息（REPL 与服务器共用）：召回 → 对话 → 落库 → 同步 → nudge。

    返回 (turn_count, turns_since_memory, persisted_count) 供调用方续接状态。
    """
    turn_count += 1
    turn_started = time.monotonic()
    # REPL 不传 events：内部收集一份，供本轮活动事件落库（前端重放还原托盘）
    if events is None:
        events = []

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
        interrupt_event=interrupt_event,
    )

    # 增量落库（对齐 Hermes：消息逐轮写入 SessionDB，中途退出也不丢）
    user_message_id = persist_messages(session_id, messages, start=persisted_count)
    persisted_count = len(messages)
    # 过程事件落库：挂在本轮用户消息 id 下，切换会话后可还原活动托盘
    duration_ms = int((time.monotonic() - turn_started) * 1000)
    persist_events(session_id, user_message_id, events, duration_ms)

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


class ReplState:
    """REPL 可变状态（斜杠命令 /resume 需要中途切换会话）。"""

    def __init__(
        self,
        *,
        session_id: str,
        client: OpenAI,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        memory_manager: MemoryManager | None,
        review_worker: SyncWorker,
        turn_count: int = 0,
        turns_since_memory: int = 0,
        persisted_count: int = 0,
    ) -> None:
        self.session_id = session_id
        self.client = client
        self.messages = messages
        self.tools = tools
        self.memory_manager = memory_manager
        self.review_worker = review_worker
        self.turn_count = turn_count
        self.turns_since_memory = turns_since_memory
        self.persisted_count = persisted_count


def _slash_help_text() -> str:
    """生成 /help 文本：列出 REPL 可用的斜杠命令。"""
    return (
        "可用命令：\n"
        "  /help            显示本帮助\n"
        "  /sessions        列出最近的会话（含 id，供 /resume 使用）\n"
        "  /resume <id>     切换到指定历史会话继续对话\n"
        "  /diff [模式|路径] 查看工作区改动（模式：working/staged/all，默认 working）\n"
        "  /exit            退出（或输入 退出 / exit / quit）"
    )


def _slash_sessions_text(limit: int = 10) -> str:
    """生成 /sessions 文本：最近会话列表（id + 标题 + 消息数）。"""
    rows = list_sessions(limit)
    if not rows:
        return "（暂无历史会话）"
    lines = ["最近会话："]
    for row in rows:
        title = (row.get("title") or row.get("preview") or "").strip() or "（无标题）"
        lines.append(
            f"  {row['session_id']}  {title}  [{row.get('message_count', 0)} 条]"
        )
    return "\n".join(lines)


def _slash_diff_text(arg: str = "") -> str:
    """生成 /diff 文本：stat 摘要 + 文件清单；指定路径时附完整 diff。"""
    mode = "working"
    paths: list[str] | None = None
    if arg:
        if arg in ("working", "staged", "all"):
            mode = arg
        else:
            paths = [arg]
    result = collect_working_diff(os.getcwd(), mode=mode, paths=paths)
    if not result.get("success"):
        return f"无法查看工作区改动：{result.get('error', '未知错误')}"

    # 指定路径：直接展示该文件 diff。注意路径过滤不折入未跟踪文件（Hermes 语义），
    # 所以先看过滤结果，没有就回退全量 working diff 按路径找（未跟踪文件也能看）
    if paths:
        files = parse_diff_files(result.get("diff", ""))
        if files:
            return files[0]["diff"]
        full = collect_working_diff(os.getcwd(), mode=mode)
        hit = [
            f
            for f in parse_diff_files(full.get("diff", ""))
            if f["path"] == paths[0]
        ]
        if hit:
            return hit[0]["diff"]
        return f"未找到路径 {paths[0]} 的改动（工作区 {mode} 模式）。"

    if result.get("empty"):
        return "工作区干净，没有改动。"
    files = parse_diff_files(result.get("diff", ""))
    summary = summarize_files(files)
    lines = [
        f"共 {summary['files']} 个文件 · 新增 +{summary['additions']} · "
        f"删除 -{summary['deletions']}（{mode}）"
    ]
    for f in files:
        label = {"added": "新增", "modified": "修改", "deleted": "删除"}.get(
            f["status"], f["status"]
        )
        lines.append(f"  {f['path']}  [{label} +{f['additions']}/-{f['deletions']}]")
    if paths and files:
        lines.append("")
        lines.append(files[0]["diff"])
    else:
        lines.append("（想看某个文件的完整 diff：/diff <路径>）")
    return "\n".join(lines)


def _slash_resume_text(arg: str, state: ReplState) -> str:
    """执行 /resume <id>：把 REPL 切到指定会话（对齐 Hermes 的 /resume）。"""
    target = arg.strip()
    if not target:
        return "用法：/resume <session_id>（先用 /sessions 查看可恢复的会话）"
    history = load_session_history(target)
    if not history:
        return f"未找到会话 {target} 或它没有消息。"
    system_prompt = load_session_prompt(target)
    if system_prompt is None:
        system_prompt = build_system_prompt(state.memory_manager)
        save_session_prompt(target, system_prompt)
    state.messages = [{"role": "system", "content": system_prompt}] + history
    hydrate_todo_store(state.messages, target)
    state.session_id = target
    state.turn_count = 0
    state.persisted_count = len(state.messages)
    state.turns_since_memory = hydrate_nudge_counter(
        sum(1 for m in state.messages if m.get("role") == "user"),
        MEMORY_NUDGE_INTERVAL,
    )
    return f"已切换到会话 {target}（{len(history)} 条历史消息）"


def run_slash_command(raw: str, state: ReplState) -> tuple[bool, str]:
    """处理 REPL 斜杠命令；返回 (是否已处理, 要展示的文本)。

    支持 /help /sessions /resume <id> /diff [模式|路径]；/exit 由主循环先拦截。
    未识别的命令返回 (False, "")，由调用方提示 /help。
    """
    parts = (raw or "").strip().split(maxsplit=1)
    cmd = parts[0].lower() if parts else ""
    arg = (parts[1] if len(parts) > 1 else "").strip()
    if cmd == "/help":
        return True, _slash_help_text()
    if cmd == "/sessions":
        return True, _slash_sessions_text()
    if cmd == "/diff":
        return True, _slash_diff_text(arg)
    if cmd == "/resume":
        return True, _slash_resume_text(arg, state)
    return False, ""


def _read_user_input() -> tuple[str, str]:
    """从终端读一行输入；返回 (输入内容, 状态)。

    状态为 "ok"（正常输入）/ "eof"（stdin 结束，如管道输入完毕）/
    "interrupt"（提示符处按了 Ctrl+C）。把 KeyboardInterrupt 在这里消化掉，
    避免裸 Traceback 打断 REPL——用户误按 Ctrl+C 时回到提示继续，而不是崩掉。
    """
    try:
        return console.input("\n[bold]你说（/help 查看命令）：[/bold] ").strip(), "ok"
    except EOFError:
        return "", "eof"
    except KeyboardInterrupt:
        return "", "interrupt"


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
            # 恢复会话时水合 todo 清单（对齐 Hermes _hydrate_todo_store）
            hydrate_todo_store(messages, session_id)
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

    # 启动展示：会话已有未完成任务清单（恢复会话水合后）就打印，方便人工接手
    todo_lines = render_todo_lines(get_todo_store(session_id))
    if todo_lines:
        console.print(
            Panel("\n".join(todo_lines), title="📋 当前任务清单", border_style="cyan")
        )
    if not one_shot_mode:
        console.print("[dim]提示：输入 /help 查看斜杠命令（/diff /sessions /resume /exit）[/dim]")

    turn_count = 0
    repl_state = ReplState(
        session_id=session_id,
        client=client,
        messages=messages,
        tools=tools,
        memory_manager=memory_manager,
        review_worker=review_worker,
        turn_count=turn_count,
        turns_since_memory=turns_since_memory,
        persisted_count=persisted_count,
    )
    while True:
        if one_shot is not None:
            user_input = one_shot
            one_shot = None  # 一次性参数用完后进入交互模式
        else:
            user_input, input_state = _read_user_input()
            if input_state == "eof":  # stdin 结束（如管道输入完毕）
                break
            if input_state == "interrupt":
                # 提示符处 Ctrl+C：取消本次输入，回到提示继续（不打印裸 Traceback）
                console.print("\n[yellow]（已取消输入，输入 /exit 退出）[/yellow]")
                continue
        if not user_input:
            continue
        if user_input in ("退出", "exit", "quit", "/exit"):
            break

        # 斜杠命令：/help /sessions /resume <id> /diff [模式|路径]（对齐 Hermes CLI）
        if user_input.startswith("/"):
            handled, slash_text = run_slash_command(user_input, repl_state)
            if handled:
                if slash_text:
                    console.print(slash_text)
                # /resume 切换会话后刷新任务清单面板
                todo_lines = render_todo_lines(get_todo_store(repl_state.session_id))
                if todo_lines:
                    console.print(
                        Panel(
                            "\n".join(todo_lines),
                            title="📋 当前任务清单",
                            border_style="cyan",
                        )
                    )
                if one_shot_mode:
                    break
                continue
            console.print("[yellow]未知命令，输入 /help 查看可用命令。[/yellow]")
            if one_shot_mode:
                break
            continue

        # REPL 中断接线：每轮一个全新 interrupt_event，Ctrl+C 打断本轮而不是杀进程
        # （对齐 Hermes：interrupt 置位后 run_agent_turn 在轮次边界停止；模型调用
        # 在途时 Ctrl+C 直接中止本轮，回到输入提示继续对话）
        turn_event = threading.Event()
        try:
            turn_count, turns_since_memory, persisted_count = process_turn(
                repl_state.client,
                repl_state.messages,
                repl_state.tools,
                repl_state.memory_manager,
                repl_state.session_id,
                user_input,
                repl_state.turn_count,
                repl_state.turns_since_memory,
                repl_state.review_worker,
                repl_state.persisted_count,
                interrupt_event=turn_event,
            )
            repl_state.turn_count = turn_count
            repl_state.turns_since_memory = turns_since_memory
            repl_state.persisted_count = persisted_count
        except KeyboardInterrupt:
            turn_event.set()
            console.print("\n[yellow]（已中断本轮，输入新问题继续对话）[/yellow]")
            # 补一条中断标记，保持消息历史连贯（对齐 run_agent_turn 的"已中断"收尾）
            if not repl_state.messages or repl_state.messages[-1].get("role") != "assistant":
                repl_state.messages.append(
                    {"role": "assistant", "content": "（已中断，本轮停止）"}
                )
            if one_shot_mode:
                break
            continue
        except Exception as exc:
            # 防御兜底：任何意外异常都不让 REPL 裸崩，提示后继续对话
            console.print(
                Panel(
                    f"[red]（本轮处理失败：{exc}）[/red]",
                    title="🤖 助手",
                    border_style="red",
                )
            )
            if one_shot_mode:
                break
            continue

        # LLM 生成会话标题（对齐 Hermes title_generator）：一次性模式同步生成后
        # 退出（进程即将结束，后台线程会被杀）；交互模式后台异步不阻塞对话
        last_msg = repl_state.messages[-1] if repl_state.messages else {}
        reply = last_msg.get("content", "") if last_msg.get("role") == "assistant" else ""
        if reply:
            if one_shot_mode:
                auto_title_session(
                    repl_state.session_id, user_input, reply, repl_state.client
                )
            else:
                maybe_auto_title(
                    repl_state.session_id,
                    user_input,
                    reply,
                    client=repl_state.client,
                    conversation_history=repl_state.messages,
                )

        if one_shot_mode:
            break  # 一次性问题已答完，退出

    # 会话结束：最后一次记忆审查（后台执行），排空后干净退出
    if repl_state.turn_count > 0:
        repl_state.review_worker.submit(
            lambda msgs=list(repl_state.messages), cli=repl_state.client: review_memory_turn(
                cli, msgs
            )
        )
    repl_state.review_worker.flush(timeout=10)
    repl_state.review_worker.shutdown()
    # 排空后台记忆同步（有界等待，不阻塞退出；同步卡住则放弃）
    if repl_state.memory_manager:
        repl_state.memory_manager.flush_pending(timeout=10)
        repl_state.memory_manager.shutdown()
    console.print(
        f"\n[dim]会话已保存。下次用 --resume {repl_state.session_id} 继续对话。[/dim]"
    )


if __name__ == "__main__":
    main()
