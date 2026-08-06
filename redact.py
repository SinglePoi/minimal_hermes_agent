# -*- coding: utf-8 -*-
"""
敏感文本脱敏模块（对齐 Hermes agent/redact.py）

职责：在文本到达"人眼 / 模型 / 日志"之前，把密钥、令牌、密码打码。
覆盖模式：
    - 已知前缀密钥（sk-、ghp_、github_pat_、AIza、AKIA、glpat- 等）
    - 环境变量赋值（DEEPSEEK_API_KEY=xxx、PASSWORD=xxx）
    - JSON 字段（"apiKey": "xxx"）与 YAML/配置键（password: xxx）
    - Authorization 请求头、JWT、私钥块、URL 里的 user:pass@

两种打码风格（与 Hermes 一致）：
    - _mask_token：保留头 6 / 尾 4 字符（适合日志与审批展示，方便辨认）
    - file_read=True 的哨兵：整段替换成 «redacted:sk-…»（不可复用、不泄露字节，
      防止模型读了打码值再写回文件，把密钥毁成假值——对齐 Hermes 的教训）

简化掉的部分（Hermes 有，骨架不做）：
    - URL 查询参数打码（Hermes 默认关闭）、手机号、DB 连接串专项
    - 每个模式家族的子串预检加速（骨架量小，直接跑正则）
"""

import os
import re
from typing import Optional

# 开关在导入时快照（对齐 Hermes：防止运行中被动态关闭，那是提权路径）
_REDACT_ENABLED = os.getenv("HERMES_REDACT_SECRETS", "true").lower() in {
    "1", "true", "yes", "on",
}

# 已知密钥前缀（Hermes _PREFIX_PATTERNS 的精简子集，覆盖常见供应商 + DeepSeek）
_PREFIX_PATTERNS = [
    r"sk-[A-Za-z0-9_-]{10,}",          # OpenAI / DeepSeek 等
    r"ghp_[A-Za-z0-9]{10,}",           # GitHub PAT
    r"github_pat_[A-Za-z0-9_]{10,}",
    r"gho_[A-Za-z0-9]{10,}",           # GitHub OAuth
    r"ghu_[A-Za-z0-9]{10,}",
    r"ghs_[A-Za-z0-9]{10,}",
    r"ghr_[A-Za-z0-9]{10,}",
    r"AIza[A-Za-z0-9_-]{30,}",         # Google API key
    r"AKIA[A-Z0-9]{16}",               # AWS Access Key ID
    r"xox[baprs]-[A-Za-z0-9-]{10,}",   # Slack token
    r"SG\.[A-Za-z0-9_-]{10,}",         # SendGrid
    r"hf_[A-Za-z0-9]{10,}",            # HuggingFace
    r"pypi-[A-Za-z0-9_-]{10,}",        # PyPI
    r"glpat-[A-Za-z0-9_\-]{10,}",      # GitLab PAT
    r"sk_live_[A-Za-z0-9]{10,}",       # Stripe
    r"sk_test_[A-Za-z0-9]{10,}",
]
_PREFIX_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(" + "|".join(_PREFIX_PATTERNS) + r")(?![A-Za-z0-9_-])"
)
# 打码标签（哨兵里保留供应商前缀，便于辨认是哪类密钥）
_PREFIX_LABELS = (
    "github_pat_", "sk_live_", "sk_test_", "glpat-",
    "sk-", "ghp_", "gho_", "ghu_", "ghs_", "ghr_", "AIza", "AKIA",
    "xox", "SG.", "hf_", "pypi-",
)

# 环境变量赋值：KEY=value（键名必须含密钥关键词）
_ENV_ASSIGN_RE = re.compile(
    r"\b([A-Z][A-Z0-9_]*(?:API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)"
    r"[A-Z0-9_]*)\s*=\s*(['\"]?)([^\s&'\"]+)\2"
)
# JSON 字段："apiKey": "value"
_JSON_FIELD_RE = re.compile(
    r'("(?:api_?[Kk]ey|token|secret|password|access_token|refresh_token|'
    r'auth_token|bearer|private_key|client_secret)")\s*:\s*"([^"]+)"'
)
# YAML/配置键：password: value（带关键词门禁，避免普通文本误伤）
_CFG_SECRET_WORD_RE = re.compile(
    r"(?:api[ _.\-]?key|token|secret|passwd|password|credential|auth)", re.IGNORECASE
)
_YAML_ASSIGN_RE = re.compile(
    r"\b(api[ _.\-]?key|token|secret|passwd|password|credential|auth)"
    r"\s*[:=]\s*(['\"]?)([^\s&'\"]+)\2",
    re.IGNORECASE,
)
# Authorization 请求头：保留认证方式，打码令牌
_AUTH_HEADER_RE = re.compile(
    r"(Authorization|Proxy-Authorization)\s*:\s*(Bearer|Basic|Token)\s+"
    r"([A-Za-z0-9._~+/=-]+)",
    re.IGNORECASE,
)
# 私钥块：整体替换（内容不可复用）
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
# JWT
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
# URL 里的 user:pass@：只打码密码
_URL_USERINFO_RE = re.compile(r"(://[^:/@\s]+):([^@\s]+)@")


