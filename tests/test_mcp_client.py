# -*- coding: utf-8 -*-
"""MCP 客户端回归测试（零依赖，python tests/test_mcp_client.py 直接跑）

用 tests/fake_mcp_server.py 做真实 stdio 子进程端到端验证：
    - 配置加载：缺失/非法 JSON/字段校验/${VAR} 插值/安全环境过滤
    - 连接与工具发现：mcp__ 前缀命名、schema 转换、分页兜底
    - 工具调用：成功文本、isError、超时、命令不存在
    - 输出截断 + 脱敏
    - 并行白名单：声明 supports_parallel_tool_calls 才可并行，默认串行
    - 与 minimal_agent 的集成：run_tool 分发、get_tools、available_tool_names
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import mcp_client  # noqa: E402
import tool_dispatch  # noqa: E402
import minimal_agent  # noqa: E402

_failures: list[str] = []
_FAKE_SERVER = str(Path(__file__).resolve().parent / "fake_mcp_server.py")


def check(label: str, cond: bool) -> None:
    """记录一条断言结果。"""
    if cond:
        print(f"  ok: {label}")
    else:
        _failures.append(label)
        print(f"  FAIL: {label}")


def _demo_config(tmp: Path, **overrides) -> Path:
    """写一份指向假 MCP 服务器的配置（timeout/parallel 等可用 overrides 覆盖）。"""
    config = {
        "command": sys.executable,
        "args": [_FAKE_SERVER],
        "timeout": 5,
        "connect_timeout": 5,
    }
    config.update(overrides)
    path = tmp / "mcp_servers.json"
    path.write_text(
        json.dumps({"demo": config}, ensure_ascii=False), encoding="utf-8"
    )
    return path


def _activate(path: Path) -> None:
    """重置单例并指向指定配置文件。"""
    mcp_client.shutdown_mcp()
    os.environ["MCP_SERVERS_PATH"] = str(path)


def _deactivate() -> None:
    """清理环境并关闭全部 MCP 子进程（测试隔离）。"""
    os.environ.pop("MCP_SERVERS_PATH", None)
    mcp_client.shutdown_mcp()


def test_config_loading() -> None:
    """配置加载：缺失/非法 JSON/字段校验。"""
    tmp = tempfile.TemporaryDirectory()
    try:
        os.environ.pop("MCP_SERVERS_PATH", None)
        servers, messages = mcp_client._load_servers_config()
        check("无配置文件 -> 空服务器列表", servers == {} and messages == [])

        bad = Path(tmp.name) / "mcp_servers.json"
        bad.write_text("{not json", encoding="utf-8")
        os.environ["MCP_SERVERS_PATH"] = str(bad)
        servers, messages = mcp_client._load_servers_config()
        check("非法 JSON -> 空列表 + 提示", servers == {} and messages)

        mixed = Path(tmp.name) / "mixed.json"
        mixed.write_text(
            json.dumps(
                {
                    "ok": {"command": "cmd", "args": ["a"]},
                    "no-cmd": {"args": ["a"]},
                    "bad-args": {"command": "cmd", "args": "x"},
                    "bad-env": {"command": "cmd", "env": []},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        os.environ["MCP_SERVERS_PATH"] = str(mixed)
        servers, messages = mcp_client._load_servers_config()
        check("合法条目保留", "ok" in servers)
        check("缺 command 跳过", "no-cmd" not in servers)
        check("args 非数组跳过", "bad-args" not in servers)
        check("env 非对象跳过", "bad-env" not in servers)
        check("跳过均有提示", len(messages) >= 3)

        # UTF-8 BOM 兼容（PowerShell/记事本保存的 JSON 常带 BOM）
        bom_path = Path(tmp.name) / "bom.json"
        bom_path.write_bytes(b"\xef\xbb\xbf" + json.dumps(
            {"ok": {"command": "cmd", "args": ["a"]}}, ensure_ascii=False
        ).encode("utf-8"))
        os.environ["MCP_SERVERS_PATH"] = str(bom_path)
        servers, _ = mcp_client._load_servers_config()
        check("UTF-8 BOM 配置可加载", "ok" in servers)
    finally:
        _deactivate()
        tmp.cleanup()


def test_env_interpolation_and_filter() -> None:
    """${VAR} 插值 + 安全环境过滤（密钥不传给子进程）。"""
    os.environ["MCP_TEST_TOKEN"] = "interp-ok"
    os.environ["DEEPSEEK_API_KEY"] = "sk-should-not-leak-1234567890"
    tmp = tempfile.TemporaryDirectory()
    try:
        interpolated = mcp_client._interpolate(
            {"command": "npx", "args": ["--token", "${env:MCP_TEST_TOKEN}"]}
        )
        check("${env:VAR} 插值", interpolated["args"][1] == "interp-ok")
        check("未定义变量保留原文", mcp_client._interpolate("${NOT_DEFINED_VAR}") == "${NOT_DEFINED_VAR}")

        env = mcp_client._build_safe_env(
            mcp_client._interpolate({"TOKEN": "${MCP_TEST_TOKEN}"})
        )
        check("显式 env 透传", env.get("TOKEN") == "interp-ok")
        check("PATH 透传", "PATH" in env)
        check("密钥不传给子进程", "DEEPSEEK_API_KEY" not in env)
    finally:
        os.environ.pop("MCP_TEST_TOKEN", None)
        os.environ.pop("DEEPSEEK_API_KEY", None)
        tmp.cleanup()


def test_connect_and_discover() -> None:
    """连接假服务器：initialize + tools/list → mcp__ 前缀 schema。"""
    tmp = tempfile.TemporaryDirectory()
    try:
        _activate(_demo_config(Path(tmp.name)))
        messages = mcp_client.get_mcp_tool_schemas()
        names = {t["function"]["name"] for t in messages}
        check("发现 3 个工具", len(names) == 3)
        check("前缀命名 mcp__demo__echo", "mcp__demo__echo" in names)
        check("描述带说明", any(
            t["function"]["name"] == "mcp__demo__echo"
            and t["function"]["description"]
            for t in messages
        ))
        check("inputSchema 透传", any(
            t["function"]["name"] == "mcp__demo__echo"
            and t["function"]["parameters"]["type"] == "object"
            for t in messages
        ))
        status = mcp_client.mcp_status()
        check("状态含 demo", len(status) == 1 and status[0]["name"] == "demo" and status[0]["tools"] == 3)
        check("加载提示含连接信息", any("已连接" in m for m in mcp_client.manager.ensure_loaded()))
    finally:
        _deactivate()
        tmp.cleanup()


def test_call_tool() -> None:
    """工具调用：成功 / isError / 非 MCP 名返回 None。"""
    tmp = tempfile.TemporaryDirectory()
    try:
        _activate(_demo_config(Path(tmp.name)))
        result = mcp_client.call_mcp_tool("mcp__demo__echo", {"text": "你好"})
        check("echo 原样返回", result == "你好")
        error = mcp_client.call_mcp_tool("mcp__demo__fail", {})
        check("fail 返回错误消息", "MCP 工具返回错误" in error and "模拟失败" in error)
        check("非 MCP 工具名 -> None", mcp_client.call_mcp_tool("get_current_time", {}) is None)
        check("非 MCP 前缀 -> None", mcp_client.call_mcp_tool("demo__echo", {}) is None)
    finally:
        _deactivate()
        tmp.cleanup()


def test_call_timeout() -> None:
    """工具调用超时：返回可读错误，服务器被终止，后续调用返回未连接。"""
    tmp = tempfile.TemporaryDirectory()
    try:
        _activate(_demo_config(Path(tmp.name), timeout=1))
        result = mcp_client.call_mcp_tool("mcp__demo__slow", {})
        check("超时返回可读错误", "超时" in result)
        after = mcp_client.call_mcp_tool("mcp__demo__echo", {"text": "x"})
        check("超时后服务器已终止", "未连接" in after)
    finally:
        _deactivate()
        tmp.cleanup()


def test_command_not_found() -> None:
    """命令不存在：加载提示含失败原因，调用返回未连接，不崩。"""
    tmp = tempfile.TemporaryDirectory()
    try:
        path = Path(tmp.name) / "mcp_servers.json"
        path.write_text(
            json.dumps(
                {"ghost": {"command": "definitely-not-a-real-command-xyz", "args": []}},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        _activate(path)
        messages = mcp_client.manager.ensure_loaded()
        check("加载提示含失败", any("连接失败" in m and "ghost" in m for m in messages))
        result = mcp_client.call_mcp_tool("mcp__ghost__anything", {})
        check("调用返回未连接", "未连接" in result)
    finally:
        _deactivate()
        tmp.cleanup()


def test_truncation_and_redaction() -> None:
    """输出截断（50000 上限）+ 密钥脱敏。"""
    tmp = tempfile.TemporaryDirectory()
    try:
        _activate(_demo_config(Path(tmp.name)))
        long_text = "x" * 60000
        result = mcp_client.call_mcp_tool("mcp__demo__echo", {"text": long_text})
        check("超长输出被截断", "OUTPUT TRUNCATED" in result and len(result) < 60000)
        secret = mcp_client.call_mcp_tool(
            "mcp__demo__echo", {"text": "token sk-abc1234567890"}
        )
        check("密钥不打明文", "sk-abc1234567890" not in secret)
        check("已打码", "***" in secret)
    finally:
        _deactivate()
        tmp.cleanup()


def test_parallel_whitelist() -> None:
    """并行判定：默认串行；声明 supports_parallel_tool_calls 才可并行。"""
    tmp = tempfile.TemporaryDirectory()
    try:
        # 默认（未声明）→ 串行
        _activate(_demo_config(Path(tmp.name)))
        mcp_client.get_mcp_tool_schemas()
        check("默认不支持并行", not mcp_client.parallel_safe_mcp_tool("mcp__demo__echo"))
        calls = [
            SimpleNamespace(
                id="c1",
                type="function",
                function=SimpleNamespace(name="mcp__demo__echo", arguments='{"text":"a"}'),
            ),
            SimpleNamespace(
                id="c2",
                type="function",
                function=SimpleNamespace(name="mcp__demo__echo", arguments='{"text":"b"}'),
            ),
        ]
        segments = tool_dispatch._plan_tool_batch_segments(calls)
        check("默认串行：两段 sequential", all(s[0] == "sequential" for s in segments))

        # 声明支持并行 → 可并行
        _activate(_demo_config(Path(tmp.name), supports_parallel_tool_calls=True))
        mcp_client.get_mcp_tool_schemas()
        check("声明后支持并行", mcp_client.parallel_safe_mcp_tool("mcp__demo__echo"))
        segments = tool_dispatch._plan_tool_batch_segments(calls)
        check("声明后单段 parallel", len(segments) == 1 and segments[0][0] == "parallel")
    finally:
        _deactivate()
        tmp.cleanup()


def test_minimal_agent_integration() -> None:
    """minimal_agent 集成：run_tool 分发 / get_tools / available_tool_names。"""
    tmp = tempfile.TemporaryDirectory()
    try:
        _activate(_demo_config(Path(tmp.name)))
        tools = minimal_agent.get_tools()
        names = {t["function"]["name"] for t in tools}
        check("get_tools 含 MCP 工具", "mcp__demo__echo" in names)
        check("available_tool_names 含 MCP 工具", "mcp__demo__echo" in minimal_agent.available_tool_names())
        result = minimal_agent.run_tool("mcp__demo__echo", {"text": "集成"})
        check("run_tool 分发到 MCP", result == "集成")
        unknown = minimal_agent.run_tool("mcp__nope__x", {})
        check("未连接服务器返回提示", "未连接" in unknown)
    finally:
        _deactivate()
        tmp.cleanup()


def main() -> None:
    """依次运行全部测试并汇总结果。"""
    print("== MCP 客户端回归测试 ==")
    for test_fn in (
        test_config_loading,
        test_env_interpolation_and_filter,
        test_connect_and_discover,
        test_call_tool,
        test_call_timeout,
        test_command_not_found,
        test_truncation_and_redaction,
        test_parallel_whitelist,
        test_minimal_agent_integration,
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
