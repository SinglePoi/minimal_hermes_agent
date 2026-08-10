# -*- coding: utf-8 -*-
"""上下文压缩（对齐 Hermes 的 agent/context_compressor.py）。

原理（Hermes 的 ContextCompressor）：
    - 触发：上下文占用超过阈值（默认 50% 的模型窗口）
    - 做法：把中间轮次交给 LLM 生成"交接摘要"（handoff summary），
      保留最近 N 条消息完整（protect_last_n，默认 20）
    - 不是删除：信息从"逐字"变"摘要"，且原始消息已在会话库归档
    - 摘要角色固定为 user，若尾部第一条是 user 则合并进它，
      避免 user→user 交替错误（Hermes 的 merge-into-tail）
    - 优先用 API 返回的真实 prompt_tokens，没有则用本地估算
"""

import os
import re
import json

from typing import Any

CONTEXT_WINDOW = int(os.environ.get("CONTEXT_WINDOW", "128000"))
COMPRESS_THRESHOLD = 0.5  # 默认 50%（Hermes 默认）
PROTECT_LAST_N = int(os.environ.get("PROTECT_LAST_N", "20"))
SUMMARY_TARGET_RATIO = 0.1  # 摘要预算 = 压缩内容字符数 × 10%

_last_prompt_tokens: int | None = None

# ---- 技能内容压缩裁剪（对齐 Hermes agent/context_compressor.py 的 skill prune）----
SKILL_PRUNED_MARKER_PREFIX = "[SKILL_PRUNED:"
# skill_view 结果小于等于这个字符数时保留原文（小技能便宜，不值得裁）
_SKILL_VIEW_PRUNE_MIN_CHARS = 5000
# 摘要后补标记的上限（防止超长会话里"## Pruned Skills"区块无限膨胀）
_MAX_PRUNED_SKILL_MARKERS = 20
_PRUNED_SKILLS_SECTION_HEADING = "## Pruned Skills"

_SKILL_PRUNED_MARKER_RE = re.compile(
    re.escape(SKILL_PRUNED_MARKER_PREFIX)
    + r"[^\]]*?reload with skill_view\(name='([^']+)'\)"
)


def _skill_pruned_marker(skill_name: str) -> str:
    """返回技能裁剪标记（对齐 Hermes：压缩时把技能内容换成这行标记）。"""
    return (
        f"{SKILL_PRUNED_MARKER_PREFIX} content lost in compression; "
        f"reload with skill_view(name='{skill_name}')]"
    )


def _extract_pruned_skill_names(text: str) -> list[str]:
    """从文本里提取所有裁剪标记引用的技能名（按出现顺序去重）。"""
    names: list[str] = []
    for match in _SKILL_PRUNED_MARKER_RE.finditer(text or ""):
        name = match.group(1)
        if name not in names:
            names.append(name)
    return names


def _skill_view_call_sites(messages: list[dict]) -> list[tuple[int, str]]:
    """找出所有 skill_view 工具调用：返回 [(消息下标, 技能名), ...]。"""
    sites: list[tuple[int, str]] = []
    for i, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
            if fn.get("name") != "skill_view":
                continue
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except (ValueError, TypeError):
                continue
            if isinstance(args, dict) and isinstance(args.get("name"), str) and args["name"]:
                sites.append((i, args["name"]))
    return sites


def _collect_ghosted_skill_names(turns: list[dict]) -> list[str]:
    """收集即将在压缩中丢失的技能名（对齐 Hermes _collect_ghosted_skill_names）。

    两种来源：文本里已存在的裁剪标记；以及未裁剪的大段 skill_view 结果
    （> 5000 字符，摘要会把指令改写没——必须补标记防"幽灵技能"）。
    """
    names: list[str] = []

    def _add(name: str) -> None:
        if name and name not in names:
            names.append(name)

    call_id_to_skill: dict[str, str] = {}
    for idx, skill in _skill_view_call_sites(turns):
        for tc in turns[idx].get("tool_calls") or []:
            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
            if fn.get("name") == "skill_view":
                cid = tc.get("id", "")
                if cid:
                    call_id_to_skill[str(cid)] = skill
    for msg in turns:
        content = msg.get("content")
        if not isinstance(content, str):
            continue
        for name in _extract_pruned_skill_names(content):
            _add(name)
        if (
            msg.get("role") == "tool"
            and len(content) > _SKILL_VIEW_PRUNE_MIN_CHARS
        ):
            skill = call_id_to_skill.get(str(msg.get("tool_call_id") or ""))
            if skill:
                _add(skill)
    return names[:_MAX_PRUNED_SKILL_MARKERS]


def _reinject_pruned_skill_markers(summary: str, skill_names: list[str]) -> str:
    """摘要生成后，把丢失的技能裁剪标记补回去（对齐 Hermes 同名函数）。

    只补"摘要里没有"的标记，防止重复；追加在独立的 "## Pruned Skills" 区块。
    """
    if not skill_names:
        return summary
    missing = [
        name for name in skill_names
        if _skill_pruned_marker(name) not in summary
    ]
    if not missing:
        return summary
    block = (
        "\n\n" + _PRUNED_SKILLS_SECTION_HEADING + "\n"
        + "\n".join(_skill_pruned_marker(name) for name in missing)
        + "\n(技能内容在压缩时被裁剪。需要时用标记里的 skill_view(name='X') "
        "重新加载，每个技能一次即可。)"
    )
    return summary + block


