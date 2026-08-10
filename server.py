# -*- coding: utf-8 -*-
"""
HTTP 服务化 + gateway 审批通知（为前端铺路，对齐 Hermes dashboard/gateway 思路）

启动：
    python server.py [host] [port]      # 默认 127.0.0.1:8000

端点：
    POST /chat              发一条消息；body: {"message": "...", "session_id": "..."?}
    POST /chat/stream       同上，但 SSE 流式返回（event: activity/token/message/error/done）
    GET  /approvals/pending 轮询待审批；query: ?session_id=xxx
    POST /approvals/resolve 解决审批；body: {"session_id", "choice": once|session|always|deny, "reason"?}
    GET  /health            探活
    GET  /                  前端页面（web/index.html）
    GET  /web/<file>        前端静态资源（app.js / style.css 等）
    GET  /sessions          会话列表（按最后活跃倒序）
    GET  /sessions/<id>/messages  指定会话的历史消息（前端回显用）
    POST /sessions/<id>/archive  归档/取消归档；body: {"archived": true|false}
    GET  /skills                 技能列表（name + description）
    GET  /plugins                记忆 provider 插件列表（name + description + active）
    GET  /tools                  可用工具列表（核心 TOOLS + provider 自带工具）
    GET  /working_diff           工作区 git 改动（stat + diff + untracked；mode/paths 查询参数）
    GET  /sessions/<id>/export   导出会话（?format=md|html，默认 md，附件下载）

审批流程（对齐 Hermes 的网关队列）：
    - /chat 请求里的 agent 线程在危险命令处通过 approval.py 的网关队列阻塞等待
      （register_gateway_notify + _await_gateway_decision + resolve_gateway_approval）
    - 客户端轮询 GET /approvals/pending 看到待审批项，POST resolve 后线程被
      "按响门铃"唤醒，/chat 返回最终结果

零新依赖：Python 标准库 http.server（ThreadingHTTPServer 天然支持并发连接，
resolve 与 /chat 可以同时进行）。
"""

import json
import hmac
import os
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
# 配置源：.env 优先于系统环境变量（与 minimal_agent.py 保持一致）
load_dotenv(override=True)

import minimal_agent  # noqa: E402
import dashboard_auth  # noqa: E402
import skills  # noqa: E402
import title_generator  # noqa: E402
import working_diff  # noqa: E402
import process_registry  # noqa: E402
import session_export  # noqa: E402
from approval import (  # noqa: E402
    list_pending_approvals,
    register_gateway_notify,
    resolve_gateway_approval,
    unregister_gateway_notify,
)
from memory_manager import SyncWorker, list_provider_plugins  # noqa: E402

# 前端静态资源目录（对齐 Hermes web/ 的命名；本骨架用原生 HTML/CSS/JS，
# 零构建、零新依赖，由 server.py 直接托管，与 API 同源避免跨域）
WEB_DIR = ROOT / "web"

_MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
}


def _is_within(path: Path, base: Path) -> bool:
    """判断 path 是否位于 base 目录内（静态文件路径穿越防护）。"""
    try:
        base_resolved = str(base.resolve())
        return os.path.commonpath([str(path.resolve()), base_resolved]) == base_resolved
    except (OSError, ValueError):
        return False


# 服务鉴权与操作审计（对齐 Hermes plugins/dashboard_auth 的思路，简化为单静态 token）：
# - SERVER_AUTH_TOKEN：Bearer token；留空 = 不鉴权（本地开发）
# - AUDIT_LOG_PATH：审计日志路径（JSON Lines，每请求一行）；空串 = 关闭审计
AUDIT_LOG_LOCK = threading.Lock()


def _auth_token() -> str:
    """读取服务鉴权 token（生产密钥只走环境变量注入，未配置则鉴权关闭）。"""
    return os.environ.get("SERVER_AUTH_TOKEN", "").strip()


def _audit_log_path() -> Path:
    """读取审计日志路径；空串表示关闭审计，默认落在项目根目录 audit.log。"""
    raw = os.environ.get("AUDIT_LOG_PATH", "").strip()
    return Path(raw) if raw else ROOT / "audit.log"


