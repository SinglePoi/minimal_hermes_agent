# -*- coding: utf-8 -*-
"""
工具并行执行模块（对齐 Hermes agent/tool_dispatch_helpers.py + agent/tool_executor.py）

对应关系：
    - _plan_tool_batch_segments()       → Hermes 同名函数：把一批 tool_calls 切成
      有序的 ("parallel", [...]) / ("sequential", [...]) 段
    - _should_parallelize_tool_batch()  → Hermes 同名函数：整批是否可并行的薄视图
    - _extract_parallel_scope_paths() / _paths_overlap()
        → Hermes 同名函数：路径作用域与子树重叠检测（为后续文件工具预留）
    - execute_tool_calls_segmented()    → Hermes agent/tool_executor.py 同名函数：
      parallel 段用线程池并发执行，sequential 段串行，结果按原始顺序回填

并行安全规则（与 Hermes 一致）：
    - 只读、无共享可变状态的工具（_PARALLEL_SAFE_TOOLS）可并行
    - 其余工具（memory 写记忆文件、terminal 有副作用）一律按顺序屏障处理
    - 路径作用域工具（未来 read_file / write_file 等）按子树重叠决定能否并行：
      读者↔读者重叠无害；任何含写者的重叠都会关闭当前并行段
    - 单元素并行段降级为顺序，相邻顺序段合并

简化掉的部分：
    - _is_destructive_command()（Hermes 用于 terminal checkpoint，骨架无 checkpoint 系统）
    - MCP 工具并行安全查询（骨架无 MCP）
    - 中断语义与 turn 级 budget/steer 收尾（Hermes 在 executor 里做）
"""

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Callable, Optional

# 对齐 Hermes run_agent._MAX_TOOL_WORKERS
MAX_TOOL_WORKERS = 8

# 永远不并行的工具（Hermes 有 {"clarify"}；骨架暂无交互工具，保留空集占位）
_NEVER_PARALLEL_TOOLS = frozenset()

# 只读、无共享可变状态的工具 → 可并行
_PARALLEL_SAFE_TOOLS = frozenset({
    "session_search",
    "memory_search",
    "vector_search",
    "skills_list",
    "skill_view",
    "get_current_time",
    "web_search",
    "web_fetch",
})

# 路径作用域工具：按目标路径重叠决定能否并行（对齐 Hermes 的集合）
_PATH_SCOPED_READERS = frozenset({"read_file", "search_files"})
_PATH_SCOPED_WRITERS = frozenset({"write_file", "patch"})
_PATH_SCOPED_TOOLS = _PATH_SCOPED_READERS | _PATH_SCOPED_WRITERS


# =========================================================================
# 工具调用访问器（兼容 openai SDK 对象与 model_dump 后的 dict 两种形态）
# =========================================================================
def _function_of(tc: Any) -> Any:
    """从工具调用里取出 function 字段（SDK 对象或 dict）。"""
    return tc.get("function") if isinstance(tc, dict) else getattr(tc, "function", None)


def tool_call_id(tc: Any) -> str:
    """返回工具调用 ID（对齐 Hermes 的 tc.id，兼容 dict 形态）。"""
    if isinstance(tc, dict):
        return str(tc.get("id", ""))
    return str(getattr(tc, "id", ""))


def tool_name(tc: Any) -> str:
    """返回工具名（对齐 Hermes 的 tc.function.name）。"""
    fn = _function_of(tc)
    if isinstance(fn, dict):
        return str(fn.get("name", ""))
    return str(getattr(fn, "name", ""))


def tool_arguments(tc: Any) -> Optional[dict]:
    """解析工具参数为 dict。

    对齐 Hermes：解析失败或非 dict 返回 None，调用方须按顺序屏障处理。
    某些 SDK 直接给 dict 参数，也兼容。
    """
    fn = _function_of(tc)
    raw = fn.get("arguments") if isinstance(fn, dict) else getattr(fn, "arguments", "{}")
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


# =========================================================================
# 路径作用域与重叠检测（对齐 Hermes，为文件工具预留）
# =========================================================================
def _canonical_path(raw_path: str, execution_cwd: Optional[Path] = None) -> Path:
    """规范化路径：展开 ~、解析符号链接、统一大小写（对齐 Hermes _canonical_path）。"""
    expanded = Path(raw_path).expanduser()
    base = execution_cwd if execution_cwd is not None else Path.cwd()
    candidate = expanded if expanded.is_absolute() else base / expanded
    resolved = os.path.normcase(os.path.realpath(os.path.abspath(str(candidate))))
    return Path(resolved)


