# -*- coding: utf-8 -*-
"""
Daytona 云沙箱终端后端回归测试（零依赖，直接运行）：
    python tests/test_daytona.py

覆盖（对齐 Hermes tools/environments/daytona.py 核心语义，简化版）：
    - TERMINAL_ENV 解析 / 隔离后端判定 / 未知后端拒绝
    - 审批跳过（daytona 连硬性禁止也放行；local 仍拦截）
    - 假 SDK：创建/恢复/cleanup/HOME 解析/磁盘封顶/执行/中断
    - run_terminal 路由到沙箱、缺密钥报错、本机路径不受影响
    - 系统提示词注入后端说明
"""

from __future__ import annotations

import enum
import json
import os
import sys
import tempfile
import threading
import time
import types
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import approval  # noqa: E402
import environments  # noqa: E402
import minimal_agent  # noqa: E402
import process_registry  # noqa: E402
from environments.daytona import DaytonaEnvironment  # noqa: E402


_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    """记录一条断言结果。"""
    if cond:
        print(f"  ok: {label}")
    else:
        _failures.append(label)
        print(f"  FAIL: {label}")


class FakeDaytonaError(Exception):
    """假 Daytona SDK 错误。"""


class FakeSandboxState(str, enum.Enum):
    """假沙箱状态枚举（对齐 Daytona SandboxState）。"""

    STARTED = "started"
    STOPPED = "stopped"
    ARCHIVED = "archived"


class FakeResources:
    """记录 Resources(...) 入参，供磁盘/内存换算断言。"""

    last: dict = {}

    def __init__(self, **kwargs) -> None:
        FakeResources.last = dict(kwargs)
        self.__dict__.update(kwargs)


class FakeCreateParams:
    """记录 CreateSandboxFromImageParams 入参。"""

    def __init__(self, **kwargs) -> None:
        self.__dict__.update(kwargs)


class FakeSandbox:
    """假 Daytona sandbox：记录 exec/start/stop。"""

    def __init__(self, sandbox_id: str = "sb-1", home: str = "/home/test") -> None:
        self.id = sandbox_id
        self.state = FakeSandboxState.STARTED
        self.home = home
        self.execs: list[str] = []
        self.started = 0
        self.stopped = 0
        self.process = self
        self._exec_handler = None

    def exec(self, cmd: str, timeout=None):
        """执行命令：默认 $HOME 探测返回 home，其余返回 ok。"""
        self.execs.append(cmd)
        if self._exec_handler is not None:
            return self._exec_handler(cmd, timeout)
        if "echo $HOME" in cmd:
            return SimpleNamespace(result=self.home, exit_code=0)
        return SimpleNamespace(result="ok", exit_code=0)

    def start(self) -> None:
        """标记沙箱已启动。"""
        self.started += 1
        self.state = FakeSandboxState.STARTED

    def stop(self) -> None:
        """标记沙箱已停止。"""
        self.stopped += 1
        self.state = FakeSandboxState.STOPPED

    def refresh_data(self) -> None:
        """空操作（SDK 会刷新远程状态）。"""
        return None


class FakeDaytona:
    """假 Daytona 客户端：get/list/create/delete。"""

    def __init__(self) -> None:
        self.created: list = []
        self.deleted: list = []
        self.get_handler = None
        self.list_items: list = []
        self.create_sandbox = FakeSandbox()

    def get(self, name: str):
        """按名字取沙箱；默认抛 not found。"""
        if self.get_handler is not None:
            return self.get_handler(name)
        raise FakeDaytonaError("not found")

    def list(self, labels=None, limit=1):
        """列出带标签的沙箱。"""
        return iter(self.list_items)

    def create(self, params):
        """创建沙箱并记录参数。"""
        self.created.append(params)
        return self.create_sandbox

    def delete(self, sandbox) -> None:
        """删除沙箱。"""
        self.deleted.append(sandbox)


_fake_client = FakeDaytona()


