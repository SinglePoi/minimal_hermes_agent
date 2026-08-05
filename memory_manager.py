# -*- coding: utf-8 -*-
"""外部记忆 provider 编排（对齐 Hermes 的 agent/memory_manager.py）。

Hermes 的 MemoryManager：builtin（MEMORY.md/USER.md）恒在 + 至多一个外部 provider。
我们项目的内置记忆仍由 minimal_agent.py 直接管理，本管理器只负责外部 provider。
"""

import importlib.util
import json
import sys
from pathlib import Path

from memory_provider import MemoryProvider

BASE_DIR = Path(__file__).parent


def build_memory_context_block(raw_context: str) -> str:
    """把召回的记忆包进围栏并加系统说明（对齐 Hermes 的 build_memory_context_block）。"""
    if not raw_context or not raw_context.strip():
        return ""
    return (
        "<memory-context>\n"
        "[System note: The following is recalled memory context, "
        "NOT new user input. Treat as authoritative reference data — "
        "this is the agent's persistent memory and should inform all responses.]\n\n"
        f"{raw_context}\n"
        "</memory-context>"
    )


def load_provider(name: str) -> MemoryProvider:
    """动态加载 providers/<name>/__init__.py 里的 Provider 类。

    对齐 Hermes 的 plugins/memory/__init__.py load_memory_provider——
    按名字扫描插件目录并动态 import。
    """
    module_path = BASE_DIR / "providers" / name / "__init__.py"
    if not module_path.exists():
        raise FileNotFoundError(f"未找到 provider 插件：providers/{name}/")
    if str(BASE_DIR) not in sys.path:
        sys.path.insert(0, str(BASE_DIR))
    spec = importlib.util.spec_from_file_location(f"providers.{name}", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"providers.{name}"] = module
    spec.loader.exec_module(module)
    return module.Provider()


class MemoryManager:
    """编排外部记忆 provider：prefetch / system prompt / sync 全部 fan-out。"""

    def __init__(self) -> None:
        self._providers: list[MemoryProvider] = []
        self._tool_to_provider: dict[str, MemoryProvider] = {}

    def add_provider(self, provider: MemoryProvider) -> None:
        self._providers.append(provider)
        # 建立"工具名 → provider"路由表（对齐 Hermes 的 add_provider）
        for schema in provider.get_tool_schemas():
            name = schema.get("function", {}).get("name")
            if name:
                self._tool_to_provider[name] = provider

    def providers(self) -> list[MemoryProvider]:
        return list(self._providers)

    def build_system_prompt(self) -> str:
        """收集所有 provider 的静态提示词块（对齐 Hermes 的 build_system_prompt）。"""
        blocks = []
        for provider in self._providers:
            try:
                block = provider.system_prompt_block()
                if block and block.strip():
                    blocks.append(block)
            except Exception:
                continue
        return "\n\n".join(blocks)

    def prefetch_all(self, query: str, *, session_id: str = "") -> str:
        """每轮召回：遍历 provider 收集相关上下文（单个失败不阻断）。"""
        parts = []
        for provider in self._providers:
            try:
                result = provider.prefetch(query, session_id=session_id) or ""
                if result.strip():
                    parts.append(result)
            except Exception:
                continue
        return "\n\n".join(parts)

    def sync_all(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: list[dict] | None = None,
        client=None,
    ) -> None:
        """对话结束后同步给所有 provider（对齐 Hermes 的 sync_all）。"""
        for provider in self._providers:
            try:
                provider.sync_turn(
                    user_content,
                    assistant_content,
                    session_id=session_id,
                    messages=messages,
                    client=client,
                )
            except Exception:
                continue

    def get_all_tool_schemas(self) -> list[dict]:
        """收集所有 provider 的自带工具定义，按名字去重（对齐 Hermes 的 get_all_tool_schemas）。"""
        schemas: list[dict] = []
        seen: set[str] = set()
        for provider in self._providers:
            try:
                for schema in provider.get_tool_schemas():
                    name = schema.get("function", {}).get("name")
                    if name and name not in seen:
                        schemas.append(schema)
                        seen.add(name)
            except Exception:
                continue
        return schemas

    def has_tool(self, tool_name: str) -> bool:
        """是否有 provider 处理这个工具（对齐 Hermes 的 has_tool）。"""
        return tool_name in self._tool_to_provider

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        """把工具调用路由给对应 provider，返回 JSON 字符串（对齐 Hermes 的 handle_tool_call）。"""
        provider = self._tool_to_provider.get(tool_name)
        if provider is None:
            return json.dumps(
                {"success": False, "error": f"没有 provider 处理工具 {tool_name}"},
                ensure_ascii=False,
            )
        try:
            return provider.handle_tool_call(tool_name, args, **kwargs)
        except Exception as exc:
            return json.dumps(
                {"success": False, "error": f"memory 工具 {tool_name} 失败：{exc}"},
                ensure_ascii=False,
            )
