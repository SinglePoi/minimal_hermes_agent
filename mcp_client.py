# -*- coding: utf-8 -*-
"""MCP（Model Context Protocol）客户端（零依赖，对齐 Hermes tools/mcp_tool.py 简化版）。

通过 stdio 传输连接外部 MCP 服务器：启动子进程 → JSON-RPC 2.0
（initialize → notifications/initialized → tools/list → tools/call），把服务器
暴露的工具注册进骨架 TOOLS，模型可以像内置工具一样调用（工具名带
``mcp__<服务器>__<工具>`` 前缀，对齐 Hermes 的 mcp_prefixed_tool_name）。

配置（默认项目根目录 mcp_servers.json，路径可用 MCP_SERVERS_PATH 覆盖；文件
已 gitignore，密钥走 ${VAR} 插值不落盘）：:

    {
      "filesystem": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
        "env": {"TOKEN": "${GITHUB_TOKEN}"},
        "timeout": 120,
        "connect_timeout": 30,
        "supports_parallel_tool_calls": false
      }
    }

安全（对齐 Hermes）：
    - 子进程环境只透传安全基线变量 + 配置显式给出的 env（防密钥泄露给外部进程）
    - command/args/env 支持 ${VAR} / ${env:VAR} 插值（从当前进程环境读取）
    - 工具结果与错误文本回传前过 truncate_output 截断 + redact_sensitive_text 脱敏
    - 工具调用超时视为服务器卡死：终止子进程，后续调用返回"未连接"，不挂死 Agent Loop

未实现（Hermes 有，骨架不做，见 HANDOFF 已知限制）：
    - HTTP / StreamableHTTP / SSE 传输（仅 stdio）
    - 自动重连、sampling（服务器请求 LLM）、resources/prompts 工具

注意：MCP 规范要求 JSON 走 UTF-8。中文 Windows 下自研 Python MCP 服务器默认
stdout 是 GBK，需要自己在服务器里 reconfigure(encoding="utf-8")（测试用的
tests/fake_mcp_server.py 已示范）；Node 等生态服务器通常天然输出 UTF-8。
"""

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from redact import redact_sensitive_text
from tool_output_limits import truncate_output

ROOT = Path(__file__).resolve().parent

# 工具名前缀与分隔符（对齐 Hermes：mcp__<sanitizedServer>__<sanitizedTool>）
MCP_TOOL_NAME_PREFIX = "mcp__"
_NAME_DELIM = "__"

# MCP 协议版本：声明 2024-11-05（广泛兼容）；服务器返回自己的版本，骨架不做强校验
_PROTOCOL_VERSION = "2024-11-05"
_CLIENT_INFO = {"name": "minimal-agent", "version": "1.0"}

# ---- 环境变量过滤（对齐 Hermes _SAFE_ENV_KEYS / _SAFE_ENV_KEYS_CASE_INSENSITIVE）----
_SAFE_ENV_KEYS = frozenset({
    "PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM", "SHELL", "TMPDIR",
})
_SAFE_ENV_KEYS_CASE_INSENSITIVE = frozenset({
    "ALLUSERSPROFILE", "APPDATA", "COMMONPROGRAMFILES",
    "COMMONPROGRAMFILES(X86)", "COMMONPROGRAMW6432", "COMPUTERNAME",
    "COMSPEC", "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA",
    "NUMBER_OF_PROCESSORS", "OS", "PATHEXT", "PROCESSOR_ARCHITECTURE",
    "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432",
    "PUBLIC", "SYSTEMDRIVE", "SYSTEMROOT", "TEMP", "TMP", "USERDOMAIN",
    "USERNAME", "USERPROFILE", "WINDIR",
})

# ${VAR} / ${env:VAR} 插值（对齐 Hermes _ENV_VAR_PATTERN / _env_ref_name）
_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")
_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_PARALLEL_TOOL_NAMES: set[str] = set()
_PARALLEL_LOCK = threading.Lock()


class McpError(RuntimeError):
    """MCP 连接/协议/调用错误（统一异常类型，调用方捕获后转可读消息）。"""


def sanitize_mcp_name_component(value: str) -> str:
    """把服务器名/工具名清洗成安全的工具名片段（非 [A-Za-z0-9_-] 替换为 _）。"""
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(value or ""))


