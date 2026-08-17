# -*- coding: utf-8 -*-
"""后台进程注册表（对齐 Hermes tools/process_registry.py，简化版）。

terminal(background=true) 启动的进程在这里登记，供模型用 process 工具管理：
- 滚动输出缓冲：stdout/stderr 由守护线程持续排空，总量封顶（默认 200KB），
  防止子进程写满管道卡死、也防止内存无限膨胀；
- poll：非阻塞查状态 + 已累积输出（返回前再截断到 50KB，避免撑爆模型上下文）；
- wait：阻塞等到进程结束（带超时），拿完整输出；
- kill：整棵树终止（Windows 用 taskkill /T，与 terminal 中断语义一致）；
- shutdown_all：会话/服务退出时兜底清理，防止孤儿进程。
- spawn_via_env：远程后端（Daytona）无本地 Popen 时，用线程 + cancel_fn 登记。

简化掉的部分（Hermes 有）：JSON 检查点崩溃恢复、finished TTL 自动回收、
gateway 会话级保护、notify_on_complete 通知。骨架按"会话内管理"处理：
退出即清理全部后台进程。
"""

import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from tool_output_limits import truncate_output

# 对齐 Hermes：滚动输出缓冲上限 200KB；返回给模型前再截断到 50KB
_MAX_OUTPUT_CHARS = 200_000
_RETURN_OUTPUT_CHARS = 50_000


@dataclass
class BackgroundProcess:
    """一个后台进程：状态 + 输出缓冲 + 句柄。"""

    session_id: str
    command: str
    pid: int
    start_time: str
    status: str = "running"  # running | exited | killed
    exit_code: Optional[int] = None
    output: list[str] = field(default_factory=list)
    stderr: list[str] = field(default_factory=list)
    proc: Any = None
    lock: Any = field(default_factory=threading.Lock)
    # 远程后端（Daytona 等）没有本地 Popen：用 done Event + cancel_fn
    done: Any = None
    cancel_fn: Any = None

    def _append(self, buf: list[str], line: str) -> None:
        """往缓冲追加一行；超上限时从头部丢最旧的行。"""
        with self.lock:
            buf.append(line)
            total = sum(len(x) for x in buf)
            while total > _MAX_OUTPUT_CHARS and len(buf) > 1:
                total -= len(buf.pop(0))

    def snapshot(self) -> dict[str, Any]:
        """取当前状态 + 输出快照（输出截断，防撑爆模型上下文）。"""
        with self.lock:
            output = truncate_output("\n".join(self.output), _RETURN_OUTPUT_CHARS)
            stderr = truncate_output("\n".join(self.stderr), _RETURN_OUTPUT_CHARS)
        return {
            "session_id": self.session_id,
            "pid": self.pid,
            "command": self.command,
            "status": self.status,
            "exit_code": self.exit_code,
            "output": output,
            "stderr": stderr,
        }


_registry: dict[str, BackgroundProcess] = {}
_registry_lock = threading.Lock()
_seq = 0


def _next_session_id() -> str:
    """生成形如 proc-<毫秒时间戳>-<序号> 的会话 id。"""
    global _seq
    with _registry_lock:
        _seq += 1
        return f"proc-{int(time.time() * 1000)}-{_seq}"


def _kill_tree(proc) -> None:
    """杀掉整棵进程树（Windows taskkill /T，与 terminal 中断语义一致）。"""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        else:
            proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _reader_thread(proc, attr: str, buf: list[str], proc_ref) -> None:
    """守护线程持续排空 stdout/stderr，避免子进程写满管道卡死。"""
    stream = getattr(proc, attr)
    try:
        for line in iter(stream.readline, ""):
            proc_ref._append(buf, line.rstrip("\n"))
    except Exception:
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass


def spawn(command: str, cwd: Optional[str] = None) -> dict[str, Any]:
    """后台启动一条命令并登记；返回 session_id / pid / 状态。"""
    session_id = _next_session_id()
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            errors="replace",
            cwd=cwd or os.getcwd(),
        )
    except Exception as exc:
        return {
            "success": False,
            "error": f"后台启动失败：{exc}",
        }

    record = BackgroundProcess(
        session_id=session_id,
        command=command,
        pid=proc.pid,
        start_time=time.strftime("%Y-%m-%d %H:%M:%S"),
        proc=proc,
    )
    threading.Thread(
        target=_reader_thread,
        args=(proc, "stdout", record.output, record),
        daemon=True,
        name=f"bg-out-{session_id}",
    ).start()
    threading.Thread(
        target=_reader_thread,
        args=(proc, "stderr", record.stderr, record),
        daemon=True,
        name=f"bg-err-{session_id}",
    ).start()

    with _registry_lock:
        _registry[session_id] = record
    return {
        "success": True,
        "session_id": session_id,
        "pid": proc.pid,
        "command": command,
        "status": "running",
        "message": (
            f"已在后台启动（{session_id}）。用 process(action=poll) 查状态、"
            "process(action=wait) 等结束、process(action=kill) 终止。"
        ),
    }


