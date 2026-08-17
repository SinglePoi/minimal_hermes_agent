# -*- coding: utf-8 -*-
"""Daytona 云沙箱执行后端（对齐 Hermes tools/environments/daytona.py，简化版）。

用 Daytona Python SDK 在云端沙箱里跑命令。持久化开启时，清理阶段 stop 而非
delete，下次按名字恢复，文件系统得以保留。

简化掉的部分（Hermes 有）：
    - FileSyncManager（~/.hermes 凭据/技能同步）
    - init_session 会话快照（env/alias/cwd 带内标记）
    - _ThreadedProcessHandle 通用句柄（骨架用线程 + Event 做中断）
"""

from __future__ import annotations

import math
import shlex
import threading
from typing import Any, Optional


def _import_daytona_sdk():
    """延迟导入 Daytona SDK，未安装时给出明确错误。"""
    try:
        from daytona import (
            CreateSandboxFromImageParams,
            Daytona,
            DaytonaError,
            Resources,
            SandboxState,
        )
    except ImportError as exc:
        raise ImportError(
            "Daytona 后端需要安装 SDK：pip install daytona"
        ) from exc
    return (
        Daytona,
        CreateSandboxFromImageParams,
        DaytonaError,
        Resources,
        SandboxState,
    )


class DaytonaEnvironment:
    """Daytona 云沙箱：创建/恢复、执行命令、中断时 stop、退出时 stop 或 delete。"""

    def __init__(
        self,
        image: str,
        cwd: str = "/home/daytona",
        timeout: int = 60,
        cpu: int = 1,
        memory: int = 5120,
        disk: int = 10240,
        persistent_filesystem: bool = True,
        task_id: str = "default",
    ) -> None:
        """创建或恢复名为 agent-{task_id} 的沙箱，并探测远程 HOME。"""
        (
            Daytona,
            CreateSandboxFromImageParams,
            DaytonaError,
            Resources,
            SandboxState,
        ) = _import_daytona_sdk()

        self.cwd = cwd
        self.timeout = timeout
        self._persistent = persistent_filesystem
        self._task_id = task_id
        self._SandboxState = SandboxState
        self._DaytonaError = DaytonaError
        self._daytona = Daytona()
        self._sandbox = None
        self._lock = threading.Lock()
        self._remote_home = "/root"
        self.disk_capped = False

        memory_gib = max(1, math.ceil(memory / 1024))
        disk_gib = max(1, math.ceil(disk / 1024))
        if disk_gib > 10:
            # Daytona 平台上限 10 GiB（对齐 Hermes）
            disk_gib = 10
            self.disk_capped = True
        resources = Resources(cpu=cpu, memory=memory_gib, disk=disk_gib)

        labels = {"agent_task_id": task_id}
        sandbox_name = f"agent-{task_id}"
        requested_cwd = cwd

        if self._persistent:
            try:
                self._sandbox = self._daytona.get(sandbox_name)
                self._sandbox.start()
            except DaytonaError:
                self._sandbox = None
            except Exception:
                self._sandbox = None

            if self._sandbox is None:
                try:
                    results = self._daytona.list(labels=labels, limit=1)
                    legacy = next(iter(results), None)
                    if legacy is not None:
                        self._sandbox = legacy
                        self._sandbox.start()
                except Exception:
                    self._sandbox = None

        if self._sandbox is None:
            self._sandbox = self._daytona.create(
                CreateSandboxFromImageParams(
                    image=image,
                    name=sandbox_name,
                    labels=labels,
                    auto_stop_interval=0,
                    resources=resources,
                )
            )

        try:
            home = (self._sandbox.process.exec("echo $HOME").result or "").strip()
            if home:
                self._remote_home = home
                if requested_cwd in {"~", "/home/daytona"}:
                    self.cwd = home
        except Exception:
            pass

    def _ensure_sandbox_ready(self) -> None:
        """沙箱被中断 stop 后，下次执行前重新 start。"""
        self._sandbox.refresh_data()
        if self._sandbox.state in {
            self._SandboxState.STOPPED,
            self._SandboxState.ARCHIVED,
        }:
            self._sandbox.start()

    def _shell_command(self, command: str) -> str:
        """把用户命令包进 bash -c，并先 cd 到沙箱工作目录。"""
        inner = f"cd {shlex.quote(self.cwd)} && {command}"
        return f"bash -c {shlex.quote(inner)}"

    def execute(
        self,
        command: str,
        timeout: Optional[int] = None,
        interrupt_event: Any = None,
    ) -> dict[str, Any]:
        """在沙箱里执行一条命令，返回 output / returncode / error / cancelled。

        interrupt_event 置位时调用 sandbox.stop() 并返回 cancelled
        （对齐 Hermes：cancel_fn = sandbox.stop）。
        """
        wait_s = int(timeout if timeout is not None else self.timeout)
        with self._lock:
            self._ensure_sandbox_ready()
        shell_cmd = self._shell_command(command)
        sandbox = self._sandbox

        if interrupt_event is None:
            return self._exec_once(sandbox, shell_cmd, wait_s)

        box: dict[str, Any] = {}
        done = threading.Event()

        def _run() -> None:
            """在后台线程里跑阻塞的 SDK exec。"""
            try:
                box.update(self._exec_once(sandbox, shell_cmd, wait_s))
            except Exception as exc:
                box["output"] = box.get("output") or ""
                box["returncode"] = 1
                box["error"] = str(exc)
            finally:
                done.set()

        threading.Thread(target=_run, daemon=True, name="daytona-exec").start()
        while not done.wait(0.2):
            if interrupt_event.is_set():
                with self._lock:
                    try:
                        sandbox.stop()
                    except Exception:
                        pass
                return {
                    "output": str(box.get("output") or ""),
                    "returncode": -1,
                    "error": "命令被用户中断",
                    "cancelled": True,
                }
        return {
            "output": str(box.get("output") or ""),
            "returncode": int(box.get("returncode") if box.get("returncode") is not None else 1),
            "error": box.get("error"),
            "cancelled": bool(box.get("cancelled")),
        }

    def _exec_once(self, sandbox: Any, shell_cmd: str, timeout: int) -> dict[str, Any]:
        """一次阻塞 SDK 调用；DaytonaError 转为 returncode=1（对齐 Hermes 不再重试）。"""
        try:
            response = sandbox.process.exec(shell_cmd, timeout=timeout)
            return {
                "output": response.result or "",
                "returncode": int(response.exit_code or 0),
            }
        except self._DaytonaError as exc:
            return {"output": "", "returncode": 1, "error": str(exc)}
        except Exception as exc:
            return {"output": "", "returncode": 1, "error": str(exc)}

    def cancel(self) -> None:
        """中断当前沙箱（后台进程 kill / 退出清理共用）。"""
        with self._lock:
            if self._sandbox is None:
                return
            try:
                self._sandbox.stop()
            except Exception:
                pass

    def cleanup(self) -> None:
        """退出时 stop（持久化，保留文件系统）或 delete（一次性沙箱）。"""
        with self._lock:
            if self._sandbox is None:
                return
            try:
                if self._persistent:
                    self._sandbox.stop()
                else:
                    self._daytona.delete(self._sandbox)
            except Exception:
                pass
            self._sandbox = None
