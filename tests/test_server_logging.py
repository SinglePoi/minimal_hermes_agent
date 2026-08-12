# -*- coding: utf-8 -*-
"""服务化日志模块回归测试（零依赖，python tests/test_server_logging.py 直接跑）

覆盖：
    - JSON Lines 格式：ts/level/event/session_id/thread/msg + 自定义字段
    - 大小轮转：超过 maxBytes 产生 .1 备份
    - 脱敏：sk- 密钥不打明文（对齐 Hermes RedactingFormatter）
    - 会话上下文：set/clear 后日志带/不带 session_id（对齐 Hermes set_session_context）
    - setup 幂等与 force 重配
    - 环境变量配置（SERVER_LOG_PATH / SERVER_LOG_MAX_MB / SERVER_LOG_BACKUP_COUNT）
"""

import json
import logging
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import server_logging  # noqa: E402
from server_logging import (  # noqa: E402
    clear_session_context,
    get_logger,
    log_event,
    set_session_context,
    setup_logging,
    shutdown_logging,
)

_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    """记录一条断言结果。"""
    if cond:
        print(f"  ok: {label}")
    else:
        _failures.append(label)
        print(f"  FAIL: {label}")


def _read_json_lines(path: Path) -> list[dict]:
    """读取日志文件并逐行解析 JSON（跳过空行）。"""
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").strip().splitlines()
        if line.strip()
    ]


def test_json_format() -> None:
    """JSON Lines 格式：字段齐全，自定义字段原样带上。"""
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "server.log"
    try:
        setup_logging(path=str(path), force=True)
        log_event(
            get_logger(),
            "turn.end",
            session_id="s-1",
            duration_ms=12,
            tools=["get_current_time"],
        )
        records = _read_json_lines(path)
        check("日志为单行 JSON", len(records) == 1)
        rec = records[0]
        check(
            "基础字段齐全",
            rec.get("event") == "turn.end"
            and rec.get("session_id") == "s-1"
            and rec.get("level") == "INFO",
        )
        check(
            "自定义字段原样带上",
            rec.get("duration_ms") == 12
            and rec.get("tools") == ["get_current_time"],
        )
        check("ts 是时间戳", "T" in rec.get("ts", ""))
        check("含线程名", isinstance(rec.get("thread"), str) and rec["thread"])
    finally:
        shutdown_logging()
        tmp.cleanup()


def test_rotation() -> None:
    """超过 maxBytes 触发轮转，产生 .1 备份。"""
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "server.log"
    try:
        setup_logging(path=str(path), max_mb=0.001, backup_count=2, force=True)
        log_event(get_logger(), "a", msg="x" * 100)
        log_event(get_logger(), "b", msg="x" * 3000)
        check("轮转产生 .1 备份", Path(str(path) + ".1").exists())
        check("主文件仍在", path.exists())
        check("主文件可解析", len(_read_json_lines(path)) >= 1)
    finally:
        shutdown_logging()
        tmp.cleanup()


def test_redaction() -> None:
    """脱敏：sk- 密钥不打明文（对齐 Hermes RedactingFormatter）。"""
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "server.log"
    try:
        setup_logging(path=str(path), force=True)
        log_event(
            get_logger(),
            "chat.error",
            level=logging.ERROR,
            msg="leak sk-abc1234567890 here",
            session_id="s-1",
        )
        content = path.read_text(encoding="utf-8")
        check("明文密钥不打入日志", "sk-abc1234567890" not in content)
        check("已打码", "***" in content)
    finally:
        shutdown_logging()
        tmp.cleanup()


def test_session_context() -> None:
    """会话上下文：set 后带 session_id，clear 后不带（对齐 Hermes）。"""
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "server.log"
    try:
        setup_logging(path=str(path), force=True)
        set_session_context("ctx-1")
        log_event(get_logger(), "a", msg="hello")
        clear_session_context()
        log_event(get_logger(), "b", msg="world")
        records = _read_json_lines(path)
        check("上下文内日志带 session_id", records[0].get("session_id") == "ctx-1")
        check("清除后日志不带 session_id", "session_id" not in records[1])
    finally:
        shutdown_logging()
        tmp.cleanup()


def test_idempotent_and_force() -> None:
    """setup 幂等不叠加 handler；force=True 重配到新路径。"""
    tmp = tempfile.TemporaryDirectory()
    p1 = Path(tmp.name) / "a.log"
    p2 = Path(tmp.name) / "b.log"
    try:
        setup_logging(path=str(p1), force=True)
        setup_logging(path=str(p1))
        check("重复 setup 不叠加 handler", len(get_logger().handlers) == 1)
        setup_logging(path=str(p2), force=True)
        check("force 重配后仍只有一个 handler", len(get_logger().handlers) == 1)
        log_event(get_logger(), "x", msg="after-force")
        check("新路径写入成功", len(_read_json_lines(p2)) == 1)
    finally:
        shutdown_logging()
        tmp.cleanup()


def test_env_config() -> None:
    """环境变量三同步：SERVER_LOG_PATH / MAX_MB / BACKUP_COUNT 生效。"""
    tmp = tempfile.TemporaryDirectory()
    try:
        os.environ["SERVER_LOG_PATH"] = str(Path(tmp.name) / "env.log")
        os.environ["SERVER_LOG_MAX_MB"] = "1"
        os.environ["SERVER_LOG_BACKUP_COUNT"] = "2"
        path = setup_logging(force=True)
        check("env 指定路径生效", path == Path(tmp.name) / "env.log")
        handler = next(h for h in get_logger().handlers)
        check("env 轮转阈值生效", handler.maxBytes == 1024 * 1024)
        check("env 备份数生效", handler.backupCount == 2)
    finally:
        for key in ("SERVER_LOG_PATH", "SERVER_LOG_MAX_MB", "SERVER_LOG_BACKUP_COUNT"):
            os.environ.pop(key, None)
        shutdown_logging()
        tmp.cleanup()


def main() -> None:
    """依次运行全部测试并汇总结果。"""
    print("== 服务化日志回归测试 ==")
    for test_fn in (
        test_json_format,
        test_rotation,
        test_redaction,
        test_session_context,
        test_idempotent_and_force,
        test_env_config,
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
