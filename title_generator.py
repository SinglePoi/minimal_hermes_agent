# -*- coding: utf-8 -*-
"""LLM 自动生成会话标题（对齐 Hermes agent/title_generator.py）。

Hermes 的设计：
- 首轮用户→助手交换完成后，后台线程调用一次 LLM 生成 3-7 词标题，
  不增加用户等待回复的延迟；
- 生成请求很小（首轮交换各截 500 字），temperature=0.3、max_tokens=500；
- 输出清洗：去引号 / "Title:" 前缀 / 只取第一行 / 超 80 字截断；
- 失败静默返回 None，绝不打断主流程；
- 落库用 set_auto_title_if_empty 原子判定（谓词+写入一个事务），
  人工改名与后台生成并发时人工改名优先，不会被覆盖。

骨架差异：LLM 失败时回退到"首条用户消息截断 40 字"（保留原有离线体验），
Hermes 不回退（保留 NULL 标题由用户手动命名）。
"""

import os
import re
import threading
from typing import Any, Optional

_TITLE_PROMPT = (
    "为以下对话生成一个简短、贴切的标题（3-7 个词）。"
    "标题应抓住对话的主要话题或意图，用与用户相同的语言书写。"
    "只输出标题文本，不要输出任何其他内容；"
    "不加引号、不加结尾标点、不加前缀。"
)

_MAX_SNIPPET_CHARS = 500  # 首轮交换各截 500 字，保持请求很小（对齐 Hermes）
_MAX_TITLE_CHARS = 80  # 标题长度上限（对齐 Hermes）
_TITLE_TIMEOUT = 30  # LLM 调用超时（秒）


def _auto_title_enabled() -> bool:
    """读取 TITLE_GENERATION_ENABLED（默认开启；0/false/off 关闭）。"""
    return os.getenv("TITLE_GENERATION_ENABLED", "true").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _clean_title(raw: str) -> str:
    """清洗模型输出：去引号与 "Title:" 前缀、只取第一行、限制长度。"""
    title = (raw or "").strip().strip('"\'')
    if title.lower().startswith("title:"):
        title = title[6:].strip()
    title = next((line.strip() for line in title.splitlines() if line.strip()), "")
    if len(title) > _MAX_TITLE_CHARS:
        title = title[: _MAX_TITLE_CHARS - 3] + "..."
    return title


def generate_title(
    user_message: str,
    assistant_response: str,
    client: Any = None,
    timeout: Optional[float] = None,
) -> Optional[str]:
    """用 LLM 从首轮交换生成会话标题；失败返回 None，绝不抛异常。

    复用主模型的 client（骨架单模型 DeepSeek，对齐 Hermes"用主 runtime 模型"）；
    client 为 None 时按主程序方式新建。请求体只有系统提示词 + 两段截断文本，
    不带工具清单，也不进主循环的 token 预算统计。
    """
    if not _auto_title_enabled():
        return None

    # 延迟导入，避免与 minimal_agent 的模块级导入互相循环
    from minimal_agent import MODEL, create_client  # noqa: PLC0415

    llm_client = client or create_client()
    user_snippet = (user_message or "")[:_MAX_SNIPPET_CHARS]
    assistant_snippet = (assistant_response or "")[:_MAX_SNIPPET_CHARS]
    messages = [
        {"role": "system", "content": _TITLE_PROMPT},
        {"role": "user", "content": f"用户：{user_snippet}\n\n助手：{assistant_snippet}"},
    ]
    try:
        response = llm_client.chat.completions.create(
            model=MODEL,
            messages=messages,
            temperature=0.3,
            max_tokens=500,
            timeout=timeout if timeout is not None else _TITLE_TIMEOUT,
        )
        content = response.choices[0].message.content or ""
        title = _clean_title(content)
        return title or None
    except Exception:
        return None


def auto_title_session(
    session_id: str,
    user_message: str,
    assistant_response: str,
    client: Any = None,
) -> None:
    """同步生成并写入会话标题（后台线程目标；REPL 一次性模式也直接调用）。

    顺序：先查现有标题（有则跳过）→ LLM 生成 → 失败回退首条用户消息截断 40 字
    → set_auto_title_if_empty 原子写入（人工改名不会被覆盖）。任何异常不外抛。
    """
    try:
        if not session_id:
            return
        # 延迟导入，避免模块级循环依赖
        from minimal_agent import get_session_title, set_auto_title_if_empty  # noqa: PLC0415

        try:
            if get_session_title(session_id):
                return
        except Exception:
            return

        title = generate_title(user_message, assistant_response, client=client)
        if not title:
            title = re.sub(r"\s+", " ", user_message).strip()[:40]
        if title:
            set_auto_title_if_empty(session_id, title)
    except Exception:
        # 后台线程目标：异常不外抛（对齐 Hermes auto_title_session 的兜底）
        return


def maybe_auto_title(
    session_id: str,
    user_message: str,
    assistant_response: str,
    client: Any = None,
    conversation_history: Optional[list[dict[str, Any]]] = None,
) -> Optional[threading.Thread]:
    """首轮交换后后台异步生成标题（对齐 Hermes maybe_auto_title）。

    仅在以下情况触发：用户消息数 ≤ 2（首轮或第二轮）、会话尚无标题、
    TITLE_GENERATION_ENABLED 开启、首轮交换内容齐全。返回后台线程（便于调用方
    join 排空），跳过时返回 None。
    """
    if not session_id or not user_message or not assistant_response:
        return None
    user_msg_count = sum(
        1 for m in (conversation_history or []) if m.get("role") == "user"
    )
    if user_msg_count > 2:
        return None
    if not _auto_title_enabled():
        return None

    thread = threading.Thread(
        target=auto_title_session,
        args=(session_id, user_message, assistant_response, client),
        daemon=True,
        name="auto-title",
    )
    thread.start()
    return thread
