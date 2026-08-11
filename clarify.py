# -*- coding: utf-8 -*-
"""中途问用户（clarify，对齐 Hermes tools/clarify_tool.py + tools/clarify_gateway.py）。

模型拿不准、需要用户拍板或收集反馈时调用 clarify 工具：
- REPL / 直接交互：终端提问（console.input），有选项时按编号选或直接输入；
- Web / 服务：走网关队列——入队挂起、前端轮询 /clarify/pending 弹窗、
  POST /clarify/resolve 按响"门铃"唤醒阻塞的 agent 线程（与审批队列同一套思路）；
- 超时（默认 300s）或会话注销时按"未回答"返回，绝不挂死线程。

支持三种形态（对齐 Hermes）：
1. 单选：choices 给最多 4 个选项，用户点一个或选"其他"自由输入；
2. 多选：multi_select=true，user_response 变成数组；
3. 开放式：不给 choices，用户自由输入。
"""

import json
import re
import threading
import time
from typing import Any, Optional

from rich.console import Console
from rich.panel import Panel

console = Console()

MAX_CHOICES = 4
DEFAULT_TIMEOUT = 300  # 网关模式等用户回答的超时（秒）

# =========================================================================
# 网关澄清队列（对齐 approval.py 的"门铃"机制）
# =========================================================================


class _ClarifyEntry:
    """一条挂起的澄清请求：event 是门铃，resolve 时按响唤醒阻塞的 agent 线程。"""

    def __init__(
        self,
        clarify_id: str,
        session_key: str,
        question: str,
        choices: Optional[list[str]],
        multi_select: bool,
    ) -> None:
        self.clarify_id = clarify_id
        self.session_key = session_key
        self.question = question
        self.choices = choices
        self.multi_select = multi_select
        self.event = threading.Event()
        self.answer: Optional[str] = None
        self.cancelled = False


_clarify_queues: dict[str, list[_ClarifyEntry]] = {}
_clarify_notify_cbs: dict[str, Any] = {}
_lock = threading.RLock()
_seq = 0


def register_clarify_notify(session_key: str, cb) -> None:
    """注册会话的通知回调（对齐审批：cb 只负责"发出消息"，这里是轮询可读）。"""
    with _lock:
        _clarify_notify_cbs[session_key] = cb


def unregister_clarify_notify(session_key: str) -> None:
    """注销回调并唤醒该会话所有阻塞线程（按"未回答/已取消"处理，防挂死）。"""
    with _lock:
        _clarify_notify_cbs.pop(session_key, None)
        entries = _clarify_queues.pop(session_key, [])
    for entry in entries:
        entry.cancelled = True
        entry.event.set()


def get_clarify_notify(session_key: str):
    """返回会话的通知回调；未注册返回 None（此时走 REPL 直接交互）。"""
    with _lock:
        return _clarify_notify_cbs.get(session_key)


def list_pending_clarify(session_key: str) -> list[dict[str, Any]]:
    """列出会话当前挂起的澄清请求（供前端轮询弹窗）。"""
    with _lock:
        queue = _clarify_queues.get(session_key, [])
        return [
            {
                "clarify_id": entry.clarify_id,
                "question": entry.question,
                "choices": list(entry.choices) if entry.choices else None,
                "multi_select": entry.multi_select,
            }
            for entry in queue
        ]


def resolve_clarify(session_key: str, clarify_id: str, answer: str) -> int:
    """按响门铃：用用户回答解决指定（或最旧一条）挂起澄清。"""
    with _lock:
        queue = _clarify_queues.get(session_key, [])
        target = next(
            (e for e in queue if e.clarify_id == clarify_id),
            queue[0] if queue else None,
        )
        if target is None:
            return 0
        queue.remove(target)
        if not queue:
            _clarify_queues.pop(session_key, None)
    target.answer = answer
    target.event.set()
    return 1


def _await_clarify_answer(
    session_key: str,
    notify_cb,
    entry: _ClarifyEntry,
    timeout: int,
) -> dict[str, Any]:
    """入队 + 通知 + 阻塞等待用户 resolve（对齐 Hermes clarify_gateway）。"""
    with _lock:
        _clarify_queues.setdefault(session_key, []).append(entry)

    def _drop() -> None:
        with _lock:
            queue = _clarify_queues.get(session_key, [])
            if entry in queue:
                queue.remove(entry)
            if not queue:
                _clarify_queues.pop(session_key, None)

    try:
        notify_cb(entry)
    except Exception:
        _drop()
        return {"resolved": False, "answer": None, "cancelled": True}

    resolved = entry.event.wait(timeout=max(int(timeout), 0))
    _drop()
    if not resolved:
        return {"resolved": False, "answer": None, "cancelled": False}
    if entry.cancelled:
        return {"resolved": False, "answer": None, "cancelled": True}
    return {"resolved": True, "answer": entry.answer, "cancelled": False}


