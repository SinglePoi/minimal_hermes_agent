# -*- coding: utf-8 -*-
"""
Skills 与上下文压缩联动（prune / reinject）的回归测试（零依赖）：
    python tests/test_skills_compression.py

覆盖（对齐 Hermes agent/context_compressor.py 的 skill prune）：
    - 裁剪标记的生成与解析往返
    - skill_view 调用点识别
    - 幽灵技能收集：大段技能结果 / 已有标记
    - 摘要后补标记：丢失的补回、已有的不重复
    - 端到端：压缩时大技能裁成标记、小技能保留原文、摘要后补回标记
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import context_compressor as cc  # noqa: E402


_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    """记录一条断言结果。"""
    if cond:
        print(f"  ok: {label}")
    else:
        _failures.append(label)
        print(f"  FAIL: {label}")


def skill_view_msg(call_id: str, name: str) -> dict:
    """构造一个 skill_view 调用的 assistant 消息（dict 形态，与 model_dump 一致）。"""
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": "skill_view", "arguments": json.dumps({"name": name})},
        }],
    }


def tool_result_msg(call_id: str, content: str) -> dict:
    """构造 skill_view 的 tool 结果消息。"""
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def skill_view_result(name: str, body: str) -> str:
    """构造 skill_view 返回的 JSON 字符串。"""
    return json.dumps(
        {"success": True, "name": name, "content": body}, ensure_ascii=False
    )


class FakeCompletions:
    """假 LLM：记录摘要请求的输入，返回固定摘要。"""

    def __init__(self, outer):
        self.outer = outer

    def create(self, **kwargs):
        msgs = kwargs.get("messages", [])
        self.outer.captured_prompt = msgs[-1]["content"] if msgs else ""
        return SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=10),
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=self.outer.summary, tool_calls=None)
            )],
        )


class FakeChat:
    def __init__(self, outer):
        self.completions = FakeCompletions(outer)


class FakeClient:
    """假 client：chat.completions.create 记录输入并返回固定摘要。"""

    def __init__(self, summary: str = "交接摘要正文"):
        self.summary = summary
        self.captured_prompt = ""
        self.chat = FakeChat(self)


def test_marker_roundtrip() -> None:
    """裁剪标记：生成后能被解析回技能名。"""
    marker = cc._skill_pruned_marker("release-check")
    check("标记含重载指令", "skill_view(name='release-check')" in marker)
    check("标记可解析回技能名",
          cc._extract_pruned_skill_names(marker) == ["release-check"])
    check("无标记文本 -> 空", cc._extract_pruned_skill_names("普通文本") == [])


def test_call_sites_and_ghosted() -> None:
    """skill_view 调用点识别 + 幽灵技能收集。"""
    big = skill_view_result("big-skill", "B" * 6000)
    small = skill_view_result("small-skill", "小技能内容")
    turns = [
        skill_view_msg("c-big", "big-skill"),
        tool_result_msg("c-big", big),
        skill_view_msg("c-small", "small-skill"),
        tool_result_msg("c-small", small),
        {"role": "user", "content": cc._skill_pruned_marker("already-marked")},
    ]
    sites = cc._skill_view_call_sites(turns)
    check("识别 2 个 skill_view 调用点",
          sorted(name for _, name in sites) == ["big-skill", "small-skill"])
    ghosted = cc._collect_ghosted_skill_names(turns)
    check("幽灵技能：大结果 + 已有标记，小结果不收集",
          set(ghosted) == {"big-skill", "already-marked"})


def test_reinject_markers() -> None:
    """摘要后补标记：丢失的补回、已有的不重复。"""
    out = cc._reinject_pruned_skill_markers("普通摘要", ["big-skill"])
    check("丢失的标记补回", "## Pruned Skills" in out
          and cc._skill_pruned_marker("big-skill") in out)

    already = cc._skill_pruned_marker("big-skill")
    out = cc._reinject_pruned_skill_markers(f"摘要里有{already}", ["big-skill"])
    check("已有标记不重复", out.count("[SKILL_PRUNED:") == 1)

    check("空技能名 -> 原样", cc._reinject_pruned_skill_markers("摘要", []) == "摘要")


def test_compression_prunes_and_reinjects() -> None:
    """端到端：压缩时大技能裁成标记、小技能保留原文、摘要后补回标记。"""
    messages: list[dict] = [{"role": "system", "content": "系统提示词"}]
    messages.append(skill_view_msg("c-big", "big-skill"))
    messages.append(tool_result_msg("c-big", skill_view_result("big-skill", "B" * 6000)))
    messages.append(skill_view_msg("c-small", "small-skill"))
    messages.append(tool_result_msg("c-small", skill_view_result("small-skill", "小技能内容")))
    for i in range(20):
        messages.append({"role": "user", "content": f"填充问题{i}"})
        messages.append({"role": "assistant", "content": f"填充回答{i}"})

    client = FakeClient()
    compressed = cc.compress_context(client, messages)
    check("压缩确实发生（消息变少）", len(compressed) < len(messages))
    check("摘要器输入：大技能是标记",
          cc._skill_pruned_marker("big-skill") in client.captured_prompt)
    check("摘要器输入：小技能保留原文", "小技能内容" in client.captured_prompt)

    full_text = "\n".join(str(m.get("content", "")) for m in compressed)
    check("压缩结果含补回的大技能标记",
          cc._skill_pruned_marker("big-skill") in full_text)
    check("压缩结果含 Pruned Skills 区块", "## Pruned Skills" in full_text)


def main() -> None:
    """依次运行全部测试并汇总结果。"""
    print("== Skills 与压缩联动回归测试 ==")
    for test_fn in (
        test_marker_roundtrip,
        test_call_sites_and_ghosted,
        test_reinject_markers,
        test_compression_prunes_and_reinjects,
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
