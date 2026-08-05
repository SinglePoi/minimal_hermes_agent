# -*- coding: utf-8 -*-
"""
危险命令审批模块（对齐 Hermes Agent 的 tools/approval.py）

对应关系：
    - DANGEROUS_PATTERNS / detect_dangerous_command()  → Hermes 同名单/同函数
    - HARDLINE_PATTERNS / detect_hardline_command()    → Hermes 的硬性禁止地板
      （Hermes 中连 yolo / approvals.mode=off 都绕不过，本骨架也保持无条件阻止）
    - approve_session() / is_approved() / approve_permanent()
        → Hermes 的会话级 / 永久级审批状态
    - prompt_dangerous_approval()                      → Hermes 的 CLI 交互提示
      （once / session / always / deny，超时默认 300 秒，与 Hermes 一致）
    - check_dangerous_command()                        → Hermes 的主入口
      （terminal 工具执行前调用）

本骨架简化掉的部分（Hermes 有，后续值得对齐）：
    - 用户自定义 deny 规则（approvals.deny）、cron / gateway 审批上下文
    - Smart Approval（辅助 LLM 自动批准低风险命令）与连续拒绝熔断
    - 会话级 YOLO（/yolo）、命令混淆检测（$()、base64 解码等）与解析器上限
    - 敏感文本脱敏（Hermes 用 agent/redact.py）
"""

import fnmatch
import json
import os
import re
import sys
import threading
from pathlib import Path
from typing import Any, Optional

from rich.console import Console
from rich.panel import Panel

console = Console()
BASE_DIR = Path(__file__).parent

# 永久允许列表持久化文件（对齐 Hermes config.yaml 的 command_allowlist 列表）。
# 使用 JSON 而非 YAML，避免给骨架引入 pyyaml 依赖；语义一致：存 pattern_key
# 描述（如 "recursive delete"）和用户手动添加的精确命令 / glob。
ALLOWLIST_FILE = BASE_DIR / "approval_allowlist.json"

# 正则编译标志：与 Hermes 一致（大小写不敏感 + DOTALL）
_RE_FLAGS = re.IGNORECASE | re.DOTALL


