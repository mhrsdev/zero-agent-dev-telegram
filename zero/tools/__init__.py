"""Zero v2 tools package — ported & adapted from Hermes Agent.

Deferred tool loading, registry with self-registration, progressive tool
disclosure (tool_search pattern), approval workflow for dangerous tools.

All builtin tool implementations live in the :mod:`zero.tools.builtin_tools`
package, where each tool is in its own file for easy maintenance and
future development.
"""
from __future__ import annotations

from zero.tools.base import Tool, ToolContext, ToolError, ToolSpec
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
from zero.tools.registry import (
    ToolEntry,
    ToolRegistry,
    ToolResult,
    dispatch,
    register,
    registry,
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
    # Base classes
    "Tool",
    "ToolContext",
    "ToolEntry",
    "ToolError",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    # Registry functions
    "dispatch",
    "register",
    "registry",
    # Injection functions (called by ZeroAgentRunner.setup)
    "set_approval_request_deps",
    "set_clarify_callback",
    "set_delegate_orchestrator",
    "set_memory_store",
    "set_send_message_callback",
    "set_todo_store",
    "submit_clarification",
]
