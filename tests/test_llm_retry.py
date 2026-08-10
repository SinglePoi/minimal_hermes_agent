# -*- coding: utf-8 -*-
"""LLM 调用重试回归测试（对齐 Hermes agent/retry_utils.py 思路，简化版）。

覆盖：退避时长、可重试/不可重试错误分类、call_with_retry 循环与次数上限、
on_retry 回调、call_llm / call_llm_stream / generate_title 接线、LLM_MAX_RETRIES=0
旁路。零依赖，python tests/test_llm_retry.py 直接跑。
"""

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import retry_utils  # noqa: E402
from retry_utils import (  # noqa: E402
    call_with_retry,
    is_retryable_error,
    jittered_backoff,
)
import minimal_agent  # noqa: E402
from title_generator import generate_title  # noqa: E402

_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    """记录一条断言结果。"""
    if cond:
        print(f"  ok: {label}")
    else:
        _failures.append(label)
        print(f"  FAIL: {label}")


class FakeHTTPError(Exception):
    """带 HTTP 状态码的假异常（模拟 OpenAI 兼容客户端的限流/服务器错误）。"""

    def __init__(self, status_code: int):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class FlakyClient:
    """create 前 fails 次抛 status_code，之后按 stream 与否返回内容。"""

    def __init__(self, fails=1, status_code=429, text="ok", stream_text="hi"):
        self.fails = fails
        self.status_code = status_code
        self.text = text
        self.stream_text = stream_text
        self.calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls += 1
        if self.calls <= self.fails:
            raise FakeHTTPError(self.status_code)
        if kwargs.get("stream"):
            chunk = SimpleNamespace(
                usage=None,
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content=self.stream_text,
                            reasoning_content=None,
                            tool_calls=None,
                        )
                    )
                ],
            )
            return [chunk]
        return SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=10),
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.text))],
        )


def _noop_sleep():
    """把 time.sleep 换成空操作并返回恢复函数（测试里不想真等退避）。"""
    original = time.sleep
    time.sleep = lambda _s: None

    def restore():
        time.sleep = original

    return restore


def test_jittered_backoff() -> None:
    """退避：递增 + 封顶 + 随机抖动范围。"""
    d1 = jittered_backoff(1, base_delay=1.0, max_delay=8.0)
    check("第 1 次约 1s（含抖动）", 1.0 <= d1 <= 1.5)
    d2 = jittered_backoff(2, base_delay=1.0, max_delay=8.0)
    check("第 2 次约 2s", 2.0 <= d2 <= 3.0)
    d4 = jittered_backoff(4, base_delay=1.0, max_delay=8.0)
    check("第 4 次封顶 8s（含抖动）", 8.0 <= d4 <= 12.0)


def test_is_retryable_error() -> None:
    """分类：429/5xx/超时/断连可重试；4xx 其他与普通异常不重试。"""
    check("429 可重试", is_retryable_error(FakeHTTPError(429)) is True)
    for code in (500, 502, 503, 504):
        check(f"{code} 可重试", is_retryable_error(FakeHTTPError(code)) is True)
    for code in (400, 401, 403, 404, 422):
        check(f"{code} 不可重试", is_retryable_error(FakeHTTPError(code)) is False)
    check("内置超时可重试", is_retryable_error(TimeoutError("slow")) is True)
    check("内置断连可重试", is_retryable_error(ConnectionError("reset")) is True)
    check("普通异常不重试", is_retryable_error(RuntimeError("boom")) is False)
    check("关键词 rate limit 可重试", is_retryable_error(Exception("rate limit exceeded")) is True)
    check("关键词 timeout 可重试", is_retryable_error(Exception("connection timed out")) is True)


