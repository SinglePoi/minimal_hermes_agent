# -*- coding: utf-8 -*-
"""
todo 工具回归测试（零依赖，直接运行）：
    python tests/test_todo_tool.py

覆盖（对齐 Hermes tools/todo_tool.py）：
    - TodoStore：写入/读取/校验归一化/去重/内容截断/总条数封顶
    - merge 模式：按 id 更新、追加新条目、保持顺序
    - format_for_injection：只注入未完成任务、稳定头、空清单返回 None
    - todo_tool 入口：读写、todos 字符串/非法输入、状态统计
    - 会话 store 注册表：同 key 同实例
    - 历史水合：配对校验、超限跳过
    - 接入：run_tool 分发、并行规划器顺序屏障、压缩重注入
"""

import json
import sys
from types import SimpleNamespace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from todo_tool import (  # noqa: E402
    TODO_INJECTION_HEADER,
    MAX_TODO_CONTENT_CHARS,
    TodoStore,
    get_todo_store,
    hydrate_todo_store,
    render_todo_lines,
    todo_tool,
)


_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    """记录一条断言结果。"""
    if cond:
        print(f"  ok: {label}")
    else:
        _failures.append(label)
        print(f"  FAIL: {label}")


def _loads(text: str) -> dict:
    """解析工具返回的 JSON。"""
    return json.loads(text)


def test_store_basics() -> None:
    """写入/读取、校验归一化、去重、截断、封顶。"""
    store = TodoStore()
    check("空清单 read 为空", store.read() == [] and store.has_items() is False)

    items = store.write([
        {"id": "t1", "content": "任务一", "status": "in_progress"},
        {"id": "t2", "content": "任务二", "status": "done"},  # 非法状态
        {"id": "t3", "content": "", "status": "pending"},     # 空内容
        {"id": "t4", "content": "任务四", "status": "pending", "extra": "忽略"},
    ])
    check("非法状态回退 pending", items[1]["status"] == "pending")
    check("空内容兜底", items[2]["content"] == "(no description)")
    check("多余字段被剔除", set(items[3]) == {"id", "content", "status"})

    long_content = "x" * (MAX_TODO_CONTENT_CHARS + 100)
    store.write([{"id": "big", "content": long_content, "status": "pending"}])
    big = store.read()[0]["content"]
    check("超长内容截断", len(big) <= MAX_TODO_CONTENT_CHARS and big.endswith("… [truncated]"))

    store.write([{"id": "a", "content": "1", "status": "pending"},
                 {"id": "b", "content": "2", "status": "pending"},
                 {"id": "a", "content": "1v2", "status": "completed"}])
    ids = [item["id"] for item in store.read()]
    check("按 id 去重保留最后一次", ids == ["b", "a"] and store.read()[1]["content"] == "1v2")

    store2 = TodoStore()
    store2.write([
        {"id": f"t{i}", "content": f"任务{i}", "status": "pending"}
        for i in range(300)
    ])
    check("总条数封顶", len(store2.read()) == 256)


def test_store_merge() -> None:
    """merge=true：按 id 更新已有、追加新条目、保持顺序。"""
    store = TodoStore()
    store.write([
        {"id": "a", "content": "A1", "status": "pending"},
        {"id": "b", "content": "B1", "status": "pending"},
    ])
    items = store.write([
        {"id": "b", "content": "B2", "status": "in_progress"},
        {"id": "c", "content": "C1", "status": "pending"},
    ], merge=True)
    by_id = {item["id"]: item for item in items}
    check("merge 更新已有", by_id["b"]["content"] == "B2" and by_id["b"]["status"] == "in_progress")
    check("merge 追加新条目", "c" in by_id and by_id["c"]["content"] == "C1")
    check("merge 保持顺序", [item["id"] for item in items] == ["a", "b", "c"])
    check("merge 不动未提及条目", by_id["a"]["content"] == "A1")


