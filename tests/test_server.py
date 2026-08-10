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
import os
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


class ToolLoopMessage:
    """模拟"模型持续要求调工具"的消息（带 model_dump，含 tool_calls）。"""

    def __init__(self, name: str = "get_current_time", args: dict | None = None):
        self.content = ""
        self.reasoning_content = ""
        self.tool_calls = [
            SimpleNamespace(
                id="call_loop",
                type="function",
                function=SimpleNamespace(
                    name=name,
                    arguments=json.dumps(args or {}, ensure_ascii=False),
                ),
            )
        ]

    def model_dump(self, exclude_none=True):
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in self.tool_calls
            ],
        }


class BudgetFakeClient:
    """计数假 client：带 tools 的调用返回工具调用，收尾调用（无 tools）返回文本。"""

    def __init__(self):
        self.calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls += 1
        if kwargs.get("tools"):
            return SimpleNamespace(
                usage=SimpleNamespace(prompt_tokens=100),
                choices=[SimpleNamespace(message=ToolLoopMessage("get_current_time"))],
            )
        return SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=50),
            choices=[SimpleNamespace(message=FakeMessage("收尾完成"))],
        )


def http_json(
    method: str,
    url: str,
    payload: dict | None = None,
    headers: dict | None = None,
) -> dict:
    """发 HTTP 请求并解析 JSON 响应。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


class ServerFixture:
    """起一个临时服务器，测完关闭。"""

    def __init__(self, client=None):
        # 备份并清空鉴权/审计环境变量，保证其他用例不受开发者 .env 影响
        self._env_backup = {
            key: os.environ.get(key)
            for key in (
                "SERVER_AUTH_TOKEN",
                "AUDIT_LOG_PATH",
                "DASHBOARD_USERNAME",
                "DASHBOARD_PASSWORD",
                "DASHBOARD_PASSWORD_HASH",
                "DASHBOARD_AUTH_SECRET",
                "DASHBOARD_SESSION_TTL_SECONDS",
                "DASHBOARD_COOKIE_SECURE",
                "TITLE_GENERATION_ENABLED",
            )
        }
        os.environ.pop("SERVER_AUTH_TOKEN", None)
        for key in (
            "DASHBOARD_USERNAME",
            "DASHBOARD_PASSWORD",
            "DASHBOARD_PASSWORD_HASH",
            "DASHBOARD_AUTH_SECRET",
            "DASHBOARD_SESSION_TTL_SECONDS",
            "DASHBOARD_COOKIE_SECURE",
        ):
            os.environ.pop(key, None)
        # 标题生成默认关闭：后台 LLM 线程会污染精确调用计数（BudgetFakeClient）与
        # 顺序假 client（seq）断言；专门的标题测试单独开启
        os.environ["TITLE_GENERATION_ENABLED"] = "0"
        self.tmp = tempfile.TemporaryDirectory()
        # 审计默认落到临时目录，避免测试污染项目根目录的 audit.log
        os.environ["AUDIT_LOG_PATH"] = str(Path(self.tmp.name) / "audit.log")
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
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


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
        index_body = body.decode("utf-8", errors="replace")
        check("首页包含页面标题", "今天想构建什么" in index_body)
        check("首页含工作区按钮", "btn-working-diff" in index_body)

        status, ctype, body = http_get(f"{fx.base}/web/app.js")
        check("GET /web/app.js 返回 200", status == 200)
        check("app.js 是 javascript", ctype.startswith("text/javascript"))
        app_body = body.decode("utf-8", errors="replace")
        check("app.js 含审批轮询逻辑", "approvals/pending" in app_body)
        check("app.js 含工作区改动视图", "working-diff-view" in app_body)

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
        check("技能列表包含示例技能", {"release-check"} <= names)
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
            {"web_search", "memory", "terminal", "read_file"} <= names,
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

        # 过程事件落库：历史接口按 user_message_id 返回，可还原活动托盘
        history = http_json("GET", f"{fx.base}/sessions/sess-ev/messages")
        hist_msgs = history.get("messages", [])
        hist_evs = history.get("events", [])
        check("历史消息带 id（供事件挂靠）",
              any(m.get("role") == "user" and m.get("id") for m in hist_msgs))
        check("历史接口返回过程事件", len(hist_evs) >= 2)
        check("事件带 user_message_id",
              all("user_message_id" in e for e in hist_evs))
        check("事件带 duration_ms（已耗时展示）",
              all("duration_ms" in e for e in hist_evs))
        user_ids = {m["id"] for m in hist_msgs if m.get("role") == "user"}
        check("事件挂靠在用户消息 id 上",
              all(e["user_message_id"] in user_ids for e in hist_evs))
        check("历史事件含思考与工具",
              any(e["type"] == "think" for e in hist_evs)
              and any(e["type"] == "tool" and e.get("name") == "terminal" for e in hist_evs))
    finally:
        fx.close()


def test_chat_narration_as_note() -> None:
    """方案 B：中间轮旁白作为 note 事件进托盘，不进入最终回复/消息气泡。"""
    tc = SimpleNamespace(
        id="call_note",
        type="function",
        function=SimpleNamespace(
            name="read_file", arguments='{"path":"README.md"}'
        ),
    )
    seq = SequenceClient(
        [
            ToolFakeMessage("让我查看一下目录结构来了解布局", [tc]),
            ToolFakeMessage("目录结构如下：……", None),
        ]
    )
    fx = ServerFixture(client=seq)
    try:
        data = http_json(
            "POST", f"{fx.base}/chat",
            {"message": "看看仓库结构", "session_id": "sess-note"},
        )
        check("旁白轮后返回最终回答", data.get("reply") == "目录结构如下：……")
        check("最终回复不含旁白", "让我查看一下目录结构" not in (data.get("reply") or ""))
        events = data.get("events", [])
        note_evs = [e for e in events if e.get("type") == "note"]
        check("包含 note 事件", len(note_evs) >= 1)
        check("note 事件内容为旁白",
              any("让我查看一下目录结构" in e.get("result", "") for e in note_evs))
        check("note 事件在工具事件之前",
              events.index(note_evs[0])
              < events.index(next(e for e in events if e.get("type") == "tool")))

        history = http_json("GET", f"{fx.base}/sessions/sess-note/messages")
        hist_evs = history.get("events", [])
        check("note 事件已落库",
              any(e["type"] == "note" and "让我查看一下目录结构" in e["result"] for e in hist_evs))
        hist_msgs = history.get("messages", [])
        check("旁白不进历史消息（只在托盘）",
              all("让我查看一下目录结构" not in (m.get("content") or "")
                  for m in hist_msgs))
    finally:
        fx.close()


def test_todo_event_and_panel_data() -> None:
    """todo 工具调用：事件含 todo 类型（完整清单），/chat 与历史接口都返回 todos。"""
    tc = SimpleNamespace(
        id="call_todo",
        type="function",
        function=SimpleNamespace(
            name="todo",
            arguments='{"todos":['
                      '{"id":"t1","content":"跑回归测试","status":"in_progress"},'
                      '{"id":"t2","content":"更新文档","status":"pending"}]}',
        ),
    )
    seq = SequenceClient(
        [
            ToolFakeMessage("", [tc]),
            ToolFakeMessage("清单已建好", None),
        ]
    )
    fx = ServerFixture(client=seq)
    try:
        data = http_json(
            "POST", f"{fx.base}/chat",
            {"message": "列任务", "session_id": "sess-todo-web"},
        )
        check("todo 轮后返回最终回答", data.get("reply") == "清单已建好")
        evs = data.get("events", [])
        todo_evs = [e for e in evs if e.get("type") == "todo"]
        check("事件含 todo 类型", len(todo_evs) >= 1)
        check("todo 事件带完整清单",
              "跑回归测试" in (todo_evs[-1].get("result") or ""))
        check("响应带 todos",
              any(t.get("id") == "t1" and t.get("status") == "in_progress"
                  for t in (data.get("todos") or [])))

        history = http_json("GET", f"{fx.base}/sessions/sess-todo-web/messages")
        check("历史接口返回 todos",
              any(t.get("content") == "跑回归测试"
                  for t in (history.get("todos") or [])))
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


def test_auth_and_audit() -> None:
    """鉴权 + 审计：配置 SERVER_AUTH_TOKEN 后 API 需 Bearer token（/health 与静态豁免），
    每个请求落一行审计 JSON（含 401/200，token 不打明文）。"""
    import urllib.error

    fx = ServerFixture()
    try:
        # 基线：未配置 token 时 API 免鉴权（fixture 已清空 SERVER_AUTH_TOKEN）
        sessions0 = http_json("GET", f"{fx.base}/sessions")
        check("未配置 token 时 API 免鉴权", "sessions" in sessions0)

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["SERVER_AUTH_TOKEN"] = "test-secret-token"
            os.environ["AUDIT_LOG_PATH"] = str(Path(tmp) / "audit.log")

            # 未带 token → 401
            try:
                http_json("GET", f"{fx.base}/sessions")
                check("未带 token -> 401", False)
            except urllib.error.HTTPError as exc:
                check("未带 token -> 401", exc.code == 401)

            # 错误 token → 401
            try:
                http_json(
                    "GET",
                    f"{fx.base}/sessions",
                    headers={"Authorization": "Bearer wrong-token"},
                )
                check("错误 token -> 401", False)
            except urllib.error.HTTPError as exc:
                check("错误 token -> 401", exc.code == 401)

            # 正确 token → 200
            sessions = http_json(
                "GET",
                f"{fx.base}/sessions",
                headers={"Authorization": "Bearer test-secret-token"},
            )
            check("正确 token -> 200", "sessions" in sessions)

            # /chat 与审批端点同样受保护
            try:
                http_json("POST", f"{fx.base}/chat", {"message": "你好"})
                check("/chat 未带 token -> 401", False)
            except urllib.error.HTTPError as exc:
                check("/chat 未带 token -> 401", exc.code == 401)

            try:
                http_json(
                    "POST",
                    f"{fx.base}/approvals/resolve",
                    {"session_id": "s1", "choice": "once"},
                )
                check("/approvals/resolve 未带 token -> 401", False)
            except urllib.error.HTTPError as exc:
                check("/approvals/resolve 未带 token -> 401", exc.code == 401)

            try:
                http_json("DELETE", f"{fx.base}/sessions/ghost-auth")
                check("DELETE 未带 token -> 401", False)
            except urllib.error.HTTPError as exc:
                check("DELETE 未带 token -> 401", exc.code == 401)

            try:
                http_json(
                    "PATCH", f"{fx.base}/sessions/ghost-auth", {"title": "x"}
                )
                check("PATCH 未带 token -> 401", False)
            except urllib.error.HTTPError as exc:
                check("PATCH 未带 token -> 401", exc.code == 401)

            # 带 token 的 /chat 正常执行（FakeClient 返回固定回复）
            reply = http_json(
                "POST",
                f"{fx.base}/chat",
                {"message": "你好", "session_id": "sess-auth"},
                headers={"Authorization": "Bearer test-secret-token"},
            )
            check("带 token /chat 正常", reply.get("reply") == "你好，我是助手")

            # /health 与静态页面豁免鉴权
            health = http_json("GET", f"{fx.base}/health")
            check("/health 免鉴权", health.get("ok") is True)
            code, _, _ = http_get(f"{fx.base}/")
            check("静态页免鉴权", code == 200)

            # 审计日志：JSON Lines，含 401 与 200，动作/会话齐全，token 不打明文
            log_file = Path(tmp) / "audit.log"
            lines = log_file.read_text(encoding="utf-8").strip().splitlines()
            entries = [json.loads(line) for line in lines]
            check("审计日志非空", len(entries) >= 8)
            check("审计含 401 记录", any(e["status"] == 401 for e in entries))
            check("审计含 200 记录", any(e["status"] == 200 for e in entries))
            check("审计记录动作名", any(e["action"] == "sessions:list" for e in entries))
            check(
                "审计记录 /chat 会话 id",
                any(e["session_id"] == "sess-auth" for e in entries),
            )
            dump = json.dumps(entries, ensure_ascii=False)
            check("审计不打 token 明文", "test-secret-token" not in dump)
    finally:
        fx.close()


def test_delete_session_endpoint() -> None:
    """DELETE /sessions/<id>：仅已归档会话可删；未归档 400；未知 404；进行中 409；审计记录。"""
    import urllib.error

    fx = ServerFixture()
    try:
        # 建一个有消息的会话（FakeClient 返回固定回复）
        http_json(
            "POST", f"{fx.base}/chat",
            {"message": "你好，待删除", "session_id": "sess-del"},
        )
        sessions = http_json("GET", f"{fx.base}/sessions")
        check(
            "删除前列表含该会话",
            any(s["session_id"] == "sess-del" for s in sessions["sessions"]),
        )
        hits = minimal_agent.search_messages_db("待删除")
        check(
            "删除前 FTS 可搜到",
            any(h.get("session_id") == "sess-del" for h in hits),
        )

        # 未归档会话不可删 → 400
        try:
            http_json("DELETE", f"{fx.base}/sessions/sess-del")
            check("未归档会话删除 -> 400", False)
        except urllib.error.HTTPError as exc:
            check("未归档会话删除 -> 400", exc.code == 400)

        # 归档后删除成功
        http_json(
            "POST", f"{fx.base}/sessions/sess-del/archive", {"archived": True}
        )
        result = http_json("DELETE", f"{fx.base}/sessions/sess-del")
        check("归档后删除返回 deleted=true", result.get("deleted") is True)
        check("归档后删除返回会话 id", result.get("session_id") == "sess-del")

        sessions = http_json("GET", f"{fx.base}/sessions")
        check(
            "删除后列表不含",
            not any(s["session_id"] == "sess-del" for s in sessions["sessions"]),
        )
        hits = minimal_agent.search_messages_db("待删除")
        check(
            "删除后 FTS 不可搜",
            not any(h.get("session_id") == "sess-del" for h in hits),
        )
        check("删除后消息为空", minimal_agent.load_session_messages("sess-del") == [])
        check("删除后进程内状态已清理", "sess-del" not in fx.app.sessions)

        # 未知会话 404
        try:
            http_json("DELETE", f"{fx.base}/sessions/ghost-404")
            check("未知会话 -> 404", False)
        except urllib.error.HTTPError as exc:
            check("未知会话 -> 404", exc.code == 404)

        # 进行中（turn 锁被占用）→ 409；释放后可删
        http_json(
            "POST", f"{fx.base}/chat",
            {"message": "正在处理", "session_id": "sess-busy"},
        )
        http_json(
            "POST", f"{fx.base}/sessions/sess-busy/archive", {"archived": True}
        )
        state = fx.app.sessions["sess-busy"]
        state["lock"].acquire()
        try:
            try:
                http_json("DELETE", f"{fx.base}/sessions/sess-busy")
                check("进行中删除 -> 409", False)
            except urllib.error.HTTPError as exc:
                check("进行中删除 -> 409", exc.code == 409)
        finally:
            state["lock"].release()
        result = http_json("DELETE", f"{fx.base}/sessions/sess-busy")
        check("释放后删除成功", result.get("deleted") is True)

        # 审计：sessions:delete + 会话 id + 409
        log_file = Path(fx.tmp.name) / "audit.log"
        entries = [
            json.loads(line)
            for line in log_file.read_text(encoding="utf-8").strip().splitlines()
        ]
        dels = [e for e in entries if e.get("action") == "sessions:delete"]
        check("审计记录删除动作", len(dels) >= 2)
        check("审计含删除会话 id", any(e.get("session_id") == "sess-del" for e in dels))
        check("审计含 409 记录", any(e.get("status") == 409 for e in entries))
    finally:
        fx.close()


def test_session_title_and_fork() -> None:
    """会话标题 + fork：LLM 自动标题、手动改名与校验、fork 复制历史、删除源不影响分支。"""
    import urllib.error

    fx = ServerFixture()
    try:
        # 开启 LLM 标题生成：首轮交换后后台线程用 FakeClient 生成"你好，我是助手"
        os.environ["TITLE_GENERATION_ENABLED"] = "1"
        http_json(
            "POST", f"{fx.base}/chat",
            {"message": "帮我规划北京行程", "session_id": "sess-tt"},
        )
        http_json(
            "POST", f"{fx.base}/chat",
            {"message": "再加一天故宫", "session_id": "sess-tt"},
        )
        # 后台线程异步落库，轮询等待标题出现
        deadline = time.time() + 6
        title = ""
        while time.time() < deadline:
            sessions = http_json("GET", f"{fx.base}/sessions")
            cur = next(
                (s for s in sessions["sessions"] if s["session_id"] == "sess-tt"), None
            )
            if cur and cur.get("title"):
                title = cur["title"]
                break
            time.sleep(0.1)
        check("LLM 自动标题（后台生成）", title == "你好，我是助手")

        # PATCH 改名
        patched = http_json(
            "PATCH", f"{fx.base}/sessions/sess-tt", {"title": "北京五天行程"}
        )
        check("改名返回标题", patched.get("title") == "北京五天行程")
        sessions = http_json("GET", f"{fx.base}/sessions")
        cur = next(s for s in sessions["sessions"] if s["session_id"] == "sess-tt")
        check("列表显示新标题", cur["title"] == "北京五天行程")

        # 后续对话不覆盖人工改名（标题只在首轮触发 + set-if-empty 原子保护）
        http_json(
            "POST", f"{fx.base}/chat",
            {"message": "帮我查一下天气", "session_id": "sess-tt"},
        )
        sessions = http_json("GET", f"{fx.base}/sessions")
        cur = next(s for s in sessions["sessions"] if s["session_id"] == "sess-tt")
        check("人工改名不被后续对话覆盖", cur["title"] == "北京五天行程")

        # PATCH 校验：缺 title 400、未知 404、超长 400
        try:
            http_json("PATCH", f"{fx.base}/sessions/sess-tt", {})
            check("PATCH 缺 title -> 400", False)
        except urllib.error.HTTPError as exc:
            check("PATCH 缺 title -> 400", exc.code == 400)
        try:
            http_json("PATCH", f"{fx.base}/sessions/ghost-title", {"title": "x"})
            check("PATCH 未知会话 -> 404", False)
        except urllib.error.HTTPError as exc:
            check("PATCH 未知会话 -> 404", exc.code == 404)
        try:
            http_json("PATCH", f"{fx.base}/sessions/sess-tt", {"title": "长" * 101})
            check("PATCH 超长标题 -> 400", False)
        except urllib.error.HTTPError as exc:
            check("PATCH 超长标题 -> 400", exc.code == 400)

        # fork：复制全部消息 + 标题，独立出现在列表
        fork = http_json("POST", f"{fx.base}/sessions/sess-tt/fork", {})
        fork_id = fork["session_id"]
        check("fork 返回新会话 id", bool(fork_id) and fork_id != "sess-tt")
        check("fork 返回源会话 id", fork.get("source_session_id") == "sess-tt")
        msgs = http_json("GET", f"{fx.base}/sessions/{fork_id}/messages")
        check("fork 复制全部消息", len(msgs["messages"]) >= 4)
        sessions = http_json("GET", f"{fx.base}/sessions")
        fork_row = next(s for s in sessions["sessions"] if s["session_id"] == fork_id)
        check("fork 出现在列表", fork_row["session_id"] == fork_id)
        check("fork 标题继承", fork_row["title"].startswith("北京五天行程"))

        # 删除源会话（先归档）不影响分支
        http_json(
            "POST", f"{fx.base}/sessions/sess-tt/archive", {"archived": True}
        )
        http_json("DELETE", f"{fx.base}/sessions/sess-tt")
        msgs2 = http_json("GET", f"{fx.base}/sessions/{fork_id}/messages")
        check("删除源后分支仍可用", len(msgs2["messages"]) == len(msgs["messages"]))

        # fork 未知源 → 404
        try:
            http_json("POST", f"{fx.base}/sessions/ghost-fork/fork", {})
            check("fork 未知源 -> 404", False)
        except urllib.error.HTTPError as exc:
            check("fork 未知源 -> 404", exc.code == 404)

        # 审计：sessions:title 与 sessions:fork
        log_file = Path(fx.tmp.name) / "audit.log"
        entries = [
            json.loads(line)
            for line in log_file.read_text(encoding="utf-8").strip().splitlines()
        ]
        check(
            "审计记录改名",
            any(e.get("action") == "sessions:title" and e["status"] == 200 for e in entries),
        )
        check(
            "审计记录 fork",
            any(e.get("action") == "sessions:fork" and e["status"] == 200 for e in entries),
        )
    finally:
        fx.close()


def test_turn_budget() -> None:
    """turn 级预算：轮数上限与 token 预算触发收尾，最终回复为收尾文本。"""
    original_turns = minimal_agent.MAX_AGENT_TURNS
    original_budget = minimal_agent.TURN_TOKEN_BUDGET
    try:
        # 轮数上限：MAX_AGENT_TURNS=3 → 3 次工具循环 + 1 次收尾 = 4 次模型调用
        minimal_agent.MAX_AGENT_TURNS = 3
        minimal_agent.TURN_TOKEN_BUDGET = 0
        fx = ServerFixture(client=BudgetFakeClient())
        try:
            reply = http_json(
                "POST", f"{fx.base}/chat",
                {"message": "一直查下去", "session_id": "sess-budget-turns"},
            )
            check("轮数上限收尾回复", reply.get("reply") == "收尾完成")
            check("轮数上限调用次数=4", fx.app.client.calls == 4)
            history = http_json(
                "GET", f"{fx.base}/sessions/sess-budget-turns/messages"
            )
            leaked = any(
                m.get("role") == "user" and "已经达到本轮执行上限" in (m.get("content") or "")
                for m in (history.get("messages") or [])
            )
            check("收尾内部指令不落库（不伪装成用户提问）", not leaked)
        finally:
            fx.close()

        # token 预算：每轮 100 token、预算 250 → 第 4 轮预检触顶 → 收尾
        minimal_agent.MAX_AGENT_TURNS = 10
        minimal_agent.TURN_TOKEN_BUDGET = 250
        fx = ServerFixture(client=BudgetFakeClient())
        try:
            reply = http_json(
                "POST", f"{fx.base}/chat",
                {"message": "一直查下去", "session_id": "sess-budget-tokens"},
            )
            check("token 预算收尾回复", reply.get("reply") == "收尾完成")
            check("token 预算调用次数=4", fx.app.client.calls == 4)
        finally:
            fx.close()
    finally:
        minimal_agent.MAX_AGENT_TURNS = original_turns
        minimal_agent.TURN_TOKEN_BUDGET = original_budget


def test_working_diff_endpoint() -> None:
    """工作区改动端点：返回 stat/diff/untracked；非法模式 400；鉴权与审计。"""
    import urllib.error

    fx = ServerFixture()
    try:
        data = http_json("GET", f"{fx.base}/working_diff")
        check("working_diff 返回 success", data.get("success") is True)
        check("working_diff 含 stat", "stat" in data)
        check("working_diff 含 diff", "diff" in data)
        check("working_diff 含 untracked", "untracked" in data)
        check("working_diff 含 files", isinstance(data.get("files"), list))
        check(
            "files 记录字段齐全",
            all(
                {"path", "status", "additions", "deletions", "diff"}
                <= set(f)
                for f in data["files"]
            ),
        )
        summary = data.get("summary", {})
        check("summary 含文件总数", summary.get("files") == len(data["files"]))
        check(
            "summary 增删行数为非负整数",
            isinstance(summary.get("additions"), int)
            and isinstance(summary.get("deletions"), int)
            and summary["additions"] >= 0
            and summary["deletions"] >= 0,
        )

        # paths 过滤（tests/test_server.py 在当前仓库内，URL 编码斜杠）
        filtered = http_json(
            "GET", f"{fx.base}/working_diff?paths=tests%2Ftest_server.py"
        )
        check("paths 过滤仍 success", filtered.get("success") is True)
        check(
            "paths 过滤 files 只含该文件",
            all(f["path"] == "tests/test_server.py" for f in filtered.get("files", [])),
        )

        # 非法模式 → 400 + 可读错误
        try:
            http_json("GET", f"{fx.base}/working_diff?mode=bogus")
            check("非法模式 -> 400", False)
        except urllib.error.HTTPError as exc:
            check("非法模式 -> 400", exc.code == 400)
            body = json.loads(exc.read().decode("utf-8", errors="replace"))
            check("非法模式错误信息", "Unknown mode" in body.get("error", ""))

        # 鉴权：配置 token 后未带 → 401；带正确 token → 200
        os.environ["SERVER_AUTH_TOKEN"] = "wdiff-secret"
        try:
            try:
                http_json("GET", f"{fx.base}/working_diff")
                check("未带 token -> 401", False)
            except urllib.error.HTTPError as exc:
                check("未带 token -> 401", exc.code == 401)
            authed = http_json(
                "GET",
                f"{fx.base}/working_diff",
                headers={"Authorization": "Bearer wdiff-secret"},
            )
            check("带 token 正常", authed.get("success") is True)
        finally:
            os.environ.pop("SERVER_AUTH_TOKEN", None)

        # 审计：动作名 working_diff + 200
        log_file = Path(fx.tmp.name) / "audit.log"
        entries = [
            json.loads(line)
            for line in log_file.read_text(encoding="utf-8").strip().splitlines()
        ]
        check(
            "审计记录 working_diff",
            any(
                e.get("action") == "working_diff" and e.get("status") == 200
                for e in entries
            ),
        )
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
        test_chat_narration_as_note,
        test_todo_event_and_panel_data,
        test_chat_stream_sse,
        test_auth_and_audit,
        test_delete_session_endpoint,
        test_session_title_and_fork,
        test_turn_budget,
        test_working_diff_endpoint,
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