def mask_secret(
    value: str,
    *,
    head: int = 4,
    tail: int = 4,
    floor: int = 12,
    placeholder: str = "***",
    empty: str = "",
) -> str:
    """打码一个密钥：保留头尾少量字符便于辨认，太短则整体替换（对齐 Hermes）。"""
    if not value:
        return empty
    if len(value) < floor:
        return placeholder
    return f"{value[:head]}...{value[-tail:]}"


def _mask_token(token: str) -> str:
    """日志/展示用打码：保留头 6 / 尾 4，短于 18 整体替换（对齐 Hermes）。"""
    if not token:
        return "***"
    return mask_secret(token, head=6, tail=4, floor=18)


def _mask_token_nonreusable(token: str) -> str:
    """文件内容用打码：替换成不可复用哨兵，保留供应商前缀标签（对齐 Hermes）。"""
    if not token:
        return "«redacted-secret»"
    for label in sorted(_PREFIX_LABELS, key=len, reverse=True):
        if token.startswith(label):
            return f"«redacted:{label}…»"
    return "«redacted-secret»"


def _redact_env(m: re.Match) -> str:
    """赋值类规则的回调：跳过 os.getenv 这类『引用变量名』的代码片段。"""
    name, quote, value = m.group(1), m.group(2), m.group(3)
    if "getenv" in value or ("(" in value and ")" in value):
        return m.group(0)
    return f"{name}={quote}{_mask_token(value)}{quote}"


def redact_sensitive_text(
    text: Optional[str],
    *,
    force: bool = False,
    code_file: bool = False,
    file_read: bool = False,
) -> Optional[str]:
    """对文本做敏感信息打码；没有命中的文本原样返回。

    对齐 Hermes redact_sensitive_text 的关键语义：
    - 默认开启（HERMES_REDACT_SECRETS=true，导入时快照）
    - force=True 时无视开关强制打码（安全边界必须打码）
    - file_read=True 时前缀密钥用不可复用哨兵（防模型把打码值写回文件）
    - code_file=True 跳过 KEY=value / JSON 字段规则（避免源码常量误伤）；
      file_read 隐含 code_file（配置/数据文件同理）
    """
    if text is None:
        return None
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return text
    if not (force or _REDACT_ENABLED):
        return text
    if file_read:
        code_file = True

    # 1. 已知前缀密钥（sk- 等）
    if any(p in text for p in ("sk-", "ghp_", "AIza", "AKIA", "glpat-", "xox", "SG.", "hf_", "pypi-")):
        replacer = _mask_token_nonreusable if file_read else _mask_token
        text = _PREFIX_RE.sub(lambda m: replacer(m.group(1)), text)

    # 2. 环境变量 / JSON / YAML（源码文件跳过，避免常量误伤）
    if not code_file:
        if "=" in text:
            text = _ENV_ASSIGN_RE.sub(_redact_env, text)
        if ":" in text and '"' in text:
            text = _JSON_FIELD_RE.sub(
                lambda m: f'{m.group(1)}: "{_mask_token(m.group(2))}"', text
            )
        if ":" in text and "://" not in text and _CFG_SECRET_WORD_RE.search(text):
            text = _YAML_ASSIGN_RE.sub(_redact_env, text)

    # 3. 请求头 / 私钥 / JWT / URL userinfo（任何场景都打码）
    if "Authorization" in text or "authorization" in text:
        text = _AUTH_HEADER_RE.sub(
            lambda m: f"{m.group(1)}: {m.group(2)} {_mask_token(m.group(3))}", text
        )
    if "PRIVATE KEY" in text:
        text = _PRIVATE_KEY_RE.sub("«redacted:private-key…»", text)
    if "eyJ" in text:
        text = _JWT_RE.sub(lambda m: _mask_token(m.group(0)), text)
    if "://" in text and "@" in text:
        text = _URL_USERINFO_RE.sub(r"\1:***@", text)
    return text