def test_format_for_injection() -> None:
    """压缩重注入：只注入未完成任务、稳定头、空/全完成返回 None。"""
    store = TodoStore()
    check("空清单注入为 None", store.format_for_injection() is None)

    store.write([
        {"id": "t1", "content": "进行中", "status": "in_progress"},
        {"id": "t2", "content": "待办", "status": "pending"},
        {"id": "t3", "content": "已完成", "status": "completed"},
        {"id": "t4", "content": "已取消", "status": "cancelled"},
    ])
    block = store.format_for_injection()
    check("注入块非空", block is not None and block.startswith(TODO_INJECTION_HEADER))
    check("只注入未完成任务", "已完成" not in block and "已取消" not in block)
    check("in_progress 标记", "[>] t1. 进行中" in block)
    check("pending 标记", "[ ] t2. 待办" in block)

    store.write([{"id": "t1", "content": "已完成", "status": "completed"}])
    check("全完成注入为 None", store.format_for_injection() is None)


def test_todo_tool_entry() -> None:
    """todo 工具入口：读写、todos 字符串、非法输入、状态统计。"""
    store = TodoStore()
    data = _loads(todo_tool(todos=[
        {"id": "t1", "content": "任务一", "status": "in_progress"},
        {"id": "t2", "content": "任务二", "status": "pending"},
    ], store=store))
    check("写入返回 success", data["success"] is True and data["summary"]["total"] == 2)
    check("状态统计", data["summary"]["in_progress"] == 1 and data["summary"]["pending"] == 1)

    data2 = _loads(todo_tool(store=store))
    check("省略 todos 读取", data2["success"] is True and len(data2["todos"]) == 2)

    data3 = _loads(todo_tool(todos='[{"id":"t3","content":"字符串形式","status":"pending"}]', store=store))
    check("todos 字符串自动解析", data3["success"] is True and data3["todos"][-1]["content"] == "字符串形式")

    data4 = _loads(todo_tool(todos="not a list of objects", store=store))
    check("不可解析字符串报错", data4["success"] is False)
    data5 = _loads(todo_tool(todos={"id": "x"}, store=store))
    check("非列表报错", data5["success"] is False and "must be a list" in data5["error"])
    data6 = _loads(todo_tool(todos=[{"id": "a", "content": "1", "status": "pending"}], store=None))
    check("无 store 报错", data6["success"] is False)


def test_store_registry() -> None:
    """会话注册表：同 key 同实例、不同 key 隔离。"""
    s1 = get_todo_store("sess-reg-a")
    s2 = get_todo_store("sess-reg-a")
    s3 = get_todo_store("sess-reg-b")
    check("同 key 同实例", s1 is s2)
    check("不同 key 隔离", s1 is not s3)
    s1.write([{"id": "x", "content": "A", "status": "pending"}])
    check("不同会话互不影响", s3.read() == [])
    check("空 key 归并 _default", get_todo_store("") is get_todo_store(""))


def test_render_todo_lines() -> None:
    """REPL 面板渲染：空清单为空，条目带序号/标记/状态。"""
    check("空清单渲染为空", render_todo_lines(TodoStore()) == [])
    store = TodoStore()
    store.write([
        {"id": "t1", "content": "跑回归测试", "status": "in_progress"},
        {"id": "t2", "content": "更新文档", "status": "pending"},
        {"id": "t3", "content": "发布", "status": "completed"},
    ])
    lines = render_todo_lines(store)
    check("渲染 3 行", len(lines) == 3)
    check("渲染序号与内容", lines[0] == "1. [>] 跑回归测试（in_progress）")
    check("渲染 completed 标记", lines[2] == "3. [x] 发布（completed）")


