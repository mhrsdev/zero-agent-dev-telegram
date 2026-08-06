"""Tests for the new enterprise tools (delegate_task, send_message, approval_request, cronjob).

Also tests the ClarifyTool's callback mechanism.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from zero.core.scope import Scope
from zero.tools.base import ToolContext
from zero.tools.builtin_tools import (
    ApprovalRequestTool,
    ClarifyTool,
    CronJobTool,
    DelegateTaskTool,
    SendMessageTool,
    set_approval_request_deps,
    set_clarify_callback,
    set_delegate_orchestrator,
    set_send_message_callback,
    submit_clarification,
)
from zero.tools.registry import registry as tool_registry


# ---------------------------------------------------------------------- fixtures


@pytest.fixture
def personal_scope() -> Scope:
    return Scope.personal(user_id="usr_test").with_default_memory_scope()


@pytest.fixture
def ctx(personal_scope: Scope) -> ToolContext:
    return ToolContext(
        scope=personal_scope,
        actor_id="usr_test",
        tool_call_id="tc_test_001",
    )


@pytest.fixture(autouse=True)
def cleanup_callbacks():
    """Reset all global callbacks + cron job registry after each test."""
    # Import here to avoid circular imports.
    from zero.tools.builtin_tools.cronjob import _cron_jobs
    # Clear cron jobs before each test.
    _cron_jobs.clear()
    yield
    # Clean up after each test too.
    _cron_jobs.clear()
    set_clarify_callback(None)
    set_send_message_callback(None)
    set_delegate_orchestrator(None)
    set_approval_request_deps(None, None)


# ---------------------------------------------------------------------- ClarifyTool


class TestClarifyTool:
    """Verify the ClarifyTool callback mechanism works."""

    @pytest.mark.asyncio
    async def test_clarify_without_callback_times_out(self, ctx: ToolContext) -> None:
        """Without a callback, clarify times out (no UI to display)."""
        tool = ClarifyTool()
        # Very short timeout for the test.
        result = await tool.execute(
            {"question": "Pick one", "choices": ["A", "B"], "timeout_seconds": 1},
            ctx,
        )
        assert "TIMEOUT" in result

    @pytest.mark.asyncio
    async def test_clarify_with_callback_invokes_it(self, ctx: ToolContext) -> None:
        """The installed callback is invoked with the clarify_id + question."""
        captured: dict[str, Any] = {}

        async def callback(clarify_id, question, choices, multi, ctx):  # type: ignore[no-untyped-def]
            captured["clarify_id"] = clarify_id
            captured["question"] = question
            captured["choices"] = choices
            captured["multi"] = multi
            # Simulate user responding immediately.
            asyncio.create_task(_delayed_submit(clarify_id, "B"))

        async def _delayed_submit(clarify_id: str, response: str) -> None:
            await asyncio.sleep(0.05)
            submit_clarification(clarify_id, response)

        set_clarify_callback(callback)

        tool = ClarifyTool()
        result = await tool.execute(
            {"question": "Pick one", "choices": ["A", "B"], "timeout_seconds": 5},
            ctx,
        )
        assert result == "B"
        assert captured["question"] == "Pick one"
        assert captured["choices"] == ["A", "B"]
        assert captured["multi"] is False

    @pytest.mark.asyncio
    async def test_clarify_validates_choices(self, ctx: ToolContext) -> None:
        """Clarify rejects empty choices and >4 choices."""
        tool = ClarifyTool()
        result = await tool.execute(
            {"question": "q", "choices": [], "timeout_seconds": 1},
            ctx,
        )
        assert "at least one choice" in result

        result = await tool.execute(
            {"question": "q", "choices": ["A", "B", "C", "D", "E"], "timeout_seconds": 1},
            ctx,
        )
        assert "max 4 choices" in result

    @pytest.mark.asyncio
    async def test_clarify_callback_error_returns_tool_error(self, ctx: ToolContext) -> None:
        """If the callback raises, clarify returns a TOOL_ERROR."""
        async def bad_callback(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("UI exploded")

        set_clarify_callback(bad_callback)

        tool = ClarifyTool()
        result = await tool.execute(
            {"question": "q", "choices": ["A"], "timeout_seconds": 1},
            ctx,
        )
        assert "TOOL_ERROR" in result
        assert "UI exploded" in result


# ---------------------------------------------------------------------- DelegateTaskTool


class TestDelegateTaskTool:
    """Verify the DelegateTaskTool works with a mocked orchestrator."""

    @pytest.mark.asyncio
    async def test_delegate_without_orchestrator_returns_error(
        self, ctx: ToolContext
    ) -> None:
        """Without an orchestrator, delegate returns TOOL_ERROR."""
        tool = DelegateTaskTool()
        result = await tool.execute(
            {"task": "do something", "agent_type": "coding"},
            ctx,
        )
        assert "TOOL_ERROR" in result
        assert "orchestrator not configured" in result

    @pytest.mark.asyncio
    async def test_delegate_with_orchestrator_returns_output(
        self, ctx: ToolContext
    ) -> None:
        """With a mocked orchestrator, delegate returns the sub-agent's output."""

        class MockOrchestrator:
            async def spawn(self, **kwargs: Any) -> Any:
                class MockResult:
                    output_text = "Sub-agent completed the task"
                    error = None
                return MockResult()

        set_delegate_orchestrator(MockOrchestrator())

        tool = DelegateTaskTool()
        result = await tool.execute(
            {"task": "implement feature X", "agent_type": "coding"},
            ctx,
        )
        assert result == "Sub-agent completed the task"

    @pytest.mark.asyncio
    async def test_delegate_invalid_agent_type(self, ctx: ToolContext) -> None:
        """Invalid agent_type returns TOOL_ERROR."""

        class MockOrchestrator:
            async def spawn(self, **kwargs: Any) -> Any:
                raise AssertionError("should not be called")

        set_delegate_orchestrator(MockOrchestrator())

        tool = DelegateTaskTool()
        result = await tool.execute(
            {"task": "do something", "agent_type": "invalid_type"},
            ctx,
        )
        assert "TOOL_ERROR" in result
        assert "unknown agent_type" in result

    @pytest.mark.asyncio
    async def test_delegate_orchestrator_error(self, ctx: ToolContext) -> None:
        """If spawn() raises, delegate returns TOOL_ERROR."""

        class MockOrchestrator:
            async def spawn(self, **kwargs: Any) -> Any:
                raise RuntimeError("spawn failed")

        set_delegate_orchestrator(MockOrchestrator())

        tool = DelegateTaskTool()
        result = await tool.execute(
            {"task": "do something", "agent_type": "coding"},
            ctx,
        )
        assert "TOOL_ERROR" in result
        assert "delegate failed" in result


