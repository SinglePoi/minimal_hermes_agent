# -*- coding: utf-8 -*-
"""工具输出截断上限（对齐 Hermes tools/tool_output_limits.py，简化版）。

Hermes 的 tool_output.max_bytes 默认 50000（terminal_tool.MAX_OUTPUT_CHARS），
骨架没有 config.yaml，直接沿用默认常量——行为与 Hermes 未配置时完全一致。
"""

from typing import Optional

# 对齐 Hermes terminal_tool.MAX_OUTPUT_CHARS
DEFAULT_MAX_BYTES = 50_000


def get_max_bytes() -> int:
    """返回终端输出上限字符数（对齐 Hermes get_max_bytes，骨架固定默认值）。"""
    return DEFAULT_MAX_BYTES


def truncate_output(text: Optional[str], max_chars: Optional[int] = None) -> str:
    """超长截断：保留头 40% + 尾 60%，中间插省略标记（对齐 Hermes terminal_tool）。

    头多留一点是因为报错信息常出现在前面；尾多留一点是因为最新输出最相关。
    """
    if not text:
        return text or ""
    limit = get_max_bytes() if max_chars is None else max_chars
    if len(text) <= limit:
        return text
    head_chars = int(limit * 0.4)
    tail_chars = limit - head_chars
    omitted = len(text) - head_chars - tail_chars
    notice = (
        f"\n\n... [OUTPUT TRUNCATED - {omitted} chars omitted "
        f"out of {len(text)} total] ...\n\n"
    )
    return text[:head_chars] + notice + text[-tail_chars:]
