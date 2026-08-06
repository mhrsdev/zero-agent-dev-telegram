"""MemorySearchTool — search memory entries for the current scope.

Searches the memory store (sync :class:`zero.memory.store.MemoryStore`
or async :class:`zero.memory.db_store.DbMemoryStore`) for entries
matching the query. Results are scoped to the current user/project —
no cross-scope leakage.

The memory store is injected via :func:`set_memory_store` (called by
:class:`zero.agents.runner.ZeroAgentRunner.setup()`).
"""
from __future__ import annotations

import asyncio
from typing import Any

from zero.memory.store import MemoryStore
from zero.tools.base import Tool, ToolContext, ToolSpec

__all__ = [
    "MemorySearchTool",
    "MEMORY_SEARCH_SCHEMA",
    "set_memory_store",
    "register",
]


MEMORY_SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Search query"},
        "limit": {"type": "integer", "default": 10},
    },
    "required": ["query"],
}

# Global memory store (injected by application setup).
# Accepts both sync MemoryStore and async DbMemoryStore.
_memory_store: MemoryStore | Any = None


def set_memory_store(store: MemoryStore | Any) -> None:
    """Inject the memory store.

    Accepts either:
        - ``MemoryStore`` (sync, in-memory, base class)
        - ``DbMemoryStore`` (async, DB-backed, enterprise)
    """
    global _memory_store
    _memory_store = store


class MemorySearchTool(Tool):
    """Search memory entries for the current scope."""

    spec = ToolSpec(
        name="memory_search",
        description="Search memory entries (scoped to current user/project)",
        parameters_schema=MEMORY_SEARCH_SCHEMA,
        required_permissions=frozenset(),
        approval_level="none",
    )

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> str:
        if _memory_store is None:
            return "[TOOL_ERROR] memory store not initialized"
        query = str(args["query"])
        limit = int(args.get("limit", 10))
        # Handle both sync (MemoryStore) and async (DbMemoryStore) retrieve.
        results_raw = _memory_store.retrieve(ctx.scope, query, limit=limit)
        # If it's a coroutine (DbMemoryStore), await it.
        if asyncio.iscoroutine(results_raw):
            results = await results_raw
        else:
            results = results_raw
        if not results:
            return "(no memory entries found)"
        lines = []
        for r in results:
            kind = r.entry.kind.value
            content = r.entry.content[:200]
            lines.append(f"[{kind}] {content}")
        return "\n---\n".join(lines)


def register() -> None:
    """Register the MemorySearchTool with the global tool registry."""
    from zero.tools.builtin_tools._helpers import register_tool  # noqa: PLC0415

    register_tool(MemorySearchTool())
