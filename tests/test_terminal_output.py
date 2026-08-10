# -*- coding: utf-8 -*-
"""终端输出清洗回归测试（对齐 Hermes tools/ansi_strip.py + terminal_tool.py）。

覆盖：截断（头 40% + 尾 60% + 省略标记）、ANSI 转义剥离、显示文本控制字符
清洗、env 类命令 KEY=value 脱敏、普通命令前缀密钥脱敏、run_terminal 清洗
接线。零依赖，python tests/test_terminal_output.py 直接跑。
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import minimal_agent  # noqa: E402
from ansi_strip import sanitize_display_text, strip_ansi  # noqa: E402
from tool_output_limits import get_max_bytes, truncate_output  # noqa: E402

_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    """记录一条断言结果。"""
    if cond:
        print(f"  ok: {label}")
    else:
        _failures.append(label)
        print(f"  FAIL: {label}")


def test_truncate_output() -> None:
    """截断：短文本原样；长文本保留头 40% + 尾 60% + 省略标记。"""
    check("短文本原样", truncate_output("hello") == "hello")
    check("空文本原样", truncate_output("") == "")
    check("上限默认 50000", get_max_bytes() == 50_000)

    long_text = "A" * 1000 + "B" * 1000
    cut = truncate_output(long_text, max_chars=200)
    check("超长触发截断", "[OUTPUT TRUNCATED" in cut)
    check("保留开头", cut.startswith("A" * 80))
    check("保留结尾", cut.endswith("B" * 120))
    check("省略数字正确", "1800 chars omitted" in cut)


def test_strip_ansi() -> None:
    """剥 ANSI：CSI/OSC 序列删掉，纯文本不动。"""
    check("无转义原样", strip_ansi("plain text") == "plain text")
    check("颜色码剥离", strip_ansi("\x1b[31mred\x1b[0m") == "red")
    check("光标移动剥离", strip_ansi("a\x1b[2Jb") == "ab")
    check("OSC 标题剥离", strip_ansi("\x1b]0;title\x07body") == "body")
    check("C1 控制剥离", strip_ansi("a\x9b") == "a")


def test_sanitize_display_text() -> None:
    """显示清洗：控制字符删掉、CR 归一化为换行、普通文本不动。"""
    check("普通文本原样", sanitize_display_text("你好\n\tworld") == "你好\n\tworld")
    check("转义+控制字符清理", sanitize_display_text("a\x1b[31m\x07b") == "ab")
    check("CRLF 归一化", sanitize_display_text("a\r\nb") == "a\nb")
    check("裸 CR 归一化", sanitize_display_text("a\rb") == "a\nb")
    check("NUL 删除", sanitize_display_text("a\x00b") == "ab")


def test_env_dump_detection() -> None:
    """env 类命令判定：首词匹配，管道/分号拆分，其他命令不误判。"""
    check("env 命中", minimal_agent._is_env_dump_command("env") is True)
    check("set 命中", minimal_agent._is_env_dump_command("set") is True)
    check("管道后命中", minimal_agent._is_env_dump_command("echo hi | printenv") is True)
    check("分号拆分命中", minimal_agent._is_env_dump_command("cd /tmp; export FOO=1") is True)
    check("普通命令不命中", minimal_agent._is_env_dump_command("cat config.py") is False)
    check("空命令不命中", minimal_agent._is_env_dump_command("") is False)
    check("powershell 前缀不命中", minimal_agent._is_env_dump_command("powershell -Command set") is False)


def test_clean_terminal_output() -> None:
    """清洗管线：截断 → 剥 ANSI → 脱敏（env 类走赋值规则，普通命令走 code_file）。"""
    # env 类命令：KEY=value 打码 + ANSI 剥离（键名须含密钥关键词才命中赋值规则）
    env_out = minimal_agent._clean_terminal_output(
        "\x1b[32mDEEPSEEK_API_KEY=abc123\x1b[0m", "printenv"
    )
    check("env 输出无 ANSI", "\x1b" not in env_out)
    check("env 输出密钥打码", "abc123" not in env_out)
    check("env 输出含掩码", "DEEPSEEK_API_KEY=***" in env_out)

    # 普通命令：前缀密钥打码（code_file=True 仍覆盖前缀规则）
    normal_out = minimal_agent._clean_terminal_output(
        "token=sk-abcdef123456xyz", "cat notes.txt"
    )
    check("普通输出前缀密钥打码", "sk-abcdef123456xyz" not in normal_out)

    # 普通命令的 KEY=value 源码常量不误伤（code_file=True 跳过赋值规则）
    src_out = minimal_agent._clean_terminal_output(
        'api_key = "local-dev-value"', "cat config.py"
    )
    check("源码常量不误伤", "local-dev-value" in src_out)

    # 截断在脱敏前生效（默认上限 50000，密钥放在尾部确保截断后仍保留）
    huge = "X" * 51000 + "DEEPSEEK_API_KEY=abc123"
    cut = minimal_agent._clean_terminal_output(huge, "printenv")
    check("截断后含省略标记", "[OUTPUT TRUNCATED" in cut)
    check("截断后密钥打码", "abc123" not in cut)
    check("截断后密钥掩码保留", "DEEPSEEK_API_KEY=***" in cut)


def test_run_terminal_cleaning() -> None:
    """run_terminal 接线：普通安全命令输出不被清洗破坏。"""
    raw = minimal_agent.run_terminal("echo hello", session_key="out-test")
    payload = json.loads(raw)
    check("echo 正常执行", payload.get("exit_code") == 0)
    check("输出保留", payload.get("output") == "hello")


def main() -> None:
    """跑全部断言。"""
    test_truncate_output()
    test_strip_ansi()
    test_sanitize_display_text()
    test_env_dump_detection()
    test_clean_terminal_output()
    test_run_terminal_cleaning()
    if _failures:
        print(f"\n{len(_failures)} 条断言失败：")
        for label in _failures:
            print(f"  - {label}")
        raise SystemExit(1)
    print("\n全部终端输出清洗断言通过")


if __name__ == "__main__":
    main()
