# -*- coding: utf-8 -*-
"""
系统提示词持久化与压缩重建的回归测试（零依赖，直接运行）：
    python tests/test_session_prompt.py

覆盖（对齐 Hermes SessionDB.update_system_prompt / _restore_or_build_system_prompt）：
    - save/load 往返：系统提示词落库后可恢复
    - UPSERT 覆盖：同一会话重复保存取最新
    - 压缩后重建：上下文压缩触发时刷新 messages[0] 并同步持久化
"""

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

import minimal_agent  # noqa: E402
import context_compressor  # noqa: E402


_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    """记录一条断言结果。"""
    if cond:
        print(f"  ok: {label}")
    else:
        _failures.append(label)
        print(f"  FAIL: {label}")


class FakeMessage:
    """主模型回复的假消息（带 model_dump）。"""

    def __init__(self, content: str, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls

    def model_dump(self, exclude_none=True):
        return {"role": "assistant", "content": self.content}


class FakeCompletions:
    """假 LLM：带 tools 参数的是主模型（给最终回答），否则是压缩摘要调用。"""

    def __init__(self, outer):
        self.outer = outer

    def create(self, **kwargs):
        self.outer.calls += 1
        if "tools" in kwargs:
            return SimpleNamespace(
                usage=SimpleNamespace(prompt_tokens=10),
                choices=[SimpleNamespace(message=FakeMessage("最终回答"))],
            )
        return SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=10),
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="摘要内容", tool_calls=None)
            )],
        )


class FakeChat:
    def __init__(self, outer):
        self.completions = FakeCompletions(outer)


class FakeClient:
    def __init__(self):
        self.calls = 0
        self.chat = FakeChat(self)


class StubManager:
    """记录 commit_memory_session 调用的假 MemoryManager。"""

    def __init__(self):
        self.commits: list[list] = []

    def commit_memory_session(self, messages, client=None):
        self.commits.append(list(messages))

    def build_system_prompt(self):
        return ""


def test_save_load_roundtrip() -> None:
    """系统提示词落库后可恢复；UPSERT 覆盖取最新。"""
    original_db = minimal_agent.SESSION_DB
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            minimal_agent.SESSION_DB = Path(tmpdir) / "sessions.db"
            minimal_agent.save_session_prompt("s1", "第一版提示词")
            check("保存后可读取", minimal_agent.load_session_prompt("s1") == "第一版提示词")
            check("未保存的会话返回 None",
                  minimal_agent.load_session_prompt("s-none") is None)
            minimal_agent.save_session_prompt("s1", "第二版提示词")
            check("UPSERT 覆盖取最新",
                  minimal_agent.load_session_prompt("s1") == "第二版提示词")
    finally:
        minimal_agent.SESSION_DB = original_db


def test_compression_rebuilds_system_prompt() -> None:
    """压缩触发后：messages[0] 重建为最新系统提示词并同步持久化。"""
    original_db = minimal_agent.SESSION_DB
    original_usage = context_compressor._last_prompt_tokens
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            minimal_agent.SESSION_DB = Path(tmpdir) / "sessions.db"
            # 强制触发压缩
            context_compressor._last_prompt_tokens = 999_999

            messages: list[dict] = [
                {"role": "system", "content": "旧的系统提示词"}
            ]
            for i in range(24):
                messages.append({"role": "user", "content": f"问题{i}"})
                messages.append({"role": "assistant", "content": f"回答{i}"})

            client = FakeClient()
            manager = StubManager()
            minimal_agent.run_agent_turn(
                client, messages, minimal_agent.get_tools(None), manager, "sess-compress"
            )

            rebuilt = minimal_agent.build_system_prompt(None)
            check("压缩边界先 commit 记忆（1 次，含压缩前全量消息）",
                  len(manager.commits) == 1 and len(manager.commits[0]) == 49)
            check("压缩真的发生了（摘要注入）",
                  any("[系统提示：下面是此前对话的压缩摘要" in str(m.get("content", ""))
                      for m in messages))
            check("messages[0] 重建为最新系统提示词",
                  messages[0]["role"] == "system" and messages[0]["content"] == rebuilt)
            check("重建结果已持久化",
                  minimal_agent.load_session_prompt("sess-compress") == rebuilt)
    finally:
        context_compressor._last_prompt_tokens = original_usage
        minimal_agent.SESSION_DB = original_db


def main() -> None:
    """依次运行全部测试并汇总结果。"""
    print("== 系统提示词持久化回归测试 ==")
    for test_fn in (
        test_save_load_roundtrip,
        test_compression_rebuilds_system_prompt,
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