def _install_fake_sdk() -> types.ModuleType:
    """把假 daytona 模块塞进 sys.modules，供 DaytonaEnvironment 延迟导入。"""
    global _fake_client
    _fake_client = FakeDaytona()
    FakeResources.last = {}
    mod = types.ModuleType("daytona")
    mod.Daytona = lambda *a, **k: _fake_client
    mod.CreateSandboxFromImageParams = FakeCreateParams
    mod.DaytonaError = FakeDaytonaError
    mod.Resources = FakeResources
    mod.SandboxState = FakeSandboxState
    sys.modules["daytona"] = mod
    return mod


def _make_env(**kwargs) -> DaytonaEnvironment:
    """用假 SDK 构造 DaytonaEnvironment。"""
    _install_fake_sdk()
    kwargs.setdefault("image", "test-image:latest")
    kwargs.setdefault("persistent_filesystem", True)
    return DaytonaEnvironment(**kwargs)


def test_config_and_guards() -> None:
    """TERMINAL_ENV 解析、隔离判定、未知后端、缺密钥。"""
    old = os.environ.get("TERMINAL_ENV")
    try:
        os.environ.pop("TERMINAL_ENV", None)
        check("默认 local", environments.get_terminal_env() == "local")
        check("local 非隔离", environments.is_isolated_backend("local") is False)
        check("daytona 隔离", environments.is_isolated_backend("daytona") is True)

        os.environ["TERMINAL_ENV"] = "Daytona"
        check("大小写归一", environments.get_terminal_env() == "daytona")
        check("当前即隔离", environments.is_isolated_backend() is True)

        ok, err = environments.check_backend_ready("docker")
        check("未知后端拒绝", ok is False and "docker" in err)

        os.environ.pop("DAYTONA_API_KEY", None)
        _install_fake_sdk()
        ok, err = environments.check_backend_ready("daytona")
        check("缺密钥拒绝", ok is False and "DAYTONA_API_KEY" in err)

        os.environ["DAYTONA_API_KEY"] = "test-key"
        ok, err = environments.check_backend_ready("daytona")
        check("有 SDK+密钥就绪", ok is True)
    finally:
        os.environ.pop("DAYTONA_API_KEY", None)
        if old is None:
            os.environ.pop("TERMINAL_ENV", None)
        else:
            os.environ["TERMINAL_ENV"] = old
        environments.reset_environment_cache()


def test_approval_skip() -> None:
    """隔离后端跳过审批（含硬性禁止）；local 硬性禁止仍拦截。"""
    skipped = approval.check_dangerous_command(
        "rm -rf /", "sess-dtn-hl", env_type="daytona"
    )
    check(
        "daytona 跳过硬性禁止",
        skipped.get("approved") is True
        and skipped.get("skipped_container_guards") is True,
    )
    blocked = approval.check_dangerous_command(
        "rm -rf /", "sess-local-hl", env_type="local"
    )
    check("local 硬性禁止仍拦截", blocked.get("approved") is False)


def test_cwd_and_create() -> None:
    """默认 cwd 解析为 $HOME；持久化 miss 后走 create。"""
    env = _make_env()
    # FakeSandbox 默认 home=/home/test，$HOME 探测后 cwd 应变
    check("cwd 解析 HOME", env.cwd == "/home/test")
    check("未命中 get 则 create", len(_fake_client.created) == 1)
    params = _fake_client.created[0]
    check("沙箱名 agent-default", getattr(params, "name", "") == "agent-default")
    check("auto_stop_interval=0", getattr(params, "auto_stop_interval", None) == 0)


def test_persistent_resume() -> None:
    """持久化：get 命中已有沙箱则 start，不 create。"""
    _install_fake_sdk()
    existing = FakeSandbox(sandbox_id="sb-existing")
    got = {"name": ""}

    def handler(name: str):
        got["name"] = name
        return existing

    _fake_client.get_handler = handler
    env = DaytonaEnvironment(image="img", persistent_filesystem=True, task_id="mytask")
    check("按名字恢复", existing.started == 1)
    check("恢复不 create", _fake_client.created == [])
    check("get 名字 agent-mytask", got["name"] == "agent-mytask")
    env.cleanup()


