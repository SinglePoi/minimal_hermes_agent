# -*- coding: utf-8 -*-
"""
todo 工具模块（对齐 Hermes tools/todo_tool.py）——任务规划与进度跟踪。

设计（与 Hermes 一致）：
    - 每个会话一个内存 TodoStore（按 session_key 注册表管理）
    - 单个 `todo` 工具：传 todos 参数即写入、省略即读取；每次调用都返回完整列表
    - 不修改系统提示词、不改工具响应格式；行为引导全部写在工具 schema 描述里
    - 上下文压缩后把未完成任务清单重新注入对话（稳定头 TODO_INJECTION_HEADER），
      任务跨压缩不丢；只注入 pending/in_progress，避免模型重复做已完成的事
    - --resume / 服务端恢复会话时从历史消息里水合最近的 todo 列表
      （要求 tool 结果与之前的 assistant todo 调用配对，防伪造消息注入）

骨架简化掉的部分：无（内存态本身就是最小实现）。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


# 合法状态集合
VALID_STATUSES = {"pending", "in_progress", "completed", "cancelled"}

# 持久化上限（对齐 Hermes）：单条内容与总条数封顶，防止膨胀压缩后的重注入块
MAX_TODO_CONTENT_CHARS = 4000
MAX_TODO_ITEMS = 256
# 水合时单条 tool 结果的大小上限（防伪造的超大结果被解析重注入）
MAX_TODO_RESULT_CHARS = 512_000
_TRUNCATION_MARKER = "… [truncated]"

# 压缩重注入的稳定头：context_compressor 靠它识别"合成任务清单"行
TODO_INJECTION_HEADER = "[Your active task list was preserved across context compression]"


class TodoStore:
    """内存任务清单：每个会话一个实例。

    条目按列表顺序即优先级；每条含 id / content / status 三个字段。
    """

    def __init__(self) -> None:
        self._items: List[Dict[str, str]] = []

    def write(self, todos: List[Dict[str, Any]], merge: bool = False) -> List[Dict[str, str]]:
        """写入 todo，返回写入后的完整列表。

        merge=False：整体替换为新清单；merge=True：按 id 更新已有条目、追加新条目。
        """
        if not merge:
            self._items = [self._validate(t) for t in self._dedupe_by_id(todos)]
        else:
            existing = {item["id"]: item for item in self._items}
            for t in self._dedupe_by_id(todos):
                item_id = str(t.get("id", "")).strip()
                if not item_id:
                    continue  # 无 id 无法合并
                if item_id in existing:
                    if "content" in t and t["content"]:
                        existing[item_id]["content"] = self._cap_content(
                            str(t["content"]).strip()
                        )
                    if "status" in t and t["status"]:
                        status = str(t["status"]).strip().lower()
                        if status in VALID_STATUSES:
                            existing[item_id]["status"] = status
                else:
                    validated = self._validate(t)
                    existing[validated["id"]] = validated
                    self._items.append(validated)
            seen: set[str] = set()
            rebuilt: List[Dict[str, str]] = []
            for item in self._items:
                current = existing.get(item["id"], item)
                if current["id"] not in seen:
                    rebuilt.append(current)
                    seen.add(current["id"])
            self._items = rebuilt
        # 总条数封顶：保留优先级最高的头部（列表顺序即优先级）
        if len(self._items) > MAX_TODO_ITEMS:
            self._items = self._items[:MAX_TODO_ITEMS]
        return self.read()

    def read(self) -> List[Dict[str, str]]:
        """返回当前列表的副本。"""
        return [item.copy() for item in self._items]

    def has_items(self) -> bool:
        """判断清单是否非空。"""
        return bool(self._items)

    def format_for_injection(self) -> Optional[str]:
        """渲染压缩后重注入块；清单为空或没有未完成任务时返回 None。

        只注入 pending / in_progress（completed/cancelled 会导致模型压缩后
        重复做已完成的事）。首行是稳定头 TODO_INJECTION_HEADER。
        """
        if not self._items:
            return None
        markers = {
            "completed": "[x]",
            "in_progress": "[>]",
            "pending": "[ ]",
            "cancelled": "[~]",
        }
        active_items = [
            item for item in self._items
            if item["status"] in {"pending", "in_progress"}
        ]
        if not active_items:
            return None
        lines = [TODO_INJECTION_HEADER]
        for item in active_items:
            marker = markers.get(item["status"], "[?]")
            lines.append(f"- {marker} {item['id']}. {item['content']} ({item['status']})")
        return "\n".join(lines)

    @staticmethod
    def _cap_content(content: str) -> str:
        """截断超长条目内容到 MAX_TODO_CONTENT_CHARS（保留头部 + 截断标记）。"""
        if len(content) > MAX_TODO_CONTENT_CHARS:
            keep = MAX_TODO_CONTENT_CHARS - len(_TRUNCATION_MARKER)
            return content[:keep] + _TRUNCATION_MARKER
        return content

    @staticmethod
    def _validate(item: Dict[str, Any]) -> Dict[str, str]:
        """校验并归一化一条 todo（id/content 兜底、status 非法回退 pending）。"""
        if not isinstance(item, dict):
            return {"id": "?", "content": "(invalid item)", "status": "pending"}
        item_id = str(item.get("id", "")).strip()
        if not item_id:
            item_id = "?"
        content = str(item.get("content", "")).strip()
        if not content:
            content = "(no description)"
        else:
            content = TodoStore._cap_content(content)
        status = str(item.get("status", "pending")).strip().lower()
        if status not in VALID_STATUSES:
            status = "pending"
        return {"id": item_id, "content": content, "status": status}

    @staticmethod
    def _dedupe_by_id(todos: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """按 id 去重，保留最后一次出现的位置。"""
        last_index: Dict[str, int] = {}
        for i, item in enumerate(todos):
            if not isinstance(item, dict):
                last_index[f"__invalid_{i}"] = i
                continue
            item_id = str(item.get("id", "")).strip() or "?"
            last_index[item_id] = i
        return [todos[i] for i in sorted(last_index.values())]


# 会话级 store 注册表（对齐 Hermes：每个会话一个 TodoStore）
_todo_stores: Dict[str, TodoStore] = {}


def get_todo_store(session_key: str) -> TodoStore:
    """按会话 id 取 TodoStore；不存在则新建（空会话 key 归并到 _default）。"""
    key = session_key or "_default"
    if key not in _todo_stores:
        _todo_stores[key] = TodoStore()
    return _todo_stores[key]


def render_todo_lines(store: TodoStore) -> list[str]:
    """把清单渲染成可读行（供 REPL 面板/启动展示），空清单返回空列表。"""
    items = store.read()
    if not items:
        return []
    marks = {
        "pending": "[ ]",
        "in_progress": "[>]",
        "completed": "[x]",
        "cancelled": "[~]",
    }
    lines: list[str] = []
    for idx, item in enumerate(items, start=1):
        mark = marks.get(item["status"], "[?]")
        lines.append(f"{idx}. {mark} {item['content']}（{item['status']}）")
    return lines


def _assistant_todo_call_ids(messages: List[Dict[str, Any]]) -> set[str]:
    """收集历史里 assistant 的 todo 工具调用 id（用于 tool 结果配对校验）。"""
    ids: set[str] = set()
    for m in messages:
        if m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
            if fn.get("name") == "todo":
                ids.add(str(tc.get("id", "")))
    return ids


def hydrate_todo_store(messages: List[Dict[str, Any]], session_key: str) -> None:
    """从历史消息恢复最近的 todo 列表（对齐 Hermes AIAgent._hydrate_todo_store）。

    倒序找最近的 todo 工具结果；只接受与之前 assistant 的 todo 调用配对的
    tool 消息（防伪造），且结果大小有上限；找不到则保持空清单。
    """
    todo_ids = _assistant_todo_call_ids(messages)
    for m in reversed(messages):
        if m.get("role") != "tool" or str(m.get("tool_call_id") or "") not in todo_ids:
            continue
        content = m.get("content") or ""
        if not isinstance(content, str) or len(content) > MAX_TODO_RESULT_CHARS:
            continue
        try:
            data = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict) and isinstance(data.get("todos"), list):
            get_todo_store(session_key).write(data["todos"], merge=False)
            return


def todo_tool(
    todos: Optional[List[Dict[str, Any]]] = None,
    merge: bool = False,
    store: Optional[TodoStore] = None,
) -> str:
    """todo 工具入口：传 todos 写入、省略读取；返回完整列表 + 状态统计。

    对齐 Hermes：todos 可能是 JSON 字符串（模型偶发），自动解析；
    非法输入返回错误而非崩溃。
    """
    if store is None:
        return json.dumps({"success": False, "error": "TodoStore not initialized"},
                          ensure_ascii=False)
    if todos is not None:
        if isinstance(todos, str):
            try:
                todos = json.loads(todos)
            except (json.JSONDecodeError, TypeError):
                return json.dumps(
                    {"success": False,
                     "error": "todos must be a list of objects, got unparseable string"},
                    ensure_ascii=False,
                )
        if not isinstance(todos, list):
            return json.dumps(
                {"success": False,
                 "error": f"todos must be a list, got {type(todos).__name__}"},
                ensure_ascii=False,
            )
        items = store.write(todos, merge)
    else:
        items = store.read()

    pending = sum(1 for i in items if i["status"] == "pending")
    in_progress = sum(1 for i in items if i["status"] == "in_progress")
    completed = sum(1 for i in items if i["status"] == "completed")
    cancelled = sum(1 for i in items if i["status"] == "cancelled")
    return json.dumps({
        "success": True,
        "todos": items,
        "summary": {
            "total": len(items),
            "pending": pending,
            "in_progress": in_progress,
            "completed": completed,
            "cancelled": cancelled,
        },
    }, ensure_ascii=False)


def check_todo_requirements() -> bool:
    """todo 工具无外部依赖，恒可用。"""
    return True


# =============================================================================
# OpenAI Function-Calling Schema（行为引导写在描述里，随静态 schema 缓存）
# =============================================================================
TODO_SCHEMA = {
    "name": "todo",
    "description": (
        "管理当前会话的任务清单。适合 3 步以上的复杂任务，或用户一次给了多个任务。"
        "不带参数调用 = 读取当前清单。\n\n"
        "写入：\n"
        "- 传 todos 数组创建/更新条目\n"
        "- merge=false（默认）：用新计划整体替换清单\n"
        "- merge=true：按 id 更新已有条目、追加新条目\n\n"
        "每条：{id: 字符串, content: 字符串, status: pending|in_progress|completed|cancelled}\n"
        "列表顺序即优先级；同一时刻只允许一条 in_progress。\n"
        "完成立即标 completed；某项失败就标 cancelled 并新增修正条目。\n\n"
        "每次调用都返回完整当前清单。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "description": "要写入的任务条目；省略则读取当前清单。",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "唯一标识"},
                        "content": {"type": "string", "description": "任务描述"},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed", "cancelled"],
                            "description": "当前状态",
                        },
                    },
                    "required": ["id", "content", "status"],
                },
            },
            "merge": {
                "type": "boolean",
                "description": "true 时按 id 合并更新，false（默认）整体替换",
            },
        },
    },
    "required": [],
}
