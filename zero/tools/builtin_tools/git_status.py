"""GitStatusTool — get git status of the working directory.

Runs ``git status --short --branch`` and returns the output. Useful for
the agent to understand the current state of a git repository before
making changes (e.g. check for uncommitted changes, see the current
branch).
"""
from __future__ import annotations

import asyncio
import shutil
from typing import Any

from zero.tools.base import Tool, ToolContext, ToolSpec

__all__ = ["GitStatusTool", "GIT_STATUS_SCHEMA", "register"]


GIT_STATUS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cwd": {"type": "string", "description": "Working directory (default: current)"},
    },
    "required": [],
}


class GitStatusTool(Tool):
    """Get git status of the working directory."""

    spec = ToolSpec(
        name="git_status",
        description="Get git status (short format) of the working directory",
        parameters_schema=GIT_STATUS_SCHEMA,
        required_permissions=frozenset({"sandbox.exec"}),
        approval_level="none",
    )

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> str:
        cwd = str(args.get("cwd", "."))
        if shutil.which("git") is None:
            return "[TOOL_ERROR] git not installed"
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "status", "--short", "--branch",
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=10)
        except (OSError, TimeoutError) as e:
            return f"[TOOL_ERROR] git status failed: {e}"
        if proc.returncode != 0:
            return f"[TOOL_ERROR] git returned {proc.returncode}: {stderr_b.decode('utf-8', errors='replace')[:200]}"
        return stdout_b.decode("utf-8", errors="replace") or "(clean)"


def register() -> None:
    """Register the GitStatusTool with the global tool registry."""
    from zero.tools.builtin_tools._helpers import register_tool  # noqa: PLC0415

    register_tool(GitStatusTool())