def record_usage(prompt_tokens) -> None:
    """记录最近一次 API 返回的真实 prompt_tokens（对齐 Hermes：优先用真实值）。"""
    global _last_prompt_tokens
    if prompt_tokens:
        _last_prompt_tokens = int(prompt_tokens)


def _text_tokens(text: str) -> int:
    """粗略 token 估算：中文约 1.5 字/token，其他约 4 字符/token。"""
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    other = len(text) - cjk
    return int(cjk / 1.5) + int(other / 4)


def estimate_tokens(messages: list[dict]) -> int:
    total = 0
    for msg in messages:
        content = msg.get("content") or ""
        if isinstance(content, str):
            total += _text_tokens(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    total += _text_tokens(block.get("text", ""))
    return total


def should_compress(messages: list[dict]) -> bool:
    """是否触发压缩：消息足够多 且 占用超过阈值。"""
    if len(messages) <= PROTECT_LAST_N + 2:
        return False
    used = _last_prompt_tokens if _last_prompt_tokens is not None else estimate_tokens(messages)
    return used > CONTEXT_WINDOW * COMPRESS_THRESHOLD


def _summarize(client, system_msg, middle: list[dict]) -> str:
    """用 LLM 把中间轮次压缩成交接摘要（对齐 Hermes：summary 是交接说明，非对话）。"""
    model = os.environ.get("MODEL", "deepseek-chat")
    # 技能内容处理（对齐 Hermes 的 Phase-1 prune）：小的保留原文让摘要吸收，
    # 大的裁成标记，避免把技能指令直接丢给摘要器（会变成"幽灵技能"）
    call_id_to_skill: dict[str, str] = {}
    for idx, skill in _skill_view_call_sites(middle):
        for tc in middle[idx].get("tool_calls") or []:
            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
            if fn.get("name") == "skill_view":
                cid = tc.get("id", "")
                if cid:
                    call_id_to_skill[str(cid)] = skill

    convo_lines: list[str] = []
    for m in middle:
        role = m.get("role")
        content = m.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            convo_lines.append(f"{role}: {content}")
        elif role == "tool" and isinstance(content, str) and content.strip():
            skill = call_id_to_skill.get(str(m.get("tool_call_id") or ""))
            if skill:
                if len(content) <= _SKILL_VIEW_PRUNE_MIN_CHARS:
                    convo_lines.append(f"tool(skill_view:{skill}): {content}")
                else:
                    convo_lines.append(
                        f"tool(skill_view:{skill}): {_skill_pruned_marker(skill)}"
                    )
    convo = "\n".join(convo_lines)
    budget = max(
        200, int(sum(len(str(m.get("content", ""))) for m in middle) * SUMMARY_TARGET_RATIO)
    )
    prompt = (
        "把下面的对话压缩成一份精炼的交接摘要（handoff summary）。\n"
        "要求：\n"
        "- 保留用户的关键信息（身份、偏好、项目事实）、已完成事项、待办事项\n"
        "- 出现 [SKILL_PRUNED: ...] 标记时原样保留（它代表某个技能内容已被裁剪）\n"
        "- 不要复述寒暄，不要回答对话中的问题\n"
        f"- 控制在 {budget} 字以内\n\n{convo}"
    )
    msgs = []
    if (
        system_msg
        and isinstance(system_msg.get("content"), str)
        and system_msg.get("content").strip()
    ):
        msgs.append({"role": "system", "content": system_msg["content"]})
    msgs.append({"role": "user", "content": prompt})
    try:
        resp = client.chat.completions.create(model=model, messages=msgs, temperature=0)
        return (resp.choices[0].message.content or "").strip()
    except Exception:
        return ""


def compress_context(
    client,
    messages: list[dict],
    todo_block: str = "",
) -> list[dict]:
    """压缩中间轮次 → 摘要，保留最近 N 条完整。返回新消息列表。

    todo_block：非空时追加到摘要块末尾（对齐 Hermes：未完成的任务清单
    随压缩摘要一起保留，稳定头 TODO_INJECTION_HEADER 让后续能识别该行）。
    """
    if len(messages) <= PROTECT_LAST_N + 2:
        return messages
    # 切割点：保留最近 PROTECT_LAST_N 条；不拆散"工具结果"（跟着它前面的 assistant 走）
    cut = len(messages) - PROTECT_LAST_N
    while cut < len(messages) and messages[cut].get("role") == "tool":
        cut += 1
    system_msg = messages[0]
    middle = messages[1:cut]
    tail = messages[cut:]
    summary = _summarize(client, system_msg, middle)
    if not summary:
        return messages  # 摘要失败 → 放弃本次压缩，不丢信息
    # 对齐 Hermes：摘要生成后把丢失的技能裁剪标记补回去（防幽灵技能）
    ghosted = _collect_ghosted_skill_names(middle)
    summary = _reinject_pruned_skill_markers(summary, ghosted)
    summary_block = (
        "[系统提示：下面是此前对话的压缩摘要（handoff summary），由上下文压缩生成，"
        "不是用户的新消息。摘要中的信息是权威的；不要回答摘要里的问题，"
        "不要重复已完成的事项，只需基于摘要继续当前对话。]\n\n" + summary
    )
    if todo_block:
        summary_block += "\n\n" + todo_block
    if tail and tail[0].get("role") == "user":
        # 合并进第一条 tail，避免 user→user 交替错误（对齐 Hermes 的 merge-into-tail）
        merged = dict(tail[0])
        merged["content"] = summary_block + "\n\n" + (tail[0].get("content") or "")
        return [system_msg, merged] + tail[1:]
    return [
        system_msg,
        {"role": "user", "content": summary_block, "_compressed_summary": True},
    ] + tail