def _audit_action(path: str, method: str = "GET") -> str:
    """把请求映射成简短动作名（审计日志用；method 用于区分同名路径的不同动作）。"""
    if path.startswith("/api/auth/login"):
        return "auth:login"
    if path.startswith("/api/auth/logout"):
        return "auth:logout"
    if path.startswith("/api/auth/me"):
        return "auth:me"
    if path.startswith("/api/auth/config"):
        return "auth:config"
    if path.startswith("/chat/stream"):
        return "chat:stream"
    if path.startswith("/chat"):
        return "chat"
    if path.startswith("/approvals/pending"):
        return "approvals:pending"
    if path.startswith("/approvals/resolve"):
        return "approvals:resolve"
    if path.startswith("/sessions/") and path.endswith("/fork"):
        return "sessions:fork"
    if path.startswith("/sessions/") and path.endswith("/archive"):
        return "sessions:archive"
    if path.startswith("/sessions/") and path.endswith("/export"):
        return "sessions:export"
    if path.startswith("/sessions/") and path.endswith("/messages"):
        return "sessions:messages"
    if path.startswith("/sessions/"):
        return "sessions:delete" if method == "DELETE" else "sessions:title"
    if path.startswith("/sessions"):
        return "sessions:list"
    if path.startswith("/skills"):
        return "skills"
    if path.startswith("/plugins"):
        return "plugins"
    if path.startswith("/tools"):
        return "tools"
    if path.startswith("/working_diff"):
        return "working_diff"
    if path.startswith("/health"):
        return "health"
    if path.startswith("/web/") or path in ("/", "/index.html"):
        return "static"
    return "other"