def mcp_prefixed_tool_name(server_name: str, tool_name: str) -> str:
    """生成注册/调用用的完整工具名：mcp__<服务器>__<工具>（对齐 Hermes）。"""
    return (
        f"{MCP_TOOL_NAME_PREFIX}"
        f"{sanitize_mcp_name_component(server_name)}"
        f"{_NAME_DELIM}{sanitize_mcp_name_component(tool_name)}"
    )


def parse_mcp_tool_name(name: str) -> tuple[str, str] | None:
    """把 mcp__<服务器>__<工具> 拆回 (服务器名, 工具名)；非 MCP 工具返回 None。"""
    if not name.startswith(MCP_TOOL_NAME_PREFIX):
        return None
    rest = name[len(MCP_TOOL_NAME_PREFIX):]
    server, _, tool = rest.partition(_NAME_DELIM)
    if not server or not tool:
        return None
    return server, tool


def _env_ref_name(ref: str) -> str:
    """规范化 ${...} 引用：支持 ${env:VAR} 前缀（对齐 Hermes）。"""
    ref = ref.strip()
    if ref.startswith("env:"):
        ref = ref[len("env:"):].strip()
    return ref


def _interpolate(value: Any) -> Any:
    """递归替换字符串里的 ${VAR} / ${env:VAR}（从当前环境读取，未定义保留原文）。"""
    if isinstance(value, str):
        def _sub(match: re.Match) -> str:
            name = _env_ref_name(match.group(1))
            return os.environ.get(name, match.group(0))
        return _ENV_VAR_PATTERN.sub(_sub, value)
    if isinstance(value, list):
        return [_interpolate(item) for item in value]
    if isinstance(value, dict):
        return {key: _interpolate(item) for key, item in value.items()}
    return value


def _build_safe_env(user_env: dict | None) -> dict:
    """构建子进程环境：只透传安全基线变量 + 配置显式给出的 env（对齐 Hermes）。"""
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if (
            key in _SAFE_ENV_KEYS
            or key.upper() in _SAFE_ENV_KEYS_CASE_INSENSITIVE
            or key.startswith("XDG_")
        ):
            env[key] = value
    for key, value in (user_env or {}).items():
        if isinstance(value, str):
            env[key] = value
    return env


def _mcp_config_path() -> Path | None:
    """返回 MCP 配置文件路径；MCP_SERVERS_PATH 为空表示禁用。"""
    raw = os.environ.get("MCP_SERVERS_PATH", "").strip()
    if raw == "":
        return None
    path = Path(raw)
    return path if path.is_absolute() else ROOT / path


def _load_servers_config() -> tuple[dict, list[str]]:
    """读取并校验 mcp_servers 配置；返回 (servers, 加载提示列表)。"""
    path = _mcp_config_path()
    if path is None or not path.exists():
        return {}, []
    try:
        # utf-8-sig：兼容 PowerShell/记事本保存时带 BOM 的 JSON（Windows 常见）
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError) as exc:
        return {}, [f"MCP 配置读取失败（{path}）：{exc}"]
    if not isinstance(data, dict):
        return {}, [f"MCP 配置格式错误（{path}）：顶层必须是对象"]
    servers: dict[str, dict] = {}
    messages: list[str] = []
    for name, cfg in data.items():
        if not isinstance(cfg, dict):
            messages.append(f"MCP 服务器 '{name}' 配置不是对象，跳过")
            continue
        command = cfg.get("command")
        if not isinstance(command, str) or not command.strip():
            messages.append(f"MCP 服务器 '{name}' 缺少 command，跳过")
            continue
        args = cfg.get("args") or []
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            messages.append(f"MCP 服务器 '{name}' 的 args 必须是字符串数组，跳过")
            continue
        env = cfg.get("env", {})
        if not isinstance(env, dict):
            messages.append(f"MCP 服务器 '{name}' 的 env 必须是对象，跳过")
            continue
        servers[name] = {
            "command": command,
            "args": list(args),
            "env": {str(k): str(v) for k, v in env.items()},
            "timeout": _positive_int(cfg.get("timeout"), 120),
            "connect_timeout": _positive_int(cfg.get("connect_timeout"), 30),
            "supports_parallel_tool_calls": bool(
                cfg.get("supports_parallel_tool_calls", False)
            ),
        }
    return servers, messages


