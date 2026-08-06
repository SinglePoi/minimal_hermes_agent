# -*- coding: utf-8 -*-
"""外部记忆 provider 编排（对齐 Hermes 的 agent/memory_manager.py）。

Hermes 的 MemoryManager：builtin（MEMORY.md/USER.md）恒在 + 至多一个外部 provider。
我们项目的内置记忆仍由 minimal_agent.py 直接管理，本管理器只负责外部 provider。

同步采用后台异步 + 合并节流（对齐 Hermes sync_all 的"单 worker 串行、不阻塞
主流程"设计）：
    - sync_all() 只把任务交给后台 worker，立即返回——慢的 provider 永远不会
      卡住对话结尾（Hermes 记录过一个 Hindsight daemon 阻塞 298s 的案例）
    - 单 worker 串行执行：第 N 轮的写入先于第 N+1 轮，provider 无需自己保证顺序
    - 合并节流：快速连发多轮时，worker 还没开始跑的旧任务会被最新任务覆盖
      （messages 是累计的全量对话，最新一次同步覆盖所有历史，不丢数据）
    - 后台线程是 daemon：即使同步卡死也不会阻止解释器退出
"""

import ast
import importlib.util
import json
import os
import sys
import threading
import time
from pathlib import Path

from memory_provider import MemoryProvider

BASE_DIR = Path(__file__).parent


class SyncWorker:
    """单线程后台同步 worker：串行 + 合并节流 + 可排空。

    - submit(fn)：把任务交给 worker；若上一个任务还没开始执行，直接覆盖它
      （合并节流——最新的任务携带全量 messages，覆盖不丢数据）
    - flush(timeout)：等当前任务和待执行任务排空；超时放弃，不阻塞退出
    - shutdown()：投递哨兵让线程退出（幂等）
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending = None
        self._active = False
        self._event = threading.Event()
        self._closed = False
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="mem-sync"
        )
        self._thread.start()

    def submit(self, fn) -> bool:
        """投递任务；worker 关闭时返回 False（调用方自行决定兜底）。"""
        with self._lock:
            if self._closed:
                return False
            self._pending = fn
            self._event.set()
            return True

    def _loop(self) -> None:
        """消费循环：取最新任务执行；哨兵（None）退出。"""
        while True:
            self._event.wait()
            with self._lock:
                fn = self._pending
                self._pending = None
                self._event.clear()
                self._active = fn is not None
            if fn is None:
                return
            try:
                fn()
            except Exception:
                pass
            finally:
                with self._lock:
                    self._active = False

    def flush(self, timeout: float | None = None) -> None:
        """等待后台任务排空（无 pending 且无正在执行的任务）。"""
        deadline = time.monotonic() + timeout if timeout is not None else None
        while True:
            with self._lock:
                idle = self._pending is None and not self._active
            if idle:
                return
            if deadline is not None and time.monotonic() >= deadline:
                return
            time.sleep(0.02)

    def shutdown(self) -> None:
        """关闭 worker：拒绝新任务并投递哨兵（幂等）。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._pending = None
            self._event.set()


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


def list_provider_plugins() -> list[dict]:
    """列出 providers/ 目录下的 memory provider 插件（对齐 Hermes plugins 目录约定）。

    每条含 name、description（模块 docstring 首行）与 active（是否当前 MEMORY_PROVIDER）。
    """
    providers_dir = BASE_DIR / "providers"
    if not providers_dir.exists():
        return []
    active = os.environ.get("MEMORY_PROVIDER", "").strip()
    plugins: list[dict] = []
    for child in sorted(providers_dir.iterdir()):
        init_file = child / "__init__.py"
        if not child.is_dir() or not init_file.exists():
            continue
        description = ""
        try:
            tree = ast.parse(init_file.read_text(encoding="utf-8"))
            doc = ast.get_docstring(tree)
            if doc:
                description = doc.strip().splitlines()[0].strip()
        except (OSError, SyntaxError):
            pass
        plugins.append(
            {
                "name": child.name,
                "description": description,
                "active": child.name == active,
            }
        )
    return plugins


class MemoryManager:
    """编排外部记忆 provider：prefetch / system prompt / sync 全部 fan-out。"""

    def __init__(self) -> None:
        self._providers: list[MemoryProvider] = []
        self._tool_to_provider: dict[str, MemoryProvider] = {}
        self._sync_worker = SyncWorker()

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
        """对话结束后异步同步给所有 provider（对齐 Hermes：后台串行，不阻塞主流程）。

        任务交给单线程后台 worker 立即返回；worker 关闭时回退为内联执行
        （保证行为不丢，对齐 Hermes 的 fail-safe 回退）。
        """
        providers = list(self._providers)
        if not providers:
            return

        def _run() -> None:
            """后台执行：逐个 provider 同步，单个失败不影响其它。"""
            for provider in providers:
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

        if not self._sync_worker.submit(_run):
            # worker 已关闭（进程退出阶段）：回退内联，尽量不丢这次同步
            try:
                _run()
            except Exception:
                pass

    def flush_pending(self, timeout: float | None = None) -> None:
        """等待后台同步排空（会话结束/退出前调用；超时放弃，不阻塞退出）。"""
        self._sync_worker.flush(timeout)

    def commit_memory_session(
        self,
        messages: list[dict],
        client=None,
    ) -> None:
        """压缩/轮换边界：同步提取当前对话的记忆并落库（对齐 Hermes commit_memory_session）。

        与 sync_all 的关键区别：
        - sync_all 是后台异步（不阻塞主流程），适合"每轮结束"的常规同步
        - commit_memory_session 是**同步执行**——必须在原文被压缩摘要掉之前
          完成提取，否则中间轮次的信息以后就只剩摘要了（Hermes 在
          conversation_compression.py 里于重写 transcript 之前调用它）

        Hermes 走 provider.on_session_end()，骨架复用 provider.sync_turn()
        （我们的提取逻辑就在 sync_turn 里，传空 user_content + 全量 messages，
        有 client 时走 LLM 提取，无 client 时启发式过滤自然跳过）。
        传入的是 messages 快照副本，避免后续压缩就地改写影响提取。
        """
        providers = list(self._providers)
        if not providers:
            return
        snapshot = list(messages)
        for provider in providers:
            try:
                provider.sync_turn(
                    "",
                    "",
                    session_id="",
                    messages=snapshot,
                    client=client,
                )
            except Exception:
                continue

    def shutdown(self) -> None:
        """停止接收新同步任务并退出后台线程（幂等，进程退出前调用）。"""
        self._sync_worker.shutdown()

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