class AgentServer:
    """持有客户端、会话状态与后台 worker；/chat 处理一条消息。"""

    def __init__(self, client=None, memory_manager=None) -> None:
        self.client = client or minimal_agent.create_client()
        self.manager = memory_manager
        self.review_worker = SyncWorker()
        self.tools = minimal_agent.get_tools(self.manager)
        self.sessions: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        # LLM 标题生成的后台线程（首轮交换后异步），shutdown 时 join 排空
        self._title_threads: list[threading.Thread] = []
        # 网关通知回调：数据本身已进 approval.py 的队列，回调只需"发出消息"；
        # 我们的"消息"就是轮询端点可读的 pending 列表
        self._notify = lambda data: None

    def get_session(self, session_id: str) -> dict[str, Any]:
        """取会话状态；首次访问时按 main() 的启动逻辑初始化（历史/提示词恢复）。"""
        with self._lock:
            if session_id in self.sessions:
                return self.sessions[session_id]

        messages: list[dict[str, Any]] = []
        history = minimal_agent.load_session_history(session_id)
        if history:
            messages = history
            # 恢复会话时水合 todo 清单（对齐 Hermes _hydrate_todo_store）
            minimal_agent.hydrate_todo_store(messages, session_id)
        system_prompt = minimal_agent.load_session_prompt(session_id)
        if system_prompt is None:
            system_prompt = minimal_agent.build_system_prompt(self.manager)
            minimal_agent.save_session_prompt(session_id, system_prompt)
        messages.insert(0, {"role": "system", "content": system_prompt})

        state: dict[str, Any] = {
            "messages": messages,
            "persisted_count": len(messages),
            "turn_count": 0,
            "turns_since_memory": (
                minimal_agent.hydrate_nudge_counter(
                    sum(1 for m in messages if m.get("role") == "user"),
                    minimal_agent.MEMORY_NUDGE_INTERVAL,
                )
                if history
                else 0
            ),
            # 同一会话的 /chat 串行执行锁（对齐 Hermes 的 turn lease 语义：
            # 前端单飞 + 服务端锁双重保险，避免并发请求竞争 messages 列表）
            "lock": threading.Lock(),
        }
        # 网关会话：注册审批通知（危险命令将走队列阻塞，而非终端输入）
        register_gateway_notify(session_id, self._notify)
        with self._lock:
            self.sessions[session_id] = state
        return state

    def handle_message(self, session_id: str, state: dict, message: str) -> tuple[str, list, list]:
        """处理一条消息，返回 (最终回答, 过程事件, 当前任务清单)。"""
        events: list[dict[str, Any]] = []
        with state["lock"]:
            turn_count, turns_since_memory, persisted_count = minimal_agent.process_turn(
                self.client,
                state["messages"],
                self.tools,
                self.manager,
                session_id,
                message,
                state["turn_count"],
                state["turns_since_memory"],
                self.review_worker,
                state["persisted_count"],
                events,
            )
            state["turn_count"] = turn_count
            state["turns_since_memory"] = turns_since_memory
            state["persisted_count"] = persisted_count
            last = state["messages"][-1] if state["messages"] else {}
            reply = last.get("content", "") if last.get("role") == "assistant" else ""
            if reply:
                thread = title_generator.maybe_auto_title(
                    session_id,
                    message,
                    reply,
                    client=self.client,
                    conversation_history=state["messages"],
                )
                if thread is not None:
                    self._title_threads.append(thread)
            todos = minimal_agent.get_todo_store(session_id).read()
            return reply, events, todos

    def shutdown(self) -> None:
        """排空后台任务、注销所有网关回调。"""
        for thread in self._title_threads:
            thread.join(timeout=5)
        self._title_threads.clear()
        # 后台进程兜底清理：服务退出即终止会话内启动的后台进程（防孤儿）
        process_registry.shutdown_all()
        for session_id in list(self.sessions):
            unregister_gateway_notify(session_id)
        self.review_worker.flush(timeout=10)
        self.review_worker.shutdown()
        if self.manager is not None:
            self.manager.flush_pending(timeout=10)
            self.manager.shutdown()

    def remove_session(self, session_id: str) -> None:
        """删除会话的进程内状态（会话字典 + 网关审批注册），供 DELETE 端点使用。"""
        with self._lock:
            if session_id in self.sessions:
                unregister_gateway_notify(session_id)
                del self.sessions[session_id]


    def handle_message_stream(
        self,
        session_id: str,
        state: dict,
        message: str,
        emit_event,
        emit_token,
        interrupt_event: threading.Event | None = None,
    ) -> str:
        """处理一条消息（SSE 流式）：思考/工具/召回事件实时经 emit_event 推送，
        回复 token 经 emit_token 推送；返回最终回答文本。

        interrupt_event 供调用方在客户端断开时置位，中断本轮工具执行（对齐 Hermes interrupt）。
        """
        events: list[dict[str, Any]] = []
        with state["lock"]:
            turn_count, turns_since_memory, persisted_count = minimal_agent.process_turn(
                self.client,
                state["messages"],
                self.tools,
                self.manager,
                session_id,
                message,
                state["turn_count"],
                state["turns_since_memory"],
                self.review_worker,
                state["persisted_count"],
                events,
                sink=emit_event,
                on_token=emit_token,
                interrupt_event=interrupt_event,
            )
            state["turn_count"] = turn_count
            state["turns_since_memory"] = turns_since_memory
            state["persisted_count"] = persisted_count
            last = state["messages"][-1] if state["messages"] else {}
            reply = last.get("content", "") if last.get("role") == "assistant" else ""
            if reply:
                thread = title_generator.maybe_auto_title(
                    session_id,
                    message,
                    reply,
                    client=self.client,
                    conversation_history=state["messages"],
                )
                if thread is not None:
                    self._title_threads.append(thread)
            return reply


