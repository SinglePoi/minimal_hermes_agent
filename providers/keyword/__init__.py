# -*- coding: utf-8 -*-
"""示例外部记忆 provider：本地 JSON 存储 + 关键词 2-gram 召回。

对齐 Hermes 的 plugins/memory/* 插件结构——实现 MemoryProvider 抽象。
Hermes 的 mem0/honcho 用向量语义检索；这里用关键词重叠打分零依赖模拟。
"""

import json
import re
from pathlib import Path

from memory_provider import MemoryProvider, extract_facts_with_llm, is_worth_memorizing

STORE_FILE = Path(__file__).parent / "memory.json"
MAX_ENTRIES = 100
TOP_K = 3

SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "memory_search",
        "description": "搜索 keyword 记忆库，返回所有匹配条目（比自动 prefetch 的 top-3 更全，适合批量回顾）。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
            },
            "required": ["query"],
        },
    },
}


class Provider(MemoryProvider):
    """keyword：知识条目存本地 JSON，按用户消息关键词召回最相关条目。"""

    @property
    def name(self) -> str:
        return "keyword"

    def is_available(self) -> bool:
        return True  # 纯本地、无外部依赖，始终可用

    def initialize(self, session_id: str = "", **kwargs) -> None:
        self._entries: list[str] = []
        if STORE_FILE.exists():
            try:
                data = json.loads(STORE_FILE.read_text(encoding="utf-8"))
                self._entries = [str(e) for e in data] if isinstance(data, list) else []
            except Exception:
                self._entries = []

    def system_prompt_block(self) -> str:
        return "## 外部记忆（keyword provider）\n你可以使用我召回的知识条目。"

    def get_tool_schemas(self) -> list[dict]:
        """provider 自带工具（对齐 Hermes 的 mem0_search——模型可主动搜索记忆库）。"""
        return [SEARCH_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        if tool_name != "memory_search":
            return json.dumps({"success": False, "error": f"未知工具 {tool_name}"}, ensure_ascii=False)
        query = str((args or {}).get("query", ""))
        tokens = self._tokenize(query)
        scored = [(len(tokens & self._tokenize(entry)), entry) for entry in self._entries]
        hits = [entry for score, entry in sorted(scored, key=lambda x: -x[0]) if score > 0]
        return json.dumps(
            {"success": True, "query": query, "count": len(hits), "results": hits[:10]},
            ensure_ascii=False,
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        tokens = self._tokenize(query)
        scored = [(len(tokens & self._tokenize(entry)), entry) for entry in self._entries]
        hits = [entry for score, entry in sorted(scored, key=lambda x: -x[0]) if score > 0][:TOP_K]
        return "\n".join(f"- {entry}" for entry in hits)

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: list[dict] | None = None,
        client=None,
    ) -> None:
        """对话结束同步：用 LLM 从本轮提取事实再入库（对齐 Hermes mem0 的 infer=True）。

        有 client 时走 LLM 提取；没有时退化为启发式过滤（只存值得记住的原话）。
        """
        if client is not None:
            facts = extract_facts_with_llm(client, messages or [], existing=self._entries)
        else:
            text = (user_content or "").strip()
            facts = [text] if is_worth_memorizing(text) else []
        changed = False
        for fact in facts:
            if fact and fact not in self._entries:
                self._entries.append(fact)
                changed = True
        if changed:
            self._entries = self._entries[-MAX_ENTRIES:]
            STORE_FILE.write_text(
                json.dumps(self._entries, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        tokens = set()
        for word in re.findall(r"[a-zA-Z0-9_]+", text.lower()):
            tokens.add(word)
        for run in re.findall(r"[\u4e00-\u9fff]+", text):
            tokens.update(run[i : i + 2] for i in range(len(run) - 1))
        return tokens
