# -*- coding: utf-8 -*-
"""
用户自定义 deny 规则的回归测试（零依赖，直接运行）：
    python tests/test_approval_deny.py

覆盖（对齐 Hermes approvals.deny 的 fnmatch glob 语义）：
    - 未配置规则 -> 不拦截
    - glob 匹配（大小写不敏感）与 ; 分隔多条规则
    - deny 先于永久允许列表：允许列表也拦不住 deny
    - deny 先于 APPROVAL_MODE=off：旁路也绕不过
    - 返回结构：user_deny=True + BLOCKED + 明确"不要重试"
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

import approval  # noqa: E402
from approval import (  # noqa: E402
    _match_user_deny_rule,
    _user_deny_block_result,
    approve_permanent,
    check_dangerous_command,
)


_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    """记录一条断言结果。"""
    if cond:
        print(f"  ok: {label}")
    else:
        _failures.append(label)
        print(f"  FAIL: {label}")


def test_match_rules() -> None:
    """规则匹配：未配置、glob、大小写、多条。"""
    original = os.environ.get("APPROVAL_DENY")
    try:
        os.environ.pop("APPROVAL_DENY", None)
        check("未配置规则 -> 不拦截",
              _match_user_deny_rule("rm -rf build") is None)

        os.environ["APPROVAL_DENY"] = "rm -rf *;git push --force*"
        check("glob 命中 rm -rf build",
              _match_user_deny_rule("rm -rf build") == "rm -rf *")
        check("glob 命中 git push --force main",
              _match_user_deny_rule("git push --force main") == "git push --force*")
        check("大小写不敏感", _match_user_deny_rule("RM -RF BUILD") == "rm -rf *")
        check("不匹配的命令放行", _match_user_deny_rule("git status") is None)
    finally:
        os.environ.pop("APPROVAL_DENY", None)
        if original is not None:
            os.environ["APPROVAL_DENY"] = original


def test_deny_wins_over_allowlist_and_off() -> None:
    """deny 先于允许列表与 mode=off：都拦不住。"""
    original_deny = os.environ.get("APPROVAL_DENY")
    original_mode = os.environ.get("APPROVAL_MODE")
    try:
        os.environ["APPROVAL_DENY"] = "rm -rf *"
        os.environ["APPROVAL_MODE"] = "off"
        approval._permanent_approved.add("delete in root path")
        result = check_dangerous_command("rm -rf build", "sess-deny", None)
        check("deny 拦截（即使 mode=off + 允许列表）",
              result["approved"] is False and result.get("user_deny") is True)
        check("拦截消息含 BLOCKED 与 Do NOT retry",
              "BLOCKED" in result["message"] and "Do NOT retry" in result["message"])
        check("拦截消息带规则名", "rm -rf *" in result["message"])
    finally:
        os.environ.pop("APPROVAL_DENY", None)
        os.environ.pop("APPROVAL_MODE", None)
        approval._permanent_approved.discard("delete in root path")
        if original_deny is not None:
            os.environ["APPROVAL_DENY"] = original_deny
        if original_mode is not None:
            os.environ["APPROVAL_MODE"] = original_mode


def test_block_result_shape() -> None:
    """_user_deny_block_result 的返回结构。"""
    result = _user_deny_block_result("no-curl")
    check("结构完整", result["approved"] is False
          and result["user_deny"] is True
          and "no-curl" in result["message"])


def main() -> None:
    """依次运行全部测试并汇总结果。"""
    print("== 用户 deny 规则回归测试 ==")
    for test_fn in (
        test_match_rules,
        test_deny_wins_over_allowlist_and_off,
        test_block_result_shape,
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