def _positive_int(value: Any, default: int) -> int:
    """解析正整数配置；非法回退默认值。"""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _convert_mcp_schema(server_name: str, tool: dict) -> dict:
    """把 MCP tools/list 里的工具条目转成骨架 TOOLS 的 OpenAI function schema。"""
    name = mcp_prefixed_tool_name(server_name, str(tool.get("name") or ""))
    description = str(tool.get("description") or "") or (
        f"MCP 工具 {tool.get('name')}（来自服务器 {server_name}）"
    )
    schema = tool.get("inputSchema") or {}
    parameters = (
        schema
        if isinstance(schema, dict)
        else {"type": "object", "properties": {}}
    )
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }


class McpServer:
    """单个 MCP 服务器的 stdio 客户端（JSON-RPC 2.0，串行请求）。"""

    def __init__(self, name: str, config: dict) -> None:
        self.name = name
        self.command: str = config["command"]
        self.args: list[str] = config["args"]
        self.env: dict = config["env"]
        self.timeout: int = config["timeout"]
        self.connect_timeout: int = config["connect_timeout"]
        self.supports_parallel: bool = config["supports_parallel_tool_calls"]
        self.tools: list[dict] = []          # 原始 MCP 工具条目（含 inputSchema）
        self.error: str = ""                 # 连接失败原因（供状态展示）
        self.proc: subprocess.Popen | None = None
        self._queue: "queue.Queue[Any]" = queue.Queue()
        self._lock = threading.Lock()
        self._id_counter = 0
        self._reader: threading.Thread | None = None
        self._stderr_lines: list[str] = []
        self._stderr_lock = threading.Lock()

    # ---- 生命周期 ----

    def connect(self) -> bool:
        """启动子进程并完成 initialize + tools/list；失败时 self.error 带原因。"""
        env = _build_safe_env(_interpolate(self.env))
        try:
            # Windows 下 npx 是 npx.cmd（无 .exe），Popen 不解析 .cmd；
            # shutil.which 按 PATHEXT 解析出真实路径（找不到则回退原命令）
            command = shutil.which(_interpolate(self.command)) or _interpolate(self.command)
            self.proc = subprocess.Popen(
                [command] + list(_interpolate(self.args)),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=str(ROOT),
                env=env,
            )
        except OSError as exc:
            self.error = f"启动命令失败：{exc}"
            self.proc = None
            return False
        self._reader = threading.Thread(
            target=self._read_loop, name=f"mcp-reader-{self.name}", daemon=True
        )
        self._reader.start()
        threading.Thread(
            target=self._drain_stderr,
            name=f"mcp-stderr-{self.name}",
            daemon=True,
        ).start()
        try:
            with self._lock:
                init_result = self._request(
                    "initialize",
                    {
                        "protocolVersion": _PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": _CLIENT_INFO,
                    },
                    timeout=self.connect_timeout,
                )
                if not isinstance(init_result, dict):
                    raise McpError("initialize 返回格式异常")
                self._write({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
                list_result = self._request("tools/list", {}, timeout=self.connect_timeout)
        except McpError as exc:
            self.error = str(exc)
            self._kill()
            return False
        self.tools = list_result.get("tools") or []
        # 分页兜底：服务器声明 nextCursor 时继续拉（对齐 Hermes _paginate_full_list）
        cursor = list_result.get("nextCursor")
        while cursor:
            try:
                page = self._request(
                    "tools/list", {"cursor": cursor}, timeout=self.connect_timeout
                )
            except McpError as exc:
                self.error = str(exc)
                self._kill()
                return False
            self.tools.extend(page.get("tools") or [])
            cursor = page.get("nextCursor")
        return True

    def call_tool(self, tool_name: str, arguments: dict) -> dict:
        """调用一个工具；返回 {"text": ..., "is_error": bool}（异常也转成消息）。"""
        try:
            with self._lock:
                result = self._request(
                    "tools/call",
                    {"name": tool_name, "arguments": arguments or {}},
                    timeout=self.timeout,
                )
        except McpError as exc:
            # 超时/服务器退出视为卡死：终止子进程，后续调用返回未连接（不挂死循环）
            self._kill()
            return {"text": f"MCP 工具调用失败：{exc}", "is_error": True}
        parts: list[str] = []
        for block in result.get("content") or []:
            if not isinstance(block, dict):
                continue
            text = block.get("text")
            if text:
                parts.append(str(text))
                continue
            resource = block.get("resource")
            if isinstance(resource, dict) and resource.get("text"):
                parts.append(str(resource["text"]))
        text = "\n".join(parts)
        if result.get("isError"):
            return {"text": f"MCP 工具返回错误：{text or '(无内容)'}", "is_error": True}
        return {"text": text, "is_error": False}

    def close(self) -> None:
        """发 exit 通知（尽力而为）并终止子进程。"""
        proc = self.proc
        self.proc = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                try:
                    proc.stdin.write(
                        json.dumps(
                            {"jsonrpc": "2.0", "method": "notifications/exit"}
                        )
                        + "\n"
                    )
                    proc.stdin.flush()
                except Exception:
                    pass
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:
                    pass

    # ---- JSON-RPC 内部 ----

    def _next_id(self) -> int:
        """生成自增请求 id。"""
        self._id_counter += 1
        return self._id_counter

    def _write(self, message: dict) -> None:
        """写一行 JSON-RPC 到子进程 stdin。"""
        proc = self.proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            raise McpError("MCP 服务器未连接")
        proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        proc.stdin.flush()

    def _request(self, method: str, params: dict, timeout: float) -> dict:
        """发送请求并等待匹配 id 的响应；超时/退出/协议错误抛 McpError。"""
        req_id = self._next_id()
        self._write({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McpError(f"MCP 请求 {method} 超时（{timeout}s）")
            try:
                message = self._queue.get(timeout=remaining)
            except queue.Empty:
                raise McpError(f"MCP 请求 {method} 超时（{timeout}s）")
            if message is None:
                tail = self._stderr_tail()
                detail = f"；服务器 stderr：{tail}" if tail else ""
                raise McpError(f"MCP 服务器已退出{detail}")
            if message.get("id") == req_id:
                if "error" in message:
                    error = message["error"] or {}
                    raise McpError(str(error.get("message") or error))
                return message.get("result") or {}
            # 其他 id 的响应或通知：串行请求下理论不会出现，忽略

    def _read_loop(self) -> None:
        """后台读 stdout 行 → 队列；EOF 放 None 哨兵。"""
        try:
            proc = self.proc
            if proc is None or proc.stdout is None:
                return
            for raw in proc.stdout:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    self._queue.put(json.loads(raw))
                except ValueError:
                    continue
        except Exception:
            pass
        finally:
            try:
                self._queue.put(None)
            except Exception:
                pass

    def _drain_stderr(self) -> None:
        """后台收集 stderr（有界保留尾部），供退出诊断。"""
        try:
            proc = self.proc
            if proc is None or proc.stderr is None:
                return
            for raw in proc.stderr:
                line = raw.rstrip("\r\n")
                with self._stderr_lock:
                    self._stderr_lines.append(line)
                    if len(self._stderr_lines) > 20:
                        del self._stderr_lines[:-20]
        except Exception:
            pass

    def _stderr_tail(self) -> str:
        """返回最近 stderr 内容（脱敏，供错误消息）。"""
        with self._stderr_lock:
            text = "\n".join(self._stderr_lines[-5:])
        return redact_sensitive_text(text, force=True)

    def _kill(self) -> None:
        """强制终止子进程并清理（连接失败/调用超时兜底）。"""
        self.close()


class McpManager:
    """MCP 服务器集合：加载/注册工具/调用路由/关闭（进程内单例）。"""

    def __init__(self) -> None:
        self._servers: dict[str, McpServer] = {}
        self._tools: list[dict] = []
        self._tool_names: set[str] = set()
        self._messages: list[str] = []
        self._loaded = False
        self._lock = threading.Lock()

    def ensure_loaded(self) -> list[str]:
        """首次调用时读取配置并连接所有服务器；返回加载提示（幂等）。"""
        with self._lock:
            if self._loaded:
                return list(self._messages)
            self._loaded = True
            servers, messages = _load_servers_config()
            self._messages.extend(messages)
            for name, config in servers.items():
                server = McpServer(name, config)
                if not server.connect():
                    self._messages.append(
                        f"MCP 服务器 '{name}' 连接失败：{server.error}"
                    )
                    continue
                added = 0
                for tool in server.tools:
                    schema = _convert_mcp_schema(name, tool)
                    prefixed = schema["function"]["name"]
                    if prefixed in self._tool_names:
                        self._messages.append(f"MCP 工具重名跳过：{prefixed}")
                        continue
                    self._tool_names.add(prefixed)
                    self._tools.append(schema)
                    if server.supports_parallel:
                        with _PARALLEL_LOCK:
                            _PARALLEL_TOOL_NAMES.add(prefixed)
                    added += 1
                self._servers[name] = server
                self._messages.append(
                    f"MCP 服务器 '{name}' 已连接：注册 {added} 个工具"
                )
            return list(self._messages)

    def tool_schemas(self) -> list[dict]:
        """返回全部 MCP 工具的 OpenAI function schema。"""
        self.ensure_loaded()
        return list(self._tools)

    def tool_names(self) -> set[str]:
        """返回全部 MCP 工具名。"""
        self.ensure_loaded()
        return set(self._tool_names)

    def parallel_tool_names(self) -> set[str]:
        """返回配置了 supports_parallel_tool_calls 的工具名（其余 MCP 工具串行）。"""
        self.ensure_loaded()
        with _PARALLEL_LOCK:
            return set(_PARALLEL_TOOL_NAMES)

    def call_tool(self, name: str, args: dict) -> str | None:
        """调用 MCP 工具；非 MCP 工具名返回 None（让 run_tool 继续走内置路由）。"""
        parsed = parse_mcp_tool_name(name)
        if parsed is None:
            return None
        self.ensure_loaded()
        server_name, tool_name = parsed
        server = self._servers.get(server_name)
        if server is None:
            return (
                f"MCP 服务器 '{server_name}' 未连接"
                "（启动失败或已关闭），不要重试该工具"
            )
        result = server.call_tool(tool_name, args)
        text = result["text"]
        if result["is_error"]:
            text = redact_sensitive_text(truncate_output(text), force=True)
            return text
        # 成功结果：截断（默认 50000）+ 脱敏后回传，防撑爆上下文/泄露密钥
        return redact_sensitive_text(truncate_output(text), force=True)

    def status(self) -> list[dict]:
        """返回各服务器连接状态（供 /plugins 等展示）。"""
        self.ensure_loaded()
        rows = []
        for name, server in self._servers.items():
            rows.append(
                {
                    "name": name,
                    "type": "mcp",
                    "active": True,
                    "tools": len(server.tools),
                    "parallel": server.supports_parallel,
                }
            )
        return rows

    def shutdown(self) -> None:
        """关闭所有 MCP 子进程并重置状态（服务退出/测试隔离）。"""
        with self._lock:
            for server in self._servers.values():
                server.close()
            self._servers.clear()
            self._tools.clear()
            self._tool_names.clear()
            self._messages.clear()
            self._loaded = False
            with _PARALLEL_LOCK:
                _PARALLEL_TOOL_NAMES.clear()


# 进程内单例 + 便捷函数
manager = McpManager()


def get_mcp_tool_schemas() -> list[dict]:
    """供 get_tools 接入：MCP 工具 schema（触发加载）。"""
    return manager.tool_schemas()


def get_mcp_tool_names() -> set[str]:
    """供 available_tool_names 接入。"""
    return manager.tool_names()


def get_mcp_parallel_tool_names() -> set[str]:
    """供 tool_dispatch 注册并行白名单（仅服务器显式声明支持时）。"""
    return manager.parallel_tool_names()


def call_mcp_tool(name: str, args: dict) -> str | None:
    """run_tool 的 MCP 分发入口；非 MCP 工具返回 None。"""
    return manager.call_tool(name, args)


def mcp_status() -> list[dict]:
    """MCP 服务器状态（供 server /plugins 展示）。"""
    return manager.status()


def shutdown_mcp() -> None:
    """关闭全部 MCP 子进程（服务退出时调用）。"""
    manager.shutdown()


def register_parallel_tool_names(names: set[str]) -> None:
    """把声明支持并行的 MCP 工具名注册进 tool_dispatch 的并行判定（幂等）。"""
    with _PARALLEL_LOCK:
        _PARALLEL_TOOL_NAMES.update(names)


def parallel_safe_mcp_tool(name: str) -> bool:
    """tool_dispatch 查询：该 MCP 工具是否显式声明可并行。"""
    with _PARALLEL_LOCK:
        return name in _PARALLEL_TOOL_NAMES
