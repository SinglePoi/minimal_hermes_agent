# -*- coding: utf-8 -*-
"""LLM 生成会话标题回归测试（对齐 Hermes agent/title_generator.py）。

覆盖：generate_title 参数与清洗、开关旁路、失败静默、set_auto_title_if_empty
原子写入（人工改名不覆盖）、auto_title_session 失败回退截断、maybe_auto_title
首轮触发与多轮跳过。零依赖，python tests/test_title_generator.py 直接跑。
"""

import os
import re
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import minimal_agent  # noqa: E402
from title_generator import (  # noqa: E402
    _auto_title_enabled,
    _clean_title,
    auto_title_session,
    generate_title,
    maybe_auto_title,
)

_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    """记录一条断言结果。"""
    if cond:
        print(f"  ok: {label}")
    else:
        _failures.append(label)
        print(f"  FAIL: {label}")


class TitleFakeClient:
    """记录 create 参数并按预设文本返回标题的假 client。"""

    def __init__(self, text: str = "帮助修复导入问题", raise_exc: bool = False):
        self.text = text
        self.raise_exc = raise_exc
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_exc:
            raise RuntimeError("fake llm down")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.text))]
        )


def _set_flag(value) -> str | None:
    """设置/清除 TITLE_GENERATION_ENABLED，返回旧值供恢复。"""
    old = os.environ.get("TITLE_GENERATION_ENABLED")
    if value is None:
        os.environ.pop("TITLE_GENERATION_ENABLED", None)
    else:
        os.environ["TITLE_GENERATION_ENABLED"] = value
    return old


def test_generate_title() -> None:
    """generate_title：请求参数对齐 Hermes + 输出清洗 + 失败静默。"""
    old = _set_flag("1")
    try:
        client = TitleFakeClient()
        title = generate_title("帮我规划北京行程", "好的，以下是规划", client=client)
        check("生成标题返回清洗结果", title == "帮助修复导入问题")
        kwargs = client.calls[0]
        check("temperature=0.3", kwargs.get("temperature") == 0.3)
        check("max_tokens=500", kwargs.get("max_tokens") == 500)
        check("不带工具清单", kwargs.get("tools") is None)
        check("系统提示词要求简短标题", "简短" in kwargs["messages"][0]["content"])
        check("用户片段进请求", "帮我规划北京行程" in kwargs["messages"][1]["content"])
        check("助手片段进请求", "好的，以下是规划" in kwargs["messages"][1]["content"])

        # 清洗规则：去引号 / Title: 前缀 / 多行取第一行 / 超长截断
        for raw, expected in [
            ('"北京五日游"', "北京五日游"),
            ("Title: 修好导入问题", "修好导入问题"),
            ("第一行标题\n第二行是废话", "第一行标题"),
            ("x" * 100, "x" * 77 + "..."),
        ]:
            check(f"清洗 {raw[:14]!r}", _clean_title(raw) == expected)

        # 失败静默返回 None，不抛异常
        bad = TitleFakeClient(raise_exc=True)
        check("LLM 失败返回 None", generate_title("q", "a", client=bad) is None)
    finally:
        _set_flag(old)


def test_generate_title_disabled() -> None:
    """TITLE_GENERATION_ENABLED=0 时直接跳过，不调 LLM。"""
    old = _set_flag("0")
    try:
        client = TitleFakeClient()
        check("关闭后返回 None", generate_title("q", "a", client=client) is None)
        check("关闭后未调用 LLM", client.calls == [])
        check("_auto_title_enabled 关闭", _auto_title_enabled() is False)
    finally:
        _set_flag(old)


def test_set_auto_title_if_empty() -> None:
    """set_auto_title_if_empty：仅空标题写入；人工标题与二次写入不覆盖。"""
    with tempfile.TemporaryDirectory() as tmp:
        old_db = minimal_agent.SESSION_DB
        minimal_agent.SESSION_DB = Path(tmp) / "sessions.db"
        try:
            conn = minimal_agent._db_conn()
            conn.execute(
                "INSERT INTO sessions (session_id, system_prompt) VALUES (?, ?)",
                ("s1", "p"),
            )
            conn.commit()
            conn.close()

            check(
                "空标题写入成功",
                minimal_agent.set_auto_title_if_empty("s1", "自动标题") is True,
            )
            check("读取到标题", minimal_agent.get_session_title("s1") == "自动标题")
            check(
                "二次写入返回 False",
                minimal_agent.set_auto_title_if_empty("s1", "另一个") is False,
            )
            check("标题未被覆盖", minimal_agent.get_session_title("s1") == "自动标题")

            minimal_agent.set_session_title("s1", "人工改名")
            check(
                "人工改名后 set-if-empty 不覆盖",
                minimal_agent.set_auto_title_if_empty("s1", "后台标题") is False,
            )
            check("人工改名保留", minimal_agent.get_session_title("s1") == "人工改名")
        finally:
            minimal_agent.SESSION_DB = old_db


