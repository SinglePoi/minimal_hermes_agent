# -*- coding: utf-8 -*-
"""
工具并行执行模块的回归测试（零依赖，直接运行）：
    python tests/test_tool_dispatch.py

覆盖（对齐 Hermes agent/tool_dispatch_helpers.py + tool_executor.py）：
    - 批分段规划：parallel 段 / sequential 屏障 / 单元素降级 / 相邻顺序段合并
    - _should_parallelize_tool_batch 的整批可并行语义
    - 路径作用域重叠检测（读者↔读者可并行；含写者的重叠关闭并行段）
    - 分段执行器：并发真实发生、结果按原始顺序回填、段提示回调
"""

import json
import re
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import tool_dispatch  # noqa: E402
import minimal_agent  # noqa: E402
from tool_dispatch import (  # noqa: E402
    _PARALLEL_SAFE_TOOLS,
    _plan_tool_batch_segments,
    _should_parallelize_tool_batch,
    execute_tool_calls_segmented,
    tool_call_id,
)


_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    """记录一条断言结果。"""
    if cond:
        print(f"  ok: {label}")
    else:
        _failures.append(label)
        print(f"  FAIL: {label}")


def make_call(call_id: str, name: str, args: dict | None = None) -> SimpleNamespace:
    """构造一个模拟 openai SDK 形态的工具调用对象。"""
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(
            name=name,
            arguments=json.dumps(args or {}, ensure_ascii=False),
        ),
    )


def kinds(segments) -> list[tuple[str, int]]:
    """把分段计划压缩成 [(kind, 调用数), ...] 便于断言。"""
    return [(kind, len(calls)) for kind, calls in segments]


def test_planner_core_tools() -> None:
    """核心工具的分段：只读工具并行、memory/terminal 为顺序屏障。"""
    # 单调用 → 顺序
    segs = _plan_tool_batch_segments([make_call("t1", "web_search", {"query": "测试"})])
    check("单调用 -> 顺序段", kinds(segs) == [("sequential", 1)])

    # 两个只读工具 → 单一并行段
    segs = _plan_tool_batch_segments([
        make_call("t1", "web_search", {"query": "测试"}),
        make_call("t2", "session_search", {"query": "评审会"}),
    ])
    check("只读 x2 -> 并行段", kinds(segs) == [("parallel", 2)])
    check("整批可并行 -> True", _should_parallelize_tool_batch([
        make_call("t1", "web_search", {"query": "测试"}),
        make_call("t2", "session_search", {"query": "评审会"}),
    ]))

    # memory 是顺序屏障：与只读工具混批后整批降级为顺序
    segs = _plan_tool_batch_segments([
        make_call("t1", "web_search", {"query": "测试"}),
        make_call("t2", "memory", {"action": "add", "target": "user", "content": "x"}),
    ])
    check("weather + memory -> 全顺序", kinds(segs) == [("sequential", 2)])
    check("weather + memory -> 不可并行",
          not _should_parallelize_tool_batch([
              make_call("t1", "web_search", {"query": "测试"}),
              make_call("t2", "memory", {"action": "add", "target": "user", "content": "x"}),
          ]))

    # 三工具：前两个只读并行，terminal 屏障后 memory_search 因单元素降级并入顺序段
    segs = _plan_tool_batch_segments([
        make_call("t1", "web_search", {"query": "测试"}),
        make_call("t2", "session_search", {"query": "评审会"}),
        make_call("t3", "terminal", {"command": "dir"}),
        make_call("t4", "memory_search", {"query": "骨架"}),
    ])
    check("混合批 -> 并行段 + 顺序段",
          kinds(segs) == [("parallel", 2), ("sequential", 2)])

    # 参数解析失败 → 顺序屏障
    broken = SimpleNamespace(
        id="t9",
        function=SimpleNamespace(name="web_search", arguments="{not-json"),
    )
    segs = _plan_tool_batch_segments([
        make_call("t1", "web_search", {"query": "测试"}),
        broken,
    ])
    check("参数解析失败 -> 全顺序", kinds(segs) == [("sequential", 2)])


