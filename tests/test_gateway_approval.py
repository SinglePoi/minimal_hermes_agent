# -*- coding: utf-8 -*-
"""
网关审批队列的回归测试（零依赖，直接运行）：
    python tests/test_gateway_approval.py

覆盖（对齐 Hermes 的 register_gateway_notify / resolve_gateway_approval 机制）：
    - 注册回调后危险命令走网关队列阻塞，resolve 唤醒并放行
    - deny -> 拦截；timeout -> 失败关闭（沉默不代表同意）
    - session 持久化、FIFO 顺序、unregister 唤醒为拒绝
"""

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

import approval  # noqa: E402
from approval import (  # noqa: E402
    check_dangerous_command,
    is_approved,
    list_pending_approvals,
    register_gateway_notify,
    resolve_gateway_approval,
    unregister_gateway_notify,
)


_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    """记录一条断言结果。"""
    if cond:
        print(f"  ok: {label}")
    else:
        _failures.append(label)
        print(f"  FAIL: {label}")


def run_blocked(session_key: str, result_box: dict, command: str = "git reset --hard HEAD~1"):
    """在后台线程里调用审批门卫（会阻塞在网关队列上）。"""
    def worker() -> None:
        result_box["result"] = check_dangerous_command(command, session_key, None)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread


def test_approve_unblocks() -> None:
    """注册回调 -> 阻塞 -> 轮询到 pending -> resolve 放行。"""
    register_gateway_notify("sess-g1", lambda data: None)
    box: dict = {}
    thread = run_blocked("sess-g1", box)
    time.sleep(0.3)
    pending = list_pending_approvals("sess-g1")
    check("pending 里有该命令",
          len(pending) == 1 and "git reset" in pending[0]["command"])
    count = resolve_gateway_approval("sess-g1", "once")
    check("resolve 返回 1", count == 1)
    thread.join(timeout=5)
    check("线程被唤醒且放行", box["result"]["approved"] is True)
    unregister_gateway_notify("sess-g1")


def test_deny_and_timeout() -> None:
    """deny -> 拦截；timeout -> 失败关闭。"""
    register_gateway_notify("sess-g2", lambda data: None)
    box: dict = {}
    thread = run_blocked("sess-g2", box)
    time.sleep(0.3)
    resolve_gateway_approval("sess-g2", "deny", reason="我不允许")
    thread.join(timeout=5)
    check("deny -> 拦截", box["result"]["approved"] is False)
    check("deny 消息带理由", "我不允许" in box["result"]["message"])
    unregister_gateway_notify("sess-g2")

    original_timeout = os.environ.get("APPROVAL_TIMEOUT")
    os.environ["APPROVAL_TIMEOUT"] = "1"
    register_gateway_notify("sess-g3", lambda data: None)
    box = {}
    thread = run_blocked("sess-g3", box)
    thread.join(timeout=6)
    check("超时 -> 失败关闭", box["result"]["approved"] is False)
    check("超时消息：沉默不代表同意", "Silence is not consent" in box["result"]["message"])
    unregister_gateway_notify("sess-g3")
    os.environ.pop("APPROVAL_TIMEOUT", None)
    if original_timeout is not None:
        os.environ["APPROVAL_TIMEOUT"] = original_timeout


def test_session_persistence_and_fifo() -> None:
    """session 选择持久化；FIFO 顺序解决多个挂起审批。"""
    register_gateway_notify("sess-g4", lambda data: None)
    box_a: dict = {}
    box_b: dict = {}
    thread_a = run_blocked("sess-g4", box_a)
    thread_b = run_blocked("sess-g4", box_b)
    time.sleep(0.3)
    check("两个挂起按 FIFO 排队", len(list_pending_approvals("sess-g4")) == 2)
    resolve_gateway_approval("sess-g4", "session")
    resolve_gateway_approval("sess-g4", "once")
    thread_a.join(timeout=5)
    thread_b.join(timeout=5)
    check("两个都放行", box_a["result"]["approved"] is True
          and box_b["result"]["approved"] is True)
    check("session 选择已持久化",
          is_approved("sess-g4", "git reset --hard (destroys uncommitted changes)"))
    unregister_gateway_notify("sess-g4")


def test_unregister_wakes_as_deny() -> None:
    """unregister 唤醒阻塞线程并按拒绝处理（防挂死）。"""
    register_gateway_notify("sess-g5", lambda data: None)
    box: dict = {}
    thread = run_blocked("sess-g5", box)
    time.sleep(0.3)
    unregister_gateway_notify("sess-g5")
    thread.join(timeout=5)
    check("unregister 后按拒绝处理", box["result"]["approved"] is False)


def main() -> None:
    """依次运行全部测试并汇总结果。"""
    print("== 网关审批队列回归测试 ==")
    for test_fn in (
        test_approve_unblocks,
        test_deny_and_timeout,
        test_session_persistence_and_fifo,
        test_unregister_wakes_as_deny,
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