# =========================================================================
# 硬性禁止地板（Hermes HARDLINE_PATTERNS 的简化版）
# =========================================================================
# 灾难级命令：无论什么模式（本骨架没有 yolo，但保持 Hermes 的分层设计），
# 一律无条件阻止，不给用户“允许”的选项。
HARDLINE_PATTERNS = [
    # rm 递归删除根文件系统（/、//、/.、/.. 等最终都会解析到根目录）
    (r"\brm\s+(-[^\s]*\s+)*/(?:\s|$)", "recursive delete of root filesystem"),
    # rm 递归删除系统目录（Hermes 的保护根列表）
    (r"\brm\s+(-[^\s]*\s+)*(?:/home|/root|/etc|/usr|/var|/bin|/sbin|/boot|/lib)\b",
     "recursive delete of system directory"),
    # 格式化文件系统
    (r"\bmkfs(\.[a-z0-9]+)?\b", "format filesystem (mkfs)"),
    # 直接写裸块设备
    (r"\bdd\b[^\n]*\bof=/dev/(sd|nvme|hd|mmcblk|vd|xvd)[a-z0-9]*",
     "dd to raw block device"),
    (r">\s*/dev/(sd|nvme|hd|mmcblk|vd|xvd)[a-z0-9]*\b", "redirect to raw block device"),
    # 经典 fork bomb
    (r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", "fork bomb"),
    # 杀死系统所有进程
    (r"\bkill\s+(-[^\s]+\s+)*-1\b", "kill all processes"),
    # 关机 / 重启：锚定到命令起始位置（行首或分隔符后），避免误伤 "echo reboot"
    (r"(?:^|[;&|`])\s*(?:sudo\s+)?(?:shutdown|reboot|halt|poweroff)\b",
     "system shutdown/reboot"),
    (r"(?:^|[;&|`])\s*(?:sudo\s+)?init\s+[06]\b", "init 0/6 (shutdown/reboot)"),
    (r"(?:^|[;&|`])\s*(?:sudo\s+)?systemctl\s+(?:poweroff|reboot|halt|kexec)\b",
     "systemctl poweroff/reboot"),
]

HARDLINE_PATTERNS_COMPILED = [
    (re.compile(pattern, _RE_FLAGS), description)
    for pattern, description in HARDLINE_PATTERNS
]


def detect_hardline_command(command: str) -> tuple[bool, Optional[str]]:
    """检测命令是否命中硬性禁止清单。

    返回 (是否命中, 描述)；未命中时描述为 None。Hermes 中该检查先于一切
    审批分支执行（含 yolo 旁路），本骨架同样保持最优先。
    """
    for pattern_re, description in HARDLINE_PATTERNS_COMPILED:
        if pattern_re.search(command):
            return True, description
    return False, None


def _hardline_block_result(description: str) -> dict[str, Any]:
    """构造硬性禁止的返回结果（对齐 Hermes 的 _hardline_block_result）。"""
    return {
        "approved": False,
        "hardline": True,
        "message": (
            f"BLOCKED (hardline): {description}. "
            "This command is on the unconditional blocklist and cannot "
            "be executed via the agent. If you genuinely need to run it, "
            "run it yourself in a terminal outside the agent."
        ),
    }


# =========================================================================
# 危险命令模式表（Hermes DANGEROUS_PATTERNS 的精选子集）
# =========================================================================
# 每个条目：(正则, 描述)。描述同时充当 pattern_key——审批允许列表按它记忆，
# 与 Hermes 完全一致（Hermes 的 pattern_key 就是 description）。
_PROJECT_ENV_CONFIG = (
    r"(?:(?:/|\.{1,2}/)?(?:[^\s/\"'`]+/)*\.env(?:\.[^/\s\"'`]+)*"
    r"|(?:(?:/|\.{1,2}/)?(?:[^\s/\"'`]+/)*config\.yaml))"
)
_WRITE_TARGET_BOUNDARY = r"(?=[\s;&|<>\"']|$)"

DANGEROUS_PATTERNS = [
    # ---- 文件系统删除 ----
    (r"\brm\s+(-[^\s]*\s+)*/", "delete in root path"),
    (r"\brm\s+-[^\s]*r", "recursive delete"),
    (r"\brm\s+--recursive\b", "recursive delete (long flag)"),
    # Windows shell 的破坏性内置命令（仅在经由 cmd/powershell 执行时触发，
    # 避免普通文本里的 "del"/"rd" 误报）
    (r"\bcmd(?:\.exe)?\s+/(?:c|k)\s+.*\b(?:del|erase|rd|rmdir)\b",
     "Windows cmd destructive delete"),
    (r"\b(?:powershell|pwsh)(?:\.exe)?\b(?:\s+-\S+)*\s+"
     r"(?:-(?:command|c)\s+)?[\"']?(?:remove-item|rmdir|erase|del|rd|ri|rm)\b",
     "Windows PowerShell destructive delete"),
    (r"\b(?:powershell|pwsh)(?:\.exe)?\b.*\s-(?:encodedcommand|enc|e)\b",
     "PowerShell encoded command execution"),
    # ---- 权限 ----
    (r"\bchmod\s+(-[^\s]*\s+)*(777|666|o\+[rwx]*w|a\+[rwx]*w)\b",
     "world/other-writable permissions"),
    (r"\bchown\s+(-[^\s]*)?R\s+root", "recursive chown to root"),
    # ---- 系统 / 磁盘 ----
    (r"\bsystemctl\s+(-[^\s]+\s+)*(stop|restart|disable|mask)\b",
     "stop/restart system service"),
    # ---- 进程 ----
    (r"\bkill\s+-9\s+-1\b", "kill all processes"),
    (r"\bpkill\s+-9\b", "force kill processes"),
    (r"\bkillall\s+(-[^\s]*\s+)*-(9|KILL|SIGKILL)\b", "force kill processes (killall -KILL)"),
    # ---- SQL ----
    (r"\bDROP\s+(TABLE|DATABASE)\b", "SQL DROP"),
    (r"\bDELETE\s+FROM\b(?![^\n]*\bWHERE\b)", "SQL DELETE without WHERE"),
    (r"\bTRUNCATE\s+(TABLE)?\s*\w", "SQL TRUNCATE"),
    # ---- 远程代码执行 ----
    (r"\b(curl|wget)\b.*\|\s*(?:[/\w]*/)?(?:ba)?sh(?:\s|$|-c)",
     "pipe remote content to shell"),
    # ---- 覆盖项目敏感文件（.env / config.yaml）----
    (rf">>?\s*[\"']?{_PROJECT_ENV_CONFIG}[\"']?{_WRITE_TARGET_BOUNDARY}",
     "overwrite project env/config via redirection"),
    (rf"\btee\b.*[\"']?{_PROJECT_ENV_CONFIG}[\"']?{_WRITE_TARGET_BOUNDARY}",
     "overwrite project env/config via tee"),
    # ---- git 破坏性操作 ----
    (r"\bgit\s+reset\s+--h(?:a(?:r(?:d)?)?)?\b",
     "git reset --hard (destroys uncommitted changes)"),
    (r"\bgit\s+push\b.*--forc[a-z]*\b", "git force push (rewrites remote history)"),
    (r"\bgit\s+push\b.*-f\b", "git force push short flag (rewrites remote history)"),
    (r"\bgit\s+clean\s+-[^\s]*f", "git clean with force (deletes untracked files)"),
    (r"\bgit\s+branch\s+-D\b", "git branch force delete"),
    # ---- 批量删除 ----
    (r"\bxargs\s+.*\brm\b", "xargs with rm"),
    (r"\bfind\b.*-exec(?:dir)?\s+(/\S*/)?rm\b", "find -exec/-execdir rm"),
    (r"\bfind\b.*-delete\b", "find -delete"),
    # ---- sudo 提权标志 ----
    (r"\bsudo\b[^;|&\n]*?\s+(?:-s\b|--st[a-z]*\b|-a\b|--a[a-z]*\b)",
     "sudo with privilege flag (stdin/askpass/shell/list)"),
    (r"\bsudo\b[^;|&\n]*?\s+-[a-z]*[sa][a-z]*\b",
     "sudo with combined-flag privilege escalation"),
]

DANGEROUS_PATTERNS_COMPILED = [
    (re.compile(pattern, _RE_FLAGS), description)
    for pattern, description in DANGEROUS_PATTERNS
]


def detect_dangerous_command(command: str) -> tuple[bool, Optional[str], Optional[str]]:
    """检测命令是否命中危险模式。

    返回 (是否危险, pattern_key, 描述)；未命中时后两项为 None。
    与 Hermes 一致：pattern_key 就是描述，供会话级 / 永久允许列表记忆。
    检测前统一转小写（Hermes 的 _command_detection_variants 也做同样归一化）。
    """
    command_lower = command.lower()
    for pattern_re, description in DANGEROUS_PATTERNS_COMPILED:
        if pattern_re.search(command_lower):
            return True, description, description
    return False, None, None


# =========================================================================
# 审批状态：会话级 + 永久级（Hermes 的 _session_approved / _permanent_approved）
# =========================================================================
_session_approved: dict[str, set] = {}
_permanent_approved: set = set()


def approve_session(session_key: str, pattern_key: str) -> None:
    """批准某个模式，仅对当前会话生效（对齐 Hermes 的 approve_session）。"""
    _session_approved.setdefault(session_key, set()).add(pattern_key)


def approve_permanent(pattern_key: str) -> None:
    """把某个模式加入永久允许列表（对齐 Hermes 的 approve_permanent）。"""
    _permanent_approved.add(pattern_key)


def is_approved(session_key: str, pattern_key: str) -> bool:
    """检查模式是否已批准（会话级或永久级，对齐 Hermes 的 is_approved）。"""
    if pattern_key in _permanent_approved:
        return True
    return pattern_key in _session_approved.get(session_key, set())


def load_permanent_allowlist() -> set:
    """从 JSON 文件加载永久允许列表，并同步进内存（对齐 Hermes 的 load_permanent_allowlist）。"""
    patterns: set = set()
    if ALLOWLIST_FILE.exists():
        try:
            data = json.loads(ALLOWLIST_FILE.read_text(encoding="utf-8"))
            patterns = set(data.get("command_allowlist", []) or [])
            _permanent_approved.update(patterns)
        except (OSError, ValueError) as exc:
            console.print(f"[yellow]⚠️ 读取允许列表失败：{exc}[/yellow]")
    return patterns


def save_permanent_allowlist() -> None:
    """把永久允许列表写回 JSON 文件（对齐 Hermes 的 save_permanent_allowlist）。"""
    try:
        ALLOWLIST_FILE.write_text(
            json.dumps(
                {"command_allowlist": sorted(_permanent_approved)},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        console.print(f"[yellow]⚠️ 写入允许列表失败：{exc}[/yellow]")


_ALLOWLIST_SHELL_OPERATOR_RE = re.compile(r"(?:\n|&&|\|\||[;&|<>`]|\$\()")


def _has_allowlist_shell_operator(command: str) -> bool:
    """判断命令是否含复合 shell 操作符（对齐 Hermes：这类命令不走精确匹配捷径）。"""
    return bool(_ALLOWLIST_SHELL_OPERATOR_RE.search(command or ""))


def _command_matches_permanent_allowlist(command: str) -> bool:
    """检查永久允许列表是否包含这条命令（精确匹配或 glob，对齐 Hermes）。"""
    command = (command or "").strip()
    if not command or _has_allowlist_shell_operator(command):
        return False
    for pattern in tuple(_permanent_approved):
        if not isinstance(pattern, str):
            continue
        pattern = pattern.strip()
        if not pattern:
            continue
        if command == pattern:
            return True
        if any(ch in pattern for ch in "*?[") and fnmatch.fnmatchcase(command, pattern):
            return True
    return False


# =========================================================================
# 交互审批提示（Hermes prompt_dangerous_approval 的 CLI 简化版）
# =========================================================================
def _get_approval_timeout() -> int:
    """读取审批超时秒数（环境变量 APPROVAL_TIMEOUT，默认 300，与 Hermes 一致）。"""
    try:
        return int(os.environ.get("APPROVAL_TIMEOUT", "300"))
    except ValueError:
        return 300


def prompt_dangerous_approval(
    command: str,
    description: str,
    timeout_seconds: Optional[int] = None,
    allow_permanent: bool = True,
) -> str:
    """交互式征求用户对危险命令的批准。

    返回 'once' | 'session' | 'always' | 'deny' | 'timeout'（与 Hermes 一致）：
    - once：仅本次放行，不记忆
    - session：本会话内放行同模式
    - always：永久放行（写入允许列表文件）
    - deny：明确拒绝；timeout：超时未应答——两者都必须失败关闭

    提示采用“子线程 + join 超时”，避免输入阻塞时无法超时（Hermes 同款思路）。
    """
    if timeout_seconds is None:
        timeout_seconds = _get_approval_timeout()

    console.print()
    console.print(
        Panel(
            f"[bold red]⚠️ 危险命令需要批准[/bold red]\n\n"
            f"[yellow]原因[/yellow]：{description}\n"
            f"[yellow]命令[/yellow]：{command}",
            title="审批",
            border_style="red",
        )
    )
    if allow_permanent:
        console.print(
            "  选择：[o] 仅此一次  [s] 本会话允许  [a] 永久允许  [d] 拒绝",
            markup=False,
        )
    else:
        console.print(
            "  选择：[o] 仅此一次  [s] 本会话允许  [d] 拒绝",
            markup=False,
        )
    sys.stdout.flush()

    result: dict[str, str] = {"choice": ""}

    def get_input() -> None:
        """子线程里读取用户输入（允许超时控制）。"""
        try:
            result["choice"] = input("  你的选择：").strip().lower()
        except (EOFError, OSError):
            result["choice"] = ""

    thread = threading.Thread(target=get_input, daemon=True)
    thread.start()
    thread.join(timeout=timeout_seconds)

    if thread.is_alive():
        console.print("[red]⏰ 审批超时，未获得用户批准。[/red]")
        return "timeout"

    choice = result["choice"]
    if choice in ("o", "once"):
        console.print("[green]✔ 仅此一次，放行。[/green]")
        return "once"
    if choice in ("s", "session"):
        console.print("[green]✔ 本会话允许，放行。[/green]")
        return "session"
    if choice in ("a", "always"):
        if not allow_permanent:
            console.print("[green]✔ 按本会话允许处理，放行。[/green]")
            return "session"
        console.print("[green]✔ 永久允许，放行。[/green]")
        return "always"
    console.print("[red]✖ 已拒绝，命令不会执行。[/red]")
    return "deny"


# =========================================================================
# 审批门卫主入口（Hermes check_dangerous_command 的简化版）
# =========================================================================
def _is_interactive_cli() -> bool:
    """判断当前是否为交互式 CLI（stdin 是 TTY）。

    对齐 Hermes：非交互上下文默认自动放行（fail-open 历史行为）并打印警告；
    骨架里大多数运行都是交互 REPL，一次性模式若从管道输入会走这条分支。
    """
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


def check_dangerous_command(command: str, session_key: str) -> dict[str, Any]:
    """执行前统一审批门卫：检测 + 会话/永久审批 + 交互提示（对齐 Hermes）。

    顺序与 Hermes 一致：
    1. 硬性禁止地板（无条件阻止，不给“允许”选项）
    2. 永久允许列表精确/glob 匹配 → 直接放行
    3. 危险模式检测；未命中 → 放行
    4. 会话级/永久级已批准 → 放行
    5. 交互式 CLI → 提示用户选择；非交互 → 打印警告后自动放行（Hermes 默认）
    6. 拒绝 / 超时 → 失败关闭，返回 BLOCKED 消息（明确“不要重试”）

    返回 dict：{"approved": bool, "message": str|None, ...}
    """
    # 1. 硬性禁止
    is_hardline, hardline_desc = detect_hardline_command(command)
    if is_hardline:
        console.print(f"[red]🚫 {hardline_desc}[/red]")
        return _hardline_block_result(hardline_desc or "hardline block")

    # 2. 永久允许列表精确匹配
    if _command_matches_permanent_allowlist(command):
        return {"approved": True, "message": None}

    # 3. 危险模式检测
    is_dangerous, pattern_key, description = detect_dangerous_command(command)
    if not is_dangerous:
        return {"approved": True, "message": None}

    # 4. 会话/永久已批准
    if is_approved(session_key, pattern_key or ""):
        return {"approved": True, "message": None, "description": description}

    # 5. 交互提示（或非交互自动放行）
    if not _is_interactive_cli():
        console.print(
            f"[yellow]⚠️ 非交互环境，危险命令自动放行（{description}）：{command}[/yellow]"
        )
        return {"approved": True, "message": None, "auto_approved": True,
                "description": description}

    choice = prompt_dangerous_approval(command, description or "")

    if choice == "timeout":
        return {
            "approved": False,
            "pattern_key": pattern_key,
            "description": description,
            "outcome": "timeout",
            "user_consent": False,
            "message": (
                "BLOCKED: Command timed out without user response. The user "
                "has NOT consented to this action. Do NOT retry this command, "
                "do NOT rephrase it, and do NOT attempt the same outcome via "
                "a different command. Silence is not consent."
            ),
        }
    if choice == "deny":
        return {
            "approved": False,
            "pattern_key": pattern_key,
            "description": description,
            "outcome": "denied",
            "user_consent": False,
            "message": (
                "BLOCKED: User denied this command. The user has NOT consented "
                "to this action. Do NOT retry this command, do NOT rephrase "
                "it, and do NOT attempt the same outcome via a different "
                "command. Stop the current workflow and wait for the user to "
                "respond before taking any further destructive or "
                "irreversible action."
            ),
        }

    # 6. once / session / always：按选择持久化（对齐 Hermes 的持久化分支）
    if choice == "session":
        approve_session(session_key, pattern_key or "")
    elif choice == "always":
        approve_session(session_key, pattern_key or "")
        approve_permanent(pattern_key or "")
        save_permanent_allowlist()

    return {
        "approved": True,
        "message": None,
        "pattern_key": pattern_key,
        "user_approved": True,
        "description": description,
    }


# 模块导入时加载永久允许列表（对齐 Hermes 末尾的 load_permanent_allowlist()）
load_permanent_allowlist()