def _extract_parallel_scope_paths(
    tool_name: str,
    function_args: dict,
    execution_cwd: Optional[Path] = None,
) -> list[Path]:
    """提取工具调用保留的路径作用域（对齐 Hermes 同名函数）。

    read_file / search_files 按 path 参数（search_files 缺省为当前目录）以读者身份
    保留；write_file / patch 以写者身份保留。patch 的 V4A 模式从补丁头解析目标文件。
    """
    if tool_name not in _PATH_SCOPED_TOOLS:
        return []
    raw_paths: list[str] = []
    path_arg = function_args.get("path")
    if isinstance(path_arg, str) and path_arg.strip():
        raw_paths.append(path_arg)
    elif tool_name == "search_files":
        # search_files 缺省搜索根是当前目录——预留整棵子树
        raw_paths.append(".")
    if tool_name == "patch" and (function_args.get("mode") or "replace") == "patch":
        body = function_args.get("patch") or ""
        if isinstance(body, str) and body:
            for m in re.finditer(r"^\*\*\*\s*(?:Update|Add|Delete)\s+File:\s*(.+)$",
                                 body, re.MULTILINE):
                p = m.group(1).strip()
                if p:
                    raw_paths.append(p)
            for m in re.finditer(r"^\*\*\*\s*Move\s+File:\s*(.+?)\s*->\s*(.+)$",
                                 body, re.MULTILINE):
                for p in (m.group(1).strip(), m.group(2).strip()):
                    if p:
                        raw_paths.append(p)
    scoped: list[Path] = []
    seen: set[str] = set()
    for raw in raw_paths:
        if not raw:
            continue
        canonical = _canonical_path(raw, execution_cwd)
        key = str(canonical)
        if key in seen:
            continue
        seen.add(key)
        scoped.append(canonical)
    return scoped


def _paths_overlap(left: Path, right: Path) -> bool:
    """判断两条（已规范化的）路径是否可能指向同一子树（对齐 Hermes _paths_overlap）。"""
    left_parts = left.parts
    right_parts = right.parts
    if not left_parts or not right_parts:
        return bool(left_parts) == bool(right_parts) and bool(left_parts)
    common_len = min(len(left_parts), len(right_parts))
    return left_parts[:common_len] == right_parts[:common_len]


# =========================================================================
# 批分段规划（Hermes _plan_tool_batch_segments 的忠实移植）
# =========================================================================
def _plan_tool_batch_segments(
    tool_calls: list[Any],
    *,
    execution_cwd: Optional[Path] = None,
) -> list[tuple[str, list[Any]]]:
    """把工具调用切成有序的 parallel / sequential 段。

    kind 为 "parallel"（一段可并发的安全调用）或 "sequential"（必须按序执行的
    屏障调用）。段保留模型原始调用顺序：后面的调用绝不会越过前面的屏障，
    因此工具结果回填顺序与全串行执行完全一致。
    """
    segments: list[list] = []
    current: list = []
    reserved_paths: list[tuple[Path, bool]] = []

    def _close_parallel() -> None:
        """关闭当前并行段（若有），清空路径预约。"""
        nonlocal current, reserved_paths
        if current:
            segments.append(["parallel", current])
            current = []
            reserved_paths = []

    def _add_sequential(tc: Any) -> None:
        """把调用追加到（或新开）当前顺序段。"""
        _close_parallel()
        if segments and segments[-1][0] == "sequential":
            segments[-1][1].append(tc)
        else:
            segments.append(["sequential", [tc]])

    for tool_call in tool_calls:
        name = tool_name(tool_call)

        if name in _NEVER_PARALLEL_TOOLS:
            _add_sequential(tool_call)
            continue

        function_args = tool_arguments(tool_call)
        if not isinstance(function_args, dict):
            # 参数解析失败 / 非 dict → 顺序屏障（与 Hermes 一致）
            _add_sequential(tool_call)
            continue

        if name in _PATH_SCOPED_TOOLS:
            scoped_paths = _extract_parallel_scope_paths(
                name, function_args, execution_cwd=execution_cwd
            )
            if not scoped_paths:
                _add_sequential(tool_call)
                continue
            is_writer = name in _PATH_SCOPED_WRITERS
            if any(
                (is_writer or existing_is_writer)
                and _paths_overlap(scoped_path, existing)
                for scoped_path in scoped_paths
                for existing, existing_is_writer in reserved_paths
            ):
                # 与本段已有预约冲突：关闭本段，让该调用等上一段落地后再开新段。
                # 读者↔读者重叠不冲突（并发读同一子树可交换）。
                _close_parallel()
            reserved_paths.extend((p, is_writer) for p in scoped_paths)
            current.append(tool_call)
            continue

        if name in _PARALLEL_SAFE_TOOLS:
            current.append(tool_call)
            continue

        # 其余工具（memory / terminal 等）→ 顺序屏障
        _add_sequential(tool_call)

    _close_parallel()

    # 单元素并行段降级为顺序；相邻顺序段合并
    normalized: list[list] = []
    for kind, calls in segments:
        if kind == "parallel" and len(calls) < 2:
            kind = "sequential"
        if normalized and normalized[-1][0] == "sequential" and kind == "sequential":
            normalized[-1][1].extend(calls)
        else:
            normalized.append([kind, calls])
    return [(kind, calls) for kind, calls in normalized]


