"""TodoTool — manage a per-scope todo list (DB-backed with in-memory fallback).

When a :class:`zero.stores.todo_store.DbTodoStore` is injected via
:func:`set_todo_store`, todos persist to the appropriate scope's DB
table (personal_todos / normal_todos / dev_todos). When no store is
injected (e.g. in unit tests), todos fall back to an in-memory dict
scoped by ``scope.retrieval_key()``.

Actions: add, list, complete, remove.
"""
from __future__ import annotations

from typing import Any

from zero.tools.base import Tool, ToolContext, ToolSpec

__all__ = [
    "TodoTool",
    "TODO_SCHEMA",
    "set_todo_store",
    "register",
]


TODO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["add", "list", "complete", "remove"]},
        "item": {"type": "string", "description": "Todo item text (for add)"},
        "index": {"type": "integer", "description": "Item index (for complete/remove)"},
    },
    "required": ["action"],
}

# Global DB todo store (injected by application setup).
_db_todo_store: Any = None


def set_todo_store(store: Any) -> None:
    """Inject the DB-backed todo store.

    Called by :class:`zero.agents.runner.ZeroAgentRunner.setup()`. After
    this is called, all TodoTool operations persist to the DB.
    """
    global _db_todo_store
    _db_todo_store = store


class TodoTool(Tool):
    """Manage a per-scope todo list (DB-backed)."""

    # Fallback in-memory store (when no DB injected).
    _memory_stores: dict[str, list[str]] = {}

    spec = ToolSpec(
        name="todo",
        description="Manage a per-scope todo list (add, list, complete, remove)",
        parameters_schema=TODO_SCHEMA,
        required_permissions=frozenset(),
        approval_level="none",
    )

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> str:
        action = str(args["action"])
        scope_key = ctx.scope.retrieval_key()

        # Use DB store if available.
        if _db_todo_store is not None:
            return await self._execute_db(action, args, ctx, scope_key)
        # Fallback to in-memory.
        return self._execute_memory(action, args, scope_key)

    async def _execute_db(
        self, action: str, args: dict[str, Any], ctx: ToolContext, scope_key: str
    ) -> str:
        store = _db_todo_store
        if action == "add":
            item = str(args.get("item", ""))
            if not item:
                return "[TOOL_ERROR] item is required for add"
            todo = await store.add_async(
                scope=ctx.scope, text=item, created_by=ctx.actor_id,
            )
            return f"added: {todo.item_text}"
        if action == "list":
            items = await store.list_async(scope=ctx.scope)
            if not items:
                return "(no todos)"
            lines = []
            for i, t in enumerate(items, 1):
                mark = "[x]" if t.completed else "[ ]"
                lines.append(f"{i}. {mark} {t.item_text}")
            return "\n".join(lines)
        if action == "complete":
            idx = int(args.get("index", 0))
            item = await store.complete_async(scope=ctx.scope, index=idx)
            if item is None:
                return f"[TOOL_ERROR] invalid index {idx}"
            return f"completed: {item.item_text}"
        if action == "remove":
            idx = int(args.get("index", 0))
            item = await store.remove_async(scope=ctx.scope, index=idx)
            if item is None:
                return f"[TOOL_ERROR] invalid index {idx}"
            return f"removed: {item.item_text}"
        return f"[TOOL_ERROR] unknown action {action!r}"

    def _execute_memory(self, action: str, args: dict[str, Any], scope_key: str) -> str:
        todos = self._memory_stores.setdefault(scope_key, [])
        if action == "add":
            item = str(args.get("item", ""))
            if not item:
                return "[TOOL_ERROR] item is required for add"
            todos.append(item)
            return f"added item {len(todos)}: {item}"
        if action == "list":
            if not todos:
                return "(no todos)"
            return "\n".join(f"{i+1}. {t}" for i, t in enumerate(todos))
        if action == "complete":
            idx = int(args.get("index", 0)) - 1
            if 0 <= idx < len(todos):
                return f"completed: {todos.pop(idx)}"
            return f"[TOOL_ERROR] invalid index {idx+1}"
        if action == "remove":
            idx = int(args.get("index", 0)) - 1
            if 0 <= idx < len(todos):
                return f"removed: {todos.pop(idx)}"
            return f"[TOOL_ERROR] invalid index {idx+1}"
        return f"[TOOL_ERROR] unknown action {action!r}"


def register() -> None:
    """Register the TodoTool with the global tool registry."""
    from zero.tools.builtin_tools._helpers import register_tool  # noqa: PLC0415

    register_tool(TodoTool())
