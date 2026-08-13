# -*- coding: utf-8 -*-
"""工具结果落盘（对齐 Hermes tools/tool_result_storage.py，简化版）。

三层防线：
1. 单工具输出上限：各工具在返回前自行截断（如 terminal_tool.MAX_OUTPUT_CHARS，
   骨架里由 tool_output_limits.truncate_output 兜底）。
2. 单结果落盘 maybe_persist_tool_result：工具返回后，若结果超过该工具的阈值，
   把完整内容写进磁盘，上下文里只保留 preview + 文件路径，模型需要时可用
   read_file 分段读回。
3. 单轮总预算 enforce_turn_budget：同一轮所有工具结果合计超过预算时，从最大的
   未落盘结果开始逐个溢写到磁盘，直到总字符数回到预算以内。

骨架没有 sandbox env.execute，所以这里直接用 Path.write_text 写到本地结果目录；
写入失败时回退成内联截断，不抛出异常。
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Optional


PERSISTED_OUTPUT_TAG = "<persisted-output>"
PERSISTED_OUTPUT_CLOSING_TAG = "</persisted-output>"
_UNSAFE_RESULT_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")
_MAX_RESULT_FILENAME_STEM = 120

# 永远不落盘的工具：read_file 返回的是分页文本，落盘会造成「读回→再落盘」死循环。
_PINNED_INFINITE_TOOLS = frozenset({"read_file"})


class ToolResultConfig:
    """工具结果落盘配置（不可变；阈值均为字符数）。"""

    def __init__(
        self,
        *,
        enabled: bool = True,
        max_chars: int = 100_000,
        turn_budget: int = 200_000,
        preview_chars: int = 1_500,
        storage_dir: str = "",
    ) -> None:
        """保存落盘配置；storage_dir 留空时使用系统临时目录下的子目录。"""
        self.enabled = enabled
        self.max_chars = max_chars
        self.turn_budget = turn_budget
        self.preview_chars = preview_chars
        self.storage_dir = _resolve_storage_dir(storage_dir)

    def threshold_for(self, tool_name: str) -> int | float:
        """返回指定工具的落盘阈值；read_file 等工具固定为无穷大（永不落盘）。"""
        if tool_name in _PINNED_INFINITE_TOOLS:
            return float("inf")
        return self.max_chars


def _env_bool(name: str, default: bool) -> bool:
    """读取布尔型环境变量；非法值回退默认，避免启动崩溃。"""
    raw = (os.environ.get(name) or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    """读取正整数环境变量；非法值回退默认，避免启动崩溃。"""
    try:
        return max(minimum, int(os.environ.get(name) or default))
    except (TypeError, ValueError):
        return default


def _resolve_storage_dir(raw: str) -> str:
    """解析落盘目录：空值用临时目录，相对路径按项目根目录展开。"""
    if not raw:
        return os.path.join(tempfile.gettempdir(), "minimal-agent-results")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    return str(path)


def get_config() -> ToolResultConfig:
    """从环境变量加载落盘配置（每次读取，便于测试与运行时热更新）。"""
    return ToolResultConfig(
        enabled=_env_bool("TOOL_RESULT_STORAGE_ENABLED", True),
        max_chars=_env_int("TOOL_RESULT_MAX_CHARS", 100_000, 1),
        turn_budget=_env_int("TOOL_RESULT_TURN_BUDGET_CHARS", 200_000, 1),
        preview_chars=_env_int("TOOL_RESULT_PREVIEW_CHARS", 1_500, 1),
        storage_dir=(os.environ.get("TOOL_RESULT_STORAGE_DIR") or "").strip(),
    )


def generate_preview(content: str, max_chars: int = 1_500) -> tuple[str, bool]:
    """在 max_chars 内按最后一个换行符截断，返回 (预览, 是否还有更多)。"""
    if len(content) <= max_chars:
        return content, False
    truncated = content[:max_chars]
    last_nl = truncated.rfind("\n")
    if last_nl > max_chars // 2:
        truncated = truncated[: last_nl + 1]
    return truncated, True


def _safe_result_filename(tool_use_id: str) -> str:
    """把工具调用 ID 转成安全文件名，过长或含非法字符时附加短哈希。"""
    raw_id = str(tool_use_id or "tool_result")
    safe_stem = _UNSAFE_RESULT_FILENAME_CHARS.sub("_", raw_id).strip("._-")
    changed = safe_stem != raw_id

    if not safe_stem:
        safe_stem = "tool_result"
        changed = True

    if changed or len(safe_stem) > _MAX_RESULT_FILENAME_STEM:
        digest = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:12]
        safe_stem = safe_stem[:_MAX_RESULT_FILENAME_STEM].rstrip("._-") or "tool_result"
        safe_stem = f"{safe_stem}_{digest}"

    return f"{safe_stem}.txt"


def _build_persisted_message(
    preview: str,
    has_more: bool,
    original_size: int,
    file_path: str,
) -> str:
    """构造 <persisted-output> 替换块：预览 + 完整结果路径 + 读取提示。"""
    size_kb = original_size / 1024
    if size_kb >= 1024:
        size_str = f"{size_kb / 1024:.1f} MB"
    else:
        size_str = f"{size_kb:.1f} KB"

    msg = f"{PERSISTED_OUTPUT_TAG}\n"
    msg += f"This tool result was too large ({original_size:,} characters, {size_str}).\n"
    msg += f"Full output saved to: {file_path}\n"
    msg += "Use the read_file tool with offset and limit to access specific sections of this output.\n\n"
    msg += f"Preview (first {len(preview)} chars):\n"
    msg += preview
    if has_more:
        msg += "\n..."
    msg += f"\n{PERSISTED_OUTPUT_CLOSING_TAG}"
    return msg


def _write_result(content: str, file_path: str) -> bool:
    """把完整结果写进磁盘；任何 I/O 错误都返回 False，由调用方回退截断。"""
    try:
        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return True
    except Exception:
        return False


def maybe_persist_tool_result(
    content: str,
    tool_name: str = "",
    tool_use_id: str = "",
    *,
    threshold: int | float | None = None,
    config: Optional[ToolResultConfig] = None,
) -> str:
    """第二层：超阈值工具结果落盘，返回 preview + 文件路径的替换块。"""
    cfg = config or get_config()
    if not cfg.enabled:
        return content

    effective_threshold = (
        cfg.threshold_for(tool_name) if threshold is None else threshold
    )
    if effective_threshold == float("inf") or len(content) <= effective_threshold:
        return content

    tool_use_id = tool_use_id or uuid.uuid4().hex
    file_path = os.path.join(cfg.storage_dir, _safe_result_filename(tool_use_id))
    preview, has_more = generate_preview(content, cfg.preview_chars)

    if _write_result(content, file_path):
        return _build_persisted_message(
            preview, has_more, len(content), file_path
        )

    # 落盘失败时退回内联截断，保证内容不消失但长度受限。
    return (
        f"{preview}\n\n"
        f"[Truncated: tool response was {len(content):,} chars. "
        f"Full output could not be saved to disk.]"
    )


def enforce_turn_budget(
    tool_messages: list[dict],
    *,
    config: Optional[ToolResultConfig] = None,
) -> list[dict]:
    """第三层：对单轮所有工具结果执行聚合预算，从最大的未落盘结果开始溢写。"""
    cfg = config or get_config()
    if not cfg.enabled:
        return tool_messages

    candidates: list[tuple[int, int]] = []
    total_size = 0
    for i, msg in enumerate(tool_messages):
        content = msg.get("content", "")
        size = len(content)
        total_size += size
        if PERSISTED_OUTPUT_TAG not in content:
            candidates.append((i, size))

    if total_size <= cfg.turn_budget:
        return tool_messages

    candidates.sort(key=lambda item: item[1], reverse=True)
    for idx, size in candidates:
        if total_size <= cfg.turn_budget:
            break
        msg = tool_messages[idx]
        content = msg["content"]
        tool_use_id = msg.get("tool_call_id", f"budget_{idx}")
        replacement = maybe_persist_tool_result(
            content=content,
            tool_name="__budget_enforcement__",
            tool_use_id=tool_use_id,
            config=cfg,
            threshold=0,
        )
        if replacement != content:
            total_size -= size
            total_size += len(replacement)
            tool_messages[idx]["content"] = replacement

    return tool_messages