def spawn_via_env(
    command: str,
    execute_fn,
    cancel_fn=None,
) -> dict[str, Any]:
    """在远程环境（Daytona 等）后台执行命令（对齐 Hermes spawn_via_env 思路）。

    execute_fn() 返回 {output, returncode, error?, cancelled?}；
    cancel_fn() 用于 kill / shutdown（例如 sandbox.stop）。
    """
    session_id = _next_session_id()
    done = threading.Event()
    record = BackgroundProcess(
        session_id=session_id,
        command=command,
        pid=0,
        start_time=time.strftime("%Y-%m-%d %H:%M:%S"),
        proc=None,
        done=done,
        cancel_fn=cancel_fn,
    )

    def _worker() -> None:
        """在后台线程里跑远程 execute，把输出写入登记表。"""
        try:
            result = execute_fn() or {}
            output = str(result.get("output") or "")
            error = str(result.get("error") or "")
            if output:
                record._append(record.output, output)
            if error:
                record._append(record.stderr, error)
            record.exit_code = int(
                result.get("returncode") if result.get("returncode") is not None else 1
            )
            with record.lock:
                if record.status == "running":
                    record.status = (
                        "killed" if result.get("cancelled") else "exited"
                    )
        except Exception as exc:
            record._append(record.stderr, str(exc))
            record.exit_code = -1
            with record.lock:
                if record.status == "running":
                    record.status = "exited"
        finally:
            done.set()

    threading.Thread(
        target=_worker, daemon=True, name=f"bg-env-{session_id}"
    ).start()
    with _registry_lock:
        _registry[session_id] = record
    return {
        "success": True,
        "session_id": session_id,
        "pid": 0,
        "command": command,
        "status": "running",
        "backend": "remote",
        "message": (
            f"已在远程沙箱后台启动（{session_id}）。用 process(action=poll) 查状态、"
            "process(action=wait) 等结束、process(action=kill) 终止。"
        ),
    }


def _get(session_id: str) -> Optional[BackgroundProcess]:
    """按 session_id 取记录；不存在返回 None。"""
    with _registry_lock:
        return _registry.get(session_id)


def _refresh_status(record: BackgroundProcess) -> None:
    """进程已结束时把状态刷成 exited（幂等）。"""
    if record.status != "running":
        return
    if record.proc is not None:
        code = record.proc.poll()
        if code is not None:
            record.status = "exited"
            record.exit_code = code
    elif record.done is not None and record.done.is_set():
        # 远程 worker 线程已结束但尚未写入 status 的兜底
        if record.status == "running":
            record.status = "exited"


def poll(session_id: str) -> dict[str, Any]:
    """非阻塞查后台进程状态 + 已累积输出。"""
    record = _get(session_id)
    if record is None:
        return {"success": False, "error": f"未知后台进程：{session_id}"}
    _refresh_status(record)
    data = record.snapshot()
    data["success"] = True
    return data


def wait(session_id: str, timeout: int = 300) -> dict[str, Any]:
    """阻塞等到进程结束（带超时）；超时返回当前状态与部分输出。"""
    record = _get(session_id)
    if record is None:
        return {"success": False, "error": f"未知后台进程：{session_id}"}
    try:
        if record.proc is not None:
            record.proc.wait(timeout=max(1, int(timeout)))
        elif record.done is not None:
            record.done.wait(timeout=max(1, int(timeout)))
    except subprocess.TimeoutExpired:
        pass
    except Exception as exc:
        return {"success": False, "error": f"等待失败：{exc}"}
    _refresh_status(record)
    data = record.snapshot()
    data["success"] = True
    if record.status == "running":
        data["message"] = f"等待超时（>{timeout}s），进程仍在运行。"
    return data


def kill(session_id: str) -> dict[str, Any]:
    """终止后台进程（整棵树）并标记 killed。"""
    record = _get(session_id)
    if record is None:
        return {"success": False, "error": f"未知后台进程：{session_id}"}
    if record.status == "running":
        if record.cancel_fn is not None:
            try:
                record.cancel_fn()
            except Exception:
                pass
        elif record.proc is not None:
            _kill_tree(record.proc)
        record.status = "killed"
        if record.done is not None:
            record.done.set()
    return {
        "success": True,
        "session_id": session_id,
        "pid": record.pid,
        "status": record.status,
    }


def shutdown_all() -> int:
    """终止所有仍运行的后台进程；返回清理数量（会话/服务退出时兜底）。"""
    with _registry_lock:
        records = list(_registry.values())
    killed = 0
    for record in records:
        if record.status == "running":
            if record.cancel_fn is not None:
                try:
                    record.cancel_fn()
                except Exception:
                    pass
            elif record.proc is not None:
                _kill_tree(record.proc)
            record.status = "killed"
            if record.done is not None:
                record.done.set()
            killed += 1
    with _registry_lock:
        _registry.clear()
    return killed


def process_tool(args: dict[str, Any]) -> str:
    """模型可见的 process 工具入口：action=poll|wait|kill，返回 JSON。"""
    action = str(args.get("action", "poll") or "poll").lower()
    session_id = str(args.get("session_id") or "").strip()
    if not session_id:
        return json.dumps(
            {"success": False, "error": "session_id 必填（terminal background=true 的返回值）"},
            ensure_ascii=False,
        )
    if action == "kill":
        result = kill(session_id)
    elif action == "wait":
        result = wait(session_id, timeout=int(args.get("timeout", 300) or 300))
    else:
        result = poll(session_id)
    return json.dumps(result, ensure_ascii=False)
