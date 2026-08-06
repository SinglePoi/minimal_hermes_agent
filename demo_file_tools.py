# -*- coding: utf-8 -*-
"""
文件工具可视化演示（离线运行，不需要 DeepSeek API Key）：
    python demo_file_tools.py

逐步演示 read_file / write_file / search_files 的实际返回，
以及敏感路径保护（写 .env 被拒绝）。演示用的临时目录 .demo_files/
在脚本结束时自动清理，不会动任何真实文件。
"""

import json
import shutil
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from file_tools import read_file_tool, search_files_tool, write_file_tool  # noqa: E402

console = Console()
DEMO_DIR = ROOT / ".demo_files"


def show(title: str, result_json: str) -> None:
    """把工具返回的 JSON 美化后打印成面板。"""
    data = json.loads(result_json)
    console.print(Panel(
        json.dumps(data, ensure_ascii=False, indent=2),
        title=title,
        border_style="cyan",
    ))


def main() -> None:
    """按步骤演示三个文件工具 + 敏感路径保护。"""
    if DEMO_DIR.exists():
        shutil.rmtree(DEMO_DIR)

    console.print(Panel(
        "[bold]文件工具演示[/bold]\n"
        "全程离线，只操作临时目录 .demo_files/，结束后自动清理。",
        title="开始",
        border_style="green",
    ))

    # ── 第 1 步：write_file 写文件（自动建父目录）──
    console.print("\n[bold yellow]第 1 步：write_file 写文件（会自动建出 笔记/ 目录）[/bold yellow]")
    show(
        "write_file 的返回",
        write_file_tool(
            str(DEMO_DIR / "笔记" / "会议记录.txt"),
            "议题：产品评审\n结论：通过\n待办：小明周五前发布",
        ),
    )
    console.print("[dim]↑ 注意返回里的 resolved_path：告诉你文件实际写在哪个绝对路径[/dim]")

    # ── 第 2 步：read_file 读文件（带行号）──
    console.print("\n[bold yellow]第 2 步：read_file 读文件（每行带行号）[/bold yellow]")
    show(
        "read_file 的返回",
        read_file_tool(str(DEMO_DIR / "笔记" / "会议记录.txt")),
    )
    console.print("[dim]↑ content 里每行前有行号，模型可以说「第 2 行结论通过了」[/dim]")

    # ── 第 3 步：分页读取（文件太长时）──
    console.print("\n[bold yellow]第 3 步：分页读取（20 行文件，每页只给 5 行）[/bold yellow]")
    long_file = DEMO_DIR / "长文件.txt"
    long_file.parent.mkdir(parents=True, exist_ok=True)
    long_file.write_text("\n".join(f"第 {i} 行内容" for i in range(1, 21)), encoding="utf-8")
    show("read_file(limit=5) 的返回", read_file_tool(str(long_file), limit=5))
    console.print("[dim]↑ truncated=True + hint：告诉模型用 offset=6 继续读下一页[/dim]")

    # ── 第 4 步：search_files 搜索 ──
    console.print("\n[bold yellow]第 4 步：search_files 搜索关键词「评审」[/bold yellow]")
    (DEMO_DIR / "其他文件.txt").write_text("今天天气不错\n", encoding="utf-8")
    show("search_files 的返回", search_files_tool(str(DEMO_DIR), "评审"))
    console.print("[dim]↑ 只命中含「评审」的文件与行，无关文件不会出现[/dim]")

    # ── 第 5 步：敏感路径保护 ──
    console.print("\n[bold yellow]第 5 步：模型想写 .env（密钥文件）→ 被拒绝[/bold yellow]")
    show(
        "write_file(.env) 的返回",
        write_file_tool(str(DEMO_DIR / ".env"), "DEEPSEEK_API_KEY=sk-假的密钥"),
    )
    console.print("[red]↑ success=False，文件没有被创建——保险柜锁死了[/red]")

    # ── 第 6 步：敏感文件读取已脱敏 ──
    console.print("\n[bold yellow]第 6 步：读 .env —— 密钥被自动打码[/bold yellow]")
    env_file = DEMO_DIR / ".env"
    env_file.write_text(
        "DEEPSEEK_API_KEY=sk-abcdef1234567890xyz\nDASHSCOPE_API_KEY=sk-zzzz9876543210\n",
        encoding="utf-8",
    )
    show("read_file(.env) 的返回", read_file_tool(str(env_file)))
    console.print("[dim]↑ 真实密钥没有出现，只剩不可复用的哨兵 «redacted:sk-…»[/dim]")

    # ── 收尾：清理演示目录 ──
    shutil.rmtree(DEMO_DIR)
    console.print(Panel(
        "演示结束，临时目录 .demo_files/ 已清理。\n"
        "回归测试：python tests\\test_file_tools.py",
        title="完成",
        border_style="green",
    ))


if __name__ == "__main__":
    main()
