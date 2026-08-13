# -*- coding: utf-8 -*-
"""
多代理/委派最小切片的回归测试（零依赖，直接运行）：
    python tests/test_delegate_tool.py

覆盖（对齐 Hermes tools/delegate_tool.py 的核心语义，简化版）：
    - 子代理工具过滤：剔除 delegate_task / clarify / memory / todo
    - delegate_task_tool：正常返回子代理最终答案、goal 为空报错
    - 超时：超时后中断子代理并返回 timeout
    - run_tool 分发：delegate_task 接入 tool 分发
"""

import json
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import delegate_tool  # noqa: E402
import minimal_agent  # noqa: E402


_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    """记录一条断言结果。"""
    if cond:
        print(f"  ok: {label}")
    else:
        _failures.append(label)
        print(f"  FAIL: {label}")


def make_tool(name: str) -> dict:
    """构造一个最小工具 schema。"""
    return {"type": "function", "function": {"name": name}}


def test_filter_child_tools() -> None:
    """子代理工具过滤：剔除有副作用/交互/递归的工具。"""
    tools = [make_tool(name) for name in (
        "read_file", "search_files", "terminal", "web_search",
        "delegate_task", "clarify", "memory", "todo",
    )]
    names = [t["function"]["name"] for t in delegate_tool._filter_child_tools(tools)]
    check("保留只读工具", "read_file" in names and "web_search" in names)
    check("剔除 delegate_task", "delegate_task" not in names)
    check("剔除 clarify", "clarify" not in names)
    check("剔除 memory", "memory" not in names)
    check("剔除 todo", "todo" not in names)


def fake_run_agent_turn(client, messages, tools, manager, session_key, **kwargs):
    """模拟子代理：直接写入一条 assistant 最终答案。"""
    messages.append({"role": "assistant", "content": "子代理答案"})


def test_delegate_task_success() -> None:
    """delegate_task_tool 正常返回子代理最终答案。"""
    tools = [make_tool("read_file"), make_tool("delegate_task")]
    out = delegate_tool.delegate_task_tool(
        {"goal": "总结一个文件", "context": "路径是 a.txt"},
        client=None,
        tools=tools,
        session_key="parent",
        interrupt_event=None,
        run_agent_turn=fake_run_agent_turn,
    )
    data = json.loads(out)
    check("返回 success", data.get("success") is True)
    check("返回子代理答案", data.get("result") == "子代理答案")


def test_delegate_task_missing_goal() -> None:
    """delegate_task_tool goal 为空返回错误。"""
    out = delegate_tool.delegate_task_tool(
        {"goal": "   "},
        client=None,
        tools=[],
        session_key="",
        interrupt_event=None,
        run_agent_turn=fake_run_agent_turn,
    )
    data = json.loads(out)
    check("goal 为空返回失败", data.get("success") is False)
    check("错误信息含 goal", "goal" in data.get("error", ""))


def test_delegate_task_timeout() -> None:
    """delegate_task_tool 超时后中断子代理并返回 timeout。"""
    def blocking_run(client, messages, tools, manager, session_key, **kwargs):
        interrupt_event = kwargs.get("interrupt_event")
        if interrupt_event is not None:
            interrupt_event.wait(5)
        messages.append({"role": "assistant", "content": "不应等到"})

    saved = os.environ.get("DELEGATE_TIMEOUT_SECONDS")
    os.environ["DELEGATE_TIMEOUT_SECONDS"] = "1"
    try:
        start = time.monotonic()
        out = delegate_tool.delegate_task_tool(
            {"goal": "慢任务"},
            client=None,
            tools=[],
            session_key="",
            interrupt_event=None,
            run_agent_turn=blocking_run,
        )
        elapsed = time.monotonic() - start
        data = json.loads(out)
        check("超时返回失败", data.get("success") is False)
        check("状态为 timeout", data.get("status") == "timeout")
        check("耗时被截断", elapsed < 4)
    finally:
        if saved is None:
            os.environ.pop("DELEGATE_TIMEOUT_SECONDS", None)
        else:
            os.environ["DELEGATE_TIMEOUT_SECONDS"] = saved


def test_run_tool_dispatch() -> None:
    """minimal_agent.run_tool 分发 delegate_task。"""
    original_run_agent_turn = minimal_agent.run_agent_turn
    minimal_agent.run_agent_turn = fake_run_agent_turn
    try:
        out = minimal_agent.run_tool(
            "delegate_task",
            {"goal": "验证分发"},
            tools=[make_tool("read_file"), make_tool("delegate_task")],
        )
        data = json.loads(out)
        check("run_tool 分发成功", data.get("success") is True)
        check("run_tool 返回子代理答案", data.get("result") == "子代理答案")
    finally:
        minimal_agent.run_agent_turn = original_run_agent_turn


def test_run_tool_emits_delegate_events() -> None:
    """run_tool 分发 delegate_task 时，向事件列表写入 started/completed 委派事件。"""
    original_run_agent_turn = minimal_agent.run_agent_turn
    minimal_agent.run_agent_turn = fake_run_agent_turn
    events: list[dict] = []
    try:
        minimal_agent.run_tool(
            "delegate_task",
            {"goal": "验证委派事件", "context": "无"},
            tools=[make_tool("read_file"), make_tool("delegate_task")],
            events=events,
        )
        delegate_events = [e for e in events if e.get("type") == "delegate"]
        check("产生两条委派事件", len(delegate_events) == 2)
        check("两条事件归属同一 delegation_id",
              delegate_events[0]["delegation_id"] == delegate_events[1]["delegation_id"])
        check("结束事件为 completed",
              json.loads(delegate_events[1]["result"]).get("status") == "completed")
    finally:
        minimal_agent.run_agent_turn = original_run_agent_turn


def main() -> None:
    """依次运行全部测试并汇总结果。"""
    print("== 多代理/委派最小切片回归测试 ==")
    for test_fn in (
        test_filter_child_tools,
        test_delegate_task_success,
        test_delegate_task_missing_goal,
        test_delegate_task_timeout,
        test_run_tool_dispatch,
        test_run_tool_emits_delegate_events,
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
