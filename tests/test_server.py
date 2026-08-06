# -*- coding: utf-8 -*-
"""
HTTP 服务化 + 网关审批端点的回归测试（零依赖，直接运行）：
    python tests/test_server.py

覆盖：
    - GET /health 探活
    - GET /approvals/pending 轮询（空 / 有挂起项）
    - POST /approvals/resolve 解决（无挂起返回 0；有挂起唤醒阻塞线程）
    - POST /chat 正常对话（假 client）+ 参数校验
    - GET / 与 /web/* 静态前端（含路径穿越拒绝）
    - GET /sessions 会话列表与 GET /sessions/<id>/messages 历史回显
    - POST /sessions/<id>/archive 归档/取消归档（默认列表隐藏，include_archived 可见）
    - GET /skills 技能列表与 GET /plugins 插件列表
    - GET /tools 工具列表（核心 TOOLS）
    - POST /chat/stream SSE 流式（思考/工具活动 + token + message + done）
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

    def __init__(self, client=None):
        self.tmp = tempfile.TemporaryDirectory()
        minimal_agent.SESSION_DB = Path(self.tmp.name) / "sessions.db"
        self.app = AgentServer(client=client or FakeClient())
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
        check("挂起项暴露 allow_permanent", pending["pending"][0]["allow_permanent"] is True)

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
        check("/chat 返回 events 列表", isinstance(reply.get("events"), list))

        import urllib.error
        try:
            http_json("POST", f"{fx.base}/chat", {"session_id": "sess-http"})
            check("缺 message -> 400", False)
        except urllib.error.HTTPError as exc:
            check("缺 message -> 400", exc.code == 400)
    finally:
        fx.close()


def http_get(url: str) -> tuple[int, str, bytes]:
    """发 GET 请求，返回 (状态码, Content-Type, 响应体)。"""
    import urllib.error

    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read()


def test_static_frontend() -> None:
    """静态前端：首页 / app.js / style.css 可访问，缺失与路径穿越返回 404。"""
    fx = ServerFixture()
    try:
        status, ctype, body = http_get(f"{fx.base}/")
        check("GET / 返回 200", status == 200)
        check("GET / 是 text/html", ctype.startswith("text/html"))
        check("首页包含页面标题", "今天想构建什么" in body.decode("utf-8", errors="replace"))

        status, ctype, body = http_get(f"{fx.base}/web/app.js")
        check("GET /web/app.js 返回 200", status == 200)
        check("app.js 是 javascript", ctype.startswith("text/javascript"))
        check("app.js 含审批轮询逻辑", "approvals/pending" in body.decode("utf-8"))

        status, _, _ = http_get(f"{fx.base}/web/style.css")
        check("GET /web/style.css 返回 200", status == 200)

        status, _, _ = http_get(f"{fx.base}/web/not-exist.js")
        check("缺失静态文件 -> 404", status == 404)

        # 路径穿越（URL 编码的 ../，客户端不会自行归一化）
        status, _, _ = http_get(f"{fx.base}/web/%2e%2e/approval.py")
        check("编码路径穿越 -> 404", status == 404)
        status, _, _ = http_get(f"{fx.base}/web/%2e%2e/%2e%2e/approval.py")
        check("深层路径穿越 -> 404", status == 404)
    finally:
        fx.close()


def test_sessions_endpoint() -> None:
    """会话列表与历史消息接口（对齐 Hermes api_server 的 list/get_messages）。"""
    fx = ServerFixture()
    try:
        empty = http_json("GET", f"{fx.base}/sessions")
        check("空库会话列表 -> []", empty["sessions"] == [])

        # 通过 /chat 产生一个真实会话（含落库消息）
        http_json(
            "POST", f"{fx.base}/chat",
            {"message": "你好", "session_id": "sess-list"},
        )

        sessions = http_json("GET", f"{fx.base}/sessions")
        ids = [s["session_id"] for s in sessions["sessions"]]
        check("列表包含新建会话", "sess-list" in ids)
        sess = next(s for s in sessions["sessions"] if s["session_id"] == "sess-list")
        check("列表含消息数", sess["message_count"] >= 2)
        check("列表含最后用户消息预览", "你好" in sess["preview"])

        history = http_json("GET", f"{fx.base}/sessions/sess-list/messages")
        check(
            "历史含用户消息",
            any(m["role"] == "user" and m["content"] == "你好" for m in history["messages"]),
        )
        check(
            "历史含助手消息",
            any(m["role"] == "assistant" for m in history["messages"]),
        )
        check("历史消息带时间戳", all("created_at" in m for m in history["messages"]))

        missing = http_json("GET", f"{fx.base}/sessions/nope/messages")
        check("不存在会话 -> 空消息列表", missing["messages"] == [])

        import urllib.error
        try:
            http_json("GET", f"{fx.base}/sessions/a%2Fb/messages")
            check("非法会话 id（含斜杠）-> 404", False)
        except urllib.error.HTTPError as exc:
            check("非法会话 id（含斜杠）-> 404", exc.code == 404)
    finally:
        fx.close()


def test_archive_endpoint() -> None:
    """归档是软标记：从默认列表隐藏，include_archived 可见，可取消归档。"""
    fx = ServerFixture()
    try:
        http_json(
            "POST", f"{fx.base}/chat",
            {"message": "你好", "session_id": "sess-arch"},
        )
        http_json(
            "POST", f"{fx.base}/chat",
            {"message": "我还没归档", "session_id": "sess-other"},
        )

        sessions = http_json("GET", f"{fx.base}/sessions")
        check(
            "归档前在最近列表",
            any(s["session_id"] == "sess-arch" for s in sessions["sessions"]),
        )

        res = http_json(
            "POST", f"{fx.base}/sessions/sess-arch/archive", {"archived": True}
        )
        check("归档返回 archived=true", res["archived"] is True)

        sessions = http_json("GET", f"{fx.base}/sessions")
        check(
            "归档后从最近列表隐藏",
            not any(s["session_id"] == "sess-arch" for s in sessions["sessions"]),
        )
        check(
            "未归档会话仍在最近列表",
            any(s["session_id"] == "sess-other" for s in sessions["sessions"]),
        )

        sessions = http_json("GET", f"{fx.base}/sessions?include_archived=1")
        arch = next(
            s for s in sessions["sessions"] if s["session_id"] == "sess-arch"
        )
        check("include_archived 可见且标记 archived", arch["archived"] is True)
        check(
            "include_archived 同时含未归档会话",
            any(s["session_id"] == "sess-other" for s in sessions["sessions"]),
        )

        sessions = http_json("GET", f"{fx.base}/sessions?archived_only=1")
        check(
            "archived_only 只含已归档会话",
            [s["session_id"] for s in sessions["sessions"]] == ["sess-arch"],
        )

        http_json(
            "POST", f"{fx.base}/sessions/sess-arch/archive", {"archived": False}
        )
        sessions = http_json("GET", f"{fx.base}/sessions")
        check(
            "取消归档后回到最近列表",
            any(s["session_id"] == "sess-arch" for s in sessions["sessions"]),
        )

        import urllib.error
        try:
            http_json(
                "POST",
                f"{fx.base}/sessions/sess-arch/archive",
                {"archived": "yes"},
            )
            check("archived 非布尔 -> 400", False)
        except urllib.error.HTTPError as exc:
            check("archived 非布尔 -> 400", exc.code == 400)

        try:
            http_json(
                "POST", f"{fx.base}/sessions/nope/archive", {"archived": True}
            )
            check("不存在的会话 -> 404", False)
        except urllib.error.HTTPError as exc:
            check("不存在的会话 -> 404", exc.code == 404)
    finally:
        fx.close()


def test_skills_and_plugins_endpoints() -> None:
    """技能列表（discover_skills）与插件列表（providers 目录）接口。"""
    fx = ServerFixture()
    try:
        skills = http_json("GET", f"{fx.base}/skills")
        names = {s["name"] for s in skills["skills"]}
        check("技能列表包含示例技能", {"weather-answer", "release-check"} <= names)
        check(
            "技能条目含描述",
            all(isinstance(s.get("description"), str) for s in skills["skills"]),
        )

        plugins = http_json("GET", f"{fx.base}/plugins")
        pnames = {p["name"] for p in plugins["plugins"]}
        check("插件列表包含 keyword", "keyword" in pnames)
        check("插件列表包含 vector", "vector" in pnames)
        check(
            "插件条目含描述",
            all(isinstance(p.get("description"), str) for p in plugins["plugins"]),
        )
        check("插件条目含 active 标记", all("active" in p for p in plugins["plugins"]))
    finally:
        fx.close()


def test_tools_endpoint() -> None:
    """工具列表接口：核心 TOOLS 全部带 name + description。"""
    fx = ServerFixture()
    try:
        tools = http_json("GET", f"{fx.base}/tools")
        names = {t["name"] for t in tools["tools"]}
        check(
            "工具列表含核心工具",
            {"get_weather", "memory", "terminal", "read_file"} <= names,
        )
        check(
            "工具条目含描述",
            all(isinstance(t.get("description"), str) for t in tools["tools"]),
        )
    finally:
        fx.close()


class ToolFakeMessage:
    """带 tool_calls 与可选推理内容的假模型消息。"""

    def __init__(self, content: str, tool_calls=None, reasoning_content: str = ""):
        self.content = content
        self.tool_calls = tool_calls
        self.reasoning_content = reasoning_content

    def model_dump(self, exclude_none=True):
        d = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in self.tool_calls
            ]
        return d


class SequenceCompletions:
    """按顺序弹出一条预设消息（先工具调用、再最终回答）。"""

    def __init__(self, messages):
        self.messages = list(messages)

    def create(self, **kwargs):
        msg = self.messages.pop(0)
        return SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=10),
            choices=[SimpleNamespace(message=msg)],
        )


class SequenceClient:
    def __init__(self, messages):
        self.chat = SimpleNamespace(completions=SequenceCompletions(messages))


def test_chat_events_tool_call() -> None:
    """/chat 的过程事件：工具调用含脱敏参数与截断结果。"""
    tc = SimpleNamespace(
        id="call_1",
        type="function",
        function=SimpleNamespace(
            name="terminal", arguments='{"command":"echo hello"}'
        ),
    )
    seq = SequenceClient(
        [
            ToolFakeMessage("", [tc], reasoning_content="先检查目标再执行"),
            ToolFakeMessage("完成", None),
        ]
    )
    fx = ServerFixture(client=seq)
    try:
        data = http_json(
            "POST", f"{fx.base}/chat",
            {"message": "执行", "session_id": "sess-ev"},
        )
        check("工具轮后返回最终回答", data.get("reply") == "完成")
        events = data.get("events", [])
        think_evs = [e for e in events if e.get("type") == "think"]
        check("包含思考事件", len(think_evs) >= 1)
        check(
            "推理内容已回显",
            any("先检查目标再执行" in e.get("result", "") for e in think_evs),
        )
        check(
            "思考事件在工具事件之前",
            events.index(think_evs[0]) < events.index(next(e for e in events if e.get("type") == "tool")),
        )
        tool_evs = [e for e in events if e.get("type") == "tool"]
        check("包含 terminal 工具事件", any(e.get("name") == "terminal" for e in tool_evs))
        ev = next(e for e in tool_evs if e.get("name") == "terminal")
        check("工具事件含参数", "echo hello" in ev.get("args", ""))
        check("工具事件含结果", "hello" in ev.get("result", ""))
    finally:
        fx.close()


class StreamDelta:
    """SSE 流式 delta 的假对象。"""

    def __init__(self, content=None, tool_calls=None, reasoning_content=None):
        self.content = content
        self.tool_calls = tool_calls
        self.reasoning_content = reasoning_content


class StreamChunk:
    def __init__(self, delta, usage=None):
        self.choices = [SimpleNamespace(delta=delta)]
        self.usage = usage


class StreamCompletions:
    """按批返回流式 chunks（每次 create 消费一批）。"""

    def __init__(self, batches):
        self.batches = list(batches)

    def create(self, **kwargs):
        return iter(self.batches.pop(0) if self.batches else [])


def test_chat_stream_sse() -> None:
    """/chat/stream：SSE 依次推送思考/工具活动、回复 token、最终 message 与 done。"""
    tool_call_chunk = StreamChunk(
        StreamDelta(
            tool_calls=[
                SimpleNamespace(
                    index=0,
                    id="call_1",
                    type="function",
                    function=SimpleNamespace(name="terminal", arguments=""),
                )
            ]
        )
    )
    args_chunk = StreamChunk(
        StreamDelta(
            tool_calls=[
                SimpleNamespace(
                    index=0,
                    id=None,
                    type=None,
                    function=SimpleNamespace(
                        name=None, arguments='{"command":"echo hello"}'
                    ),
                )
            ]
        )
    )
    batches = [
        [
            StreamChunk(StreamDelta(reasoning_content="先想一下")),
            tool_call_chunk,
            args_chunk,
        ],
        [
            StreamChunk(StreamDelta(content="完")),
            StreamChunk(
                StreamDelta(content="成"), usage=SimpleNamespace(prompt_tokens=10)
            ),
        ],
    ]
    client = SimpleNamespace(chat=SimpleNamespace(completions=StreamCompletions(batches)))
    fx = ServerFixture(client=client)
    try:
        payload = json.dumps(
            {"message": "执行", "session_id": "sess-sse"}
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{fx.base}/chat/stream", data=payload, method="POST"
        )
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        check("SSE 含思考活动与推理内容", "event: activity" in body and "先想一下" in body)
        check("SSE 含工具活动", "terminal" in body)
        check("SSE 含 token 事件", "event: token" in body)
        check(
            "SSE 含 message 事件与最终回复",
            'event: message' in body and '"reply": "完成"' in body,
        )
        check("SSE 含 done", "event: done" in body)
    finally:
        fx.close()


def main() -> None:
    """依次运行全部测试并汇总结果。"""
    print("== HTTP 服务化回归测试 ==")
    for test_fn in (
        test_health_and_pending,
        test_chat_endpoint,
        test_static_frontend,
        test_sessions_endpoint,
        test_archive_endpoint,
        test_skills_and_plugins_endpoints,
        test_tools_endpoint,
        test_chat_events_tool_call,
        test_chat_stream_sse,
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
