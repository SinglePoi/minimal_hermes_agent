# -*- coding: utf-8 -*-
"""
HTTP 服务化 + gateway 审批通知（为前端铺路，对齐 Hermes dashboard/gateway 思路）

启动：
    python server.py [host] [port]      # 默认 127.0.0.1:8000

端点：
    POST /chat              发一条消息；body: {"message": "...", "session_id": "..."?}
    GET  /approvals/pending 轮询待审批；query: ?session_id=xxx
    POST /approvals/resolve 解决审批；body: {"session_id", "choice": once|session|always|deny, "reason"?}
    GET  /health            探活

审批流程（对齐 Hermes 的网关队列）：
    - /chat 请求里的 agent 线程在危险命令处通过 approval.py 的网关队列阻塞等待
      （register_gateway_notify + _await_gateway_decision + resolve_gateway_approval）
    - 客户端轮询 GET /approvals/pending 看到待审批项，POST resolve 后线程被
      "按响门铃"唤醒，/chat 返回最终结果

零新依赖：Python 标准库 http.server（ThreadingHTTPServer 天然支持并发连接，
resolve 与 /chat 可以同时进行）。
"""

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
load_dotenv()

import minimal_agent  # noqa: E402
from approval import (  # noqa: E402
    list_pending_approvals,
    register_gateway_notify,
    resolve_gateway_approval,
    unregister_gateway_notify,
)
from memory_manager import SyncWorker  # noqa: E402


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
        }
        # 网关会话：注册审批通知（危险命令将走队列阻塞，而非终端输入）
        register_gateway_notify(session_id, self._notify)
        with self._lock:
            self.sessions[session_id] = state
        return state

    def handle_message(self, session_id: str, state: dict, message: str) -> str:
        """处理一条消息，返回助手最终回答文本。"""
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
        )
        state["turn_count"] = turn_count
        state["turns_since_memory"] = turns_since_memory
        state["persisted_count"] = persisted_count
        last = state["messages"][-1] if state["messages"] else {}
        return last.get("content", "") if last.get("role") == "assistant" else ""

    def shutdown(self) -> None:
        """排空后台任务、注销所有网关回调。"""
        for session_id in list(self.sessions):
            unregister_gateway_notify(session_id)
        self.review_worker.flush(timeout=10)
        self.review_worker.shutdown()
        if self.manager is not None:
            self.manager.flush_pending(timeout=10)
            self.manager.shutdown()


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
        if parsed.path == "/approvals/pending":
            session_id = (parse_qs(parsed.query).get("session_id") or [""])[0]
            if not session_id:
                self._send_json(400, {"error": "session_id required"})
                return
            pending = list_pending_approvals(session_id)
            self._send_json(200, {"session_id": session_id, "pending": pending})
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
                reply = self.server.app.handle_message(session_id, state, message)
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
                return
            self._send_json(200, {"session_id": session_id, "reply": reply})
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
