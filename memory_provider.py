# -*- coding: utf-8 -*-
"""外部记忆 provider 抽象基类（对齐 Hermes 的 agent/memory_provider.py）。"""

import json
import os
import re
from abc import ABC, abstractmethod

from retry_utils import call_with_retry

_QUESTION_WORDS = (
    "什么", "怎么", "如何", "为什么", "哪里", "谁", "何时", "多少", "是否", "吗", "呢",
)


def is_worth_memorizing(text: str) -> bool:
    """判断一段用户输入是否值得存入长期记忆。

    对齐 Hermes 的思路：只有"事实/知识"值得记住（mem0 由后端 LLM 做事实提取），
    疑问句、过短/过长的输入不存，避免污染记忆库。
    """
    text = (text or "").strip()
    if not text:
        return False
    if len(text) < 4 or len(text) > 200:
        return False
    if text.endswith(("？", "?", "吗", "呢")):
        return False
    if any(word in text for word in _QUESTION_WORDS):
        return False
    return True


def extract_facts_with_llm(
    client,
    messages: list[dict],
    existing: list[str] | None = None,
    model: str | None = None,
) -> list[str]:
    """用 LLM 从一轮对话中提取值得长期记住的事实（对齐 Hermes mem0 的 infer=True）。

    messages 是完成后的 OpenAI 风格消息列表（含工具调用，Hermes 原样传给后端）；
    existing 是已入库的事实，提示模型避免重复提取。
    返回事实列表；提取失败返回空列表，不影响主流程。
    """
    if model is None:
        model = os.environ.get("MODEL", "deepseek-chat")
    convo = "\n".join(
        f"{m.get('role')}: {m.get('content')}"
        for m in (messages or [])[-6:]
        if isinstance(m.get("content"), str) and m.get("content").strip()
    )
    existing_text = "\n".join(f"- {e}" for e in (existing or [])) or "（无）"
    prompt = (
        "从下面的对话中提取值得长期记住的事实/知识"
        "（用户信息、偏好、项目知识等）。\n"
        "要求：\n"
        '- 只输出 JSON 字符串数组，例如：["用户喜欢喝美式咖啡"]\n'
        "- 不要提取一次性信息、疑问句、寒暄、工具执行细节\n"
        "- 每条是一个独立、完整的事实陈述\n"
        "- 与已存在事实重复的不提取\n"
        f"已存在的事实：\n{existing_text}\n\n"
        f"对话：\n{convo}"
    )
    try:
        resp = call_with_retry(
            lambda: client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
            ),
            what="记忆提取",
        )
        text = resp.choices[0].message.content or ""
        match = re.search(r"\[.*\]", text, re.S)
        data = json.loads(match.group(0)) if match else []
        return [str(item).strip() for item in data if str(item).strip()]
    except Exception:
        return []


class MemoryProvider(ABC):
    """外部记忆 provider 的接口契约。

    对齐 Hermes 的 MemoryProvider——骨架代码只依赖这个抽象，
    不知道具体 provider（mem0/honcho/keyword...）的实现。
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """短标识，例如：keyword / mem0 / honcho。"""

    @abstractmethod
    def is_available(self) -> bool:
        """是否配置就绪（只查配置/依赖，不做网络调用）。"""

    @abstractmethod
    def initialize(self, session_id: str = "", **kwargs) -> None:
        """会话启动时初始化（建连接、加载数据等）。"""

    def system_prompt_block(self) -> str:
        """静态信息注入系统提示词；返回空串表示不注入。"""
        return ""

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """每轮召回：根据当前用户消息返回相关上下文文本；无关返回空串。"""
        return ""

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: list[dict] | None = None,
        client=None,
    ) -> None:
        """对话结束后把本轮同步给 provider 做事实提取/存档。

        对齐 Hermes 的 sync_turn：messages 是完成的对话消息列表（含工具调用）；
        client 是本项目的 LLM 客户端，供 provider 做"LLM 提取事实后再入库"
        （Hermes 的 mem0 由后端 infer=True 完成，这里显式传入客户端）。
        """

    def get_tool_schemas(self) -> list[dict]:
        """返回 provider 自带工具的定义（OpenAI function schema 列表）。

        对齐 Hermes 的 get_tool_schemas()——provider 可以给模型提供自己的工具
        （如 mem0 的 mem0_search），这些工具会合并进主工具清单。
        """
        return []

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        """处理 provider 自带工具调用，返回 JSON 字符串结果。"""
        return (
            '{"success": false, "error": "provider 未实现工具 ' + tool_name + '"}'
        )
