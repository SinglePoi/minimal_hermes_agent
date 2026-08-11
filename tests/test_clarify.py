# -*- coding: utf-8 -*-
"""中途问用户（clarify）回归测试（对齐 Hermes tools/clarify_tool.py + clarify_gateway.py）。

覆盖：选项清洗与上限、参数校验、REPL 交互（单选/多选/开放式/EOF）、
网关队列（pending/resolve/注销唤醒/超时）、run_tool 分发与 TOOLS 注册、
并行白名单（clarify 不并行）。零依赖，python tests/test_clarify.py 直接跑。
"""

import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import clarify  # noqa: E402
from clarify import (  # noqa: E402
    _flatten_choice,
    clarify_tool,
    list_pending_clarify,
    register_clarify_notify,
    resolve_clarify,
    unregister_clarify_notify,
)
import minimal_agent  # noqa: E402
import tool_dispatch  # noqa: E402

_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    """记录一条断言结果。"""
    if cond:
        print(f"  ok: {label}")
    else:
        _failures.append(label)
        print(f"  FAIL: {label}")


def _repl_input(answers):
    """把 clarify 的 console.input 替换成预设答案队列（支持多轮输入）。"""
    original = clarify.console.input
    queue = list(answers)

    def fake(*_a, **_k):
        if not queue:
            raise EOFError
        return queue.pop(0)

    clarify.console.input = fake
    return original


def test_flatten_and_validate() -> None:
    """选项归一 + 校验：dict 解包、上限 4、空选项转开放式、参数错误。"""
    check("字符串选项原样", _flatten_choice(" 北京 ") == "北京")
    check("dict 取 label", _flatten_choice({"label": "方案 A"}) == "方案 A")
    check("dict 取 description", _flatten_choice({"description": "描述"}) == "描述")
    check("垃圾 dict 丢弃", _flatten_choice({"name": "x"}) == "")

    err = json.loads(clarify_tool("  "))
    check("空问题报错", err.get("success") is False)
    err = json.loads(clarify_tool("q", choices="not-list"))
    check("choices 非数组报错", err.get("success") is False)


def test_repl_single_choice() -> None:
    """REPL 单选：编号选择 / 0 走其他 / 自由输入。"""
    original = _repl_input(["2"])
    try:
        data = json.loads(
            clarify_tool("去哪部署？", choices=["staging", "prod", "dev"])
        )
        check("按编号选第 2 项", data["user_response"] == "prod")
        check("choices_offered 保留", data["choices_offered"] == ["staging", "prod", "dev"])
    finally:
        clarify.console.input = original

    original = _repl_input(["0", "自己写个答案"])
    try:
        data = json.loads(
            clarify_tool("去哪部署？", choices=["staging", "prod"])
        )
        check("0 走其他自由输入", data["user_response"] == "自己写个答案")
    finally:
        clarify.console.input = original

    original = _repl_input(["随便"])
    try:
        data = json.loads(clarify_tool("开放式问题", choices=[]))
        check("空 choices 转开放式", data["user_response"] == "随便" and data["choices_offered"] is None)
    finally:
        clarify.console.input = original


def test_repl_multi_and_eof() -> None:
    """REPL 多选（编号 → 数组）与 EOF（非交互）报错。"""
    original = _repl_input(["1,3"])
    try:
        data = json.loads(
            clarify_tool(
                "选哪些？",
                choices=["A", "B", "C"],
                multi_select=True,
            )
        )
        check("多选返回数组", data["user_response"] == ["A", "C"])
    finally:
        clarify.console.input = original

    original = _repl_input([])
    try:
        data = json.loads(clarify_tool("非交互提问"))
        check("EOF 报错", data.get("success") is False and "非交互" in data.get("error", ""))
    finally:
        clarify.console.input = original


def test_gateway_queue() -> None:
    """网关队列：入队挂起 → pending 可见 → resolve 唤醒返回回答；注销唤醒为取消。"""
    register_clarify_notify("sess-c", lambda entry: None)
    box: dict = {}

    def ask():
        box["raw"] = clarify_tool(
            "目标环境？", choices=["staging", "prod"], session_key="sess-c", timeout=10
        )

    thread = threading.Thread(target=ask, daemon=True)
    thread.start()
    time.sleep(0.2)
    pending = list_pending_clarify("sess-c")
    check("pending 可见", len(pending) == 1 and "目标环境？" in pending[0]["question"])
    check("pending 带选项", pending[0]["choices"] == ["staging", "prod"])

    resolved = resolve_clarify("sess-c", pending[0]["clarify_id"], "prod")
    check("resolve 成功", resolved == 1)
    thread.join(timeout=5)
    data = json.loads(box.get("raw", "{}"))
    check("阻塞线程拿到回答", data.get("user_response") == "prod")

    # 注销：阻塞线程按"未回答"返回
    box2: dict = {}

    def ask2():
        box2["raw"] = clarify_tool(
            "还没回答的问题", session_key="sess-c", timeout=10
        )

    thread2 = threading.Thread(target=ask2, daemon=True)
    thread2.start()
    time.sleep(0.2)
    unregister_clarify_notify("sess-c")
    thread2.join(timeout=5)
    data2 = json.loads(box2.get("raw", "{}"))
    check("注销唤醒为未回答", data2.get("success") is False)


def test_registration_and_dispatch() -> None:
    """TOOLS 注册 / 并行白名单 / run_tool 分发（REPL 路径）。"""
    names = [t["function"]["name"] for t in minimal_agent.TOOLS]
    check("TOOLS 注册 clarify", "clarify" in names)
    check("clarify 不并行", "clarify" in tool_dispatch._NEVER_PARALLEL_TOOLS)

    original = _repl_input(["可以"])
    try:
        raw = minimal_agent.run_tool("clarify", {"question": "继续吗？", "choices": ["可以", "算了"]})
        data = json.loads(raw)
        check("run_tool 分发返回回答", data.get("user_response") == "可以")
    finally:
        clarify.console.input = original


def main() -> None:
    """跑全部断言。"""
    test_flatten_and_validate()
    test_repl_single_choice()
    test_repl_multi_and_eof()
    test_gateway_queue()
    test_registration_and_dispatch()
    if _failures:
        print(f"\n{len(_failures)} 条断言失败：")
        for label in _failures:
            print(f"  - {label}")
        raise SystemExit(1)
    print("\n全部 clarify 断言通过")


if __name__ == "__main__":
    main()