def test_call_with_retry() -> None:
    """重试循环：抖动失败后成功；不可重试立即失败；次数上限；回调。"""
    restore = _noop_sleep()
    try:
        attempts: list[int] = []

        def flaky():
            attempts.append(1)
            if len(attempts) < 3:
                raise FakeHTTPError(429)
            return "ok"

        check("重试后成功", call_with_retry(flaky, what="测试") == "ok")
        check("总尝试 3 次", len(attempts) == 3)

        # 不可重试：一次就抛，不重试
        attempts.clear()

        def bad():
            attempts.append(1)
            raise FakeHTTPError(400)

        try:
            call_with_retry(bad)
            check("400 立即抛", False)
        except FakeHTTPError:
            check("400 立即抛", len(attempts) == 1)

        # 耗尽重试次数后把最后一个异常抛出去
        attempts.clear()

        def always_fail():
            attempts.append(1)
            raise FakeHTTPError(429)

        try:
            call_with_retry(always_fail, max_retries=2)
            check("耗尽后抛出", False)
        except FakeHTTPError:
            check("耗尽后抛出", len(attempts) == 3)

        # on_retry 回调收到 what/attempt/delay
        calls: list[tuple] = []
        attempts.clear()

        def flaky_once():
            attempts.append(1)
            if len(attempts) == 1:
                raise FakeHTTPError(429)
            return 1

        call_with_retry(
            flaky_once,
            what="模型调用",
            on_retry=lambda w, a, d, e: calls.append((w, a, d, e)),
        )
        check("回调收到上下文", len(calls) == 1 and calls[0][0] == "模型调用" and calls[0][1] == 1)
    finally:
        restore()


def test_max_retries_zero() -> None:
    """LLM_MAX_RETRIES=0：完全不重试。"""
    old = os.environ.get("LLM_MAX_RETRIES")
    os.environ["LLM_MAX_RETRIES"] = "0"
    restore = _noop_sleep()
    try:
        client = FlakyClient(fails=1)
        try:
            minimal_agent.call_llm(client, [{"role": "user", "content": "hi"}], [])
            check("0 重试时直接抛", False)
        except FakeHTTPError:
            check("0 重试时直接抛", client.calls == 1)
    finally:
        restore()
        if old is None:
            os.environ.pop("LLM_MAX_RETRIES", None)
        else:
            os.environ["LLM_MAX_RETRIES"] = old


def test_call_llm_integration() -> None:
    """call_llm：429 抖动两次后成功，返回消息且只按需重试。"""
    restore = _noop_sleep()
    try:
        client = FlakyClient(fails=2, text="你好")
        msg, tokens = minimal_agent.call_llm(
            client, [{"role": "user", "content": "hi"}], []
        )
        check("重试后拿到回复", getattr(msg, "content", "") == "你好")
        check("调用次数 = 首次 + 2 次重试", client.calls == 3)
        check("prompt_tokens 统计", tokens == 10)
    finally:
        restore()


def test_call_llm_stream_integration() -> None:
    """call_llm_stream：只重试"接通"阶段，成功后正常累积 token。"""
    restore = _noop_sleep()
    try:
        client = FlakyClient(fails=1, stream_text="完成")
        msg, _tokens = minimal_agent.call_llm_stream(
            client, [{"role": "user", "content": "hi"}], []
        )
        check("流式重试后拿到内容", msg.content == "完成")
        check("流式调用次数 = 首次 + 1 次重试", client.calls == 2)
    finally:
        restore()


def test_generate_title_integration() -> None:
    """标题生成：429 抖动一次后成功。"""
    restore = _noop_sleep()
    try:
        client = FlakyClient(fails=1, text="旅行规划")
        title = generate_title("帮我规划行程", "好的", client=client)
        check("标题生成重试后成功", title == "旅行规划")
        check("标题生成调用次数=2", client.calls == 2)
    finally:
        restore()


def main() -> None:
    """跑全部断言。"""
    test_jittered_backoff()
    test_is_retryable_error()
    test_call_with_retry()
    test_max_retries_zero()
    test_call_llm_integration()
    test_call_llm_stream_integration()
    test_generate_title_integration()
    if _failures:
        print(f"\n{len(_failures)} 条断言失败：")
        for label in _failures:
            print(f"  - {label}")
        raise SystemExit(1)
    print("\n全部 LLM 重试断言通过")


if __name__ == "__main__":
    main()
