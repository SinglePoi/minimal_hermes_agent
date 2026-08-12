# -*- coding: utf-8 -*-
"""测试用假 MCP 服务器（stdio + JSON-RPC 2.0，零依赖）。

供 tests/test_mcp_client.py 与 test_server.py 端到端验证使用。实现
initialize / notifications/initialized / tools/list / tools/call，暴露三个工具：
    echo   —— 原样返回传入的 text
    fail   —— 固定返回 isError=true 的错误内容
    slow   —— 睡 3 秒再返回（超时测试用）
"""

import json
import sys
import time

# MCP 规范要求 JSON 用 UTF-8；中文 Windows 下 Python 子进程默认 GBK，
# 不强制 UTF-8 会导致父进程按 UTF-8 解码乱码（真实 MCP 服务器同样应输出 UTF-8）
for stream in (sys.stdin, sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _send(message: dict) -> None:
    """写一行 JSON-RPC 响应（换行分隔）。"""
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


TOOLS = [
    {
        "name": "echo",
        "description": "原样返回输入文本",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "fail",
        "description": "固定返回错误",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "slow",
        "description": "睡 3 秒再返回",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def main() -> None:
    """逐行读 stdin，按方法分发 JSON-RPC 请求。"""
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            message = json.loads(raw)
        except ValueError:
            continue
        method = message.get("method")
        msg_id = message.get("id")
        if msg_id is None:
            continue  # 通知（如 notifications/initialized）直接忽略
        if method == "initialize":
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fake-mcp", "version": "1.0"},
                    },
                }
            )
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = message.get("params") or {}
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if name == "echo":
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "content": [{"type": "text", "text": arguments.get("text", "")}],
                            "isError": False,
                        },
                    }
                )
            elif name == "fail":
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "content": [{"type": "text", "text": "模拟失败"}],
                            "isError": True,
                        },
                    }
                )
            elif name == "slow":
                time.sleep(3)
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "content": [{"type": "text", "text": "慢工出细活"}],
                            "isError": False,
                        },
                    }
                )
            else:
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "error": {"code": -32602, "message": f"未知工具 {name}"},
                    }
                )


if __name__ == "__main__":
    main()
