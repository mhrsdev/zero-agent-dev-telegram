"""ListFilesTool — list files in a directory.

Enterprise version: supports ``recursive`` flag (the legacy builtin only
listed the top-level directory) and reports file sizes.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from zero.tools.base import Tool, ToolContext, ToolSpec

__all__ = ["ListFilesTool", "LIST_FILES_SCHEMA", "register"]


LIST_FILES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "Directory to list"},
        "pattern": {"type": "string", "description": "Glob pattern (default: *)"},
        "recursive": {"type": "boolean", "default": False},
    },
    "required": ["path"],
}


class ListFilesTool(Tool):
    """List files in a directory."""

    spec = ToolSpec(
        name="list_files",
        description="List files in a directory (optionally recursive)",
        parameters_schema=LIST_FILES_SCHEMA,
        required_permissions=frozenset({"sandbox.exec"}),
        approval_level="none",
    )

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> str:
        path_str = str(args["path"])
        pattern = str(args.get("pattern", "*"))
        recursive = bool(args.get("recursive", False))
        p = Path(path_str).expanduser()
        if not p.exists():
            return f"[TOOL_ERROR] directory not found: {path_str}"
        if not p.is_dir():
            return f"[TOOL_ERROR] not a directory: {path_str}"
        if recursive:
            entries = sorted(p.rglob(pattern))
        else:
            entries = sorted(p.glob(pattern))
        lines: list[str] = []
        for e in entries[:500]:
            rel = e.relative_to(p)
            if e.is_dir():
                lines.append(f"{rel}/")
            else:
                size = e.stat().st_size
                lines.append(f"{rel} ({size} bytes)")
        if len(entries) > 500:
            lines.append(f"[truncated: {len(entries) - 500} more entries]")
        return "\n".join(lines) if lines else "(empty directory)"


def register() -> None:
    """Register the ListFilesTool with the global tool registry."""
    from zero.tools.builtin_tools._helpers import register_tool  # noqa: PLC0415

    register_tool(ListFilesTool())