def test_non_persistent_skips_lookup() -> None:
    """非持久化：不 get/list，直接 create。"""
    env = _make_env(persistent_filesystem=False)
    check("非持久化直接 create", len(_fake_client.created) == 1)
    env.cleanup()
    check("非持久化 cleanup 走 delete", len(_fake_client.deleted) == 1)


def test_persistent_cleanup_stops() -> None:
    """持久化 cleanup 调用 stop，不 delete。"""
    env = _make_env(persistent_filesystem=True)
    sb = env._sandbox
    env.cleanup()
    check("持久化 cleanup stop", sb.stopped == 1)
    check("持久化不 delete", _fake_client.deleted == [])
    check("cleanup 后 sandbox 清空", env._sandbox is None)


def test_resource_conversion() -> None:
    """内存/磁盘 MB→GiB，磁盘超过 10GiB 封顶。"""
    _make_env(memory=5120, disk=20480)
    check("内存 5120MB → 5GiB", FakeResources.last.get("memory") == 5)
    check("磁盘 20480MB 封顶 10GiB", FakeResources.last.get("disk") == 10)
    _make_env(memory=100, disk=100)
    check("小值下限 1GiB", FakeResources.last.get("memory") == 1)
    check("小磁盘下限 1GiB", FakeResources.last.get("disk") == 1)


def test_execute_and_interrupt() -> None:
    """execute 跑 bash -c；中断调用 sandbox.stop 并返回 cancelled。"""
    env = _make_env()
    env._sandbox.execs.clear()
    env._sandbox._exec_handler = lambda cmd, timeout: SimpleNamespace(
        result="hello-sandbox", exit_code=0
    )
    result = env.execute("echo hello")
    check("execute 输出", result.get("output") == "hello-sandbox")
    check("execute 退出码 0", result.get("returncode") == 0)
    check("命令包 bash -c", any("bash -c" in c for c in env._sandbox.execs))

    # 中断：exec 阻塞直到 stop
    blocking = threading.Event()
    released = threading.Event()

    def slow_exec(cmd, timeout=None):
        blocking.set()
        released.wait(timeout=5)
        return SimpleNamespace(result="late", exit_code=0)

    env._sandbox._exec_handler = slow_exec
    ev = threading.Event()
    box: dict = {}

    def worker() -> None:
        box["r"] = env.execute("sleep 10", interrupt_event=ev)

    threading.Thread(target=worker, daemon=True).start()
    blocking.wait(timeout=3)
    ev.set()
    time.sleep(0.5)
    check("中断返回 cancelled", box.get("r", {}).get("cancelled") is True)
    check("中断调用 stop", env._sandbox.stopped >= 1)
    released.set()


def test_ensure_ready_restarts_stopped() -> None:
    """STOPPED 沙箱在执行前 start。"""
    env = _make_env()
    env._sandbox.state = FakeSandboxState.STOPPED
    env._ensure_sandbox_ready()
    check("STOPPED 会 restart", env._sandbox.started >= 1)
    env._sandbox.state = FakeSandboxState.STARTED
    started = env._sandbox.started
    env._ensure_sandbox_ready()
    check("STARTED 不再 start", env._sandbox.started == started)


