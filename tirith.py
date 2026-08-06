# -*- coding: utf-8 -*-
"""
内容级安全扫描（Hermes tools/tirith_security.py 的 Python 简化版）

Hermes 的 tirith 是一个外部二进制扫描器，检测正则认不出的"内容级"威胁：
同形字域名、管道到解释器、终端注入等。骨架不依赖二进制，用纯 Python
实现同类的核心检查：
    - 终端注入：ANSI 转义序列、控制字符（可能把内容藏起来骗人）
    - 隐形字符：零宽字符、双向覆盖符（RLO，可把文字顺序倒过来）
    - 回车注入：单独出现的 \\r（日志/终端篡改）
    - 同形字域名：域名里混入西里尔/希腊等易混淆字符（钓鱼）
    - 管道到解释器：把内容喂给 python/node/perl/ruby/php（不限于 shell）

返回契约对齐 Hermes：{"action": "allow"|"warn"|"block", "findings": [...], "summary": str}
    - block 发现 -> 需要审批（像命中危险模式一样）
    - warn 发现  -> 同样进入审批，但描述标注为警告
    - 扫描器自身异常按 TIRITH_FAIL_OPEN 处理（默认放行，对齐 Hermes）

简化掉的部分（Hermes 有，骨架不做）：外部二进制、自动安装、熔断器、
平台检测、JSON 富化输出。
"""

import os
import re
from typing import Any

# 开关在导入时快照（对齐 Hermes 的 tirith_enabled；默认开启）
TIRITH_ENABLED = os.getenv("TIRITH_ENABLED", "true").lower() in {
    "1", "true", "yes", "on",
}
# 扫描器自身异常时：true = 放行（fail-open，Hermes 默认）；false = 拦截
TIRITH_FAIL_OPEN = os.getenv("TIRITH_FAIL_OPEN", "true").lower() in {
    "1", "true", "yes", "on",
}

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff]")
_BIDI_RE = re.compile(r"[\u202a-\u202e]")
_CR_ALONE_RE = re.compile(r"(?<![\r\n])\r(?![\r\n])")
_URL_RE = re.compile(r"https?://([^/\s'\"]+)", re.IGNORECASE)
# 同形字常见来源：西里尔、希腊、希伯来、阿拉伯等非 ASCII 字母区段
_CONFUSABLE_RE = re.compile(r"[\u0370-\u06ff]")
_PIPE_INTERPRETER_RE = re.compile(
    r"\|\s*(?:python[0-9.]*|node|perl|ruby|php)\b", re.IGNORECASE
)


def _finding(finding_type: str, severity: str, description: str) -> dict[str, str]:
    """构造一条发现。"""
    return {"type": finding_type, "severity": severity, "description": description}


def check_command_security(command: str | None) -> dict[str, Any]:
    """内容级扫描一条命令，返回 {"action", "findings", "summary"}（对齐 Hermes 契约）。

    action = block（有拦截级发现）/ warn（只有警告级发现）/ allow。
    扫描器内部异常按 TIRITH_FAIL_OPEN 处理：默认放行，绝不因扫描器自身问题卡住执行。
    """
    if not TIRITH_ENABLED:
        return {"action": "allow", "findings": [], "summary": ""}
    if not isinstance(command, str) or not command:
        return {"action": "allow", "findings": [], "summary": ""}

    findings: list[dict[str, str]] = []
    try:
        # 1. 终端注入：ANSI 转义序列 / 控制字符（可能隐藏内容或篡改显示）
        if _ANSI_RE.search(command):
            findings.append(_finding(
                "terminal_injection", "block",
                "命令含 ANSI 转义序列（可能隐藏内容或篡改终端显示）",
            ))
        if _CTRL_RE.search(command):
            findings.append(_finding(
                "control_chars", "block",
                "命令含控制字符（可能隐藏内容）",
            ))
        # 2. 隐形字符：零宽 / 双向覆盖符（RLO 可倒序显示文字）
        if _ZERO_WIDTH_RE.search(command) or _BIDI_RE.search(command):
            findings.append(_finding(
                "invisible_chars", "block",
                "命令含零宽字符或双向覆盖符（混淆/隐藏文字）",
            ))
        # 3. 回车注入：单独出现的 \r（日志/终端篡改）
        if _CR_ALONE_RE.search(command):
            findings.append(_finding(
                "cr_injection", "block",
                "命令含单独回车符（日志/终端篡改风险）",
            ))
        # 4. 同形字域名：域名里混入西里尔/希腊等易混淆字符（钓鱼）
        for match in _URL_RE.finditer(command):
            host = match.group(1).rstrip(".,;")
            if _CONFUSABLE_RE.search(host):
                findings.append(_finding(
                    "homograph_url", "block",
                    f"URL 域名含易混淆的非 ASCII 字符（同形字钓鱼风险）：{host}",
                ))
                break
        # 5. 管道到解释器：把内容喂给脚本解释器（不限于 shell）
        if _PIPE_INTERPRETER_RE.search(command):
            findings.append(_finding(
                "pipe_to_interpreter", "warn",
                "命令把输出管道给脚本解释器（python/node/perl/ruby/php）",
            ))
    except Exception:
        if TIRITH_FAIL_OPEN:
            return {"action": "allow", "findings": [],
                    "summary": "tirith scanner error (fail-open)"}
        return {"action": "block",
                "findings": [_finding("scanner_error", "block", "扫描器自身异常（fail-closed）")],
                "summary": "tirith scanner error (fail-closed)"}

    if any(f["severity"] == "block" for f in findings):
        action = "block"
    elif findings:
        action = "warn"
    else:
        action = "allow"
    summary = findings[0]["description"] if findings else ""
    return {"action": action, "findings": findings, "summary": summary}
