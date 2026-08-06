"""Enterprise builtin tools — legacy module (re-exports from ``builtin_tools`` package).

This file is kept for backward compatibility. All tool implementations
have been moved to the :mod:`zero.tools.builtin_tools` package, where
each tool lives in its own file for easier maintenance and future
development.

New code should import directly from :mod:`zero.tools.builtin_tools`:

    from zero.tools.builtin_tools import (
        ReadFileTool, WriteFileTool, PatchFileTool, ListFilesTool,
        SearchFilesTool, BashExecTool, WebFetchTool, TodoTool, ClarifyTool,
        GitStatusTool, MemorySearchTool,
        DelegateTaskTool, SendMessageTool, ApprovalRequestTool, CronJobTool,
        set_approval_request_deps, set_clarify_callback, set_delegate_orchestrator,
        set_memory_store, set_send_message_callback, set_todo_store,
        submit_clarification,
    )

Old code that imports from ``zero.tools.enterprise_builtin`` will
continue to work because this module re-exports everything.
"""
from __future__ import annotations

# Re-export everything from the new modular package.
from zero.tools.builtin_tools import (
    ApprovalRequestTool,
    BashExecTool,
    ClarifyTool,
    CronJobTool,
    DelegateTaskTool,
    GitStatusTool,
    ListFilesTool,
    MemorySearchTool,
    PatchFileTool,
    ReadFileTool,
    SearchFilesTool,
    SendMessageTool,
    TodoTool,
    WebFetchTool,
    WriteFileTool,
    set_approval_request_deps,
    set_clarify_callback,
    set_delegate_orchestrator,
    set_memory_store,
    set_send_message_callback,
    set_todo_store,
    submit_clarification,
)

__all__ = [
    # Tool classes
    "ApprovalRequestTool",
    "BashExecTool",
    "ClarifyTool",
    "CronJobTool",
    "DelegateTaskTool",
    "GitStatusTool",
    "ListFilesTool",
    "MemorySearchTool",
    "PatchFileTool",
    "ReadFileTool",
    "SearchFilesTool",
    "SendMessageTool",
    "TodoTool",
    "WebFetchTool",
    "WriteFileTool",
    # Injection functions
    "set_approval_request_deps",
    "set_clarify_callback",
    "set_delegate_orchestrator",
    "set_memory_store",
    "set_send_message_callback",
    "set_todo_store",
    "submit_clarification",
]
