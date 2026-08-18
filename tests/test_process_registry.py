# -*- coding: utf-8 -*-
"""后台进程注册表回归测试（对齐 Hermes tools/process_registry.py，简化版）。

覆盖：spawn 登记、poll 非阻塞查状态、wait 阻塞等结束与超时、kill 整树终止、
未知 session_id 报错、process 工具入口、run_terminal background=true 接线、
run_tool 分发、shutdown_all 退出清理。零依赖，python tests/test_process_registry.py 直接跑。
"""

import json
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import process_registry  # noqa: E402
from process_registry import (  # noqa: E402
    kill,
    poll,
    process_tool,
    shutdown_all,
    spawn,
    spawn_via_env,
    wait,
)
import minimal_agent  # noqa: E402

_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    """记录一条断言结果。"""
    if cond:
        print(f"  ok: {label}")
    else:
        _failures.append(label)
        print(f"  FAIL: {label}")


def _py_cmd(code: str) -> str:
    """拼一条用当前解释器跑的小命令（避免依赖 PATH 里的 python）。"""
    return f'"{sys.executable}" -c "{code}"'


def test_spawn_poll_wait() -> None:
    """spawn → poll（运行中）→ wait（结束）：输出完整、退出码正确。"""
    res = spawn(_py_cmd("import time; print('start'); time.sleep(1); print('done')"))
    check("spawn 成功", res.get("success") is True)
    sid = res.get("session_id")
    check("返回 session_id", bool(sid))
    check("返回 pid", isinstance(res.get("pid"), int) and res["pid"] > 0)
    try:
        time.sleep(0.3)
        p = poll(sid)
        check("poll 带状态", p.get("status") in ("running", "exited"))

        w = wait(sid, timeout=10)
        check("wait 结束", w.get("status") == "exited")
        check("wait 退出码 0", w.get("exit_code") == 0)
        out = w.get("output", "")
        check("输出含 start", "start" in out)
        check("输出含 done", "done" in out)
    finally:
        kill(sid)


def test_wait_timeout() -> None:
    """wait 超时：进程仍运行，返回部分状态 + 提示。"""
    res = spawn(_py_cmd("import time; time.sleep(30)"))
    sid = res["session_id"]
    try:
        w = wait(sid, timeout=1)
        check("wait 超时仍 running", w.get("status") == "running")
        check("wait 超时带提示", "超时" in w.get("message", ""))
    finally:
        kill(sid)


def test_kill() -> None:
    """kill：整棵树终止并标记 killed。"""
    res = spawn(_py_cmd("import time; print('hi'); time.sleep(30)"))
    sid = res["session_id"]
    try:
        time.sleep(0.3)
        k = kill(sid)
        check("kill 成功", k.get("success") is True and k.get("status") == "killed")
        p = poll(sid)
        check("kill 后状态 killed", p.get("status") == "killed")
    finally:
        kill(sid)


def test_has_running_owner() -> None:
    """has_running 只统计指定聊天会话的后台进程。"""
    res = spawn(_py_cmd("import time; time.sleep(30)"), owner_key="chat-a")
    sid = res["session_id"]
    try:
        check("本会话有运行中进程", process_registry.has_running("chat-a") is True)
        check("其他会话没有", process_registry.has_running("chat-b") is False)
        check("空 owner 不误报", process_registry.has_running("") is False)
    finally:
        kill(sid)


def test_unknown() -> None:
    """未知 session_id：poll/kill 返回可读错误。"""
    check("未知 poll 报错", poll("proc-nope").get("success") is False)
    check("未知 kill 报错", kill("proc-nope").get("success") is False)


def test_process_tool() -> None:
    """process 工具入口：wait 拿输出；缺 session_id 报错。"""
    res = spawn(_py_cmd("import time; print('tool'); time.sleep(1)"))
    sid = res["session_id"]
    try:
        data = json.loads(process_tool({"session_id": sid, "action": "wait", "timeout": 10}))
        check("process_tool wait 结束", data.get("status") == "exited")
        check("process_tool 输出", "tool" in data.get("output", ""))
        bad = json.loads(process_tool({"action": "kill"}))
        check("process_tool 缺 session_id 报错", bad.get("success") is False)
    finally:
        kill(sid)


def test_run_terminal_background() -> None:
    """run_terminal(background=true) → run_tool process：端到端接线。"""
    os.environ["TERMINAL_ENV"] = "local"
    raw = minimal_agent.run_terminal(
        _py_cmd("import time; print('bg'); time.sleep(0.5)"),
        session_key="bg-test",
        background=True,
    )
    data = json.loads(raw)
    check("background 返回 session_id", data.get("success") is True and bool(data.get("session_id")))
    sid = data["session_id"]
    try:
        raw2 = minimal_agent.run_tool(
            "process", {"session_id": sid, "action": "wait", "timeout": 10}
        )
        data2 = json.loads(raw2)
        check("process wait 结束", data2.get("status") == "exited")
        check("process 输出含 bg", "bg" in data2.get("output", ""))
    finally:
        kill(sid)


def test_spawn_via_env() -> None:
    """远程后端 spawn_via_env：线程执行 + wait 拿输出 + kill 调 cancel_fn。"""
    cancelled = {"n": 0}

    def execute_ok():
        time.sleep(0.15)
        return {"output": "hello-remote", "returncode": 0}

    def cancel_fn():
        cancelled["n"] += 1

    res = spawn_via_env("echo remote", execute_ok, cancel_fn)
    check("spawn_via_env 成功", res.get("success") is True)
    sid = res["session_id"]
    waited = wait(sid, timeout=5)
    check("wait 拿到远程输出", "hello-remote" in (waited.get("output") or ""))
    check("wait 后 exited", waited.get("status") == "exited")
    check("远程 pid 为 0", waited.get("pid") == 0)

    gate = threading.Event()

    def execute_slow():
        gate.wait(timeout=5)
        return {"output": "late", "returncode": 0}

    res2 = spawn_via_env("sleep", execute_slow, cancel_fn)
    sid2 = res2["session_id"]
    killed = kill(sid2)
    gate.set()
    check("kill 远程成功", killed.get("success") is True)
    check("kill 调用 cancel_fn", cancelled["n"] >= 1)


def test_shutdown_all() -> None:
    """shutdown_all：终止全部登记进程并清空注册表。"""
    res1 = spawn(_py_cmd("import time; time.sleep(30)"))
    res2 = spawn(_py_cmd("import time; time.sleep(30)"))
    killed = shutdown_all()
    check("shutdown_all 清理 2 个", killed == 2)
    check("注册表已清空", poll(res1["session_id"]).get("success") is False)
    check("第二个也不在", poll(res2["session_id"]).get("success") is False)


def main() -> None:
    """跑全部断言。"""
    test_spawn_poll_wait()
    test_wait_timeout()
    test_kill()
    test_has_running_owner()
    test_unknown()
    test_process_tool()
    test_run_terminal_background()
    test_spawn_via_env()
    test_shutdown_all()
    if _failures:
        print(f"\n{len(_failures)} 条断言失败：")
        for label in _failures:
            print(f"  - {label}")
        raise SystemExit(1)
    print("\n全部后台进程断言通过")


if __name__ == "__main__":
    main()