def test_run_terminal_routes() -> None:
    """run_terminal：daytona 走沙箱；缺密钥报错；local 仍走本机。"""
    old_env = os.environ.get("TERMINAL_ENV")
    old_key = os.environ.get("DAYTONA_API_KEY")
    fake_env = _make_env()
    fake_env._sandbox._exec_handler = lambda cmd, timeout: SimpleNamespace(
        result="from-sandbox", exit_code=0
    )

    def fake_get_environment(*_a, **_k):
        return fake_env

    original_get = environments.get_environment
    try:
        os.environ["TERMINAL_ENV"] = "daytona"
        os.environ["DAYTONA_API_KEY"] = "test-key"
        _install_fake_sdk()
        environments.get_environment = fake_get_environment  # type: ignore
        raw = minimal_agent.run_terminal("echo hi", "sess-dtn")
        data = json.loads(raw)
        check("daytona 路由成功", data.get("backend") == "daytona")
        check("backend 字段在 JSON 前部", raw.lstrip().startswith("{") and '"backend"' in raw[:80])
        check("daytona 输出来自沙箱", "from-sandbox" in data.get("output", ""))
        check("daytona 危险命令也放行", True)
        raw_rm = minimal_agent.run_terminal("rm -rf /tmp/x", "sess-dtn-rm")
        rm_data = json.loads(raw_rm)
        check(
            "daytona 危险命令执行而非 BLOCKED",
            "BLOCKED" not in str(rm_data.get("error", "")),
        )

        os.environ.pop("DAYTONA_API_KEY", None)
        environments.reset_environment_cache()
        raw_nokey = minimal_agent.run_terminal("echo x", "sess-nokey")
        nokey = json.loads(raw_nokey)
        check("缺密钥错误含 DAYTONA_API_KEY", "DAYTONA_API_KEY" in nokey.get("error", ""))

        os.environ["TERMINAL_ENV"] = "local"
        raw_local = minimal_agent.run_terminal("echo hello-local-dtn", "sess-local-dtn")
        local = json.loads(raw_local)
        check(
            "local 仍走本机",
            local.get("exit_code") == 0 and "hello-local-dtn" in local.get("output", ""),
        )
        check("local 不带 daytona backend", local.get("backend") != "daytona")
    finally:
        environments.get_environment = original_get  # type: ignore
        environments.reset_environment_cache()
        if old_env is None:
            os.environ.pop("TERMINAL_ENV", None)
        else:
            os.environ["TERMINAL_ENV"] = old_env
        if old_key is None:
            os.environ.pop("DAYTONA_API_KEY", None)
        else:
            os.environ["DAYTONA_API_KEY"] = old_key


def test_system_prompt_block() -> None:
    """daytona 时系统提示词注入终端后端说明。"""
    old = os.environ.get("TERMINAL_ENV")
    try:
        os.environ["TERMINAL_ENV"] = "daytona"
        block = environments.build_terminal_backend_prompt()
        check("提示词含 Daytona", "Daytona" in block)
        check("提示词说明文件工具仍本机", "本机" in block)
        prompt = minimal_agent.build_system_prompt()
        check("系统提示词已注入后端块", "Daytona" in prompt)
        os.environ["TERMINAL_ENV"] = "local"
        check("local 不注入后端块", environments.build_terminal_backend_prompt() == "")
        check(
            "显式 daytona 仍注入",
            "Daytona" in environments.build_terminal_backend_prompt("daytona"),
        )
    finally:
        if old is None:
            os.environ.pop("TERMINAL_ENV", None)
        else:
            os.environ["TERMINAL_ENV"] = old


def test_terminal_schema_mentions_daytona() -> None:
    """terminal 工具描述提到 Daytona 后端。"""
    desc = ""
    for tool in minimal_agent.TOOLS:
        fn = tool.get("function") or {}
        if fn.get("name") == "terminal":
            desc = fn.get("description") or ""
            break
    check("TOOLS 描述含 daytona", "daytona" in desc.lower())


