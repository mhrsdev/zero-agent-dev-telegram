"""Zero v2 builtin tools — modular package.

Each tool lives in its own file for easy maintenance and future
development. To add a new tool:

    1. Create a new file ``my_tool.py`` in this directory.
    2. Define a ``Tool`` subclass and a ``register()`` function.
    3. Add ``from . import my_tool`` to the import block below.
    4. Add ``my_tool.register()`` to the ``_register_all()`` call list.

Importing this package (``import zero.tools.builtin_tools``) registers
all tools with the global :data:`zero.tools.registry.registry`.

Re-exports the tool classes and the ``set_*()`` injection functions for
backward compatibility with code that imports from
``zero.tools.enterprise_builtin``.
"""
from __future__ import annotations

# Import each tool module. This makes the tool classes available and
# ensures the ``register()`` function is callable.
from zero.tools.builtin_tools import (
    approval_request,
    bash_exec,
    clarify,
    cronjob,
    delegate_task,
    git_status,
    list_files,
    memory_search,
    patch_file,
    read_file,
    search_files,
    send_message,
    todo,
    web_fetch,
    write_file,
)

# Re-export tool classes for backward compatibility.
from zero.tools.builtin_tools.approval_request import (
    ApprovalRequestTool,
    set_approval_request_deps,
)
from zero.tools.builtin_tools.bash_exec import BashExecTool
from zero.tools.builtin_tools.clarify import (
    ClarifyTool,
    set_clarify_callback,
    submit_clarification,
)
from zero.tools.builtin_tools.cronjob import CronJobTool
from zero.tools.builtin_tools.delegate_task import (
    DelegateTaskTool,
    set_delegate_orchestrator,
)
from zero.tools.builtin_tools.git_status import GitStatusTool
from zero.tools.builtin_tools.list_files import ListFilesTool
from zero.tools.builtin_tools.memory_search import (
    MemorySearchTool,
    set_memory_store,
)
from zero.tools.builtin_tools.patch_file import PatchFileTool
from zero.tools.builtin_tools.read_file import ReadFileTool
from zero.tools.builtin_tools.search_files import SearchFilesTool
from zero.tools.builtin_tools.send_message import (
    SendMessageTool,
    set_send_message_callback,
)
from zero.tools.builtin_tools.todo import TodoTool, set_todo_store
from zero.tools.builtin_tools.web_fetch import WebFetchTool
from zero.tools.builtin_tools.write_file import WriteFileTool

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
    # Injection functions (called by ZeroAgentRunner.setup)
    "set_approval_request_deps",
    "set_clarify_callback",
    "set_delegate_orchestrator",
    "set_memory_store",
    "set_send_message_callback",
    "set_todo_store",
    "submit_clarification",
]


def _register_all() -> None:
    """Register all builtin tools with the global registry.

    Called automatically when this package is imported. Each tool module
    provides a ``register()`` function that handles its own registration.
    """
    read_file.register()
    write_file.register()
    patch_file.register()
    list_files.register()
    search_files.register()
    bash_exec.register()
    web_fetch.register()
    todo.register()
    clarify.register()
    git_status.register()
    memory_search.register()
    delegate_task.register()
    send_message.register()
    approval_request.register()
    cronjob.register()


# Register all tools on import.
_register_all()
