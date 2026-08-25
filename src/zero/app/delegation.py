"""Subagent delegation primitives (GAP 8).

Per ``docs/gap-designs/GAP-08-subagents.md``: a running agent may
delegate a bounded subtask to an isolated child context. Children keep
a fresh conversation, a narrowed tool set (intersection-only), their
own provider requests tagged ``sub_agent_type`` so whole-tree aggregation
stays correct, and a hard nesting depth cap (Claude Code parity: 3).
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager

from zero.domain.providers import ToolDeclaration

DELEGATE_TOOL_NAME = "delegate"
MAX_DELEGATION_DEPTH = 3

#: Workspace-mutating tools are excluded from delegated children unless
#: explicitly listed AND permitted by the parent's policy.
_WORKSPACE_TOOLS = frozenset({"read_file", "write_file", "run_command", "capture_diff"})

_delegate_depth: contextvars.ContextVar[int] = contextvars.ContextVar(
    "zero_delegate_depth", default=0
)


def current_delegation_depth() -> int:
    return _delegate_depth.get()


@contextmanager
def delegation_depth_increased():
    """Run a block at depth+1 (child context)."""
    token = _delegate_depth.set(_delegate_depth.get() + 1)
    try:
        yield
    finally:
        _delegate_depth.reset(token)


DELEGATE_TOOL_DESCRIPTION = (
    "Delegate a bounded subtask to an isolated sub-agent with its own "
    "conversation. The sub-agent returns its final answer inline."
)

DELEGATE_INPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "objective": {
            "type": "string",
            "description": "Concrete outcome the sub-agent must produce.",
        },
        "tools": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional narrower tool set (never wider than yours).",
        },
        "model": {
            "type": "string",
            "description": "Optional model override for this subtask.",
        },
    },
    "required": ["objective"],
    "additionalProperties": False,
}


def delegate_declaration() -> ToolDeclaration:
    return ToolDeclaration(
        name=DELEGATE_TOOL_NAME,
        description=DELEGATE_TOOL_DESCRIPTION,
        parameters=dict(DELEGATE_INPUT_SCHEMA),
    )


__all__ = [
    "DELEGATE_INPUT_SCHEMA",
    "DELEGATE_TOOL_DESCRIPTION",
    "DELEGATE_TOOL_NAME",
    "MAX_DELEGATION_DEPTH",
    "current_delegation_depth",
    "delegate_declaration",
    "delegation_depth_increased",
]