# ---------------------------------------------------------------------- SendMessageTool


class TestSendMessageTool:
    """Verify the SendMessageTool works with a mocked callback."""

    @pytest.mark.asyncio
    async def test_send_message_without_callback_returns_error(
        self, ctx: ToolContext
    ) -> None:
        tool = SendMessageTool()
        result = await tool.execute(
            {"chat_id": "12345", "text": "hello"},
            ctx,
        )
        assert "TOOL_ERROR" in result
        assert "callback not configured" in result

    @pytest.mark.asyncio
    async def test_send_message_success(self, ctx: ToolContext) -> None:
        captured: dict[str, Any] = {}

        async def callback(chat_id, text, topic_id, parse_mode):  # type: ignore[no-untyped-def]
            captured["chat_id"] = chat_id
            captured["text"] = text
            captured["topic_id"] = topic_id
            captured["parse_mode"] = parse_mode
            return True

        set_send_message_callback(callback)

        tool = SendMessageTool()
        result = await tool.execute(
            {"chat_id": "12345", "text": "hello", "parse_mode": "html"},
            ctx,
        )
        assert result == "✅ sent"
        assert captured["chat_id"] == "12345"
        assert captured["text"] == "hello"
        assert captured["parse_mode"] == "html"

    @pytest.mark.asyncio
    async def test_send_message_callback_returns_false(self, ctx: ToolContext) -> None:
        async def callback(*args: Any, **kwargs: Any) -> bool:
            return False

        set_send_message_callback(callback)

        tool = SendMessageTool()
        result = await tool.execute(
            {"chat_id": "12345", "text": "hello"},
            ctx,
        )
        assert result == "❌ failed to send"

    @pytest.mark.asyncio
    async def test_send_message_callback_raises(self, ctx: ToolContext) -> None:
        async def callback(*args: Any, **kwargs: Any) -> bool:
            raise RuntimeError("network error")

        set_send_message_callback(callback)

        tool = SendMessageTool()
        result = await tool.execute(
            {"chat_id": "12345", "text": "hello"},
            ctx,
        )
        assert "TOOL_ERROR" in result
        assert "network error" in result


# ---------------------------------------------------------------------- ApprovalRequestTool