def test_planner_path_scope() -> None:
    """路径作用域：读者↔读者可并行；含写者的重叠关闭并行段（注入假文件工具验证）。"""
    original_readers = tool_dispatch._PATH_SCOPED_READERS
    original_writers = tool_dispatch._PATH_SCOPED_WRITERS
    original_tools = tool_dispatch._PATH_SCOPED_TOOLS
    try:
        tool_dispatch._PATH_SCOPED_READERS = frozenset({"read_file"})
        tool_dispatch._PATH_SCOPED_WRITERS = frozenset({"write_file"})
        tool_dispatch._PATH_SCOPED_TOOLS = frozenset({"read_file", "write_file"})

        def read(path):
            return make_call(f"r-{path}", "read_file", {"path": path})

        def write(path):
            return make_call(f"w-{path}", "write_file", {"path": path})

        # 读者↔读者同一路径 → 并行（并发读可交换）
        segs = _plan_tool_batch_segments([read("src/a.py"), read("src/a.py")])
        check("读者+读者同路径 -> 并行", kinds(segs) == [("parallel", 2)])

        # 写者+写者不同路径 → 并行
        segs = _plan_tool_batch_segments([write("src/a.py"), write("src/b.py")])
        check("写者+写者不同路径 -> 并行", kinds(segs) == [("parallel", 2)])

        # 写者+写者同路径 → 冲突 → 顺序
        segs = _plan_tool_batch_segments([write("src/a.py"), write("src/a.py")])
        check("写者+写者同路径 -> 顺序", kinds(segs) == [("sequential", 2)])

        # 读 + 写同路径 → 顺序（写与读重叠）
        segs = _plan_tool_batch_segments([read("src/a.py"), write("src/a.py")])
        check("读者+写者同路径 -> 顺序", kinds(segs) == [("sequential", 2)])

        # 读者+写者不同路径 → 并行
        segs = _plan_tool_batch_segments([read("src/a.py"), write("src/b.py")])
        check("读者+写者不同路径 -> 并行", kinds(segs) == [("parallel", 2)])

        # 子树重叠：写 src/ 与读 src/a.py 冲突
        segs = _plan_tool_batch_segments([write("src"), read("src/a.py")])
        check("子树重叠（写目录+读子文件）-> 顺序", kinds(segs) == [("sequential", 2)])

        # 三个调用：写不同文件 + 读旧文件 → 全部并行
        segs = _plan_tool_batch_segments([
            read("src/a.py"),
            write("src/b.py"),
            read("src/a.py"),
        ])
        check("读a + 写b + 读a -> 并行", kinds(segs) == [("parallel", 3)])
    finally:
        tool_dispatch._PATH_SCOPED_READERS = original_readers
        tool_dispatch._PATH_SCOPED_WRITERS = original_writers
        tool_dispatch._PATH_SCOPED_TOOLS = original_tools


def test_executor_parallel_and_order() -> None:
    """执行器：并行真实发生、结果按原始顺序回填、段提示回调触发。"""
    active = 0
    max_active = 0
    lock = threading.Lock()
    seen_kinds: list[str] = []

    def run_one(tc) -> str:
        """记录并发峰值并模拟耗时，返回与调用 ID 绑定的结果。"""
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.15)
        with lock:
            active -= 1
        return f"result-{tool_call_id(tc)}"

    calls = [
        make_call("c1", "web_search", {"query": "测试"}),
        make_call("c2", "session_search", {"query": "评审会"}),
    ]
    messages: list[dict] = []

    def on_segment(kind, seg_calls):
        seen_kinds.append(kind)

    execute_tool_calls_segmented(calls, messages, run_one, on_segment=on_segment)

    check("并行段真实并发（峰值=2）", max_active == 2)
    check("提示回调收到 parallel", seen_kinds == ["parallel"])
    check("结果按原始顺序回填",
          [m["tool_call_id"] for m in messages] == ["c1", "c2"])
    check("结果内容正确",
          [m["content"] for m in messages] == ["result-c1", "result-c2"])


def test_executor_mixed_batch() -> None:
    """执行器：混合批次按段顺序执行，结果仍按原始调用顺序回填。"""
    run_trace: list[str] = []

    def run_one(tc) -> str:
        run_trace.append(tool_call_id(tc))
        return f"result-{tool_call_id(tc)}"

    calls = [
        make_call("c1", "web_search", {"query": "测试"}),
        make_call("c2", "session_search", {"query": "评审会"}),
        make_call("c3", "terminal", {"command": "dir"}),
        make_call("c4", "memory_search", {"query": "骨架"}),
    ]
    messages: list[dict] = []
    execute_tool_calls_segmented(calls, messages, run_one)

    check("混合批执行顺序 = 原始顺序", run_trace == ["c1", "c2", "c3", "c4"])
    check("混合批结果回填顺序 = 原始顺序",
          [m["tool_call_id"] for m in messages] == ["c1", "c2", "c3", "c4"])


