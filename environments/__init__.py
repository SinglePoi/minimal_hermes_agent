# -*- coding: utf-8 -*-
"""终端执行环境后端（对齐 Hermes tools/environments/）。

骨架目前支持：
    - local（默认）：本机 subprocess，走危险命令审批
    - daytona：Daytona 云沙箱（Linux），沙箱即隔离边界，跳过危险命令审批

选择方式：环境变量 TERMINAL_ENV=local|daytona 是进程默认（对齐 Hermes TERMINAL_ENV）；
会话可覆盖（骨架的会话级手动切换），互不影响。
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
# 按 task_id 缓存沙箱：进程默认共用 "default"；会话显式选 daytona 时用会话 id
_active_envs: dict[str, Any] = {}


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


def normalize_backend(name: Optional[str]) -> str:
    """把后端名归一成小写；不支持的返回空串。"""
    raw = (name or "").strip().lower()
    return raw if raw in SUPPORTED_BACKENDS else ""


def get_terminal_env() -> str:
    """返回进程默认终端后端名（小写，默认 local）。"""
    raw = (os.environ.get("TERMINAL_ENV", "local") or "local").strip().lower()
    return raw if raw in SUPPORTED_BACKENDS else "local"


def sandbox_task_id(session_id: str) -> str:
    """把会话 id 收成 Daytona 沙箱名可用的 task_id。"""
    raw = "".join(
        ch if ch.isalnum() or ch in "._-" else "-"
        for ch in (session_id or "").strip()
    )
    raw = raw.strip("-.") or "session"
    return raw[:48]


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


def build_terminal_backend_prompt(env_type: Optional[str] = None) -> str:
    """注入系统提示词的终端后端说明（对齐 Hermes prompt_builder 的环境描述）。"""
    env_type = normalize_backend(env_type) or get_terminal_env()
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


def get_environment(env_type: Optional[str] = None, task_id: Optional[str] = None):
    """返回终端后端实例；local 返回 None（由 run_terminal 走本机 subprocess）。

    Daytona 按 task_id 缓存（对齐 Hermes 按 task_id 复用沙箱）：
    进程默认共用配置里的 task_id；会话显式选 daytona 时传入该会话的 id。
    """
    resolved = normalize_backend(env_type) or get_terminal_env()
    if resolved != "daytona":
        return None
    cfg = get_daytona_config()
    key = (task_id or cfg["task_id"] or "default").strip() or "default"
    with _env_lock:
        cached = _active_envs.get(key)
        if cached is not None:
            return cached
        from environments.daytona import DaytonaEnvironment

        env = DaytonaEnvironment(
            image=cfg["image"],
            cwd=cfg["cwd"],
            timeout=cfg["timeout"],
            cpu=cfg["cpu"],
            memory=cfg["memory"],
            disk=cfg["disk"],
            persistent_filesystem=cfg["persistent"],
            task_id=key,
        )
        _active_envs[key] = env
        return env


def release_environment(task_id: str) -> None:
    """释放指定 task_id 的沙箱缓存（切换离 daytona 时调用；持久化则 stop）。"""
    key = (task_id or "").strip()
    if not key:
        return
    with _env_lock:
        env = _active_envs.pop(key, None)
    if env is not None:
        try:
            env.cleanup()
        except Exception:
            pass


def cleanup_environments() -> None:
    """进程退出时清理全部远程沙箱（持久化则 stop，否则 delete）。"""
    with _env_lock:
        envs = list(_active_envs.values())
        _active_envs.clear()
    for env in envs:
        try:
            env.cleanup()
        except Exception:
            pass


def reset_environment_cache() -> None:
    """测试用：丢弃缓存的环境实例（不调用 cleanup，避免碰到假 SDK）。"""
    with _env_lock:
        _active_envs.clear()
