# -*- coding: utf-8 -*-
"""多代理/委派最小切片（对齐 Hermes tools/delegate_tool.py 的核心语义，简化版）。

只实现同步单层委派：父代理调用 delegate_task，子代理在独立上下文中运行一轮
Agent Loop 并返回最终答案。子代理工具集剔除 delegate_task / clarify / memory /
todo，避免递归委派、打扰用户、写共享记忆或污染父任务清单。

子代理结果先作为普通工具结果回传，再走骨架既有的 tool_result_storage 落盘，
因此不会二次撑爆父上下文。

超时用线程 + interrupt_event 实现：超时后置位中断信号，让子代理在当前模型/工具
边界尽快停下，再给 3 秒优雅退出窗口。
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any, Callable, Optional


DELEGATE_BLOCKED_TOOLS = frozenset({"delegate_task", "clarify", "memory", "todo"})
DEFAULT_TIMEOUT_SECONDS = 120

DELEGATE_TASK_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "delegate_task",
        "description": (
            "把一项独立子任务交给一个精简子代理处理，返回子代理的最终答案。"
            "适合可拆分的独立调查/检索/读写任务；goal 要自包含，因为子代理看不到"
            "当前对话历史。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "子代理要完成的目标，尽量具体、自包含。",
                },
                "context": {
                    "type": "string",
                    "description": "子代理需要的背景信息：文件路径、错误信息、约束等。",
                },
            },
            "required": ["goal"],
        },
    },
}


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    """读取正整数环境变量；非法值回退默认。"""
    try:
        return max(minimum, int(os.environ.get(name) or default))
    except (TypeError, ValueError):
        return default


def timeout_seconds() -> int:
    """返回子代理超时秒数（对齐 Hermes 的可配委派超时思路）。"""
    return _env_int("DELEGATE_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS, 1)


def _error(message: str, status: str = "error") -> str:
    """构造委派失败结果（JSON，含 success=false）。"""
    return json.dumps(
        {"success": False, "error": message, "status": status},
        ensure_ascii=False,
    )


def _filter_child_tools(tools: list[dict]) -> list[dict]:
    """剔除子代理不应持有的工具。"""
    blocked = DELEGATE_BLOCKED_TOOLS
    filtered: list[dict] = []
    for tool in tools:
        name = tool.get("function", {}).get("name")
        if name not in blocked:
            filtered.append(tool)
    return filtered


def _child_system_prompt(goal: str, context: str) -> str:
    """构造子代理的精简系统提示词。"""
    prompt = (
        "你是为完成一个具体委派任务而运行的精简子代理。\n"
        "只处理给出的任务，完成后直接给出最终答案；不要反问用户，"
        "不要写共享记忆，不要再委派给其他代理。\n"
        f"任务目标：{goal}\n"
    )
    if context:
        prompt += f"背景信息：\n{context}\n"
    return prompt


def _child_user_content(goal: str, context: str) -> str:
    """构造子代理的首条用户消息。"""
    if context:
        return f"{goal}\n\n背景信息：\n{context}"
    return goal


def delegate_task_tool(
    args: dict,
    *,
    client: Any,
    tools: list[dict],
    session_key: str,
    interrupt_event: Optional[threading.Event],
    run_agent_turn: Callable[..., None],
) -> str:
    """执行 delegate_task 工具：跑一个同步子代理并返回最终答案。"""
    goal = str(args.get("goal") or "").strip()
    context = str(args.get("context") or "").strip()
    if not goal:
        return _error("delegate_task 的 goal 不能为空")

    child_tools = _filter_child_tools(tools)
    child_messages: list[dict[str, Any]] = [
        {"role": "system", "content": _child_system_prompt(goal, context)},
        {"role": "user", "content": _child_user_content(goal, context)},
    ]

    child_interrupt = threading.Event()
    box: dict[str, str] = {}

    def _run_child() -> None:
        """在后台线程里跑子代理 Agent Loop。"""
        try:
            run_agent_turn(
                client,
                child_messages,
                child_tools,
                None,
                "",
                events=None,
                sink=None,
                on_token=None,
                interrupt_event=child_interrupt,
            )
            last = child_messages[-1] if child_messages else {}
            box["reply"] = (
                last.get("content", "")
                if last.get("role") == "assistant"
                else ""
            )
        except Exception as exc:
            box["error"] = str(exc)

    timeout = timeout_seconds()
    worker = threading.Thread(target=_run_child, daemon=True)
    worker.start()
    worker.join(timeout)

    if worker.is_alive():
        child_interrupt.set()
        worker.join(3)
        return _error(f"委派超时（>{timeout}s），已中断子代理", status="timeout")

    if "error" in box:
        return _error(box["error"])

    reply = (box.get("reply") or "").strip()
    return json.dumps({"success": True, "result": reply}, ensure_ascii=False)
