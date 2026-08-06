"""SearchFilesTool — regex search across file contents.

Walks a directory recursively, reads each file, and searches each line
for a regex pattern. Returns matching lines with file:line: prefix.

Useful for code exploration (find all callers of a function, find all
TODO comments, find all uses of a deprecated API, etc.).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from zero.tools.base import Tool, ToolContext, ToolSpec

__all__ = ["SearchFilesTool", "SEARCH_FILES_SCHEMA", "register"]


SEARCH_FILES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "Directory to search in"},
        "pattern": {"type": "string", "description": "Regex pattern to search for"},
        "file_glob": {"type": "string", "default": "*", "description": "File name glob"},
        "max_results": {"type": "integer", "default": 50},
    },
    "required": ["path", "pattern"],
}


class SearchFilesTool(Tool):
    """Search file contents with regex."""

    spec = ToolSpec(
        name="search_files",
        description="Search file contents using a regex pattern",
        parameters_schema=SEARCH_FILES_SCHEMA,
        required_permissions=frozenset({"sandbox.exec"}),
        approval_level="none",
    )

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> str:
        path_str = str(args["path"])
        pattern = str(args["pattern"])
        file_glob = str(args.get("file_glob", "*"))
        max_results = int(args.get("max_results", 50))

        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return f"[TOOL_ERROR] invalid regex: {e}"

        p = Path(path_str).expanduser()
        if not p.exists():
            return f"[TOOL_ERROR] directory not found: {path_str}"

        results: list[str] = []
        for filepath in p.rglob(file_glob):
            if not filepath.is_file():
                continue
            try:
                text = filepath.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    results.append(f"{filepath}:{i}: {line.strip()[:200]}")
                    if len(results) >= max_results:
                        results.append(f"[truncated at {max_results} results]")
                        return "\n".join(results)
        return "\n".join(results) if results else "(no matches)"


def register() -> None:
    """Register the SearchFilesTool with the global tool registry."""
    from zero.tools.builtin_tools._helpers import register_tool  # noqa: PLC0415

    register_tool(SearchFilesTool())
