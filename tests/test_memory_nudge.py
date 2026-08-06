# -*- coding: utf-8 -*-
"""
记忆 nudge 的回归测试（零依赖，直接运行）：
    python tests/test_memory_nudge.py

覆盖（对齐 Hermes memory.nudge_interval 的计数语义 + 后台 review）：
    - should_run_memory_nudge：计数达间隔触发并清零；间隔 <=0 禁用
    - hydrate_nudge_counter：恢复会话时按历史轮次 % 间隔对齐
    - SyncWorker 后台执行：提交立即返回、flush 后排空、串行执行
"""

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

import minimal_agent  # noqa: E402
from memory_manager import SyncWorker  # noqa: E402


_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    """记录一条断言结果。"""
    if cond:
        print(f"  ok: {label}")
    else:
        _failures.append(label)
        print(f"  FAIL: {label}")


def test_nudge_trigger() -> None:
    """间隔计数：达到间隔触发并清零；间隔 <=0 禁用。"""
    triggered, counter = minimal_agent.should_run_memory_nudge(9, 10)
    check("计数 9/10 -> 触发且清零", triggered is True and counter == 0)

    triggered, counter = minimal_agent.should_run_memory_nudge(5, 10)
    check("计数 5/10 -> 不触发且 +1", triggered is False and counter == 6)

    triggered, counter = minimal_agent.should_run_memory_nudge(0, 10)
    check("计数 0/10 -> 不触发且 +1", triggered is False and counter == 1)

    triggered, counter = minimal_agent.should_run_memory_nudge(99, 0)
    check("间隔 0 -> 禁用且计数不变", triggered is False and counter == 99)


def test_hydrate_counter() -> None:
    """恢复会话时按历史轮次对齐计数（跨会话连续）。"""
    check("25 轮 % 10 = 5", minimal_agent.hydrate_nudge_counter(25, 10) == 5)
    check("恰好 10 轮 -> 0", minimal_agent.hydrate_nudge_counter(10, 10) == 0)
    check("间隔 0 -> 0", minimal_agent.hydrate_nudge_counter(25, 0) == 0)
    check("无历史 -> 0", minimal_agent.hydrate_nudge_counter(0, 10) == 0)


def test_review_worker_background() -> None:
    """SyncWorker：提交立即返回；慢任务不阻塞；flush 后排空；串行。"""
    worker = SyncWorker()
    executed: list[str] = []
    started = threading.Event()
    release = threading.Event()

    def slow_review(name: str) -> None:
        executed.append(name)
        if name == "A":
            started.set()
            release.wait(timeout=5)

    start = time.monotonic()
    worker.submit(lambda: slow_review("A"))
    check("提交立即返回（<0.3s）", time.monotonic() - start < 0.3)
    check("任务 A 已开始", started.wait(timeout=5))
    worker.submit(lambda: slow_review("B"))
    release.set()
    worker.flush(timeout=5)
    check("flush 后排空且串行 A->B", executed == ["A", "B"])
    worker.shutdown()


def main() -> None:
    """依次运行全部测试并汇总结果。"""
    print("== 记忆 nudge 回归测试 ==")
    for test_fn in (
        test_nudge_trigger,
        test_hydrate_counter,
        test_review_worker_background,
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