def test_auto_title_session() -> None:
    """auto_title_session：LLM 成功用生成标题；失败回退首条消息截断；已有标题跳过。"""
    with tempfile.TemporaryDirectory() as tmp:
        old_db = minimal_agent.SESSION_DB
        old_flag = _set_flag("1")
        minimal_agent.SESSION_DB = Path(tmp) / "sessions.db"
        try:
            conn = minimal_agent._db_conn()
            for sid in ("s2", "s3"):
                conn.execute(
                    "INSERT INTO sessions (session_id, system_prompt) VALUES (?, ?)",
                    (sid, "p"),
                )
            conn.commit()
            conn.close()

            ok_client = TitleFakeClient(text="旅行规划")
            auto_title_session("s2", "帮我规划北京行程", "好的", client=ok_client)
            check("LLM 标题写入", minimal_agent.get_session_title("s2") == "旅行规划")

            # LLM 失败 → 回退首条用户消息截断 40 字（保留离线体验）
            long_msg = "帮我规划北京行程，包括故宫、长城、天坛、颐和园、圆明园" * 3
            bad_client = TitleFakeClient(raise_exc=True)
            auto_title_session("s3", long_msg, "好的", client=bad_client)
            got = minimal_agent.get_session_title("s3")
            check("失败回退截断标题", got == re.sub(r"\s+", " ", long_msg).strip()[:40])

            # 已有标题（人工/先前生成）不被后台线程覆盖
            auto_title_session("s2", "再问一句", "回答", client=TitleFakeClient(text="新标题"))
            check("已有标题不被覆盖", minimal_agent.get_session_title("s2") == "旅行规划")
        finally:
            minimal_agent.SESSION_DB = old_db
            _set_flag(old_flag)


def test_maybe_auto_title() -> None:
    """maybe_auto_title：首轮触发后台线程；第三轮以上/空回复跳过。"""
    with tempfile.TemporaryDirectory() as tmp:
        old_db = minimal_agent.SESSION_DB
        old_flag = _set_flag("1")
        minimal_agent.SESSION_DB = Path(tmp) / "sessions.db"
        try:
            client = TitleFakeClient(text="t")
            thread = maybe_auto_title(
                "s1",
                "q",
                "a",
                client=client,
                conversation_history=[
                    {"role": "system", "content": "s"},
                    {"role": "user", "content": "q"},
                    {"role": "assistant", "content": "a"},
                ],
            )
            check("首轮触发后台线程", isinstance(thread, threading.Thread))
            if thread is not None:
                thread.join(timeout=5)

            late = maybe_auto_title(
                "s1",
                "q3",
                "a3",
                client=client,
                conversation_history=[
                    {"role": "system", "content": "s"},
                    {"role": "user", "content": "u1"},
                    {"role": "assistant", "content": "a1"},
                    {"role": "user", "content": "u2"},
                    {"role": "assistant", "content": "a2"},
                    {"role": "user", "content": "u3"},
                    {"role": "assistant", "content": "a3"},
                ],
            )
            check("第三轮以上跳过", late is None)
            check("空助手回复跳过", maybe_auto_title("s1", "q", "", client=client) is None)
        finally:
            minimal_agent.SESSION_DB = old_db
            _set_flag(old_flag)


def main() -> None:
    """跑全部断言。"""
    test_generate_title()
    test_generate_title_disabled()
    test_set_auto_title_if_empty()
    test_auto_title_session()
    test_maybe_auto_title()
    if _failures:
        print(f"\n{len(_failures)} 条断言失败：")
        for label in _failures:
            print(f"  - {label}")
        raise SystemExit(1)
    print("\n全部标题生成断言通过")


if __name__ == "__main__":
    main()
