"""Tests for the AgentLoop context compression integration.

Verifies that the AgentLoop compresses long conversation histories before
calling the Router, preventing token budget overflow.
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from zero.agents.budget import BudgetTracker
from zero.agents.definition import AgentDefinition, AgentType
from zero.agents.loop import AgentLoop, AgentLoopResult
from zero.agents.router_client import RouterMessage, RouterResponse
from zero.core.scope import Scope


@pytest.fixture
def personal_scope() -> Scope:
    return Scope.personal(user_id="usr_test").with_default_memory_scope()


@pytest.fixture
def agent_def(personal_scope: Scope) -> AgentDefinition:
    return AgentDefinition(
        name="test-agent",
        agent_type=AgentType.TRIAGE,
        scope=personal_scope,
        system_prompt="You are a test agent.",
        effort_tier="zero/cheap",
        tool_allowlist=frozenset(),
        max_turns=10,
        budget_usd=1.0,
    )


@pytest.fixture
def budget_tracker() -> BudgetTracker:
    return BudgetTracker()


@pytest.fixture
def mock_router() -> MagicMock:
    """Build a mock router that captures a COPY of the messages list at call time.

    This is necessary because Python lists are mutable — without copying,
    the mock would store a reference to the same list object that the loop
    continues to mutate (appending assistant messages, etc.).
    """
    router = MagicMock()
    captured_messages: list[list] = []

    async def capture_complete(**kwargs: Any) -> RouterResponse:
        # Capture a copy of the messages list at call time.
        captured_messages.append(list(kwargs.get("messages", [])))
        return RouterResponse(content="ok", finish_reason="stop")

    router.complete = capture_complete
    router.captured_messages = captured_messages  # type: ignore[attr-defined]
    return router


@pytest.fixture
def mock_dispatcher() -> AsyncMock:
    return AsyncMock()


class TestAgentLoopCompression:
    """Verify the AgentLoop compresses long histories."""

    @pytest.mark.asyncio
    async def test_short_history_not_compressed(
        self,
        agent_def: AgentDefinition,
        budget_tracker: BudgetTracker,
        mock_router: MagicMock,
        mock_dispatcher: AsyncMock,
    ) -> None:
        """Short histories are NOT compressed (no need)."""
        loop = AgentLoop(
            router=mock_router,  # type: ignore[arg-type]
            agent_def=agent_def,
            budget_tracker=budget_tracker,
            tool_dispatcher=mock_dispatcher,
            max_context_tokens=10_000,  # high limit
        )
        result = await loop.run(user_message="hi", launched_by="usr_test")

        # Router was called once with the full (uncompressed) message list.
        assert len(mock_router.captured_messages) == 1  # type: ignore[attr-defined]
        messages = mock_router.captured_messages[0]  # type: ignore[attr-defined]
        # system + user = 2 messages at call time.
        assert len(messages) == 2

    @pytest.mark.asyncio
    async def test_long_history_is_compressed(
        self,
        agent_def: AgentDefinition,
        budget_tracker: BudgetTracker,
        mock_router: MagicMock,
        mock_dispatcher: AsyncMock,
    ) -> None:
        """Long histories ARE compressed before the Router call."""
        # Build a very long history that exceeds the token budget.
        long_history = [
            RouterMessage(role="user", content="x" * 1000),
            RouterMessage(role="assistant", content="y" * 1000),
        ] * 20  # 40 messages, ~20K tokens

        loop = AgentLoop(
            router=mock_router,  # type: ignore[arg-type]
            agent_def=agent_def,
            budget_tracker=budget_tracker,
            tool_dispatcher=mock_dispatcher,
            max_context_tokens=2_000,  # low limit → will compress
            keep_last_exchanges=2,  # keep last 2 exchanges
        )
        result = await loop.run(
            user_message="final question",
            launched_by="usr_test",
            history=long_history,
        )

        # Router was called with compressed messages.
        assert len(mock_router.captured_messages) == 1  # type: ignore[attr-defined]
        messages = mock_router.captured_messages[0]  # type: ignore[attr-defined]
        # Should be much smaller than 42 (system + 40 history + 1 user).
        assert len(messages) < 20
        # First message should be the compression summary.
        assert "[CONTEXT COMPACTION]" in (messages[0].content or "")

    @pytest.mark.asyncio
    async def test_compression_preserves_recent_messages(
        self,
        agent_def: AgentDefinition,
        budget_tracker: BudgetTracker,
        mock_router: MagicMock,
        mock_dispatcher: AsyncMock,
    ) -> None:
        """Compression preserves the most recent N exchanges verbatim."""
        # Build history with distinct recent messages.
        history = []
        for i in range(20):
            history.append(RouterMessage(role="user", content=f"old-question-{i} " + "x" * 200))
            history.append(RouterMessage(role="assistant", content=f"old-answer-{i} " + "y" * 200))

        # Add recent messages that should be preserved.
        history.append(RouterMessage(role="user", content="RECENT_Q_1"))
        history.append(RouterMessage(role="assistant", content="RECENT_A_1"))

        loop = AgentLoop(
            router=mock_router,  # type: ignore[arg-type]
            agent_def=agent_def,
            budget_tracker=budget_tracker,
            tool_dispatcher=mock_dispatcher,
            max_context_tokens=1_000,  # very low → will compress aggressively
            keep_last_exchanges=2,  # keep last 2 exchanges
        )
        await loop.run(
            user_message="FINAL_QUESTION",
            launched_by="usr_test",
            history=history,
        )

        messages = mock_router.captured_messages[0]  # type: ignore[attr-defined]
        # The recent messages + the final question should be in the compressed list.
        all_content = " ".join(m.content or "" for m in messages)
        assert "RECENT_Q_1" in all_content
        assert "RECENT_A_1" in all_content
        assert "FINAL_QUESTION" in all_content

    @pytest.mark.asyncio
    async def test_compression_disabled_with_high_limit(
        self,
        agent_def: AgentDefinition,
        budget_tracker: BudgetTracker,
        mock_router: MagicMock,
        mock_dispatcher: AsyncMock,
    ) -> None:
        """With a very high token limit, compression never triggers."""
        history = [
            RouterMessage(role="user", content="short"),
            RouterMessage(role="assistant", content="reply"),
        ]

        loop = AgentLoop(
            router=mock_router,  # type: ignore[arg-type]
            agent_def=agent_def,
            budget_tracker=budget_tracker,
            tool_dispatcher=mock_dispatcher,
            max_context_tokens=1_000_000,  # 1M tokens — never compresses
        )
        await loop.run(
            user_message="hi",
            launched_by="usr_test",
            history=history,
        )

        messages = mock_router.captured_messages[0]  # type: ignore[attr-defined]
        # system + 2 history + 1 user = 4 messages at call time.
        assert len(messages) == 4
