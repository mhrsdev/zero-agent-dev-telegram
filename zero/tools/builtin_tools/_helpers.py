"""Shared helpers for builtin tool modules.

Each tool module in this package defines:
    - A ``Tool`` subclass (the tool implementation)
    - A ``register()`` function that registers the tool with the global registry

The package ``__init__.py`` imports and calls each tool's ``register()``
function, so importing ``zero.tools.builtin_tools`` registers all tools.

This modular structure makes it easy to add new tools in the future — just
create a new file in this directory and add it to the ``__init__.py`` imports.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from zero.tools.base import Tool, ToolContext
from zero.tools.registry import register as _registry_register

__all__ = [
    "ToolHandler",
    "register_tool",
]

# Type alias for tool handler functions.
ToolHandler = Callable[[dict[str, Any], ToolContext], Awaitable[str]]


def register_tool(tool: Tool) -> None:
    """Register a tool instance with the global registry.

    Creates an async handler closure that delegates to ``tool.execute()``
    and registers it with ``override=True`` so enterprise tools replace
    the legacy builtin tools of the same name.

    Args:
        tool: An instantiated :class:`zero.tools.base.Tool` subclass.
    """
    async def _handler(args: dict[str, Any], ctx: ToolContext, _t: Tool = tool) -> str:
        return await _t.execute(args, ctx)

    _registry_register(
        name=tool.spec.name,
        spec=tool.spec,
        handler=_handler,
        override=True,
    )
