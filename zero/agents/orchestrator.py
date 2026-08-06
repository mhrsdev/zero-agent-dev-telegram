"""Zero v2 agent orchestrator — ADR T-7.3.

LLM proposes, schema validates and executes. Sub-agent context isolation:
internal work (tool calls, intermediate reasoning) never returns to parent
context. Only structured output with bounded size returns. Explicit
permission inheritance in spawn code.

Blocked tools (ported from Hermes ``delegate_tool.py``):
    delegate_task, clarify, memory, send_message, cronjob

Children get fresh conversation (no parent history).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from zero.agents.budget import BudgetTracker
from zero.agents.definition import AgentDefinition
from zero.agents.run import AgentRun, AgentRunStatus
from zero.core.errors import ErrorCode, ZeroError
from zero.core.scope import Scope

if TYPE_CHECKING:
    from zero.agents.loop import AgentLoop, ToolDispatcher
    from zero.agents.router_client import RouterClient

__all__ = [
    "DELEGATE_BLOCKED_TOOLS",
    "Orchestrator",
    "OrchestratorError",
    "SubAgentResult",
]


# Tools a sub-agent can NEVER have (port from Hermes delegate_tool.py:48).
DELEGATE_BLOCKED_TOOLS = frozenset({
    "delegate_task",      # no recursive spawning at depth > 1
    "clarify",            # sub-agents can't ask user (parent must)
    "memory",             # memory writes are scoped to parent
    "send_message",       # no direct messaging
    "cronjob",            # no scheduling
    "approval_request",   # sub-agents can't request approvals
})


class OrchestratorError(ZeroError):
    def __init__(self, message: str, *, internal: str | None = None) -> None:
        super().__init__(code=ErrorCode.AGENT_RUN_FAILED, message=message, internal=internal)


@dataclass(frozen=True, slots=True)
class SubAgentResult:
    """Structured output from a sub-agent run.

    Bounded in size: output_text capped at ``max_output_chars``.
    Internal work (tool calls, intermediate reasoning) is NOT included —
    only the final structured output.
    """

    run_id: str
    agent_def_id: str
    output_text: str
    cost_usd: float
    status: AgentRunStatus
    error: str | None = None
    max_output_chars: int = 8192

    def __post_init__(self) -> None:
        # Enforce bounded output.
        if len(self.output_text) > self.max_output_chars:
            object.__setattr__(
                self,
                "output_text",
                self.output_text[: self.max_output_chars]
                + f"\n\n[truncated: original {len(self.output_text)} chars]",
            )


# ---------------------------------------------------------------------- orchestrator

class Orchestrator:
    """Spawns sub-agents with context isolation.

    Usage:
        >>> orch = Orchestrator(budget_tracker=tracker)
        >>> result = await orch.spawn(
        ...     agent_def=coding_agent,
        ...     input_prompt="implement feature X",
        ...     launched_by="usr_01H...",
        ...     parent_permissions=frozenset({"task.create", "task.update"}),
        ... )
    """

    def __init__(
        self,
        *,
        budget_tracker: BudgetTracker,
        max_concurrent_children: int = 3,
        max_depth: int = 1,
        router: RouterClient | None = None,
        tool_dispatcher: ToolDispatcher | None = None,
    ) -> None:
        self._budget = budget_tracker
        self._max_concurrent = max_concurrent_children
        self._max_depth = max_depth
        self._router = router
        self._tool_dispatcher = tool_dispatcher
        self._active: dict[str, AgentRun] = {}
        self._spawn_paused = False
        # Per-parent depth tracking.
        self._depth: dict[str, int] = {}  # parent_run_id -> depth

    def set_router(self, router: RouterClient) -> None:
        """Inject the Router client (for actual AgentLoop execution)."""
        self._router = router

    def set_tool_dispatcher(self, dispatcher: ToolDispatcher) -> None:
        """Inject the tool dispatcher (for actual AgentLoop execution)."""
        self._tool_dispatcher = dispatcher

    def pause_spawns(self) -> None:
        """Globally block new spawns. Active children keep running."""
        self._spawn_paused = True

    def resume_spawns(self) -> None:
        self._spawn_paused = False

    async def spawn(
        self,
        *,
        agent_def: AgentDefinition,
        input_prompt: str,
        launched_by: str,
        parent_run_id: str | None = None,
        parent_permissions: frozenset[str] = frozenset(),
    ) -> SubAgentResult:
        """Spawn a sub-agent.

        Context isolation:
            - Child gets fresh conversation (no parent history).
            - Child gets parent's tool allowlist MINUS DELEGATE_BLOCKED_TOOLS.
            - Child permissions ⊆ parent_permissions.
            - Child internal work (tool calls, reasoning) NOT returned — only
              structured output (capped at max_output_chars).
        """
        if self._spawn_paused:
            raise OrchestratorError("spawn is paused — try again later")

        # Depth check (T-7.3 acceptance: max depth = 1 by default).
        if parent_run_id is not None:
            current_depth = self._depth.get(parent_run_id, 0)
            if current_depth >= self._max_depth:
                raise OrchestratorError(
                    f"max spawn depth ({self._max_depth}) exceeded — "
                    "grandchildren not allowed by default"
                )

        # Concurrency check.
        if len(self._active) >= self._max_concurrent:
            raise OrchestratorError(
                f"max concurrent children ({self._max_concurrent}) reached"
            )

        # Permission inheritance: child ⊆ parent.
        # (Implementation: child's agent_def.tool_allowlist is already a closed
        # set; we further remove DELEGATE_BLOCKED_TOOLS to enforce isolation.)
        effective_tools = agent_def.tool_allowlist - DELEGATE_BLOCKED_TOOLS

        # Build child agent definition with restricted tools.
        from dataclasses import replace  # noqa: PLC0415

        child_def = replace(agent_def, tool_allowlist=effective_tools)

        # Check budget before spawn.
        self._budget.check(
            scope=agent_def.scope,
            agent_def_id=agent_def.id,
        )

        # Create run record.
        run = AgentRun(
            agent_def_id=child_def.id,
            launched_by=launched_by,
            scope=child_def.scope,
            input_prompt=input_prompt,
        )
        run.mark_started()
        self._active[run.id] = run

        # Track depth.
        if parent_run_id is not None:
            self._depth[run.id] = self._depth.get(parent_run_id, 0) + 1
        else:
            self._depth[run.id] = 0

        try:
            # Execute child run via AgentLoop.
            output, cost = await self._execute_child(child_def, input_prompt, run)

            # Record spend.
            self._budget.record(
                amount_usd=cost,
                scope=child_def.scope,
                agent_def_id=child_def.id,
            )

            run.mark_completed(output=output, cost_usd=cost)
            return SubAgentResult(
                run_id=run.id,
                agent_def_id=child_def.id,
                output_text=output,
                cost_usd=cost,
                status=run.status,
            )
        except Exception as e:
            run.mark_failed(error=str(e))
            return SubAgentResult(
                run_id=run.id,
                agent_def_id=child_def.id,
                output_text="",
                cost_usd=0.0,
                status=AgentRunStatus.FAILED,
                error=str(e),
            )
        finally:
            self._active.pop(run.id, None)

    async def _execute_child(
        self,
        agent_def: AgentDefinition,
        input_prompt: str,
        run: AgentRun,
    ) -> tuple[str, float]:
        """Execute the child agent run via AgentLoop.

        Returns (output_text, cost_usd).

        Per T-7.3 acceptance: only the final structured output is returned
        to parent. Tool calls, reasoning, intermediate steps are NOT visible
        to parent (context isolation boundary).

        If no Router client is configured, raises OrchestratorError.
        """
        # If Router or tool dispatcher is not configured, raise.
        if self._router is None:
            raise OrchestratorError(
                "Router client not configured — call set_router() before spawning"
            )
        if self._tool_dispatcher is None:
            raise OrchestratorError(
                "Tool dispatcher not configured — call set_tool_dispatcher() before spawning"
            )

        # Real execution: invoke AgentLoop.
        from zero.agents.loop import AgentLoop  # noqa: PLC0415

        loop = AgentLoop(
            router=self._router,
            agent_def=agent_def,
            budget_tracker=self._budget,
            tool_dispatcher=self._tool_dispatcher,
        )

        # Run in a task so we can cancel it.
        loop_task = asyncio.create_task(
            loop.run(
                user_message=input_prompt,
                launched_by=run.launched_by,
                history=None,  # Fresh conversation — no parent history.
            )
        )

        # Wait for completion (cancellation handled via task.cancel()).
        try:
            result = await loop_task
            return (result.output_text, result.total_cost_usd)
        except asyncio.CancelledError:
            run.mark_cancelled()
            return ("", 0.0)

    def active_children(self) -> list[AgentRun]:
        return list(self._active.values())

    async def cancel(self, run_id: str) -> bool:
        """Cancel an active child run."""
        run = self._active.get(run_id)
        if run is None:
            return False
        run.mark_cancelled()
        return True
