# -*- coding: utf-8 -*-
"""会话导出（对齐 Hermes hermes_cli/session_export_md.py + session_export_html.py，简化版）。

只读格式化：把会话库里的消息渲染成 Markdown / 独立 HTML 文件，不改动任何
会话数据。骨架简化掉的部分（Hermes 有）：SHA256 导出校验、压缩分段、
tool_calls 明细、模型/提供商等元信息。
"""

import html as _html
import time
from pathlib import Path
from typing import Any, Optional

_EXPORTER_VERSION = "minimal-agent session export v1"
_ROLE_LABELS = {
    "user": "用户",
    "assistant": "助手",
    "system": "系统",
    "tool": "工具",
}


def _iso_now() -> str:
    """当前时间（本地，YYYY-MM-DD HH:MM:SS）。"""
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _session_meta(session_id: str) -> dict[str, Any]:
    """取会话元信息：标题 / 消息 / 首末时间（延迟导入避免循环依赖）。"""
    from minimal_agent import get_session_title, load_session_messages  # noqa: PLC0415

    messages = load_session_messages(session_id)
    title = get_session_title(session_id) or session_id
    return {
        "title": title,
        "messages": messages,
        "created_at": messages[0].get("created_at", "") if messages else "",
        "updated_at": messages[-1].get("created_at", "") if messages else "",
    }


def _role_label(role: str) -> str:
    """角色名转中文标签（未知角色原样返回）。"""
    return _ROLE_LABELS.get(role or "", role or "消息")


def export_session_md(session_id: str) -> str:
    """把会话渲染成 Markdown（frontmatter + 逐条消息标题）。"""
    meta = _session_meta(session_id)
    messages = meta["messages"]
    lines = [
        "---",
        f"session_id: {session_id!r}",
        f"title: {meta['title']!r}",
        f"created_at: {meta['created_at']!r}",
        f"updated_at: {meta['updated_at']!r}",
        f"message_count: {len(messages)}",
        "format: markdown",
        f"exported_at: {_iso_now()!r}",
        f"exporter: {_EXPORTER_VERSION!r}",
        "---",
        "",
        f"# {meta['title']}",
        "",
        f"Session ID: `{session_id}`",
        "",
        "## Messages",
        "",
    ]
    if not messages:
        lines.append("_该会话没有消息。_")
    for msg in messages:
        role = _role_label(msg.get("role", ""))
        ts = msg.get("created_at", "") or ""
        lines.append(f"### {role}" + (f" — {ts}" if ts else ""))
        lines.append("")
        lines.append(str(msg.get("content", "")).rstrip())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def export_session_html(session_id: str) -> str:
    """把会话渲染成独立 HTML（内容全部转义防 XSS，零依赖内联样式）。"""
    meta = _session_meta(session_id)
    messages = meta["messages"]
    esc = _html.escape

    body = [
        f"<h1>{esc(meta['title'])}</h1>",
        f'<p class="meta">会话 ID：<code>{esc(session_id)}</code> · '
        f"消息数：{len(messages)} · 导出时间：{esc(_iso_now())}</p>",
    ]
    if not messages:
        body.append('<p class="empty">该会话没有消息。</p>')
    for msg in messages:
        role = _role_label(msg.get("role", ""))
        ts = esc(msg.get("created_at", "") or "")
        content = esc(msg.get("content", "") or "")
        body.append(
            f'<div class="msg {esc(msg.get("role", ""))}">'
            f'<div class="role">{esc(role)}<span class="ts">{ts}</span></div>'
            f"<pre>{content}</pre></div>"
        )

    style = (
        "<style>"
        "body{max-width:820px;margin:32px auto;padding:0 20px;"
        'font-family:"Microsoft YaHei","Segoe UI",system-ui,sans-serif;color:#333}'
        ".meta{color:#888;font-size:13px}"
        ".msg{margin:14px 0;border:1px solid #eee;border-radius:10px;overflow:hidden}"
        ".role{padding:6px 12px;font-size:12px;font-weight:600;background:#f6f6f6;"
        "display:flex;justify-content:space-between;color:#666}"
        ".msg.user .role{background:#eef4ff;color:#2f6fd0}"
        ".msg.assistant .role{background:#eefaf0;color:#2ea043}"
        "pre{margin:0;padding:12px;white-space:pre-wrap;word-break:break-word;"
        "font-size:14px;line-height:1.6}"
        "</style>"
    )
    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh"><head><meta charset="utf-8">'
        f"<title>{esc(meta['title'])}</title>{style}</head><body>"
        + "".join(body)
        + "</body></html>"
    )


def export_session_file(
    session_id: str,
    fmt: str = "md",
    out_dir: Optional[str] = None,
) -> dict[str, Any]:
    """把会话写到文件（默认 ./exports/ 下）；返回 {success, path} 或错误。"""
    fmt = (fmt or "md").lower().strip()
    if fmt not in ("md", "html"):
        return {"success": False, "error": f"未知导出格式：{fmt}（支持 md / html）"}
    from minimal_agent import load_session_messages  # noqa: PLC0415

    if not load_session_messages(session_id):
        return {"success": False, "error": f"未找到会话 {session_id} 或它没有消息。"}
    base = Path(out_dir) if out_dir else Path.cwd() / "exports"
    base.mkdir(parents=True, exist_ok=True)
    ext = "html" if fmt == "html" else "md"
    path = base / f"session-{session_id}.{ext}"
    content = (
        export_session_html(session_id) if fmt == "html" else export_session_md(session_id)
    )
    path.write_text(content, encoding="utf-8")
    return {"success": True, "path": str(path)}
