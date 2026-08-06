"""PatchFileTool — apply a V4A-format patch to one or more files.

The V4A patch format is ported from Hermes Agent's patch_parser.py. It
supports add / update / delete file operations with context-anchored
diffs:

    *** Begin Patch
    *** Update File: path/to/file.py
    @@ context hint @@
     context line (space prefix)
    -removed line
    +added line
    *** Add File: path/to/new.py
    *** Delete File: path/to/old.py
    *** End Patch

Requires standard approval because it modifies files.
"""
from __future__ import annotations

from typing import Any

from zero.tools.base import Tool, ToolContext, ToolSpec

__all__ = ["PatchFileTool", "PATCH_FILE_SCHEMA", "register"]


PATCH_FILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "patch": {"type": "string", "description": "V4A patch format (see tools/patch_parser.py)"},
    },
    "required": ["patch"],
}


class PatchFileTool(Tool):
    """Apply a V4A patch to files (ported from Hermes patch_parser.py)."""

    spec = ToolSpec(
        name="patch_file",
        description="Apply a V4A-format patch to one or more files",
        parameters_schema=PATCH_FILE_SCHEMA,
        required_permissions=frozenset({"sandbox.exec"}),
        approval_level="standard",
    )

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> str:
        patch_text = str(args["patch"])
        from zero.tools.patch_parser import parse_patch, apply_patch  # noqa: PLC0415

        try:
            operations = parse_patch(patch_text)
            results = apply_patch(operations)
            return f"applied {len(results)} operations:\n" + "\n".join(results)
        except Exception as e:
            return f"[TOOL_ERROR] patch failed: {e}"


def register() -> None:
    """Register the PatchFileTool with the global tool registry."""
    from zero.tools.builtin_tools._helpers import register_tool  # noqa: PLC0415

    register_tool(PatchFileTool())
