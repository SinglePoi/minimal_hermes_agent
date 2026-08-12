# -*- coding: utf-8 -*-
"""服务化结构化日志 + 大小轮转（对齐 Hermes hermes_logging.py，骨架单进程简化版）。

设计对齐点：
    - 集中式 setup_logging()：Hermes 的 CLI/gateway 启动早期调用一次，骨架在
      run_server() 里调用；重复调用幂等（force=True 才重配）
    - RotatingFileHandler 按大小轮转（对齐 Hermes 默认 5MB / 3 份备份）
    - 脱敏格式器：所有字符串字段经 redact_sensitive_text(force=True) 再落盘，
      密钥不打明文（对齐 Hermes 的 RedactingFormatter）
    - 会话关联 set_session_context / clear_session_context（thread-local，
      同线程后续日志自动带 session_id，对齐 Hermes 同名函数）
    - Windows 单进程场景用标准库 RotatingFileHandler 即可；Hermes 多进程会换
      concurrent-log-handler 做跨进程轮转锁，骨架是单进程服务，不需要

环境变量（新增必须三同步：.env / .env.example / README 环境变量表）：
    SERVER_LOG_PATH          日志文件路径（默认 logs/server.log，相对项目根）
    SERVER_LOG_MAX_MB        单文件轮转阈值 MB（默认 5）
    SERVER_LOG_BACKUP_COUNT  保留的轮转备份数（默认 3）
"""

import json
import logging
import os
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from redact import redact_sensitive_text

ROOT = Path(__file__).resolve().parent

# 标准 LogRecord 保留属性：extra 里的自定义字段不能与它们重名，格式器也只挑
# 非保留属性序列化（对齐 Hermes：日志字段扁平、可被日志平台直接消费）
_STANDARD_RECORD_ATTRS = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "message", "asctime", "taskName",
    }
)

# 线程局部会话上下文（对齐 Hermes set_session_context / clear_session_context）
_session_context = threading.local()


def _env_int(name: str, default: int, min_value: int = 0) -> int:
    """读取整型环境变量；非法或缺失时回退默认值（下限保护防配置错误）。"""
    try:
        value = int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        value = default
    return value if value >= min_value else min_value


def get_logger(name: str = "server") -> logging.Logger:
    """返回服务日志 logger（不触发 setup；无 handler 时 INFO 级日志静默丢弃）。"""
    return logging.getLogger(name)


def set_session_context(session_id: str) -> None:
    """设置当前线程的会话上下文：本线程后续日志自动带 session_id（对齐 Hermes）。"""
    _session_context.session_id = str(session_id)


def clear_session_context() -> None:
    """清除当前线程的会话上下文（会话结束/异常路径必须配对调用）。"""
    _session_context.session_id = None


class _RedactingJSONFormatter(logging.Formatter):
    """JSON Lines 格式器：ts/level/event/session_id/thread/msg + 自定义字段。

    所有字符串值落盘前过 redact_sensitive_text(force=True)，密钥不打明文
    （对齐 Hermes 的 RedactingFormatter）。
    """

    def format(self, record: logging.LogRecord) -> str:
        """把一条日志记录序列化成单行 JSON。"""
        data: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "thread": record.threadName or "",
        }
        event = getattr(record, "event", None)
        if event:
            data["event"] = str(event)
        session_id = getattr(record, "session_id", None)
        if not session_id:
            session_id = getattr(_session_context, "session_id", None)
        if session_id:
            data["session_id"] = str(session_id)
        message = record.getMessage() or (str(event) if event else "")
        data["msg"] = redact_sensitive_text(message, force=True)
        # 自定义字段：extra 里非保留属性按原名带上（脱敏后）
        for key, value in record.__dict__.items():
            if key in _STANDARD_RECORD_ATTRS or key in ("event", "session_id"):
                continue
            if isinstance(value, str):
                data[key] = redact_sensitive_text(value, force=True)
            elif isinstance(value, (int, float, bool, list, dict)) or value is None:
                data[key] = value
        return json.dumps(data, ensure_ascii=False)


def log_event(
    logger: logging.Logger,
    event: str,
    msg: str = "",
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    """记录一个带事件名的结构化日志；fields 会成为 JSON 里的自定义字段。"""
    logger.log(level, msg or event, extra={"event": event, **fields})


def setup_logging(
    *,
    path: str | None = None,
    max_mb: int | None = None,
    backup_count: int | None = None,
    force: bool = False,
) -> Path:
    """配置服务日志：JSON Lines + 大小轮转；重复调用幂等（force=True 重配）。

    参数缺省时读环境变量（SERVER_LOG_PATH / SERVER_LOG_MAX_MB /
    SERVER_LOG_BACKUP_COUNT），再回退默认值（logs/server.log、5MB、3 份）。
    返回日志文件路径（父目录自动创建）。
    """
    log_path = Path(
        path or os.environ.get("SERVER_LOG_PATH", "") or (ROOT / "logs" / "server.log")
    )
    if not log_path.is_absolute():
        log_path = ROOT / log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)
    max_bytes = (max_mb if max_mb is not None else _env_int("SERVER_LOG_MAX_MB", 5, 1)) * 1024 * 1024
    backups = backup_count if backup_count is not None else _env_int(
        "SERVER_LOG_BACKUP_COUNT", 3, 0
    )

    logger = get_logger()
    if force:
        shutdown_logging()
    if any(isinstance(h, RotatingFileHandler) for h in logger.handlers):
        return log_path

    handler = RotatingFileHandler(
        str(log_path),
        maxBytes=max_bytes,
        backupCount=backups,
        encoding="utf-8",
    )
    handler.setFormatter(_RedactingJSONFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return log_path


def shutdown_logging() -> None:
    """关闭并移除服务日志 handler（测试隔离 / 服务退出时调用）。"""
    logger = get_logger()
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass


if __name__ == "__main__":
    # 快速自检：python server_logging.py
    target = setup_logging(force=True)
    log_event(get_logger(), "self.test", session_id="demo", duration_ms=1)
    print(f"日志已写入：{target}（含一条 self.test 记录）")
    sys.exit(0)
