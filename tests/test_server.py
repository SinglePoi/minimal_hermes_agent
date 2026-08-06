# -*- coding: utf-8 -*-
"""
HTTP 服务化 + 网关审批端点的回归测试（零依赖，直接运行）：
    python tests/test_server.py

覆盖：
    - GET /health 探活
    - GET /approvals/pending 轮询（空 / 有挂起项）
    - POST /approvals/resolve 解决（无挂起返回 0；有挂起唤醒阻塞线程）
    - POST /chat 正常对话（假 client）+ 参数校验
"""

import json
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import minimal_agent  # noqa: E402
from approval import check_dangerous_command, register_gateway_notify  # noqa: E402
from server import AgentServer, _Server  # noqa: E402


_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    """记录一条断言结果。"""
    if cond:
        print(f"  ok: {label}")
    else:
        _failures.append(label)
        print(f"  FAIL: {label}")


class FakeMessage:
    """主模型回复的假消息（带 model_dump）。"""

    def __init__(self, content: str, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls

    def model_dump(self, exclude_none=True):
        return {"role": "assistant", "content": self.content}


class FakeCompletions:
    def __init__(self, outer):
        self.outer = outer

    def create(self, **kwargs):
        return SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=10),
            choices=[SimpleNamespace(message=FakeMessage("你好，我是助手"))],
        )


class FakeChat:
    def __init__(self, outer):
        self.completions = FakeCompletions(outer)


class FakeClient:
    def __init__(self):
        self.chat = FakeChat(self)


def http_json(method: str, url: str, payload: dict | None = None) -> dict:
    """发 HTTP 请求并解析 JSON 响应。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


class ServerFixture:
    """起一个临时服务器，测完关闭。"""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        minimal_agent.SESSION_DB = Path(self.tmp.name) / "sessions.db"
        self.app = AgentServer(client=FakeClient())
        self.server = _Server(("127.0.0.1", 0), self.app)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.app.shutdown()
        self.tmp.cleanup()
        minimal_agent.SESSION_DB = Path(ROOT) / "sessions.db"


def test_health_and_pending() -> None:
    """/health 探活；/approvals/pending 空与有挂起项。"""
    fx = ServerFixture()
    try:
        health = http_json("GET", f"{fx.base}/health")
        check("/health 返回 ok", health.get("ok") is True)

        pending = http_json("GET", f"{fx.base}/approvals/pending?session_id=s1")
        check("无挂起 -> 空列表", pending["pending"] == [])

        # 造一个挂起的审批（不通过服务器，直接走 approval 队列）
        register_gateway_notify("s1", lambda data: None)
        box: dict = {}
        thread = threading.Thread(
            target=lambda: box.setdefault(
                "r", check_dangerous_command("git reset --hard HEAD~1", "s1", None)
            ),
            daemon=True,
        )
        thread.start()
        time.sleep(0.3)
        pending = http_json("GET", f"{fx.base}/approvals/pending?session_id=s1")
        check("有挂起 -> 1 条", len(pending["pending"]) == 1)
        check("挂起项含命令", "git reset" in pending["pending"][0]["command"])

        resolved = http_json(
            "POST", f"{fx.base}/approvals/resolve",
            {"session_id": "s1", "choice": "once"},
        )
        check("resolve 返回 1", resolved["resolved"] == 1)
        thread.join(timeout=5)
        check("阻塞线程被唤醒且放行", box["r"]["approved"] is True)

        noop = http_json(
            "POST", f"{fx.base}/approvals/resolve",
            {"session_id": "s1", "choice": "deny"},
        )
        check("无挂起 resolve -> 0", noop["resolved"] == 0)
    finally:
        fx.close()


def test_chat_endpoint() -> None:
    """POST /chat：正常对话返回回复；缺 message 报 400。"""
    fx = ServerFixture()
    try:
        reply = http_json(
            "POST", f"{fx.base}/chat",
            {"message": "你好", "session_id": "sess-http"},
        )
        check("/chat 返回助手回复", reply.get("reply") == "你好，我是助手")
        check("/chat 返回会话 id", reply.get("session_id") == "sess-http")

        import urllib.error
        try:
            http_json("POST", f"{fx.base}/chat", {"session_id": "sess-http"})
            check("缺 message -> 400", False)
        except urllib.error.HTTPError as exc:
            check("缺 message -> 400", exc.code == 400)
    finally:
        fx.close()


def main() -> None:
    """依次运行全部测试并汇总结果。"""
    print("== HTTP 服务化回归测试 ==")
    for test_fn in (
        test_health_and_pending,
        test_chat_endpoint,
    ):
        print(f"[{test_fn.__name__}]")
        test_fn()
    print()
    if _failures:
        print(f"共 {len(_failures)} 个用例失败：")
        for label in _failures:
            print(f"  - {label}")
        sys.exit(1)
    print("全部用例通过 ✅")


if __name__ == "__main__":
    main()