def _should_parallelize_tool_batch(tool_calls: list[Any]) -> bool:
    """整批是否可并行（Hermes 语义：规划器只产出单一 all-parallel 段才算）。"""
    if len(tool_calls) <= 1:
        return False
    segments = _plan_tool_batch_segments(tool_calls)
    return len(segments) == 1 and segments[0][0] == "parallel"


# =========================================================================
# 分段执行器（Hermes execute_tool_calls_segmented 的简化移植）
# =========================================================================
def _safe_run(tc: Any, run_one: Callable[[Any], str]) -> str:
    """执行单个工具调用；异常兜底为错误文本，避免拖垮整批。"""
    try:
        return run_one(tc)
    except Exception as exc:
        return f"执行失败：{exc}"


def _append_tool_result(tc: Any, messages: list[dict], content: str) -> None:
    """按原始顺序把工具结果回填进消息历史（对齐 Hermes 的逐条回填）。"""
    messages.append({
        "role": "tool",
        "tool_call_id": tool_call_id(tc),
        "content": content,
    })


def _cancelled_result() -> str:
    """中断时未执行工具的结果（对齐 Hermes executor 的 _cancelled_tool_result）。"""
    return json.dumps(
        {
            "success": False,
            "error": "Tool execution cancelled by user interrupt",
            "status": "cancelled",
        },
        ensure_ascii=False,
    )


def _execute_parallel(
    calls: list[Any],
    messages: list[dict],
    run_one: Callable[[Any], str],
    interrupt_event: Any = None,
) -> None:
    """并发执行一段 parallel-safe 工具：线程池 + 结果按原顺序回填；支持中断。

    中断语义（对齐 Hermes executor）：
    - 等待期间轮询 interrupt_event（0.2s 间隔）
    - 置位后取消未启动的 future，给运行中的工具最多 3s 优雅退出
    - 未完成/已取消的工具回填 {"status": "cancelled"}，不阻塞主流程
    """
    max_workers = min(len(calls), MAX_TOOL_WORKERS)
    executor = ThreadPoolExecutor(max_workers=max_workers)
    futures = {executor.submit(_safe_run, tc, run_one): tc for tc in calls}
    pending = set(futures)
    interrupted = False
    while pending:
        if interrupt_event is not None and interrupt_event.is_set():
            interrupted = True
            break
        done, pending = wait(pending, timeout=0.2)
    if interrupted:
        for future in pending:
            future.cancel()
        # 给运行中的工具一个优雅退出窗口（对齐 Hermes 的 3s grace）
        wait(pending, timeout=3.0)
        # 放弃卡住的线程：不 join（wait=False），避免整个回合被拖死
        executor.shutdown(wait=False)
    else:
        executor.shutdown(wait=True)
    results: dict[int, str] = {}
    for future, tc in futures.items():
        if future.cancelled() or not future.done():
            results[id(tc)] = _cancelled_result()
        else:
            try:
                results[id(tc)] = future.result()
            except Exception:
                results[id(tc)] = "执行失败"
    for tc in calls:
        _append_tool_result(tc, messages, results.get(id(tc), _cancelled_result()))


def execute_tool_calls_segmented(
    tool_calls: list[Any],
    messages: list[dict],
    run_one: Callable[[Any], str],
    *,
    on_segment: Optional[Callable[[str, list[Any]], None]] = None,
    interrupt_event: Any = None,
) -> None:
    """按计划分段执行：parallel 并发、sequential 串行，结果按原始顺序回填。

    on_segment(kind, calls) 在每段执行前回调（用于 UI 提示，如"并行执行 N 个工具"）。
    interrupt_event 置位时：未执行的调用跳过并回填 cancelled 结果，进行中的并行段
    取消 pending future（对齐 Hermes 的 pre-flight 检查 + 等待轮询 + 3s 优雅退出）。
    """
    if interrupt_event is not None and interrupt_event.is_set():
        for tc in tool_calls:
            _append_tool_result(tc, messages, _cancelled_result())
        return
    for kind, calls in _plan_tool_batch_segments(tool_calls):
        if interrupt_event is not None and interrupt_event.is_set():
            for tc in calls:
                _append_tool_result(tc, messages, _cancelled_result())
            continue
        if on_segment is not None:
            on_segment(kind, calls)
        if kind == "parallel":
            _execute_parallel(calls, messages, run_one, interrupt_event)
        else:
            for tc in calls:
                if interrupt_event is not None and interrupt_event.is_set():
                    _append_tool_result(tc, messages, _cancelled_result())
                    continue
                _append_tool_result(tc, messages, _safe_run(tc, run_one))
