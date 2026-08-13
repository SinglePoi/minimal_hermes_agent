# -*- coding: utf-8 -*-
"""网站访问策略（对齐 Hermes tools/website_policy.py，简化版）。

Hermes 从 ~/.hermes/config.yaml 读取 security.website_blocklist；骨架没有
config.yaml，因此按项目惯例改为环境变量驱动：
    - WEBSITE_POLICY_ENABLED：开关，默认 false（关闭 = 不额外拦截）
    - WEBSITE_POLICY_DENY：禁访域名名单，用「;」分隔，支持 *.domain.com 通配

规则归一化与匹配语义与 Hermes 一致：
    - 域名转小写、去掉末尾点、去掉 www. 前缀
    - 规则 example.com 同时匹配 example.com 与 sub.example.com
    - 规则 *.example.com 用 fnmatch 匹配

策略异常一律 fail-open：解析失败返回 None（放行），不让配置错误拖垮联网工具。
"""

from __future__ import annotations

import fnmatch
import os
from typing import Any, Optional
from urllib.parse import urlparse


class WebsitePolicyError(Exception):
    """网站策略配置格式错误时抛出的异常。"""


def _env_bool(name: str, default: bool) -> bool:
    """读取布尔型环境变量；非法值回退默认。"""
    raw = (os.environ.get(name) or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


def _normalize_host(host: str) -> str:
    """归一化域名：小写、去末尾点。"""
    return (host or "").strip().lower().rstrip(".")


def _normalize_rule(rule: Any) -> Optional[str]:
    """把用户写的规则归一化成纯域名模式；空值/注释返回 None。"""
    if not isinstance(rule, str):
        return None
    value = rule.strip().lower()
    if not value or value.startswith("#"):
        return None
    if "://" in value:
        parsed = urlparse(value)
        value = parsed.netloc or parsed.path
    value = value.split("/", 1)[0].strip().rstrip(".")
    if value.startswith("www."):
        value = value[4:]
    return value or None


def _extract_host_from_urlish(url: str) -> str:
    """从 URL 或「域名/带端口」形式的字符串里提取主机名。"""
    parsed = urlparse(url)
    host = _normalize_host(parsed.hostname or parsed.netloc)
    if host:
        return host

    if "://" not in url:
        schemeless = urlparse(f"//{url}")
        host = _normalize_host(schemeless.hostname or schemeless.netloc)
        if host:
            return host
    return ""


def _match_host_against_rule(host: str, pattern: str) -> bool:
    """判断主机名是否命中某条规则（对齐 Hermes 的匹配语义）。"""
    if not host or not pattern:
        return False
    if pattern.startswith("*."):
        return fnmatch.fnmatch(host, pattern)
    return host == pattern or host.endswith(f".{pattern}")


def get_policy() -> dict[str, Any]:
    """从环境变量加载当前网站访问策略。"""
    enabled = _env_bool("WEBSITE_POLICY_ENABLED", False)
    raw_deny = os.environ.get("WEBSITE_POLICY_DENY") or ""

    rules: list[dict[str, str]] = []
    seen: set[str] = set()
    for chunk in raw_deny.split(";"):
        normalized = _normalize_rule(chunk)
        if normalized and normalized not in seen:
            rules.append({"pattern": normalized, "source": "env"})
            seen.add(normalized)

    return {"enabled": enabled, "rules": rules}


def check_website_access(url: str) -> Optional[dict[str, str]]:
    """检查 URL 是否被网站策略拦截。

    返回 None 表示允许访问；命中名单时返回包含 host / rule / message 的字典。
    策略关闭、域名解析失败或配置异常时返回 None（fail-open）。
    """
    if not url:
        return None

    host = _extract_host_from_urlish(url)
    if not host:
        return None

    policy = get_policy()
    if not policy.get("enabled"):
        return None

    for rule in policy.get("rules", []):
        pattern = rule.get("pattern", "")
        if _match_host_against_rule(host, pattern):
            return {
                "url": url,
                "host": host,
                "rule": pattern,
                "source": rule.get("source", "env"),
                "message": (
                    f"Blocked by website policy: '{host}' matched rule "
                    f"'{pattern}' from {rule.get('source', 'env')}"
                ),
            }
    return None
