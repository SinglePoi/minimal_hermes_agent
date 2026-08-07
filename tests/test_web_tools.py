# -*- coding: utf-8 -*-
"""联网工具回归测试（零依赖，直接运行）：
    python tests/test_web_tools.py

覆盖：
    - web_search：DuckDuckGo HTML 解析（标题/真实 URL/摘要）、limit、空关键词、无结果、请求失败
    - web_fetch：HTML → 可读文本（去 script/style/标签）、截断、charset 识别、请求失败
    - SSRF 防护：file/ftp 协议、localhost、回环/私网/链路本地/未指定/保留/组播 IP 全部拒绝
    - run_tool 分发：web_search / web_fetch 经主程序 run_tool 可用，并进并行白名单
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import minimal_agent  # noqa: E402
import web_tools  # noqa: E402
from tool_dispatch import _PARALLEL_SAFE_TOOLS  # noqa: E402


_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    """记录一条断言结果。"""
    if cond:
        print(f"  ok: {label}")
    else:
        _failures.append(label)
        print(f"  FAIL: {label}")


class FakeResponse:
    """模拟 urlopen 响应（可迭代 with、read(n)、headers）。"""

    def __init__(self, body: bytes, content_type: str = "text/html; charset=utf-8"):
        self._body = body
        self.headers = {"Content-Type": content_type}

    def read(self, n: int = -1) -> bytes:
        if n == -1 or n >= len(self._body):
            return self._body
        return self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


_DDG_HTML = (
    '<div class="result results_links_deep">'
    '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage&amp;rut=1">'
    "示例标题 <b>加粗</b></a>"
    '<a class="result__snippet" href="//duckduckgo.com/l/?uddg=x">这是一段摘要内容</a>'
    "</div>"
    '<div class="result">'
    '<a class="result__a" href="https://example.org/">第二个结果</a>'
    '<a class="result__snippet" href="https://example.org/">第二段摘要</a>'
    "</div>"
)

_FETCH_HTML = (
    "<html><head><title>页面</title><script>var bad=1;</script>"
    "<style>body{color:red}</style></head>"
    "<body><h1>网页标题</h1><p>正文第一段。</p><p>正文第二段。</p>"
    "<script>alert(1)</script></body></html>"
)

_BING_RSS = (
    '<?xml version="1.0"?><rss version="2.0"><channel>'
    "<item><title>宁波市（浙江省辖地级市）_百度百科</title>"
    "<link>https://baike.baidu.com/item/%E5%AE%81%E6%B3%A2%E5%B8%82/590</link>"
    "<description>宁波市是浙江省下辖地级市…</description></item>"
    "<item><title>宁波市人民政府</title>"
    "<link>https://www.ningbo.gov.cn/</link>"
    "<description>宁波市人民政府官方网站</description></item>"
    "</channel></rss>"
)


def _patch_urlopen(fn):
    """临时替换 web_tools._urlopen，返回原函数供 finally 恢复。"""
    original = web_tools._urlopen
    web_tools._urlopen = fn
    return original


def test_web_search_parse() -> None:
    """DuckDuckGo HTML 解析：标题/真实 URL/摘要/条数。"""
    original = _patch_urlopen(lambda req: FakeResponse(_DDG_HTML.encode("utf-8")))
    try:
        out = web_tools.web_search("示例", limit=5)
        check("包含标题", "示例标题" in out)
        check("还原真实 URL", "https://example.com/page" in out)
        check("包含摘要", "这是一段摘要内容" in out)
        check("包含第二个结果", "第二个结果" in out)
        check("标注条数", "共 2 条结果" in out)
    finally:
        web_tools._urlopen = original


def test_web_search_bing_primary() -> None:
    """必应 RSS 优先：有结果时不再尝试 DuckDuckGo（只调一次 _urlopen）。"""
    calls: list[str] = []

    def fake(req):
        calls.append(req.full_url)
        return FakeResponse(_BING_RSS.encode("utf-8"))

    original = _patch_urlopen(fake)
    try:
        out = web_tools.web_search("宁波天气", limit=5)
        check("必应结果标题", "宁波市（浙江省辖地级市）_百度百科" in out)
        check("必应结果链接", "https://baike.baidu.com" in out)
        check("必应结果摘要", "浙江省下辖地级市" in out)
        check("必应优先只调一次", len(calls) == 1)
        check("请求的是必应", "cn.bing.com" in calls[0])
    finally:
        web_tools._urlopen = original


def test_web_search_all_providers_fail() -> None:
    """全部源失败：错误信息带上各来源原因。"""

    def boom(req):
        raise TimeoutError("超时")

    original = _patch_urlopen(boom)
    try:
        out = web_tools.web_search("x")
        check("错误含来源名", "必应" in out and "DuckDuckGo" in out)
        check("错误含原因", "超时" in out)
    finally:
        web_tools._urlopen = original


def test_web_search_limits_and_errors() -> None:
    """limit、空关键词、无结果、请求失败。"""
    original = _patch_urlopen(lambda req: FakeResponse(_DDG_HTML.encode("utf-8")))
    try:
        out = web_tools.web_search("x", limit=1)
        check("limit=1 只列第一条", "第二个结果" not in out)
        check("空关键词报错", "不能为空" in web_tools.web_search("  "))
    finally:
        web_tools._urlopen = original

    def boom(req):
        raise ConnectionError("网络不通")

    original = _patch_urlopen(boom)
    try:
        check("请求失败返回错误信息", "搜索失败" in web_tools.web_search("x"))
    finally:
        web_tools._urlopen = original

    original = _patch_urlopen(lambda req: FakeResponse(b"<html>no results</html>"))
    try:
        check("无结果提示", "未找到相关结果" in web_tools.web_search("x"))
    finally:
        web_tools._urlopen = original


def test_web_fetch_strip_and_truncate() -> None:
    """去 script/style/标签、截断、charset、请求失败。"""
    original = _patch_urlopen(lambda req: FakeResponse(_FETCH_HTML.encode("utf-8")))
    try:
        out = web_tools.web_fetch("https://example.com/", max_chars=2000)
        check("包含正文", "网页标题" in out and "正文第一段" in out)
        check("去掉 script", "alert(1)" not in out and "var bad" not in out)
        check("去掉 style", "color:red" not in out)
        check("无残留标签", "<" not in out)
    finally:
        web_tools._urlopen = original

    original = _patch_urlopen(lambda req: FakeResponse(("正文" * 150).encode("utf-8")))
    try:
        out = web_tools.web_fetch("https://example.com/", max_chars=200)
        check("截断生效", "已截断" in out and len(out) < 400)
    finally:
        web_tools._urlopen = original

    gbk_body = "中文标题".encode("gbk")
    original = _patch_urlopen(
        lambda req: FakeResponse(gbk_body, content_type="text/html; charset=gbk")
    )
    try:
        check("识别 charset=gbk", "中文标题" in web_tools.web_fetch("https://example.com/"))
    finally:
        web_tools._urlopen = original

    def boom(req):
        raise TimeoutError("超时")

    original = _patch_urlopen(boom)
    try:
        check("抓取失败返回错误信息", "抓取失败" in web_tools.web_fetch("https://example.com/"))
    finally:
        web_tools._urlopen = original


def test_ssrf_guard() -> None:
    """只允许 http/https 公网；本地/内网/保留地址全部拒绝。"""
    blocked = [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "http://localhost:8000/",
        "http://127.0.0.1/",
        "http://0.0.0.0/",
        "http://10.0.0.5/",
        "http://172.16.0.1/",
        "http://192.168.1.5/",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/",
        "http://example.com.evil.localhost/x",
    ]
    for url in blocked:
        check(f"拒绝 {url}", "拒绝访问" in web_tools.web_fetch(url))
    check("公网 http 允许", web_tools._is_allowed_url("https://example.com/"))
    check("公网 https 允许", web_tools._is_allowed_url("https://sub.example.com/x"))


def test_run_tool_dispatch() -> None:
    """run_tool 分发 + 并行白名单。"""
    original = _patch_urlopen(lambda req: FakeResponse(_DDG_HTML.encode("utf-8")))
    try:
        out = minimal_agent.run_tool("web_search", {"query": "示例"})
        check("run_tool 分发 web_search", "示例标题" in out)
    finally:
        web_tools._urlopen = original

    original = _patch_urlopen(lambda req: FakeResponse(_FETCH_HTML.encode("utf-8")))
    try:
        out = minimal_agent.run_tool(
            "web_fetch", {"url": "https://example.com/", "max_chars": 500}
        )
        check("run_tool 分发 web_fetch", "正文第一段" in out)
    finally:
        web_tools._urlopen = original

    check("web_search 进并行白名单", "web_search" in _PARALLEL_SAFE_TOOLS)
    check("web_fetch 进并行白名单", "web_fetch" in _PARALLEL_SAFE_TOOLS)
    schemas = [t["function"]["name"] for t in minimal_agent.TOOLS]
    check("web_search 注册进 TOOLS", "web_search" in schemas)
    check("web_fetch 注册进 TOOLS", "web_fetch" in schemas)


def main() -> None:
    """依次运行全部测试并汇总结果。"""
    print("== 联网工具回归测试 ==")
    for test_fn in (
        test_web_search_parse,
        test_web_search_bing_primary,
        test_web_search_all_providers_fail,
        test_web_search_limits_and_errors,
        test_web_fetch_strip_and_truncate,
        test_ssrf_guard,
        test_run_tool_dispatch,
    ):
        print(f"[{test_fn.__name__}]")
        test_fn()
    print()
    if _failures:
        print(f"共 {len(_failures)} 个用例失败：")
        for label in _failures:
            print(f"  - {label}")
        sys.exit(1)
    print("全部用例通过 ✅")


if __name__ == "__main__":
    main()
