# -*- coding: utf-8 -*-
"""
记忆后台同步的回归测试（零依赖，直接运行）：
    python tests/test_memory_sync.py

覆盖（对齐 Hermes memory_manager sync_all 的"后台串行 + 不阻塞"设计）：
    - sync_all 异步：主流程立即返回，慢 provider 不阻塞
    - 单 worker 串行：多轮同步按提交顺序落库
    - 合并节流：worker 未开始的旧任务被最新任务覆盖（不丢数据）
    - flush 有界等待：同步卡死时超时返回，不挂死
    - shutdown 后回退内联执行；无 provider 时直接返回
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

from memory_manager import MemoryManager  # noqa: E402
from memory_provider import MemoryProvider  # noqa: E402


_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    """记录一条断言结果。"""
    if cond:
        print(f"  ok: {label}")
    else:
        _failures.append(label)
        print(f"  FAIL: {label}")


class StubProvider(MemoryProvider):
    """可编程的假 provider：记录 sync_turn 收到的 user_content。"""

    def __init__(self, name: str = "stub", hook=None):
        self._name = name
        self._hook = hook
        self.executed: list[str] = []

    @property
    def name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str = "", **kwargs) -> None:
        pass

    def get_tool_schemas(self) -> list[dict]:
        return []

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: list[dict] | None = None,
        client=None,
    ) -> None:
        self.executed.append(user_content)
        if self._hook is not None:
            self._hook(user_content)


def test_sync_all_is_async() -> None:
    """sync_all 立即返回；慢 provider 不阻塞主流程。"""
    slow = StubProvider("slow", hook=lambda _: time.sleep(0.5))
    manager = MemoryManager()
    manager.add_provider(slow)
    try:
        start = time.monotonic()
        manager.sync_all("问题1", "回答1")
        elapsed = time.monotonic() - start
        check("sync_all 立即返回（<0.4s）", elapsed < 0.4)
        check("返回时同步尚未完成", slow.executed == [])
        manager.flush_pending(timeout=5)
        check("flush 后同步完成", slow.executed == ["问题1"])
    finally:
        manager.shutdown()


def test_serialized_order() -> None:
    """多轮同步按提交顺序执行（单 worker 串行：B 等 A 完成后才跑）。"""
    started = threading.Event()
    release = threading.Event()

    def hook(user_content: str) -> None:
        if user_content == "问题A":
            started.set()
            release.wait(timeout=5)

    provider = StubProvider("ordered", hook=hook)
    manager = MemoryManager()
    manager.add_provider(provider)
    try:
        manager.sync_all("问题A", "回答A")  # 开始执行并阻塞
        check("A 已开始", started.wait(timeout=5))
        manager.sync_all("问题B", "回答B")  # B 排队等 A
        release.set()
        manager.flush_pending(timeout=5)
        check("串行顺序 A -> B（B 未覆盖 A）",
              provider.executed == ["问题A", "问题B"])
    finally:
        release.set()
        manager.shutdown()


def test_coalescing_throttle() -> None:
    """合并节流：worker 忙碌时连发多次，未开始的旧任务被最新覆盖。"""
    started = threading.Event()
    release = threading.Event()

    def hook(user_content: str) -> None:
        if user_content == "问题1":
            started.set()
            release.wait(timeout=5)

    provider = StubProvider("coalesce", hook=hook)
    manager = MemoryManager()
    manager.add_provider(provider)
    try:
        manager.sync_all("问题1", "回答1")  # 会真正执行并阻塞
        check("worker 已开始任务1", started.wait(timeout=5))
        manager.sync_all("问题2", "回答2")  # 合并进 pending
        manager.sync_all("问题3", "回答3")  # 覆盖 pending（最新胜出）
        release.set()
        manager.flush_pending(timeout=5)
        check("合并节流只执行 1 和 3（2 被覆盖）",
              provider.executed == ["问题1", "问题3"])
    finally:
        release.set()
        manager.shutdown()


def test_flush_timeout() -> None:
    """同步卡死时 flush 有界超时返回，不挂死。"""
    release = threading.Event()
    provider = StubProvider("wedge", hook=lambda _: release.wait(timeout=30))
    manager = MemoryManager()
    manager.add_provider(provider)
    try:
        manager.sync_all("卡住", "回答")
        time.sleep(0.1)  # 让 worker 开始执行
        start = time.monotonic()
        manager.flush_pending(timeout=0.2)
        elapsed = time.monotonic() - start
        check("flush 超时返回（<1s）", elapsed < 1.0)
    finally:
        release.set()
        manager.shutdown()


def test_shutdown_fallback_and_empty() -> None:
    """shutdown 后 sync_all 回退内联；无 provider 时直接返回。"""
    provider = StubProvider()
    manager = MemoryManager()
    manager.add_provider(provider)
    manager.shutdown()
    manager.sync_all("内联问题", "回答")  # worker 已关 → 内联执行
    check("shutdown 后回退内联执行", provider.executed == ["内联问题"])
    manager.shutdown()  # 幂等，不崩

    empty = MemoryManager()
    start = time.monotonic()
    empty.sync_all("无人接", "回答")
    check("无 provider 直接返回", time.monotonic() - start < 0.2)
    empty.shutdown()


def main() -> None:
    """依次运行全部测试并汇总结果。"""
    print("== 记忆后台同步回归测试 ==")
    for test_fn in (
        test_sync_all_is_async,
        test_serialized_order,
        test_coalescing_throttle,
        test_flush_timeout,
        test_shutdown_fallback_and_empty,
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