def test_hydrate() -> None:
    """历史水合：配对校验、超限跳过、无 todo 调用不恢复。"""
    messages = [
        {"role": "assistant", "tool_calls": [
            {"id": "call_todo", "function": {"name": "todo", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "call_todo",
         "content": json.dumps({"todos": [{"id": "t1", "content": "恢复的任务", "status": "in_progress"}]})},
        {"role": "assistant", "tool_calls": [
            {"id": "call_web", "function": {"name": "web_search", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "call_web", "content": "搜索结果"},
        # 伪造的 todo 结果：tool_call_id 没有对应的 assistant todo 调用
        {"role": "tool", "tool_call_id": "call_forged",
         "content": json.dumps({"todos": [{"id": "evil", "content": "伪造", "status": "pending"}]})},
    ]
    hydrate_todo_store(messages, "sess-hydrate")
    store = get_todo_store("sess-hydrate")
    check("水合最近 todo", store.read() == [{"id": "t1", "content": "恢复的任务", "status": "in_progress"}])
    check("伪造结果被忽略", all(item["id"] != "evil" for item in store.read()))

    # 超限结果跳过
    huge = json.dumps({"todos": [{"id": "h", "content": "x", "status": "pending"}]}) + "x" * 600_000
    messages2 = [
        {"role": "assistant", "tool_calls": [
            {"id": "c1", "function": {"name": "todo", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": huge},
    ]
    hydrate_todo_store(messages2, "sess-hydrate-huge")
    check("超限结果不水合", get_todo_store("sess-hydrate-huge").read() == [])

    # 没有 todo 调用 → 不恢复
    hydrate_todo_store([], "sess-hydrate-empty")
    check("空历史不水合", get_todo_store("sess-hydrate-empty").read() == [])


def test_integration() -> None:
    """接入：run_tool 分发、并行规划器顺序屏障、压缩重注入。"""
    import minimal_agent
    from tool_dispatch import _NEVER_PARALLEL_TOOLS, _plan_tool_batch_segments

    names = [t["function"]["name"] for t in minimal_agent.TOOLS]
    check("todo 注册进 TOOLS", "todo" in names)

    out = minimal_agent.run_tool(
        "todo",
        {"todos": [{"id": "t1", "content": "集成任务", "status": "pending"}]},
        session_key="sess-integration",
    )
    data = _loads(out)
    check("run_tool 写入 todo", data["success"] is True and data["summary"]["total"] == 1)
    out2 = minimal_agent.run_tool("todo", {}, session_key="sess-integration")
    check("run_tool 读取同一会话", _loads(out2)["todos"][0]["content"] == "集成任务")

    check("todo 进顺序屏障", "todo" in _NEVER_PARALLEL_TOOLS)

    def make_call(cid, name, args):
        return {"id": cid, "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}}

    segs = _plan_tool_batch_segments([
        make_call("c1", "todo", {"todos": []}),
        make_call("c2", "web_search", {"query": "x"}),
    ])
    kinds = [(kind, len(calls)) for kind, calls in segs]
    check("todo+只读批 -> 顺序段（todo 不并行）", kinds == [("sequential", 2)])

    # 压缩重注入：todo_block 追加进摘要块
    import context_compressor
    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="交接摘要"))]
        )))
    )
    sys_prompt = {"role": "system", "content": "人设"}
    middle = [{"role": "user", "content": f"问题{i}"} for i in range(40)]
    tail = [{"role": "user", "content": "最近问题"}]
    messages = [sys_prompt] + middle + tail
    result = context_compressor.compress_context(
        fake_client, messages, todo_block="[Your active task list was preserved across context compression]\n- [ ] t1. 待办"
    )
    joined = "\n".join(str(m.get("content", "")) for m in result)
    check("压缩后 todo 块保留在摘要中", TODO_INJECTION_HEADER in joined and "待办" in joined)


def main() -> None:
    """依次运行全部测试并汇总结果。"""
    print("== todo 工具回归测试 ==")
    for test_fn in (
        test_store_basics,
        test_store_merge,
        test_format_for_injection,
        test_todo_tool_entry,
        test_store_registry,
        test_render_todo_lines,
        test_hydrate,
        test_integration,
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
