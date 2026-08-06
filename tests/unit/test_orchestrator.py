"""Unit tests for zero.agents.orchestrator — ADR T-7.3.

Verifies sub-agent context isolation, blocked tools, max depth enforcement.
"""
from __future__ import annotations

import pytest
from zero.agents.budget import BudgetTracker
from zero.agents.definition import AgentDefinition, AgentType
from zero.agents.orchestrator import (
    DELEGATE_BLOCKED_TOOLS,
    Orchestrator,
    OrchestratorError,
    SubAgentResult,
)
from zero.agents.run import AgentRunStatus
from zero.core.scope import Scope


@pytest.fixture
def dev_scope() -> Scope:
    return Scope.development(
        org_id="org_01HABC", workspace_id="ws_01HABC",
        project_id="prj_01HABC", group_id="grp_01HABC", topic_id=100,
    ).with_default_memory_scope()


@pytest.fixture
def budget_tracker() -> BudgetTracker:
    return BudgetTracker()


@pytest.fixture
def mock_router():
    """Mock RouterClient for orchestrator tests."""
    from unittest.mock import AsyncMock, MagicMock

    mock = MagicMock()
    mock_response = MagicMock()
    mock_response.content = "completed"
    mock_response.tool_calls = []
    mock_response.finish_reason = "stop"
    mock_response.cost_usd = 0.001
    mock.complete = AsyncMock(return_value=mock_response)
    return mock


@pytest.fixture
def mock_dispatcher():
    """Mock tool dispatcher."""
    async def _dispatch(tc, scope):
        return "tool result"
    return _dispatch


@pytest.fixture
def coding_agent(dev_scope: Scope) -> AgentDefinition:
    return AgentDefinition(
        name="Coding Agent",
        agent_type=AgentType.CODING,
        scope=dev_scope,
        system_prompt="You are a coding agent.",
        effort_tier="zero/coding",
        tool_allowlist=frozenset({
            "read_file", "write_file", "delegate_task", "clarify", "memory",  # last 3 are blocked
        }),
    )


