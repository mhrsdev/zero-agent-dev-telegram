"""Zero v2 builtin tools — legacy module (re-exports from ``builtin_tools`` package).

This file is kept for backward compatibility. All tool implementations
have been moved to the :mod:`zero.tools.builtin_tools` package, where
each tool lives in its own file for easier maintenance and future
development.

New code should import directly from :mod:`zero.tools.builtin_tools`:

    from zero.tools.builtin_tools import ReadFileTool, WriteFileTool

Old code that imports from ``zero.tools.builtin`` will continue to work
because this module re-exports everything.
"""
from __future__ import annotations

# Re-export everything from the new modular package.
from zero.tools.builtin_tools import (
    ClarifyTool,
    ListFilesTool,
    ReadFileTool,
    TodoTool,
    WebFetchTool,
    WriteFileTool,
)

__all__ = [
    "ClarifyTool",
    "ListFilesTool",
    "ReadFileTool",
    "TodoTool",
    "WebFetchTool",
    "WriteFileTool",
]
