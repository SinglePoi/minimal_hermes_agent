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

审批流程（对齐 Hermes 的网关队列）：
    - /chat 请求里的 agent 线程在危险命令处通过 approval.py 的网关队列阻塞等待
      （register_gateway_notify + _await_gateway_decision + resolve_gateway_approval）
    - 客户端轮询 GET /approvals/pending 看到待审批项，POST resolve 后线程被
      "按响门铃"唤醒，/chat 返回最终结果

零新依赖：Python 标准库 http.server（ThreadingHTTPServer 天然支持并发连接，
resolve 与 /chat 可以同时进行）。
"""

import json
import os
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
load_dotenv()

import minimal_agent  # noqa: E402
import skills  # noqa: E402
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


class AgentServer:
    """持有客户端、会话状态与后台 worker；/chat 处理一条消息。"""

    def __init__(self, client=None, memory_manager=None) -> None:
        self.client = client or minimal_agent.create_client()
        self.manager = memory_manager
        self.review_worker = SyncWorker()
        self.tools = minimal_agent.get_tools(self.manager)
        self.sessions: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
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

    def handle_message(self, session_id: str, state: dict, message: str) -> tuple[str, list]:
        """处理一条消息，返回助手最终回答文本。"""
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
            return reply, events

    def shutdown(self) -> None:
        """排空后台任务、注销所有网关回调。"""
        for session_id in list(self.sessions):
            unregister_gateway_notify(session_id)
        self.review_worker.flush(timeout=10)
        self.review_worker.shutdown()
        if self.manager is not None:
            self.manager.flush_pending(timeout=10)
            self.manager.shutdown()


    def handle_message_stream(
        self,
        session_id: str,
        state: dict,
        message: str,
        emit_event,
        emit_token,
    ) -> str:
        """处理一条消息（SSE 流式）：思考/工具/召回事件实时经 emit_event 推送，
        回复 token 经 emit_token 推送；返回最终回答文本。"""
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
            )
            state["turn_count"] = turn_count
            state["turns_since_memory"] = turns_since_memory
            state["persisted_count"] = persisted_count
            last = state["messages"][-1] if state["messages"] else {}
            return last.get("content", "") if last.get("role") == "assistant" else ""


class _Handler(BaseHTTPRequestHandler):
    """HTTP 端点处理（通过 self.server.app 访问 AgentServer）。"""

    # 访问日志太吵，静默
    def log_message(self, format: str, *args) -> None:
        pass

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, rel_path: str) -> None:
        """从 web/ 目录提供静态文件；拒绝路径穿越与不存在的文件。"""
        target = (WEB_DIR / rel_path).resolve()
        if not _is_within(target, WEB_DIR) or not target.is_file():
            self._send_json(404, {"error": "not found"})
            return
        mime = _MIME_TYPES.get(target.suffix.lower(), "application/octet-stream")
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _begin_sse(self) -> None:
        """开始 SSE 响应（text/event-stream 头）。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        # SSE 结束后由服务器关闭连接（close-delimited），客户端读到 EOF 即结束
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

    def _sse(self, event: str, data: Any) -> None:
        """写一条 SSE 帧（客户端断开时静默，不抛错）。"""
        try:
            body = (
                f"event: {event}\n"
                f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
            ).encode("utf-8")
            self.wfile.write(body)
            self.wfile.flush()
        except Exception:
            pass

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

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send_json(200, {"ok": True})
            return
        if parsed.path in ("/", "/index.html"):
            self._serve_static("index.html")
            return
        if parsed.path.startswith("/web/"):
            self._serve_static(unquote(parsed.path[len("/web/"):]))
            return
        if parsed.path == "/approvals/pending":
            session_id = (parse_qs(parsed.query).get("session_id") or [""])[0]
            if not session_id:
                self._send_json(400, {"error": "session_id required"})
                return
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
        if parsed.path.startswith("/sessions/") and parsed.path.endswith("/messages"):
            session_id = unquote(parsed.path[len("/sessions/"):-len("/messages")])
            if not session_id or "/" in session_id:
                self._send_json(404, {"error": "not found"})
                return
            messages = minimal_agent.load_session_messages(session_id)
            self._send_json(
                200,
                {"session_id": session_id, "messages": messages},
            )
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        body = self._read_body()
        if parsed.path == "/chat":
            message = body.get("message", "")
            if not isinstance(message, str) or not message.strip():
                self._send_json(400, {"error": "message required"})
                return
            session_id = body.get("session_id") or time.strftime(
                "session-%Y%m%d-%H%M%S"
            )
            try:
                state = self.server.app.get_session(session_id)
                reply, events = self.server.app.handle_message(session_id, state, message)
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
                return
            self._send_json(
                200,
                {
                    "session_id": session_id,
                    "reply": reply,
                    "events": events,
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
            self._begin_sse()
            try:
                state = self.server.app.get_session(session_id)
                reply = self.server.app.handle_message_stream(
                    session_id,
                    state,
                    message,
                    emit_event=lambda ev: self._sse("activity", ev),
                    emit_token=lambda text: self._sse("token", {"text": text}),
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
        self._send_json(404, {"error": "not found"})


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
