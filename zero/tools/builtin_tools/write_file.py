"""WriteFileTool — write content to a file (overwrites or appends).

Enterprise version: supports ``append`` mode (the legacy builtin only
overwrote). Requires standard approval because file writes are
destructive (overwrite) or persistence (append).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from zero.tools.base import Tool, ToolContext, ToolSpec

__all__ = ["WriteFileTool", "WRITE_FILE_SCHEMA", "register"]


WRITE_FILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "content": {"type": "string"},
        "append": {"type": "boolean", "default": False, "description": "Append to file instead of overwrite"},
    },
    "required": ["path", "content"],
}


class WriteFileTool(Tool):
    """Write a file. Requires standard approval."""

    spec = ToolSpec(
        name="write_file",
        description="Write content to a file (overwrites if exists, or append)",
        parameters_schema=WRITE_FILE_SCHEMA,
        required_permissions=frozenset({"sandbox.exec"}),
        approval_level="standard",
    )

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> str:
        path_str = str(args["path"])
        content = str(args["content"])
        append = bool(args.get("append", False))
        p = Path(path_str).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            if append:
                with p.open("a", encoding="utf-8") as f:
                    f.write(content)
            else:
                p.write_text(content, encoding="utf-8")
        except OSError as e:
            return f"[TOOL_ERROR] cannot write file: {e}"
        action = "appended to" if append else "wrote"
        return f"{action} {len(content)} chars in {p}"


def register() -> None:
    """Register the WriteFileTool with the global tool registry."""
    from zero.tools.builtin_tools._helpers import register_tool  # noqa: PLC0415

    register_tool(WriteFileTool())