# =========================================================================
# 选项清洗与回答解析（对齐 Hermes clarify_tool）
# =========================================================================


def _flatten_choice(c) -> str:
    """把选项归一成用户可见文本：dict 取 label/description/text/title，垃圾丢弃。"""
    if c is None:
        return ""
    if isinstance(c, str):
        return c.strip()
    if isinstance(c, dict):
        for key in ("label", "description", "text", "title"):
            v = c.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""
    if isinstance(c, (list, tuple)):
        return " ".join(_flatten_choice(x) for x in c).strip()
    return str(c).strip()


def _parse_multi_select_response(raw_response) -> list[str]:
    """多选回答解析：数组 / JSON 数组 / 逗号分隔三种形态。"""
    if isinstance(raw_response, list):
        return [str(r).strip() for r in raw_response if str(r).strip()]
    raw = str(raw_response).strip()
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(p).strip() for p in parsed if str(p).strip()]
        except json.JSONDecodeError:
            pass
    return [s.strip() for s in re.split(r"[,，;；\s]+", raw) if s.strip()]


def _ask_repl(
    question: str,
    choices: Optional[list[str]],
    multi_select: bool,
) -> Optional[str]:
    """REPL 交互提问：返回用户原始回答；EOF 返回 None。"""
    if choices:
        console.print(
            Panel(f"[bold]{question}[/bold]", title="❓ 向你确认", border_style="yellow")
        )
        for i, option in enumerate(choices, 1):
            console.print(f"  [cyan]{i}.[/cyan] {option}")
        if multi_select:
            console.print("  （可多选：输入多个编号，逗号分隔，如 1,3）")
        else:
            console.print("  [dim]0. 其他（自己输入）[/dim]")
        answer = console.input("\n[bold]你的回答（编号或直接输入）：[/bold] ").strip()
        if multi_select:
            picked = [
                choices[i - 1]
                for i in _parse_numbers(answer)
                if 1 <= i <= len(choices)
            ]
            if picked:
                return json.dumps(picked, ensure_ascii=False)
            return answer
        try:
            n = int(answer)
        except ValueError:
            return answer
        if n == 0:
            return console.input("[bold]你的答案：[/bold] ").strip()
        if 1 <= n <= len(choices):
            return choices[n - 1]
        return answer
    return console.input(f"\n[bold]❓ {question}[/bold] ").strip()


def _parse_numbers(text: str) -> list[int]:
    """从文本里解析编号（支持中文/英文逗号、分号、空格分隔）。"""
    nums = []
    for part in re.split(r"[,，;；\s]+", (text or "").strip()):
        try:
            nums.append(int(part))
        except ValueError:
            continue
    return nums


# =========================================================================
# 工具入口
# =========================================================================


def clarify_tool(
    question: str,
    choices: Optional[list] = None,
    multi_select: bool = False,
    session_key: str = "",
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    """向用户提一个问题（可带最多 4 个选项 / 多选），返回 JSON 回答。"""
    if not question or not str(question).strip():
        return json.dumps({"success": False, "error": "question 不能为空"}, ensure_ascii=False)
    question = str(question).strip()

    if choices is not None:
        if not isinstance(choices, list):
            return json.dumps(
                {"success": False, "error": "choices 必须是字符串数组"}, ensure_ascii=False
            )
        choices = [c for c in (_flatten_choice(x) for x in choices) if c]
        choices = choices[:MAX_CHOICES] or None

    notify = get_clarify_notify(session_key)
    if notify is not None:
        # 网关模式（Web）：入队 + 阻塞等前端 resolve
        with _lock:
            global _seq
            _seq += 1
            clarify_id = f"clarify-{int(time.time() * 1000)}-{_seq}"
        entry = _ClarifyEntry(
            clarify_id, session_key, question, choices, bool(multi_select)
        )
        result = _await_clarify_answer(session_key, notify, entry, timeout)
        if not result.get("resolved") or result.get("cancelled"):
            return json.dumps(
                {
                    "success": False,
                    "error": "未收到用户回答（超时或会话结束）",
                },
                ensure_ascii=False,
            )
        raw = result.get("answer", "")
    else:
        # REPL / 直接交互
        try:
            raw = _ask_repl(question, choices, bool(multi_select))
        except EOFError:
            return json.dumps(
                {"success": False, "error": "无法在非交互模式提问"}, ensure_ascii=False
            )
        if raw is None or not str(raw).strip():
            return json.dumps({"success": False, "error": "用户未回答"}, ensure_ascii=False)

    if multi_select and choices:
        user_response = _parse_multi_select_response(raw)
    else:
        user_response = str(raw).strip()
    return json.dumps(
        {
            "question": question,
            "choices_offered": choices,
            "user_response": user_response,
        },
        ensure_ascii=False,
    )
