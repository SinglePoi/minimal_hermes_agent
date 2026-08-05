# -*- coding: utf-8 -*-
"""
最简单的 Agent 骨架（第二步：系统提示词 + 简单记忆）
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

新增功能：
    1. 系统提示词：集中管理角色/规则（也可用 SYSTEM_PROMPT.md 覆盖）
    2. 简单记忆：对话结束后让模型提取值得记住的信息，存到 memory.json，
       下次运行时自动注入系统提示词（跨会话学习）
"""

import json
import os
import re
import sys
from pathlib import Path
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

# 文件都放在脚本同目录，方便查看
BASE_DIR = Path(__file__).parent
MEMORY_FILE = BASE_DIR / "memory.json"
MAX_MEMORIES = 50  # 记忆条数上限，防止无限增长

console = Console()


# ---------------- 系统提示词 ----------------
# 集中管理"你是谁、怎么做事、什么时候用工具"
SYSTEM_PROMPT = """你是「小助手」，一个乐于助人的 AI 助手。

## 行为规则
1. 回答要简洁、准确、友好。
2. 需要实时信息（如天气）时，必须先调用 get_weather 工具，再基于工具结果回答。
3. 不确定的信息要如实说明，不要编造。
4. 如果用户提到了关于自己的信息（名字、偏好、习惯），在对话结束时我会帮你记住。"""


def load_system_prompt() -> str:
    """优先读取同目录下的 SYSTEM_PROMPT.md（方便不改代码直接改人设），否则用内置的。"""
    prompt_file = BASE_DIR / "SYSTEM_PROMPT.md"
    if prompt_file.exists():
        return prompt_file.read_text(encoding="utf-8").strip()
    return SYSTEM_PROMPT


# ---------------- 简单记忆 ----------------
def load_memory() -> list[str]:
    """从 memory.json 读取已记住的信息，没有则返回空列表。"""
    if MEMORY_FILE.exists():
        try:
            data = json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
            return [str(item) for item in data] if isinstance(data, list) else []
        except Exception:
            return []
    return []


def save_memory(memory: list[str]) -> None:
    """把记忆列表写回 memory.json。"""
    MEMORY_FILE.write_text(
        json.dumps(memory, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def build_system_prompt(memory: list[str]) -> str:
    """组装系统提示词：基础人设 + 已记住的用户信息。"""
    prompt = load_system_prompt()
    if memory:
        facts = "\n".join(f"- {item}" for item in memory)
        prompt += f"\n\n## 你记住的用户信息\n{facts}"
    return prompt


def extract_memories(client: OpenAI, messages: list[dict[str, Any]]) -> list[str]:
    """让模型从本次对话中提取"值得长期记住的用户信息"。

    返回 JSON 数组，例如 ["用户喜欢喝美式咖啡"]。
    提取失败时返回空列表（不让记忆问题影响主流程）。
    """
    extract_prompt = (
        "从上面的对话中，提取值得长期记住的用户信息（名字、偏好、身份、习惯等）。\n"
        "不要提取一次性信息（例如某次查询的天气结果）。\n"
        '只输出 JSON 字符串数组，例如：["用户喜欢喝美式咖啡"]。\n'
        "如果没有值得记住的，输出 []。"
    )
    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=messages + [{"role": "user", "content": extract_prompt}],
            temperature=0,
        )
        text = resp.choices[0].message.content or ""
        # 容错：去掉可能的 ```json 代码块包裹
        match = re.search(r"\[.*\]", text, re.S)
        data = json.loads(match.group(0)) if match else []
        return [str(item).strip() for item in data if str(item).strip()]
    except Exception as exc:
        console.print(f"[dim]（记忆提取失败，已跳过：{exc}）[/dim]")
        return []


def merge_memories(existing: list[str], new: list[str]) -> list[str]:
    """合并新旧记忆：去掉重复，超限时只保留最新的。"""
    seen = set(existing)
    merged = list(existing)
    for item in new:
        if item not in seen:
            seen.add(item)
            merged.append(item)
    return merged[-MAX_MEMORIES:]


# ---------------- 第 1 步：调用大模型 ----------------
def create_client() -> OpenAI:
    """创建 OpenAI 兼容客户端（DeepSeek / 其他兼容接口通用）。"""
    return OpenAI(api_key=API_KEY, base_url=BASE_URL)


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

    # 加载记忆并注入系统提示词（跨会话"记得你"的关键）
    memory = load_memory()
    if memory:
        console.print(Panel("\n".join(f"- {m}" for m in memory), title="🧠 已记住的信息", border_style="yellow"))

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": build_system_prompt(memory)},
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
        break
    else:
        console.print("\n[yellow]（达到最大轮数，循环结束）[/yellow]")

    # 对话结束后：提取新记忆 → 合并 → 保存（这就是"学习闭环"）
    new_facts = extract_memories(client, messages)
    if new_facts:
        merged = merge_memories(memory, new_facts)
        if merged != memory:
            save_memory(merged)
            console.print(Panel("\n".join(f"+ {m}" for m in new_facts), title="🧠 本次新记住", border_style="cyan"))


if __name__ == "__main__":
    main()