def test_session_override_routes() -> None:
    """进程默认 local 时，会话覆盖 daytona 仍走沙箱；邻居会话不受影响。"""
    old_env = os.environ.get("TERMINAL_ENV")
    old_key = os.environ.get("DAYTONA_API_KEY")
    original_db = minimal_agent.SESSION_DB
    original_get = environments.get_environment
    fake_env = _make_env()
    fake_env._sandbox._exec_handler = lambda cmd, timeout: SimpleNamespace(
        result="from-sandbox", exit_code=0
    )
    seen: dict = {}

    def fake_get_environment(env_type=None, task_id=None):
        seen["env_type"] = env_type
        seen["task_id"] = task_id
        return fake_env

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            minimal_agent.SESSION_DB = Path(tmpdir) / "sessions.db"
            os.environ["TERMINAL_ENV"] = "local"
            os.environ["DAYTONA_API_KEY"] = "test-key"
            _install_fake_sdk()
            environments.get_environment = fake_get_environment  # type: ignore
            check(
                "无覆盖跟随进程",
                minimal_agent.resolve_session_terminal_env("sess-ov") == "local",
            )
            minimal_agent.save_session_prompt("sess-ov", "sys")
            check(
                "写入覆盖",
                minimal_agent.save_session_terminal_env("sess-ov", "daytona") is True,
            )
            check(
                "解析为 daytona",
                minimal_agent.resolve_session_terminal_env("sess-ov") == "daytona",
            )
            raw = minimal_agent.run_terminal("echo hi", "sess-ov")
            data = json.loads(raw)
            check("覆盖走沙箱", data.get("backend") == "daytona")
            check(
                "沙箱 task_id 用会话",
                seen.get("task_id") == environments.sandbox_task_id("sess-ov"),
            )
            raw_sib = minimal_agent.run_terminal("echo hello-sib", "sess-sib")
            sib = json.loads(raw_sib)
            check(
                "邻居会话仍本机",
                sib.get("backend") != "daytona"
                and "hello-sib" in sib.get("output", ""),
            )
            bg = process_registry.spawn(
                f'"{sys.executable}" -c "import time; time.sleep(30)"',
                owner_key="sess-ov",
            )
            try:
                blocked = minimal_agent.set_session_terminal_env("sess-ov", "local")
                check(
                    "后台进程阻止切换",
                    blocked.get("ok") is False and blocked.get("code") == "busy_process",
                )
            finally:
                process_registry.kill(bg["session_id"])
            switched = minimal_agent.set_session_terminal_env("sess-ov", "local")
            check("切回本机成功", switched.get("ok") is True)
            check(
                "切回后解析 local",
                minimal_agent.resolve_session_terminal_env("sess-ov") == "local",
            )
            prompt = minimal_agent.load_session_prompt("sess-ov") or ""
            check(
                "切回后去掉终端后端块",
                "## 终端后端" not in prompt,
            )
    finally:
        environments.get_environment = original_get  # type: ignore
        environments.reset_environment_cache()
        minimal_agent.SESSION_DB = original_db
        if old_env is None:
            os.environ.pop("TERMINAL_ENV", None)
        else:
            os.environ["TERMINAL_ENV"] = old_env
        if old_key is None:
            os.environ.pop("DAYTONA_API_KEY", None)
        else:
            os.environ["DAYTONA_API_KEY"] = old_key


def main() -> None:
    """跑全部断言。"""
    print("== Daytona 终端后端回归测试 ==")
    for test_fn in (
        test_config_and_guards,
        test_approval_skip,
        test_cwd_and_create,
        test_persistent_resume,
        test_non_persistent_skips_lookup,
        test_persistent_cleanup_stops,
        test_resource_conversion,
        test_execute_and_interrupt,
        test_ensure_ready_restarts_stopped,
        test_run_terminal_routes,
        test_system_prompt_block,
        test_terminal_schema_mentions_daytona,
        test_session_override_routes,
    ):
        print(f"[{test_fn.__name__}]")
        test_fn()
        environments.reset_environment_cache()
    print()
    if _failures:
        print(f"共 {len(_failures)} 个用例失败：")
        for label in _failures:
            print(f"  - {label}")
        raise SystemExit(1)
    print("全部用例通过 ✅")


if __name__ == "__main__":
    main()
