# -*- coding: utf-8 -*-
"""
最简单的 Agent 骨架（第一步：Agent Loop 最小实现）
—— DeepSeek 版

使用业内常用依赖：
    - openai：官方 OpenAI SDK（DeepSeek 兼容 OpenAI 格式）
    - python-dotenv：从 .env 文件加载环境变量
    - rich：终端美化输出

安装依赖：
    pip install -r requirements.txt

设置环境变量（或在项目根目录创建 .env）：
    DEEPSEEK_API_KEY=你的key
    （可选）DEEPSEEK_BASE_URL=https://api.deepseek.com
    （可选）MODEL=deepseek-chat
"""

import json
import os
import sys
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI
from rich.console import Console
from rich.panel import Panel

load_dotenv()

# ---------------- 配置 ----------------
API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
MODEL = os.environ.get("MODEL", "deepseek-chat")

console = Console()


def create_client() -> OpenAI:
    """创建 OpenAI 兼容客户端（DeepSeek / 其他兼容接口通用）。"""
    return OpenAI(api_key=API_KEY, base_url=BASE_URL)


# ---------------- 第 1 步：调用大模型 ----------------
def call_llm(client: OpenAI, messages: list[dict[str, Any]], tools: list[dict[str, Any]]):
    """把对话消息 + 工具清单发给大模型，返回模型的回复（message 对象）。"""
    response = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        tools=tools,
    )
    return response.choices[0].message


# ---------------- 工具：定义 + 执行 ----------------
def get_weather(city: str) -> str:
    """示例工具：假装查天气（离线假数据，不需要真联网）。"""
    fake = {"北京": "晴，25°C", "上海": "多云，27°C", "广州": "阵雨，30°C"}
    return fake.get(city, f"{city}：暂无数据，建议看天气预报网站")


# 工具清单：这就是告诉模型"你有哪些工具可用"的说明书
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询指定城市的天气",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "城市名，例如：北京"},
                },
                "required": ["city"],
            },
        },
    }
]


def run_tool(name: str, args: dict[str, Any]) -> str:
    """根据工具名找到对应函数并执行。以后加新工具就在这里加一行。"""
    if name == "get_weather":
        return get_weather(args.get("city", ""))
    return f"未知工具：{name}"


# ---------------- 主循环：Agent Loop ----------------
def main():
    if not API_KEY:
        console.print(
            "[red]❌ 请先设置 DEEPSEEK_API_KEY[/red]\n"
            '  PowerShell: [cyan]$env:DEEPSEEK_API_KEY="你的key"[/cyan]\n'
            "  或在项目根目录创建 .env 文件，参考 README.md"
        )
        return

    # 支持两种输入方式：命令行参数 或 交互输入
    user_input = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else console.input("[bold]你说：[/bold] ")
    if not user_input.strip():
        console.print("[yellow]没有输入内容。[/yellow]")
        return

    client = create_client()

    # 对话历史 = 系统提示（角色/规则） + 用户消息
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "你是一个乐于助人的助手。需要天气信息时，请使用 get_weather 工具。"},
        {"role": "user", "content": user_input},
    ]

    max_turns = 5  # 最大轮数：防止模型无限调工具

    for turn in range(max_turns):
        console.print(f"\n[bold blue]--- 第 {turn + 1} 轮：调用大模型 ---[/bold blue]")
        msg = call_llm(client, messages, TOOLS)

        if msg.tool_calls:
            # 模型要求调用工具：先把 assistant 消息（含 tool_calls）放回历史
            messages.append(msg.model_dump(exclude_none=True))

            for tc in msg.tool_calls:
                name = tc.function.name
                args = json.loads(tc.function.arguments or "{}")
                console.print(f"  [yellow]🔧 模型要调用工具：[/yellow]{name}({args})")

                result = run_tool(name, args)
                console.print(f"  [green]📦 工具返回：[/green]{result}")

                # 关键一步：把工具结果放回对话历史
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })

            continue  # 回到循环开头，把结果再发给大模型

        # 模型直接给了文字回答 → 输出并结束
        console.print()
        console.print(Panel(msg.content or "", title="🤖 助手", border_style="green"))
        return

    console.print("\n[yellow]（达到最大轮数，循环结束）[/yellow]")


if __name__ == "__main__":
    main()
