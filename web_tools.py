# -*- coding: utf-8 -*-
"""联网工具：web_search（搜索）+ web_fetch（抓网页），零第三方依赖。

对齐 Hermes `plugins/web/` 的联网能力思路（tavily / searxng / firecrawl 等），
骨架按惯例简化为标准库实现：
  - web_search：多源链式回退——必应 RSS（中国大陆可达性好，优先）→ DuckDuckGo HTML 兜底；
    无需 API key，正则解析标题/链接/摘要；全部失败时错误信息带上各来源原因
  - web_fetch：抓取网页正文，粗略去 script/style/标签、自动识别 charset、截断

安全（防 SSRF）：
  - 只允许 http/https 公网地址；拒绝 file/ftp 等协议
  - 拒绝 localhost、回环/私网/链路本地/保留/组播 IP（域名解析到内网无法预检，简化）

环境变量：无（零依赖、零密钥；超时与上限为代码常量）。
"""

import html
import ipaddress
import re
import urllib.parse
import urllib.request

_BING_URL = "https://cn.bing.com/search"
_DDG_URL = "https://html.duckduckgo.com/html/"
_TIMEOUT_SECONDS = 8
_MAX_BYTES = 1024 * 1024  # web_fetch 最多读 1MB
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# DuckDuckGo HTML 结果块：标题链接 + 摘要链接
_RESULT_RE = re.compile(
    r'<div class="result[^"]*".*?'
    r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?'
    r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
    re.S,
)

# 必应 RSS 输出：<item> 里的标题/链接/描述（format=rss 免 JS 渲染）
_ITEM_RE = re.compile(
    r"<item>.*?<title>(.*?)</title>.*?<link>(.*?)</link>.*?"
    r"<description>(.*?)</description>",
    re.S,
)

_PRIVATE_HOSTS = ("localhost",)


def _urlopen(req: urllib.request.Request) -> object:
    """打开 URL（独立函数便于测试 monkeypatch；超时统一 _TIMEOUT_SECONDS）。"""
    return urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS)


def _is_allowed_url(url: str) -> bool:
    """只允许 http/https 公网地址（防 SSRF，拒绝 file/ftp 与内网/回环）。"""
    try:
        parts = urllib.parse.urlparse(url)
        if parts.scheme not in ("http", "https"):
            return False
        host = (parts.hostname or "").lower()
        if not host:
            return False
        if host in _PRIVATE_HOSTS or host.endswith(".localhost"):
            return False
        try:
            ip = ipaddress.ip_address(host)
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_unspecified
                or ip.is_reserved
                or ip.is_multicast
            ):
                return False
        except ValueError:
            pass  # 域名走 DNS 解析，无法在请求前校验解析结果（简化）
        return True
    except ValueError:
        return False


def _clean_ddg_url(href: str) -> str:
    """把 DuckDuckGo 的跳转链接还原成真实 URL（uddg 参数）。"""
    match = re.search(r"[?&]uddg=([^&]+)", href)
    if match:
        return urllib.parse.unquote(match.group(1))
    if href.startswith("//"):
        return "https:" + href
    return href


def _strip_tags(text: str) -> str:
    """去掉 HTML 标签并反转义实体。"""
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = text.replace("]]>", "")  # 清理 RSS CDATA 尾部标记
    return html.unescape(text).strip()


def _parse_ddg_results(html_text: str) -> list[tuple[str, str, str]]:
    """从 DuckDuckGo HTML 里提取 (标题, URL, 摘要) 结果列表。"""
    results = []
    for href, title, snippet in _RESULT_RE.findall(html_text):
        results.append(
            (
                _strip_tags(title),
                _clean_ddg_url(html.unescape(href)),
                _strip_tags(snippet),
            )
        )
    return results


def _search_bing(query: str) -> list[tuple[str, str, str]]:
    """必应 RSS 搜索（中国大陆可达性好，作为首选源）：解析 (标题, 链接, 摘要)。"""
    params = urllib.parse.urlencode({"q": query, "format": "rss"})
    req = urllib.request.Request(
        f"{_BING_URL}?{params}", headers={"User-Agent": _UA}
    )
    with _urlopen(req) as resp:
        body = resp.read(400000).decode("utf-8", errors="replace")
    return [
        (_strip_tags(title), link.strip(), _strip_tags(desc))
        for title, link, desc in _ITEM_RE.findall(body)
    ]


def _search_ddg_html(query: str) -> list[tuple[str, str, str]]:
    """DuckDuckGo HTML 搜索（兜底源）：解析 (标题, 真实链接, 摘要)。"""
    data = urllib.parse.urlencode({"q": query}).encode("utf-8")
    req = urllib.request.Request(
        _DDG_URL, data=data, headers={"User-Agent": _UA}
    )
    with _urlopen(req) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    return _parse_ddg_results(body)


def _format_results(query: str, results: list[tuple[str, str, str]], limit: int) -> str:
    """把结果列表格式化成模型可读文本（共 N 条，展示前 limit 条）。"""
    lines = [f"关键词：{query}", f"共 {len(results)} 条结果："]
    for index, (title, url, snippet) in enumerate(results[:limit], 1):
        lines.append(f"{index}. {title}\n   {url}\n   {snippet}")
    return "\n".join(lines)


def web_search(query: str, limit: int = 5) -> str:
    """联网搜索：必应 RSS 优先、DuckDuckGo 兜底，返回标题/链接/摘要。

    单个源失败/无结果会自动尝试下一个；全部失败时返回带各来源原因的可读错误，
    而不是抛错中断 Agent Loop。
    """
    query = (query or "").strip()
    if not query:
        return "错误：搜索关键词不能为空"
    try:
        limit = max(1, min(int(limit), 10))
    except (TypeError, ValueError):
        limit = 5
    errors: list[str] = []
    for source, fetch in (("必应", _search_bing), ("DuckDuckGo", _search_ddg_html)):
        try:
            results = fetch(query)
        except Exception as exc:
            errors.append(f"{source} {exc}")
            continue
        if results:
            return _format_results(query, results, limit)
    if errors:
        return "搜索失败：" + "；".join(errors)
    return f"关键词：{query}\n未找到相关结果"


def _html_to_text(html_text: str) -> str:
    """粗略把 HTML 转成可读文本：去 script/style/标签、折叠空白。"""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html_text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def web_fetch(url: str, max_chars: int = 4000) -> str:
    """抓取网页正文：去标签后返回可读文本（对齐 Hermes 网页工具，零依赖简化版）。

    只允许 http/https 公网地址（SSRF 防护）；最多读 1MB、返回截断到 max_chars。
    """
    url = (url or "").strip()
    if not _is_allowed_url(url):
        return f"拒绝访问：仅允许 http/https 公网地址（{url}）"
    try:
        max_chars = max(200, min(int(max_chars), 20000))
    except (TypeError, ValueError):
        max_chars = 4000
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with _urlopen(req) as resp:
            raw = resp.read(_MAX_BYTES)
            charset = "utf-8"
            match = re.search(
                r"charset=([\w-]+)", resp.headers.get("Content-Type", "")
            )
            if match:
                charset = match.group(1)
            html_text = raw.decode(charset, errors="replace")
    except Exception as exc:
        return f"抓取失败：{exc}"
    text = _html_to_text(html_text)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n…（已截断，全文 {len(text)} 字符）"
    return text or "页面无可读文本"
