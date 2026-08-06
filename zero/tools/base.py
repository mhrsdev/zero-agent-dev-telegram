"""Zero v2 tool base classes.

A Tool is an async callable with:
    - Name (stable identifier)
    - JSON Schema for parameters
    - Short description (for tool list)
    - Required permissions (checked before dispatch)
    - Required approval level (none / standard / elevated)
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Literal

from zero.core.scope import Scope

__all__ = [
    "ApprovalLevel",
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolSpec",
]


ApprovalLevel = Literal["none", "standard", "elevated"]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Static specification of a tool."""

    name: str
    description: str  # short, one-line — used in tool list
    parameters_schema: dict[str, Any]  # JSON Schema
    required_permissions: frozenset[str] = field(default_factory=frozenset)
    approval_level: ApprovalLevel = "none"
    is_async: bool = True
    # If True, output is treated as untrusted data (always quoted as data,
    # never as instructions to the model).
    untrusted_output: bool = True


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Runtime context passed to every tool invocation."""

    scope: Scope
    actor_id: str
    tool_call_id: str
    # Carried for approval workflow.
    requested_by: str = ""


class ToolError(Exception):
    """Raised by a tool to signal a recoverable error."""


class Tool(abc.ABC):
    """Abstract base class for tools.

    Subclasses implement :meth:`execute` and provide :attr:`spec`.
    """

    spec: ToolSpec

    @abc.abstractmethod
    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> str:
        """Run the tool. Returns output string (treated as untrusted data)."""
        ...

    @property
    def name(self) -> str:
        return self.spec.name