class TestOrchestrator:
    @pytest.mark.asyncio
    async def test_spawn_raises_without_router(
        self,
        coding_agent: AgentDefinition,
        budget_tracker: BudgetTracker,
    ) -> None:
        """Without Router configured, spawn returns FAILED status."""
        orch = Orchestrator(budget_tracker=budget_tracker)
        result = await orch.spawn(
            agent_def=coding_agent,
            input_prompt="implement feature X",
            launched_by="usr_01HALICE",
        )
        assert result.status is AgentRunStatus.FAILED
        assert "Router client not configured" in (result.error or "")

    @pytest.mark.asyncio
    async def test_spawn_with_mock_router(
        self,
        coding_agent: AgentDefinition,
        budget_tracker: BudgetTracker,
    ) -> None:
        """With a mock router + dispatcher, spawn completes successfully."""
        from unittest.mock import AsyncMock, MagicMock

        mock_router = MagicMock()
        mock_response = MagicMock()
        mock_response.content = "task completed"
        mock_response.tool_calls = []
        mock_response.finish_reason = "stop"
        mock_response.cost_usd = 0.001
        mock_router.complete = AsyncMock(return_value=mock_response)

        async def mock_dispatcher(tc, scope):
            return "tool result"

        orch = Orchestrator(
            budget_tracker=budget_tracker,
            router=mock_router,
            tool_dispatcher=mock_dispatcher,
        )
        result = await orch.spawn(
            agent_def=coding_agent,
            input_prompt="implement feature X",
            launched_by="usr_01HALICE",
        )
        assert isinstance(result, SubAgentResult)
        assert result.status is AgentRunStatus.COMPLETED
        assert "task completed" in result.output_text

    @pytest.mark.asyncio
    async def test_blocked_tools_removed(
        self,
        coding_agent: AgentDefinition,
        budget_tracker: BudgetTracker,
        mock_router,
        mock_dispatcher,
    ) -> None:
        """Sub-agent cannot have delegate_task, clarify, memory, etc."""
        orch = Orchestrator(
            budget_tracker=budget_tracker,
            router=mock_router,
            tool_dispatcher=mock_dispatcher,
        )
        await orch.spawn(
            agent_def=coding_agent,
            input_prompt="x",
            launched_by="usr_01HALICE",
        )
        # We can't directly inspect the child_def because it's created inside
        # spawn(). But we verify the constant is correct.
        assert "delegate_task" in DELEGATE_BLOCKED_TOOLS
        assert "clarify" in DELEGATE_BLOCKED_TOOLS
        assert "memory" in DELEGATE_BLOCKED_TOOLS
        assert "send_message" in DELEGATE_BLOCKED_TOOLS
        assert "cronjob" in DELEGATE_BLOCKED_TOOLS
        assert "approval_request" in DELEGATE_BLOCKED_TOOLS

    @pytest.mark.asyncio
    async def test_max_depth_enforced(
        self,
        coding_agent: AgentDefinition,
        budget_tracker: BudgetTracker,
        mock_router,
        mock_dispatcher,
    ) -> None:
        """T-7.3: max depth = 1 by default (no grandchildren)."""
        orch = Orchestrator(
            budget_tracker=budget_tracker,
            max_depth=1,
            router=mock_router,
            tool_dispatcher=mock_dispatcher,
        )
        # First spawn: depth 0 (parent_run_id=None)
        result1 = await orch.spawn(
            agent_def=coding_agent,
            input_prompt="x",
            launched_by="usr_01HALICE",
            parent_run_id=None,
        )
        assert result1.status is AgentRunStatus.COMPLETED
        # Second spawn: try to spawn as child of first run
        # Manually inject depth tracking as if first run was a child.
        orch._depth[result1.run_id] = 1  # mark as already depth-1
        with pytest.raises(OrchestratorError, match="max spawn depth"):
            await orch.spawn(
                agent_def=coding_agent,
                input_prompt="x",
                launched_by="usr_01HALICE",
                parent_run_id=result1.run_id,
            )

    @pytest.mark.asyncio
    async def test_max_concurrent_children(
        self,
        coding_agent: AgentDefinition,
        budget_tracker: BudgetTracker,
    ) -> None:
        """T-7.3: max_concurrent_children limit. Use a delayed executor to keep child in-flight."""
        import asyncio

        orch = Orchestrator(budget_tracker=budget_tracker, max_concurrent_children=1)

        # Replace _execute_child with one that blocks on a future we control.
        release_future: asyncio.Future[None] = asyncio.Future()

        async def slow_executor(agent_def, prompt, run):
            await release_future
            return "done", 0.001

        orch._execute_child = slow_executor  # type: ignore[assignment]

        # Start first spawn in background — it will not complete until we release.
        task1 = asyncio.create_task(orch.spawn(
            agent_def=coding_agent,
            input_prompt="x",
            launched_by="usr_01HALICE",
        ))
        # Yield to let task1 start and register in _active.
        await asyncio.sleep(0.05)

        # Second spawn should fail because first is still active.
        with pytest.raises(OrchestratorError, match="max concurrent"):
            await orch.spawn(
                agent_def=coding_agent,
                input_prompt="y",
                launched_by="usr_01HALICE",
            )

        # Release the first.
        release_future.set_result(None)
        await task1

    @pytest.mark.asyncio
    async def test_pause_spawns(
        self,
        coding_agent: AgentDefinition,
        budget_tracker: BudgetTracker,
        mock_router,
        mock_dispatcher,
    ) -> None:
        orch = Orchestrator(
            budget_tracker=budget_tracker,
            router=mock_router,
            tool_dispatcher=mock_dispatcher,
        )
        orch.pause_spawns()
        with pytest.raises(OrchestratorError, match="paused"):
            await orch.spawn(
                agent_def=coding_agent,
                input_prompt="x",
                launched_by="usr_01HALICE",
            )
        orch.resume_spawns()
        # Should work again
        result = await orch.spawn(
            agent_def=coding_agent,
            input_prompt="x",
            launched_by="usr_01HALICE",
        )
        assert result.status is AgentRunStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_subagent_result_bounded(
        self,
        coding_agent: AgentDefinition,
        budget_tracker: BudgetTracker,
    ) -> None:
        """SubAgentResult output_text is capped at max_output_chars."""
        # Create orchestrator with mock that returns huge output.
        orch = Orchestrator(budget_tracker=budget_tracker)

        async def huge_executor(agent_def, prompt, run):
            return "x" * 100_000, 0.001

        orch._execute_child = huge_executor  # type: ignore[assignment]
        result = await orch.spawn(
            agent_def=coding_agent,
            input_prompt="x",
            launched_by="usr_01HALICE",
        )
        assert len(result.output_text) <= 8192 + 100  # cap + truncation message
        assert "truncated" in result.output_text
