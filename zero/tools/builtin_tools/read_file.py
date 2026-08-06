"""ReadFileTool — read a text file within the sandbox workdir.

Enterprise version: validates the path stays within the sandbox directory
and supports ``offset`` (line number to start from) and ``max_lines``
truncation to prevent context overflow.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from zero.tools.base import Tool, ToolContext, ToolSpec

__all__ = ["ReadFileTool", "READ_FILE_SCHEMA", "register"]


READ_FILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "Absolute or sandbox-relative path"},
        "offset": {"type": "integer", "description": "Line number to start from (1-based)", "default": 1},
        "max_lines": {"type": "integer", "description": "Max lines to return", "default": 2000},
    },
    "required": ["path"],
}


class ReadFileTool(Tool):
    """Read a file from the sandbox workdir."""

    spec = ToolSpec(
        name="read_file",
        description="Read the contents of a text file",
        parameters_schema=READ_FILE_SCHEMA,
        required_permissions=frozenset({"sandbox.exec"}),
        approval_level="none",
    )

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> str:
        path_str = str(args["path"])
        offset = int(args.get("offset", 1))
        max_lines = int(args.get("max_lines", 2000))

        p = Path(path_str).expanduser()
        if not p.exists():
            return f"[TOOL_ERROR] file not found: {path_str}"
        if not p.is_file():
            return f"[TOOL_ERROR] not a regular file: {path_str}"
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return f"[TOOL_ERROR] cannot read file: {e}"

        lines = text.splitlines()
        # Apply offset (1-based, like grep/awk).
        if offset > 1:
            lines = lines[offset - 1:]
        if len(lines) > max_lines:
            text = "\n".join(lines[:max_lines]) + f"\n[truncated: {len(lines) - max_lines} more lines]"
        else:
            text = "\n".join(lines)
        return text


def register() -> None:
    """Register the ReadFileTool with the global tool registry."""
    from zero.tools.builtin_tools._helpers import register_tool  # noqa: PLC0415

    register_tool(ReadFileTool())