class _Handler(BaseHTTPRequestHandler):
    """HTTP 端点处理（通过 self.server.app 访问 AgentServer）。"""

    # 访问日志太吵，静默
    def log_message(self, format: str, *args) -> None:
        pass

    def _send_json(
        self,
        status: int,
        payload: dict,
        extra_headers: dict | None = None,
    ) -> None:
        """发送 JSON 响应并记录状态码（供审计使用）。"""
        self._last_status = status
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        """302 跳转（未登录访问页面时指向 /login，对齐 Hermes HTML 路由行为）。"""
        self._last_status = 302
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _serve_static(self, rel_path: str) -> None:
        """从 web/ 目录提供静态文件；拒绝路径穿越与不存在的文件。"""
        target = (WEB_DIR / rel_path).resolve()
        if not _is_within(target, WEB_DIR) or not target.is_file():
            self._send_json(404, {"error": "not found"})
            return
        mime = _MIME_TYPES.get(target.suffix.lower(), "application/octet-stream")
        body = target.read_bytes()
        self._last_status = 200
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _begin_sse(self) -> None:
        """开始 SSE 响应（text/event-stream 头）。"""
        self._last_status = 200
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        # SSE 结束后由服务器关闭连接（close-delimited），客户端读到 EOF 即结束
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

    def _sse(self, event: str, data: Any) -> bool:
        """写一条 SSE 帧；客户端断开返回 False（供中断信号使用），不抛错。"""
        try:
            body = (
                f"event: {event}\n"
                f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
            ).encode("utf-8")
            self.wfile.write(body)
            self.wfile.flush()
            return True
        except Exception:
            return False

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return {}

    def _authorized(self) -> bool:
        """请求鉴权：有效 session cookie（人）或 Bearer token（机器）任一通过。

        对齐 Hermes dashboard_auth：机器走 token_auth seam（Bearer），人走会话
        cookie；两者都未配置时免鉴权。token 用 hmac 常量时间比较防时序侧信道。
        校验结果同时写入 self._audit_identity（审计用，token 一律打码）。
        """
        if not (_auth_token() or dashboard_auth.auth_enabled()):
            self._audit_identity = ""
            return True

        # 机器通道：Authorization: Bearer <token>
        token = _auth_token()
        if token:
            header = (self.headers.get("Authorization") or "").strip()
            if hmac.compare_digest(
                header.encode("utf-8"), f"Bearer {token}".encode("utf-8")
            ):
                self._audit_identity = "«redacted»"
                return True

        # 人机通道：session cookie（HMAC 签名的无状态会话）
        session_token = dashboard_auth.read_session_token(
            self.headers.get("Cookie") or ""
        )
        payload = dashboard_auth.verify_session(session_token) if session_token else None
        if payload is not None:
            self._audit_identity = dashboard_auth.session_username(payload)
            return True

        self._audit_identity = ""
        return False

    def _audit(self, method: str, path: str) -> None:
        """操作审计：每个请求追加一行 JSON（时间/来源/动作/会话/状态），token 不打明文。"""
        try:
            target = _audit_log_path()
            if not target:
                return
            status = getattr(self, "_last_status", 0)
            entry = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "remote": self.client_address[0] if self.client_address else "",
                "method": method,
                "path": path,
                "action": _audit_action(path, method),
                "session_id": getattr(self, "_audit_session_id", "") or "",
                "status": status,
                "ok": status < 400,
                "identity": getattr(self, "_audit_identity", "") or "",
            }
            with AUDIT_LOG_LOCK:
                with open(target, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass  # 审计失败不影响主流程

    def do_GET(self) -> None:
        """GET 入口：统一兜底状态码并记录审计（对齐 Hermes dashboard 请求日志）。"""
        self._last_status = 404
        try:
            self._handle_GET()
        finally:
            self._audit("GET", self.path)

    def _handle_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send_json(200, {"ok": True})
            return
        if parsed.path == "/login":
            self._serve_static("login.html")
            return
        if parsed.path == "/api/auth/config":
            # 前端启动时探测：服务是否提供用户名密码登录（401 时决定弹 token 还是跳登录页）
            self._send_json(200, {"login_available": dashboard_auth.auth_enabled()})
            return
        if parsed.path in ("/", "/index.html"):
            if dashboard_auth.auth_enabled() and not self._authorized():
                self._redirect("/login")
                return
            self._serve_static("index.html")
            return
        if parsed.path.startswith("/web/"):
            self._serve_static(unquote(parsed.path[len("/web/"):]))
            return
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return
        if parsed.path == "/api/auth/me":
            session_token = dashboard_auth.read_session_token(
                self.headers.get("Cookie") or ""
            )
            payload = (
                dashboard_auth.verify_session(session_token) if session_token else None
            )
            if payload is None:
                self._send_json(401, {"error": "unauthorized"})
                return
            self._send_json(
                200,
                {
                    "username": dashboard_auth.session_username(payload),
                    "expires_at": payload.get("exp", 0),
                },
            )
            return
        if parsed.path == "/approvals/pending":
            session_id = (parse_qs(parsed.query).get("session_id") or [""])[0]
            if not session_id:
                self._send_json(400, {"error": "session_id required"})
                return
            self._audit_session_id = session_id
            pending = list_pending_approvals(session_id)
            self._send_json(200, {"session_id": session_id, "pending": pending})
            return
        if parsed.path == "/sessions":
            try:
                limit = int((parse_qs(parsed.query).get("limit") or ["50"])[0])
            except ValueError:
                limit = 50
            include_archived = (parse_qs(parsed.query).get("include_archived") or ["0"])[
                0
            ].lower() in ("1", "true", "yes")
            archived_only = (parse_qs(parsed.query).get("archived_only") or ["0"])[
                0
            ].lower() in ("1", "true", "yes")
            self._send_json(
                200,
                {
                    "sessions": minimal_agent.list_sessions(
                        limit,
                        include_archived=include_archived,
                        archived_only=archived_only,
                    )
                },
            )
            return
        if parsed.path == "/skills":
            skill_rows = [
                {
                    "name": s.get("name", ""),
                    "description": s.get("description", ""),
                }
                for s in skills.discover_skills()
            ]
            self._send_json(200, {"skills": skill_rows})
            return
        if parsed.path == "/plugins":
            self._send_json(200, {"plugins": list_provider_plugins()})
            return
        if parsed.path == "/tools":
            tool_rows = []
            for tool in self.server.app.tools:
                fn = tool.get("function") if isinstance(tool, dict) else None
                if isinstance(fn, dict):
                    tool_rows.append(
                        {
                            "name": fn.get("name", ""),
                            "description": fn.get("description", ""),
                        }
                    )
            self._send_json(200, {"tools": tool_rows})
            return
        if parsed.path == "/working_diff":
            query = parse_qs(parsed.query)
            mode = (query.get("mode") or ["working"])[0]
            paths_raw = query.get("paths")
            paths = [p for p in paths_raw if p] if paths_raw else None
            result = working_diff.collect_working_diff(os.getcwd(), mode=mode, paths=paths)
            if not result.get("success") and str(result.get("error", "")).startswith(
                "Unknown mode"
            ):
                self._send_json(400, result)
            else:
                # 附带按文件拆分的记录，供前端"右侧目录 + 左侧逐文件 diff"渲染
                result["files"] = working_diff.parse_diff_files(result.get("diff", ""))
                result["summary"] = working_diff.summarize_files(result["files"])
                self._send_json(200, result)
            return
        if parsed.path.startswith("/sessions/") and parsed.path.endswith("/export"):
            session_id = unquote(parsed.path[len("/sessions/"):-len("/export")])
            if not session_id or "/" in session_id:
                self._send_json(404, {"error": "not found"})
                return
            self._audit_session_id = session_id
            fmt = (parse_qs(parsed.query).get("format") or ["md"])[0].lower()
            if fmt not in ("md", "html"):
                self._send_json(400, {"error": "format must be md or html"})
                return
            if not minimal_agent.load_session_messages(session_id):
                self._send_json(404, {"error": "session not found or empty"})
                return
            content = (
                session_export.export_session_html(session_id)
                if fmt == "html"
                else session_export.export_session_md(session_id)
            )
            filename = f"session-{session_id}.{fmt}"
            self._last_status = 200
            self.send_response(200)
            self.send_header(
                "Content-Type",
                "text/markdown; charset=utf-8" if fmt == "md" else "text/html; charset=utf-8",
            )
            self.send_header(
                "Content-Disposition", f'attachment; filename="{filename}"'
            )
            self.send_header("Content-Length", str(len(content.encode("utf-8"))))
            self.end_headers()
            self.wfile.write(content.encode("utf-8"))
            return
        if parsed.path.startswith("/sessions/") and parsed.path.endswith("/messages"):
            session_id = unquote(parsed.path[len("/sessions/"):-len("/messages")])
            if not session_id or "/" in session_id:
                self._send_json(404, {"error": "not found"})
                return
            self._audit_session_id = session_id
            messages = minimal_agent.load_session_messages(session_id)
            events = minimal_agent.load_session_events(session_id)
            # 恢复会话时水合 todo 清单，随历史一并返回（网页常驻任务清单卡片）
            minimal_agent.hydrate_todo_store(messages, session_id)
            todos = minimal_agent.get_todo_store(session_id).read()
            self._send_json(
                200,
                {
                    "session_id": session_id,
                    "messages": messages,
                    "events": events,
                    "todos": todos,
                },
            )
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        """POST 入口：统一兜底状态码并记录审计。"""
        self._last_status = 404
        try:
            self._handle_POST()
        finally:
            self._audit("POST", self.path)

    def _handle_POST(self) -> None:
        parsed = urlparse(self.path)
        body = self._read_body()
        if parsed.path == "/api/auth/login":
            username = str(body.get("username", ""))
            password = str(body.get("password", ""))
            self._audit_identity = username  # 审计记录尝试登录的用户名（非密钥）
            token = dashboard_auth.complete_password_login(username, password)
            if token is None:
                self._send_json(401, {"error": "invalid username or password"})
                return
            self._send_json(
                200,
                {"ok": True, "username": username},
                extra_headers={
                    "Set-Cookie": dashboard_auth.session_cookie_value(
                        token, dashboard_auth.session_ttl_seconds()
                    )
                },
            )
            return
        if parsed.path == "/api/auth/logout":
            # 注销不要求已登录：无会话时也能清 cookie
            self._send_json(
                200,
                {"ok": True},
                extra_headers={"Set-Cookie": dashboard_auth.clear_session_cookie_value()},
            )
            return
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return
        if parsed.path == "/chat":
            message = body.get("message", "")
            if not isinstance(message, str) or not message.strip():
                self._send_json(400, {"error": "message required"})
                return
            session_id = body.get("session_id") or time.strftime(
                "session-%Y%m%d-%H%M%S"
            )
            self._audit_session_id = session_id
            try:
                state = self.server.app.get_session(session_id)
                reply, events, todos = self.server.app.handle_message(
                    session_id, state, message
                )
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
                return
            self._send_json(
                200,
                {
                    "session_id": session_id,
                    "reply": reply,
                    "events": events,
                    "todos": todos,
                },
            )
            return
        if parsed.path == "/chat/stream":
            message = body.get("message", "")
            if not isinstance(message, str) or not message.strip():
                self._send_json(400, {"error": "message required"})
                return
            session_id = body.get("session_id") or time.strftime(
                "session-%Y%m%d-%H%M%S"
            )
            self._audit_session_id = session_id
            self._begin_sse()
            try:
                state = self.server.app.get_session(session_id)
                # 客户端断开（SSE 写失败）→ 置位中断信号，停止本轮工具执行
                interrupt_event = threading.Event()

                def emit_activity(ev: dict) -> None:
                    if not self._sse("activity", ev):
                        interrupt_event.set()

                def emit_token(text: str) -> None:
                    if not self._sse("token", {"text": text}):
                        interrupt_event.set()

                reply = self.server.app.handle_message_stream(
                    session_id,
                    state,
                    message,
                    emit_event=emit_activity,
                    emit_token=emit_token,
                    interrupt_event=interrupt_event,
                )
            except Exception as exc:
                self._sse("error", {"error": str(exc)})
                reply = ""
            self._sse("message", {"reply": reply, "session_id": session_id})
            self._sse("done", {})
            return
        if parsed.path == "/approvals/resolve":
            session_id = body.get("session_id", "")
            choice = body.get("choice", "")
            if not session_id or choice not in ("once", "session", "always", "deny"):
                self._send_json(
                    400,
                    {"error": "session_id and choice (once/session/always/deny) required"},
                )
                return
            self._audit_session_id = session_id
            count = resolve_gateway_approval(
                session_id, choice, reason=body.get("reason")
            )
            self._send_json(200, {"resolved": count})
            return
        if parsed.path.startswith("/sessions/") and parsed.path.endswith("/archive"):
            session_id = unquote(parsed.path[len("/sessions/"):-len("/archive")])
            if not session_id or "/" in session_id:
                self._send_json(404, {"error": "not found"})
                return
            self._audit_session_id = session_id
            archived = body.get("archived")
            if not isinstance(archived, bool):
                self._send_json(
                    400,
                    {"error": "archived (boolean) required"},
                )
                return
            updated = minimal_agent.set_session_archived(session_id, archived)
            if not updated:
                self._send_json(404, {"error": "session not found"})
                return
            self._send_json(
                200,
                {"session_id": session_id, "archived": archived},
            )
            return
        if parsed.path.startswith("/sessions/") and parsed.path.endswith("/fork"):
            source_id = unquote(parsed.path[len("/sessions/"):-len("/fork")])
            if not source_id or "/" in source_id:
                self._send_json(404, {"error": "not found"})
                return
            self._audit_session_id = source_id
            requested = str(body.get("id") or body.get("session_id") or "").strip()
            if requested and ("/" in requested or "\x00" in requested):
                self._send_json(400, {"error": "invalid session id"})
                return
            fork_id = requested or (
                time.strftime("session-%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
            )
            title = str(body.get("title") or "")
            if not minimal_agent.fork_session(source_id, fork_id, title):
                if minimal_agent.load_session_prompt(source_id) is None:
                    self._send_json(404, {"error": "session not found"})
                else:
                    self._send_json(409, {"error": "session already exists"})
                return
            self._send_json(
                200,
                {
                    "session_id": fork_id,
                    "source_session_id": source_id,
                    "title": title or "",
                },
            )
            return
        self._send_json(404, {"error": "not found"})

    def do_PATCH(self) -> None:
        """PATCH 入口：统一兜底状态码并记录审计。"""
        self._last_status = 404
        try:
            self._handle_PATCH()
        finally:
            self._audit("PATCH", self.path)

    def _handle_PATCH(self) -> None:
        """处理 PATCH 请求：目前只有 PATCH /sessions/<id> 会话标题更新。"""
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return
        parsed = urlparse(self.path)
        body = self._read_body()
        rest = (
            parsed.path[len("/sessions/"):]
            if parsed.path.startswith("/sessions/")
            else ""
        )
        if not rest or "/" in rest:
            self._send_json(404, {"error": "not found"})
            return
        session_id = unquote(rest)
        self._audit_session_id = session_id
        title = body.get("title")
        if not isinstance(title, str):
            self._send_json(400, {"error": "title (string) required"})
            return
        title = title.strip()
        if len(title) > 100:
            self._send_json(400, {"error": "title too long (max 100)"})
            return
        if not minimal_agent.set_session_title(session_id, title):
            self._send_json(404, {"error": "session not found"})
            return
        self._send_json(200, {"session_id": session_id, "title": title})

    def do_DELETE(self) -> None:
        """DELETE 入口：统一兜底状态码并记录审计。"""
        self._last_status = 404
        try:
            self._handle_DELETE()
        finally:
            self._audit("DELETE", self.path)

    def _handle_DELETE(self) -> None:
        """处理 DELETE 请求：目前只有 DELETE /sessions/<id> 会话删除（仅限已归档会话）。"""
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return
        parsed = urlparse(self.path)
        rest = parsed.path[len("/sessions/"):] if parsed.path.startswith("/sessions/") else ""
        if not rest or "/" in rest:
            self._send_json(404, {"error": "not found"})
            return
        session_id = unquote(rest)
        self._audit_session_id = session_id

        # 只允许删除已归档会话（用户交互要求，服务端一并强制，防 API 误删未归档数据）
        found = next(
            (
                s
                for s in minimal_agent.list_sessions(
                    limit=200, include_archived=True
                )
                if s["session_id"] == session_id
            ),
            None,
        )
        if found is None:
            self._send_json(404, {"error": "session not found"})
            return
        if not found["archived"]:
            self._send_json(400, {"error": "only archived sessions can be deleted"})
            return

        # 会话正在处理中（turn 锁被占用）→ 409，避免删除进行中的对话（对齐 Hermes turn lease）
        state = self.server.app.sessions.get(session_id)
        if state is not None and not state["lock"].acquire(blocking=False):
            self._send_json(409, {"error": "session is busy"})
            return
        if state is not None:
            state["lock"].release()
            self.server.app.remove_session(session_id)

        deleted = minimal_agent.delete_session(session_id)
        if not deleted:
            self._send_json(404, {"error": "session not found"})
            return
        self._send_json(200, {"session_id": session_id, "deleted": True})


class _Server(ThreadingHTTPServer):
    """把 AgentServer 挂到服务器上，供 Handler 访问。"""

    def __init__(self, addr, app: AgentServer) -> None:
        super().__init__(addr, _Handler)
        self.app = app


def run_server(
    host: str = "127.0.0.1",
    port: int = 8000,
    client=None,
    memory_manager=None,
) -> None:
    """启动服务（Ctrl+C 停止）。"""
    app = AgentServer(client=client, memory_manager=memory_manager)
    server = _Server((host, port), app)
    try:
        print(f"🌐 服务已启动：http://{host}:{port}（Ctrl+C 停止）")
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown()
        server.server_close()
        print("服务已停止")


if __name__ == "__main__":
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8000
    run_server(host, port)
