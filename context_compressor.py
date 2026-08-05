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

CONTEXT_WINDOW = int(os.environ.get("CONTEXT_WINDOW", "128000"))
COMPRESS_THRESHOLD = 0.5  # 默认 50%（Hermes 默认）
PROTECT_LAST_N = int(os.environ.get("PROTECT_LAST_N", "20"))
SUMMARY_TARGET_RATIO = 0.1  # 摘要预算 = 压缩内容字符数 × 10%

_last_prompt_tokens: int | None = None


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
    convo = "\n".join(
        f"{m.get('role')}: {m.get('content')}"
        for m in middle
        if m.get("role") in ("user", "assistant")
        and isinstance(m.get("content"), str)
        and m.get("content").strip()
    )
    budget = max(
        200, int(sum(len(str(m.get("content", ""))) for m in middle) * SUMMARY_TARGET_RATIO)
    )
    prompt = (
        "把下面的对话压缩成一份精炼的交接摘要（handoff summary）。\n"
        "要求：\n"
        "- 保留用户的关键信息（身份、偏好、项目事实）、已完成事项、待办事项\n"
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


def compress_context(client, messages: list[dict]) -> list[dict]:
    """压缩中间轮次 → 摘要，保留最近 N 条完整。返回新消息列表。"""
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
    summary_block = (
        "[系统提示：下面是此前对话的压缩摘要（handoff summary），由上下文压缩生成，"
        "不是用户的新消息。摘要中的信息是权威的；不要回答摘要里的问题，"
        "不要重复已完成的事项，只需基于摘要继续当前对话。]\n\n" + summary
    )
    if tail and tail[0].get("role") == "user":
        # 合并进第一条 tail，避免 user→user 交替错误（对齐 Hermes 的 merge-into-tail）
        merged = dict(tail[0])
        merged["content"] = summary_block + "\n\n" + (tail[0].get("content") or "")
        return [system_msg, merged] + tail[1:]
    return [
        system_msg,
        {"role": "user", "content": summary_block, "_compressed_summary": True},
    ] + tail