class TestApprovalRequestTool:
    """Verify the ApprovalRequestTool works with a mocked store."""

    @pytest.mark.asyncio
    async def test_approval_without_store_returns_error(
        self, ctx: ToolContext
    ) -> None:
        tool = ApprovalRequestTool()
        result = await tool.execute(
            {"action": "file.write", "description": "write /etc/passwd"},
            ctx,
        )
        assert "TOOL_ERROR" in result
        assert "approval store not configured" in result

    @pytest.mark.asyncio
    async def test_approval_request_creates_and_times_out(
        self, ctx: ToolContext
    ) -> None:
        """Approval request is created and times out if not resolved."""

        class MockStore:
            def __init__(self) -> None:
                self.created: list[Any] = []

            async def create_async(self, req: Any) -> Any:
                self.created.append(req)
                return req

            async def get_async(self, approval_id: str) -> Any:
                # Return the request as still pending.
                if self.created:
                    req = self.created[0]
                    req.status = type("S", (), {"value": "pending"})()
                    return req
                return None

        store = MockStore()
        set_approval_request_deps(store=store, send_keyboard=None)

        tool = ApprovalRequestTool()
        result = await tool.execute(
            {
                "action": "file.write",
                "description": "write file",
                "timeout_seconds": 1,
            },
            ctx,
        )
        assert "TIMEOUT" in result
        assert len(store.created) == 1
        assert store.created[0].action == "file.write"


# ---------------------------------------------------------------------- CronJobTool


class TestCronJobTool:
    """Verify the CronJobTool create/list/delete/run actions."""

    @pytest.mark.asyncio
    async def test_create_and_list(self, ctx: ToolContext) -> None:
        tool = CronJobTool()
        result = await tool.execute(
            {
                "action": "create",
                "name": "daily-standup",
                "schedule": "0 9 * * *",
                "task": "Post standup reminder",
            },
            ctx,
        )
        assert "✅ Created cron job" in result
        assert "daily-standup" in result

        # List should show the created job.
        result = await tool.execute({"action": "list"}, ctx)
        assert "daily-standup" in result
        assert "0 9 * * *" in result

    @pytest.mark.asyncio
    async def test_create_validation(self, ctx: ToolContext) -> None:
        tool = CronJobTool()
        result = await tool.execute(
            {"action": "create", "name": "", "schedule": "", "task": ""},
            ctx,
        )
        assert "TOOL_ERROR" in result
        assert "requires name, schedule, and task" in result

    @pytest.mark.asyncio
    async def test_delete(self, ctx: ToolContext) -> None:
        tool = CronJobTool()
        # Create a job first.
        create_result = await tool.execute(
            {
                "action": "create",
                "name": "test-job",
                "schedule": "0 9 * * *",
                "task": "test task",
            },
            ctx,
        )
        # Extract job_id from "✅ Created cron job cron_xxx: test-job (0 9 * * *)"
        import re
        match = re.search(r"cron_[a-f0-9]+", create_result)
        assert match is not None
        job_id = match.group(0)

        # Delete it.
        result = await tool.execute({"action": "delete", "job_id": job_id}, ctx)
        assert "✅ Deleted" in result

        # List should be empty now.
        result = await tool.execute({"action": "list"}, ctx)
        assert "no cron jobs" in result

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self, ctx: ToolContext) -> None:
        tool = CronJobTool()
        result = await tool.execute(
            {"action": "delete", "job_id": "cron_nonexistent"},
            ctx,
        )
        assert "TOOL_ERROR" in result
        assert "not found" in result

    @pytest.mark.asyncio
    async def test_run(self, ctx: ToolContext) -> None:
        tool = CronJobTool()
        # Create a job first.
        create_result = await tool.execute(
            {
                "action": "create",
                "name": "runnable-job",
                "schedule": "0 9 * * *",
                "task": "run me",
            },
            ctx,
        )
        import re
        match = re.search(r"cron_[a-f0-9]+", create_result)
        job_id = match.group(0)

        result = await tool.execute({"action": "run", "job_id": job_id}, ctx)
        assert "Would run" in result
        assert "run me" in result

    @pytest.mark.asyncio
    async def test_unknown_action(self, ctx: ToolContext) -> None:
        tool = CronJobTool()
        result = await tool.execute({"action": "unknown"}, ctx)
        assert "TOOL_ERROR" in result
        assert "unknown action" in result


# ---------------------------------------------------------------------- tool registry


class TestToolRegistry:
    """Verify all new tools are registered in the global registry."""

    def test_delegate_task_registered(self) -> None:
        entry = tool_registry.get("delegate_task")
        assert entry is not None
        assert "sub-agent" in entry.spec.description.lower()

    def test_send_message_registered(self) -> None:
        entry = tool_registry.get("send_message")
        assert entry is not None
        assert "telegram" in entry.spec.description.lower()

    def test_approval_request_registered(self) -> None:
        entry = tool_registry.get("approval_request")
        assert entry is not None
        assert "approval" in entry.spec.description.lower()

    def test_cronjob_registered(self) -> None:
        entry = tool_registry.get("cronjob")
        assert entry is not None
        assert "scheduled" in entry.spec.description.lower() or "cron" in entry.spec.description.lower()
