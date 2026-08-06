"""BashExecTool — execute a shell command with timeout and output cap.

Runs the command via ``sh -c`` so shell features (pipes, redirection,
environment variables) work. Output is capped at 50KB (head + tail) to
prevent context overflow from verbose commands.

Requires standard approval because shell execution is high-risk
(arbitrary code can run).
"""
from __future__ import annotations

import asyncio
from typing import Any

from zero.tools.base import Tool, ToolContext, ToolSpec

__all__ = ["BashExecTool", "BASH_EXEC_SCHEMA", "register"]


BASH_EXEC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "description": "Shell command to execute"},
        "cwd": {"type": "string", "description": "Working directory (default: sandbox root)"},
        "timeout_seconds": {"type": "integer", "default": 30},
    },
    "required": ["command"],
}


class BashExecTool(Tool):
    """Execute a shell command with timeout and output cap.

    Output is capped at 50KB (head + tail). Returns exit code + output.
    """

    spec = ToolSpec(
        name="bash_exec",
        description="Execute a shell command with timeout (output capped at 50KB)",
        parameters_schema=BASH_EXEC_SCHEMA,
        required_permissions=frozenset({"sandbox.exec"}),
        approval_level="standard",
    )

    MAX_OUTPUT_CHARS = 50_000

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> str:
        command = str(args["command"])
        cwd = str(args.get("cwd", "."))
        timeout = int(args.get("timeout_seconds", 30))

        try:
            proc = await asyncio.create_subprocess_exec(
                "sh", "-c", command,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            return f"[TOOL_ERROR] cannot start process: {e}"

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return f"[TOOL_ERROR] command timed out after {timeout}s"

        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        exit_code = proc.returncode or 0

        # Truncate large output (head + tail).
        if len(stdout) > self.MAX_OUTPUT_CHARS:
            head = stdout[: self.MAX_OUTPUT_CHARS // 2]
            tail = stdout[-self.MAX_OUTPUT_CHARS // 2 :]
            stdout = f"{head}\n...[truncated {len(stdout) - self.MAX_OUTPUT_CHARS} chars]...\n{tail}"

        output = f"exit_code={exit_code}\n--- stdout ---\n{stdout}"
        if stderr:
            stderr_truncated = stderr[:5000]
            output += f"\n--- stderr ---\n{stderr_truncated}"
        return output


def register() -> None:
    """Register the BashExecTool with the global tool registry."""
    from zero.tools.builtin_tools._helpers import register_tool  # noqa: PLC0415

    register_tool(BashExecTool())
