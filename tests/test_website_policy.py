# -*- coding: utf-8 -*-
"""
网站访问策略模块的回归测试（零依赖，直接运行）：
    python tests/test_website_policy.py

覆盖（对齐 Hermes tools/website_policy.py）：
    - 规则归一化：带协议、www.、末尾点、注释、空值
    - 主机提取：完整 URL、无协议、带端口
    - 匹配语义：精确、子域、*.通配
    - 环境变量加载：开关与名单解析、去重
    - 策略判定：关闭时放行、开启时命中拦截
    - 与 web_tools 集成：web_fetch 拒绝命中域名、web_search 过滤命中链接
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import website_policy as wp  # noqa: E402
import web_tools  # noqa: E402


_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    """记录一条断言结果。"""
    if cond:
        print(f"  ok: {label}")
    else:
        _failures.append(label)
        print(f"  FAIL: {label}")


def env_patch(**values):
    """临时设置环境变量并在退出后恢复。"""
    class _Ctx:
        def __enter__(self):
            self.saved = {k: os.environ.get(k) for k in values}
            for k, v in values.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            return self

        def __exit__(self, exc_type, exc, tb):
            for k, v in self.saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    return _Ctx()


def test_normalize_rule() -> None:
    """规则归一化：协议、www.、末尾点、注释与空值。"""
    check("空值返回 None", wp._normalize_rule("") is None)
    check("注释返回 None", wp._normalize_rule("# 说明") is None)
    check("普通域名小写", wp._normalize_rule("Example.COM") == "example.com")
    check("去掉末尾点", wp._normalize_rule("example.com.") == "example.com")
    check("去掉 www. 前缀", wp._normalize_rule("www.example.com") == "example.com")
    check("带协议取 netloc", wp._normalize_rule("https://Example.com/path") == "example.com")


def test_extract_host() -> None:
    """主机提取：完整 URL、无协议、带端口。"""
    check("完整 URL", wp._extract_host_from_urlish("https://Example.com/a") == "example.com")
    check("无协议", wp._extract_host_from_urlish("example.com") == "example.com")
    check("带端口", wp._extract_host_from_urlish("example.com:8080") == "example.com")
    check("空值返回空", wp._extract_host_from_urlish("") == "")


def test_match_rule() -> None:
    """匹配语义：精确、子域、*.通配、不误伤相似后缀。"""
    check("精确匹配", wp._match_host_against_rule("example.com", "example.com"))
    check("子域匹配", wp._match_host_against_rule("sub.example.com", "example.com"))
    check("不误伤相似后缀", not wp._match_host_against_rule("notexample.com", "example.com"))
    check("通配匹配子域", wp._match_host_against_rule("a.example.com", "*.example.com"))
    check("通配不匹配裸域", not wp._match_host_against_rule("example.com", "*.example.com"))


def test_get_policy() -> None:
    """环境变量加载：开关解析、名单解析与去重。"""
    with env_patch(
        WEBSITE_POLICY_ENABLED="true",
        WEBSITE_POLICY_DENY="Example.COM;*.company.com;example.com",
    ):
        policy = wp.get_policy()
        check("开关解析为开启", policy["enabled"] is True)
        check("名单解析与去重", [r["pattern"] for r in policy["rules"]] == ["example.com", "*.company.com"])

    with env_patch(WEBSITE_POLICY_ENABLED="off", WEBSITE_POLICY_DENY=""):
        policy = wp.get_policy()
        check("off 解析为关闭", policy["enabled"] is False)
        check("空名单返回空规则", policy["rules"] == [])


def test_check_website_access() -> None:
    """策略判定：关闭放行、开启命中拦截。"""
    with env_patch(WEBSITE_POLICY_ENABLED="false", WEBSITE_POLICY_DENY="example.com"):
        check("关闭时放行", wp.check_website_access("https://example.com/x") is None)

    with env_patch(WEBSITE_POLICY_ENABLED="true", WEBSITE_POLICY_DENY="example.com;*.company.com"):
        blocked = wp.check_website_access("https://sub.example.com/x")
        check("命中规则返回拦截", blocked is not None and blocked["host"] == "sub.example.com")
        check("未命中返回 None", wp.check_website_access("https://ok.org/x") is None)


def test_web_fetch_blocks_policy() -> None:
    """web_fetch 在发起请求前拒绝命中网站策略的域名。"""
    with env_patch(WEBSITE_POLICY_ENABLED="true", WEBSITE_POLICY_DENY="example.com"):
        out = web_tools.web_fetch("https://example.com/page")
        check("返回策略拒绝信息", "网站策略禁止访问" in out and "example.com" in out)


def test_web_search_filters_results() -> None:
    """web_search 过滤搜索结果中命中名单的链接。"""
    def fake_bing(query):
        return [
            ("被禁止", "https://internal.example.com/1", "不应出现"),
            ("允许", "https://ok.org/2", "应保留"),
        ]

    def fake_ddg(query):
        return []

    with env_patch(WEBSITE_POLICY_ENABLED="true", WEBSITE_POLICY_DENY="example.com"):
        original_bing = web_tools._search_bing
        original_ddg = web_tools._search_ddg_html
        web_tools._search_bing = fake_bing
        web_tools._search_ddg_html = fake_ddg
        try:
            out = web_tools.web_search("测试")
            check("保留允许链接", "ok.org" in out)
            check("过滤命中链接", "internal.example.com" not in out)
        finally:
            web_tools._search_bing = original_bing
            web_tools._search_ddg_html = original_ddg


def main() -> None:
    """依次运行全部测试并汇总结果。"""
    print("== 网站访问策略回归测试 ==")
    for test_fn in (
        test_normalize_rule,
        test_extract_host,
        test_match_rule,
        test_get_policy,
        test_check_website_access,
        test_web_fetch_blocks_policy,
        test_web_search_filters_results,
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
