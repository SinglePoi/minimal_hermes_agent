# -*- coding: utf-8 -*-
"""LLM 调用重试（对齐 Hermes agent/retry_utils.py 的 jittered_backoff 思路，简化版）。

大白话：以前调大模型像"打一次电话，没人接就挂断"。重试 = 占线/对方服务器抽风
时等一小会儿再打，最多打 LLM_MAX_RETRIES 次。但不是所有"打不通"都值得重试：

- 会重试：限流 429、服务器 5xx、网络超时/断连——属于"线路忙 / 对方抽风"，
  等一会儿可能就好了；
- 不重试：参数错误 400、没权限 401/403、资源不存在 404——属于"打错号码 /
  没权限"，重试多少次结果都一样，还白花钱，直接放弃。

等待时间用"指数退避 + 随机抖动"：第 1 次重试前等约 1s、第 2 次约 2s、
第 3 次约 4s（封顶 8s），再叠加一个随机量——防止多个请求同时重试
（打雷时大家都同时重拨，会把线路再次占满）。
"""

import os
import random
import time
from typing import Any, Callable, Optional

_DEFAULT_MAX_RETRIES = 3
_BASE_DELAY = 1.0  # 第一次重试前的基准等待（秒）
_MAX_DELAY = 8.0  # 等待封顶（秒）


def _default_max_retries() -> int:
    """读取 LLM_MAX_RETRIES（默认 3；0 = 不重试，坏值回退默认）。"""
    try:
        return max(0, int(os.getenv("LLM_MAX_RETRIES", str(_DEFAULT_MAX_RETRIES))))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_RETRIES


def jittered_backoff(
    attempt: int,
    *,
    base_delay: float = _BASE_DELAY,
    max_delay: float = _MAX_DELAY,
    jitter_ratio: float = 0.5,
) -> float:
    """计算第 attempt 次重试前的等待秒数（指数退避 + 随机抖动）。

    attempt 从 1 开始：等待 ≈ min(base * 2^(attempt-1), max_delay)，
    再加 [0, jitter_ratio * delay] 的随机量打散同步重试。
    """
    exponent = max(0, attempt - 1)
    if exponent >= 63 or base_delay <= 0:
        delay = max_delay
    else:
        delay = min(base_delay * (2 ** exponent), max_delay)
    jitter = random.uniform(0, jitter_ratio * delay)
    return delay + jitter


def is_retryable_error(exc: Exception) -> bool:
    """判断这次失败是否值得重试：429/5xx/超时/断连 → True，其他 → False。"""
    # OpenAI 兼容 SDK 的异常一般带 status_code；兼容各种客户端形态
    status = getattr(exc, "status_code", None)
    if status is not None:
        try:
            status = int(status)
        except (TypeError, ValueError):
            status = None
    if status is not None:
        return status == 429 or status >= 500
    # 没有状态码：内置超时/断连类型直接算可重试
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    # 类型名 + 消息里的关键词兜底（不同 OpenAI 兼容客户端的错误类型不一样）
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(
        key in text
        for key in (
            "rate limit",
            "rate_limit",
            "timeout",
            "timed out",
            "connection",
            "temporarily",
            "overloaded",
            "server error",
            "internal server",
        )
    )


def call_with_retry(
    fn: Callable[[], Any],
    *,
    what: str = "LLM 调用",
    max_retries: Optional[int] = None,
    base_delay: float = _BASE_DELAY,
    max_delay: float = _MAX_DELAY,
    on_retry: Optional[Callable[[str, int, float, Exception], None]] = None,
) -> Any:
    """带重试地执行 fn：可重试错误按退避等待重来，最多 max_retries 次。

    总尝试次数 = 首次 + max_retries。不可重试的错误立即原样抛出。
    on_retry(what, attempt, delay, exc) 用于给用户可见的重试提示。
    """
    if max_retries is None:
        max_retries = _default_max_retries()
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as exc:
            if attempt >= max_retries or not is_retryable_error(exc):
                raise
            attempt += 1
            delay = jittered_backoff(
                attempt, base_delay=base_delay, max_delay=max_delay
            )
            if on_retry is not None:
                try:
                    on_retry(what, attempt, delay, exc)
                except Exception:
                    pass
            time.sleep(delay)
