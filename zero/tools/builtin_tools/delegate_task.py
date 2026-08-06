"""DelegateTaskTool — delegate a sub-task to a sub-agent with context isolation.

Per ADR T-7.3:
    - Child gets fresh conversation (no parent history).
    - Child's tool allowlist is the parent's MINUS blocked tools.
    - Child permissions ⊆ parent permissions.
    - Only the final structured output is returned to parent.
    - Internal work (tool calls, reasoning) is NOT returned.

Blocked tools (sub-agents can NEVER have these):
    delegate_task, clarify, memory, send_message, cronjob, approval_request

The orchestrator is injected via :func:`set_delegate_orchestrator`
(called by :class:`zero.agents.runner.ZeroAgentRunner.setup()`).
"""
from __future__ import annotations

from typing import Any

from zero.tools.base import Tool, ToolContext, ToolSpec

__all__ = [
    "DelegateTaskTool",
    "DELEGATE_SCHEMA",
    "set_delegate_orchestrator",
    "register",
]


DELEGATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task": {"type": "string", "description": "The task to delegate to a sub-agent"},
        "agent_type": {
            "type": "string",
            "enum": ["coding", "testing", "documentation", "security", "release", "triage"],
            "description": "Type of agent to spawn (default: coding)",
        },
        "max_turns": {"type": "integer", "default": 20},
    },
    "required": ["task"],
}

# Global orchestrator (injected by runner).
_delegate_orchestrator: Any = None


def set_delegate_orchestrator(orch: Any) -> None:
    """Inject the orchestrator (called by ZeroAgentRunner.setup)."""
    global _delegate_orchestrator
    _delegate_orchestrator = orch


class DelegateTaskTool(Tool):
    """Delegate a sub-task to a sub-agent with context isolation."""

    spec = ToolSpec(
        name="delegate_task",
        description="Delegate a sub-task to a sub-agent. Returns the sub-agent's final output.",
        parameters_schema=DELEGATE_SCHEMA,
        required_permissions=frozenset({"agent.spawn"}),
        approval_level="none",
    )

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> str:
        if _delegate_orchestrator is None:
            return "[TOOL_ERROR] orchestrator not configured — cannot delegate"
        task = str(args["task"])
        agent_type_str = str(args.get("agent_type", "coding"))
        max_turns = int(args.get("max_turns", 20))

        from zero.agents.definition import AgentDefinition, AgentType, AGENT_TYPE_TO_EFFORT_TIER  # noqa: PLC0415

        try:
            agent_type = AgentType(agent_type_str)
        except ValueError:
            return f"[TOOL_ERROR] unknown agent_type {agent_type_str!r}"

        # Build a child agent definition with the same scope as the caller.
        # The orchestrator will further restrict the tool allowlist.
        child_def = AgentDefinition(
            name=f"delegate-{agent_type.value}-{ctx.tool_call_id[:8]}",
            agent_type=agent_type,
            scope=ctx.scope,
            system_prompt=(
                f"You are a {agent_type.value} sub-agent. Complete the task, then stop. "
                f"Do not ask for clarification — make a reasonable assumption and proceed. "
                f"Your output will be returned to the parent agent as-is."
            ),
            effort_tier=AGENT_TYPE_TO_EFFORT_TIER[agent_type],
            tool_allowlist=frozenset({
                "read_file", "write_file", "list_files", "search_files",
                "bash_exec", "web_fetch", "todo", "git_status", "memory_search",
            }),
            max_turns=max_turns,
            budget_usd=2.0,
        )

        try:
            result = await _delegate_orchestrator.spawn(
                agent_def=child_def,
                input_prompt=task,
                launched_by=ctx.actor_id,
            )
        except Exception as e:
            return f"[TOOL_ERROR] delegate failed: {e}"

        if result.error:
            return f"[DELEGATE_ERROR] {result.error}"
        return result.output_text or "(sub-agent returned no output)"


def register() -> None:
    """Register the DelegateTaskTool with the global tool registry."""
    from zero.tools.builtin_tools._helpers import register_tool  # noqa: PLC0415

    register_tool(DelegateTaskTool())
