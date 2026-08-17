# -*- coding: utf-8 -*-
"""终端执行环境后端（对齐 Hermes tools/environments/）。

骨架目前支持：
    - local（默认）：本机 subprocess，走危险命令审批
    - daytona：Daytona 云沙箱（Linux），沙箱即隔离边界，跳过危险命令审批

选择方式：环境变量 TERMINAL_ENV=local|daytona（对齐 Hermes TERMINAL_ENV）。
"""

from __future__ import annotations

import os
import threading
from typing import Any, Optional

# 对齐 Hermes：隔离容器后端的命令无法伤害宿主机，审批层跳过
ISOLATED_BACKENDS = frozenset({"daytona"})
SUPPORTED_BACKENDS = frozenset({"local", "daytona"})
DEFAULT_DAYTONA_IMAGE = "nikolaik/python-nodejs:python3.11-nodejs20"

_env_lock = threading.Lock()
_active_env: Any = None


def _env_int(name: str, default: int, min_value: int = 0) -> int:
    """读取环境变量整数配置（非法值回退默认）。"""
    try:
        return max(min_value, int(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool = True) -> bool:
    """读取环境变量布尔配置。"""
    raw = (os.environ.get(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def get_terminal_env() -> str:
    """返回当前终端后端名（小写，默认 local）。"""
    raw = (os.environ.get("TERMINAL_ENV", "local") or "local").strip().lower()
    return raw or "local"


def is_isolated_backend(env_type: Optional[str] = None) -> bool:
    """该后端是否隔离到足以跳过危险命令审批（对齐 Hermes _should_skip_container_guards）。"""
    name = (env_type if env_type is not None else get_terminal_env()).strip().lower()
    return name in ISOLATED_BACKENDS


def get_daytona_config() -> dict[str, Any]:
    """读取 Daytona 后端配置（镜像 / 资源 / 持久化）。"""
    return {
        "image": os.environ.get("TERMINAL_DAYTONA_IMAGE", DEFAULT_DAYTONA_IMAGE)
        or DEFAULT_DAYTONA_IMAGE,
        "cpu": _env_int("TERMINAL_CONTAINER_CPU", 1, 1),
        "memory": _env_int("TERMINAL_CONTAINER_MEMORY", 5120, 1),
        "disk": _env_int("TERMINAL_CONTAINER_DISK", 10240, 1),
        "persistent": _env_bool("TERMINAL_CONTAINER_PERSISTENT", True),
        "cwd": os.environ.get("TERMINAL_CWD", "") or "/home/daytona",
        "timeout": _env_int("TERMINAL_TIMEOUT", 180, 1),
        "task_id": os.environ.get("TERMINAL_TASK_ID", "default") or "default",
    }


def check_backend_ready(env_type: Optional[str] = None) -> tuple[bool, str]:
    """检查当前（或指定）后端是否可用；不可用时返回可读错误。

    local 永远就绪。daytona 需要 SDK + DAYTONA_API_KEY。未知后端直接失败。
    """
    name = (env_type if env_type is not None else get_terminal_env()).strip().lower()
    if name == "local":
        return True, ""
    if name not in SUPPORTED_BACKENDS:
        supported = ", ".join(sorted(SUPPORTED_BACKENDS))
        return False, (
            f"未知 TERMINAL_ENV={name!r}。骨架目前支持：{supported}。"
        )
    if name == "daytona":
        try:
            from daytona import Daytona  # noqa: F401
        except ImportError:
            return False, (
                "Daytona 后端需要安装 SDK：pip install daytona"
            )
        if not (os.environ.get("DAYTONA_API_KEY") or "").strip():
            return False, (
                "Daytona 后端需要 DAYTONA_API_KEY 环境变量。"
                "注册并创建密钥：https://daytona.io"
            )
        return True, ""
    return False, f"未实现的终端后端：{name}"


def build_terminal_backend_prompt() -> str:
    """注入系统提示词的终端后端说明（对齐 Hermes prompt_builder 的环境描述）。"""
    env_type = get_terminal_env()
    if env_type == "daytona":
        cfg = get_daytona_config()
        return (
            "## 终端后端\n"
            "当前 `terminal` 工具在 Daytona 云沙箱（Linux）中执行，"
            f"镜像 `{cfg['image']}`，工作目录 `{cfg['cwd']}`。"
            "破坏性命令只影响沙箱、不会伤害宿主机，系统会跳过危险命令审批。\n"
            "注意：`read_file` / `write_file` / `patch` / `search_files` 仍操作**本机工作区**，"
            "不会自动同步进沙箱。若要在沙箱里放文件，请用 `terminal` 写入"
            "（如 `cat > file.py <<'EOF' ... EOF`）。"
        )
    if env_type != "local":
        return (
            f"## 终端后端\n当前 TERMINAL_ENV={env_type}。"
            "若后端不可用，terminal 工具会返回错误。"
        )
    return ""


def get_environment():
    """返回当前终端后端实例；local 返回 None（由 run_terminal 走本机 subprocess）。

    Daytona 环境按进程单例缓存（对齐 Hermes 按 task_id 复用沙箱），
    首次调用时创建/恢复沙箱。
    """
    global _active_env
    env_type = get_terminal_env()
    if env_type != "daytona":
        return None
    with _env_lock:
        if _active_env is not None:
            return _active_env
        from environments.daytona import DaytonaEnvironment

        cfg = get_daytona_config()
        _active_env = DaytonaEnvironment(
            image=cfg["image"],
            cwd=cfg["cwd"],
            timeout=cfg["timeout"],
            cpu=cfg["cpu"],
            memory=cfg["memory"],
            disk=cfg["disk"],
            persistent_filesystem=cfg["persistent"],
            task_id=cfg["task_id"],
        )
        return _active_env


def cleanup_environments() -> None:
    """进程退出时清理远程沙箱（持久化则 stop，否则 delete）。"""
    global _active_env
    with _env_lock:
        env = _active_env
        _active_env = None
    if env is not None:
        try:
            env.cleanup()
        except Exception:
            pass


def reset_environment_cache() -> None:
    """测试用：丢弃缓存的环境实例（不调用 cleanup，避免碰到假 SDK）。"""
    global _active_env
    with _env_lock:
        _active_env = None
