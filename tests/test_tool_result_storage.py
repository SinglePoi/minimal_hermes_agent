# -*- coding: utf-8 -*-
"""
工具结果落盘模块的回归测试（零依赖，直接运行）：
    python tests/test_tool_result_storage.py

覆盖（对齐 Hermes tools/tool_result_storage.py）：
    - 配置加载：环境变量布尔/整数解析、默认值
    - generate_preview：小文本原样返回、大文本按换行截断
    - 文件名安全化：非法字符/超长 ID 处理
    - 单结果落盘：小结果不落盘、read_file 固定不落盘、关闭时旁路、
      大结果写盘 + 返回 <persisted-output> 预览、写盘失败回退截断
    - 单轮聚合预算：超限后从最大未落盘结果开始溢写、跳过已落盘结果
    - 与 tool_dispatch 执行器的集成：大结果回填被替换为落盘预览
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import tool_result_storage as trs  # noqa: E402
from tool_dispatch import execute_tool_calls_segmented  # noqa: E402


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


def make_config(**overrides) -> trs.ToolResultConfig:
    """构造落盘配置，默认指向临时目录。"""
    tmpdir = tempfile.mkdtemp(prefix="tool-result-test-")
    defaults = dict(
        enabled=True,
        max_chars=100,
        turn_budget=500,
        preview_chars=30,
        storage_dir=tmpdir,
    )
    defaults.update(overrides)
    return trs.ToolResultConfig(**defaults)


def test_config_loading() -> None:
    """配置加载：环境变量解析、非法值回退、默认值。"""
    saved = {k: os.environ.get(k) for k in (
        "TOOL_RESULT_STORAGE_ENABLED",
        "TOOL_RESULT_MAX_CHARS",
        "TOOL_RESULT_TURN_BUDGET_CHARS",
        "TOOL_RESULT_PREVIEW_CHARS",
        "TOOL_RESULT_STORAGE_DIR",
    )}
    try:
        for k in saved:
            os.environ.pop(k, None)
        cfg = trs.get_config()
        check("默认开启", cfg.enabled is True)
        check("默认单结果阈值", cfg.max_chars == 100_000)
        check("默认单轮预算", cfg.turn_budget == 200_000)
        check("默认预览字符数", cfg.preview_chars == 1_500)

        os.environ["TOOL_RESULT_STORAGE_ENABLED"] = "off"
        os.environ["TOOL_RESULT_MAX_CHARS"] = "not-a-number"
        os.environ["TOOL_RESULT_TURN_BUDGET_CHARS"] = "999"
        os.environ["TOOL_RESULT_PREVIEW_CHARS"] = "42"
        cfg = trs.get_config()
        check("off 解析为关闭", cfg.enabled is False)
        check("非法阈值回退默认", cfg.max_chars == 100_000)
        check("单轮预算从环境变量读取", cfg.turn_budget == 999)
        check("预览字符数从环境变量读取", cfg.preview_chars == 42)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_generate_preview() -> None:
    """generate_preview：小文本原样返回，大文本按最后一个换行截断。"""
    check("小文本原样返回", trs.generate_preview("abc", 10) == ("abc", False))
    text = "line1\nline2\nline3"
    preview, has_more = trs.generate_preview(text, 12)
    check("按换行截断", preview == "line1\nline2\n")
    check("存在更多内容", has_more is True)
    preview, has_more = trs.generate_preview("abcdef", 3)
    check("无换行直接截断", preview == "abc" and has_more is True)


def test_safe_filename() -> None:
    """文件名安全化：非法字符替换、空值兜底、超长附加哈希。"""
    check("普通 ID 保持", trs._safe_result_filename("call_123") == "call_123.txt")
    name = trs._safe_result_filename("../a b:c")
    check("非法字符被清洗", ".." not in name and " " not in name and ":" not in name)
    long_name = trs._safe_result_filename("x" * 200)
    check("超长 ID 被截断并附加哈希", len(Path(long_name).stem) <= 120 + 1 + 12)


def test_maybe_persist_small_and_pinned() -> None:
    """单结果落盘：小结果、read_file、关闭时均不落盘。"""
    cfg = make_config(max_chars=100)
    check("小结果原样返回", trs.maybe_persist_tool_result("small", "web_search", "t1", config=cfg) == "small")

    big = "x" * 300
    check("read_file 固定不落盘",
          trs.maybe_persist_tool_result(big, "read_file", "t2", config=cfg) == big)

    off_cfg = make_config(enabled=False, max_chars=1)
    check("关闭时旁路", trs.maybe_persist_tool_result(big, "web_search", "t3", config=off_cfg) == big)


def test_maybe_persist_writes_file() -> None:
    """单结果落盘：大结果写盘，上下文替换为 <persisted-output> 预览。"""
    cfg = make_config(max_chars=100, preview_chars=30)
    content = "A" * 300
    out = trs.maybe_persist_tool_result(content, "web_search", "t_big", config=cfg)
    check("返回持久化标记", trs.PERSISTED_OUTPUT_TAG in out)
    check("返回完整结果路径", "Full output saved to:" in out)

    saved_path = None
    for line in out.splitlines():
        if line.startswith("Full output saved to: "):
            saved_path = line[len("Full output saved to: "):].strip()
            break
    check("找到落盘路径", saved_path is not None)
    if saved_path:
        check("磁盘文件内容与原始结果一致", Path(saved_path).read_text(encoding="utf-8") == content)


def test_maybe_persist_fallback() -> None:
    """单结果落盘：写盘失败回退成内联截断，不抛异常。"""
    cfg = make_config(max_chars=100, preview_chars=30)
    original_write = trs._write_result
    trs._write_result = lambda content, file_path: False
    try:
        content = "B" * 300
        out = trs.maybe_persist_tool_result(content, "web_search", "t_fail", config=cfg)
        check("失败回退包含截断提示", "Full output could not be saved" in out)
        check("失败回退不包含持久化标记", trs.PERSISTED_OUTPUT_TAG not in out)
    finally:
        trs._write_result = original_write


def test_enforce_turn_budget() -> None:
    """单轮聚合预算：超限从最大未落盘结果开始溢写，跳过已落盘结果。"""
    cfg = make_config(max_chars=100, turn_budget=600, preview_chars=20)
    messages = [
        {"role": "tool", "tool_call_id": "a", "content": "A" * 50},
        {"role": "tool", "tool_call_id": "b", "content": "B" * 1000},
        {"role": "tool", "tool_call_id": "c", "content": "C" * 10},
    ]
    trs.enforce_turn_budget(messages, config=cfg)
    check("超限后最大结果已落盘", trs.PERSISTED_OUTPUT_TAG in messages[1]["content"])
    check("较小结果未被落盘",
          trs.PERSISTED_OUTPUT_TAG not in messages[0]["content"]
          and trs.PERSISTED_OUTPUT_TAG not in messages[2]["content"])
    total = sum(len(m["content"]) for m in messages)
    check("总字符数回到预算以内", total <= cfg.turn_budget)

    # 已落盘的结果在下一轮预算检查里应被跳过，不重复写盘
    persisted = messages[1]["content"]
    trs.enforce_turn_budget(messages, config=cfg)
    check("已落盘结果不重复替换", messages[1]["content"] == persisted)


def test_enforce_turn_budget_disabled() -> None:
    """单轮聚合预算：关闭时不做任何修改。"""
    cfg = make_config(enabled=False, turn_budget=1)
    messages = [
        {"role": "tool", "tool_call_id": "a", "content": "A" * 500},
    ]
    original = list(messages)
    trs.enforce_turn_budget(messages, config=cfg)
    check("关闭时原样返回", messages == original)


def test_executor_integration() -> None:
    """与 tool_dispatch 集成：大结果回填被替换为落盘预览。"""
    tmpdir = tempfile.mkdtemp(prefix="tool-result-integration-")
    saved = {k: os.environ.get(k) for k in (
        "TOOL_RESULT_STORAGE_ENABLED",
        "TOOL_RESULT_MAX_CHARS",
        "TOOL_RESULT_STORAGE_DIR",
        "TOOL_RESULT_TURN_BUDGET_CHARS",
        "TOOL_RESULT_PREVIEW_CHARS",
    )}
    try:
        os.environ["TOOL_RESULT_STORAGE_ENABLED"] = "true"
        os.environ["TOOL_RESULT_MAX_CHARS"] = "50"
        os.environ["TOOL_RESULT_STORAGE_DIR"] = tmpdir
        os.environ["TOOL_RESULT_TURN_BUDGET_CHARS"] = "100000"
        os.environ["TOOL_RESULT_PREVIEW_CHARS"] = "20"

        calls = [make_call("c1", "web_search", {"query": "x"})]
        messages: list[dict] = []
        execute_tool_calls_segmented(calls, messages, lambda tc: "D" * 300)

        check("工具结果被落盘预览替换", trs.PERSISTED_OUTPUT_TAG in messages[0]["content"])
        check("消息仍保留 tool_call_id", messages[0]["tool_call_id"] == "c1")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def main() -> None:
    """依次运行全部测试并汇总结果。"""
    print("== 工具结果落盘回归测试 ==")
    for test_fn in (
        test_config_loading,
        test_generate_preview,
        test_safe_filename,
        test_maybe_persist_small_and_pinned,
        test_maybe_persist_writes_file,
        test_maybe_persist_fallback,
        test_enforce_turn_budget,
        test_enforce_turn_budget_disabled,
        test_executor_integration,
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
