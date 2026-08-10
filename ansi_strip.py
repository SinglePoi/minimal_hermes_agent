# -*- coding: utf-8 -*-
"""ANSI 转义序列清洗（对齐 Hermes tools/ansi_strip.py）。

strip_ansi() 用在终端工具把输出返回给模型之前——防止 ANSI 代码进入模型上下文，
进而被模型抄进文件写入（Hermes 定位的根因）。

覆盖完整 ECMA-48：CSI（含私有模式 ? 前缀、冒号分隔参数、中间字节）、
OSC（BEL 与 ST 两种终止符）、DCS/SOS/PM/APC 字符串、nF 多字节转义、
Fp/Fe/Fs 单字节转义、8-bit C1 控制字符。
"""

import re

_ANSI_ESCAPE_RE = re.compile(
    r"\x1b"
    r"(?:"
        r"\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]"     # CSI sequence
        r"|\][\s\S]*?(?:\x07|\x1b\\)"                  # OSC (BEL or ST terminator)
        r"|[PX^_][\s\S]*?(?:\x1b\\)"                   # DCS/SOS/PM/APC strings
        r"|[\x20-\x2f]+[\x30-\x7e]"                    # nF escape sequences
        r"|[\x30-\x7e]"                                # Fp/Fe/Fs single-byte
    r")"
    r"|\x9b[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]"       # 8-bit CSI
    r"|\x9d[\s\S]*?(?:\x07|\x9c)"                       # 8-bit OSC
    r"|[\x80-\x9f]",                                    # Other 8-bit C1 controls
    re.DOTALL,
)

# 快速路径：文本里没有 ESC/C1 字节就不跑完整正则
_HAS_ESCAPE = re.compile(r"[\x1b\x80-\x9f]")

# C0 控制字符（除 tab/换行/回车单独处理外）+ DEL。strip_ansi() 只删"成形的
# 转义序列"，这些裸控制字符仍可能捣乱（BEL 响铃、退格/DEL 覆盖、NUL 截断）。
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# sanitize_display_text 的快速路径：任何 C0 控制（除 tab/换行）、CR、ESC、C1
_HAS_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def strip_ansi(text: str) -> str:
    """去掉文本里的 ANSI 转义序列（无转义时原样快速返回）。"""
    if not text or not _HAS_ESCAPE.search(text):
        return text
    return _ANSI_ESCAPE_RE.sub("", text)


def sanitize_display_text(text: str) -> str:
    """清洗存储/外部文本再回显到终端：删转义序列 + 裸控制字符。

    只保留换行与 tab（回车归一化为换行，防止 \\r 覆盖式欺骗隐藏内容）。
    历史消息回显（--resume 摘要等）前应先过这里：夹带转义的文本不得能清屏、
    改窗口标题、移光标或重排相邻 UI（对齐 Hermes，参考 openai/codex#31494）。
    """
    if not text or not _HAS_CONTROL.search(text):
        return text
    text = strip_ansi(text)
    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    return _CONTROL_CHARS_RE.sub("", text)