def test_real_file_tools_path_scope() -> None:
    """真实文件工具名已接入路径重叠：写同一文件顺序、读写不同文件并行。"""
    def read(path):
        return make_call(f"r-{path}", "read_file", {"path": path})

    def write(path):
        return make_call(f"w-{path}", "write_file", {"path": path})

    def patch(path):
        return make_call(f"p-{path}", "patch", {
            "path": path, "old_string": "a", "new_string": "b",
        })

    segs = _plan_tool_batch_segments([write("src/a.py"), write("src/a.py")])
    check("真实工具：同路径双写 -> 顺序", kinds(segs) == [("sequential", 2)])

    segs = _plan_tool_batch_segments([read("src/a.py"), write("src/b.py")])
    check("真实工具：读+写不同路径 -> 并行", kinds(segs) == [("parallel", 2)])

    segs = _plan_tool_batch_segments([read("src/a.py"), write("src/a.py")])
    check("真实工具：读+写同路径 -> 顺序", kinds(segs) == [("sequential", 2)])

    segs = _plan_tool_batch_segments([patch("src/a.py"), write("src/a.py")])
    check("真实工具：patch+写同路径 -> 顺序", kinds(segs) == [("sequential", 2)])

    segs = _plan_tool_batch_segments([patch("src/a.py"), write("src/b.py")])
    check("真实工具：patch+写不同路径 -> 并行", kinds(segs) == [("parallel", 2)])


def test_get_current_time_tool() -> None:
    """get_current_time：返回本地日期时间+星期，注册进 TOOLS/run_tool/并行白名单。"""
    out = minimal_agent.run_tool("get_current_time", {})
    pattern = r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} (周一|周二|周三|周四|周五|周六|周日)$"
    check("返回日期时间+星期", re.match(pattern, out) is not None)
    names = [t["function"]["name"] for t in minimal_agent.TOOLS]
    check("注册进 TOOLS", "get_current_time" in names)
    check("进并行白名单", "get_current_time" in _PARALLEL_SAFE_TOOLS)


def test_executor_interrupt() -> None:
    """执行器中断：预置中断全跳过；并行段取消 pending；顺序段中断后续跳过。"""
    # 1) 预置中断：全部跳过，run_one 不执行，结果按原始顺序回填 cancelled
    ev = threading.Event()
    ev.set()
    calls = [
        make_call("c1", "web_search", {"query": "x"}),
        make_call("c2", "web_search", {"query": "y"}),
    ]
    messages: list[dict] = []
    ran: list[str] = []

    def run_one(tc):
        ran.append(tool_call_id(tc))
        return "ok"

    execute_tool_calls_segmented(calls, messages, run_one, interrupt_event=ev)
    check("预置中断 -> run_one 未执行", ran == [])
    check("预置中断 -> 全部 cancelled",
          len(messages) == 2
          and all('"status": "cancelled"' in m["content"] for m in messages))
    check("预置中断 -> 结果按原始顺序",
          [m["tool_call_id"] for m in messages] == ["c1", "c2"])

    # 2) 并行段执行中置位：已完成的保留真实结果，阻塞中的回填 cancelled
    ev = threading.Event()
    release = threading.Event()
    c2_blocked = threading.Event()

    def run_gated(tc):
        if tool_call_id(tc) == "c1":
            return "done-1"
        c2_blocked.set()
        release.wait(timeout=15)
        return "done-2"

    def watcher():
        c2_blocked.wait(timeout=5)
        ev.set()

    threading.Thread(target=watcher, daemon=True).start()
    calls = [
        make_call("c1", "web_search", {"query": "a"}),
        make_call("c2", "web_search", {"query": "b"}),
    ]
    messages = []
    execute_tool_calls_segmented(calls, messages, run_gated, interrupt_event=ev)
    check("并行中断：已完成保留真实结果", messages[0]["content"] == "done-1")
    check("并行中断：阻塞中回填 cancelled",
          '"status": "cancelled"' in messages[1]["content"])
    check("并行中断：结果按原始顺序",
          [m["tool_call_id"] for m in messages] == ["c1", "c2"])
    release.set()  # 放行后台线程，避免测试进程退出时 join 等待

    # 3) 顺序段：第一个执行后置位 → 后续跳过并回填 cancelled
    ev = threading.Event()
    calls = [
        make_call("s1", "terminal", {"command": "echo a"}),
        make_call("s2", "terminal", {"command": "echo b"}),
    ]
    messages = []

    def run_seq(tc):
        ev.set()  # 第一个执行完就触发中断
        return "ok-1"

    execute_tool_calls_segmented(calls, messages, run_seq, interrupt_event=ev)
    check("顺序中断：第一个正常",
          messages[0]["content"] == "ok-1" and messages[0]["tool_call_id"] == "s1")
    check("顺序中断：后续 cancelled",
          '"status": "cancelled"' in messages[1]["content"])


def main() -> None:
    """依次运行全部测试并汇总结果。"""
    print("== 工具并行执行回归测试 ==")
    for test_fn in (
        test_planner_core_tools,
        test_planner_path_scope,
        test_executor_parallel_and_order,
        test_executor_mixed_batch,
        test_real_file_tools_path_scope,
        test_get_current_time_tool,
        test_executor_interrupt,
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
