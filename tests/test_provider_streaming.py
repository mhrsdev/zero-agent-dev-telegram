"""Regression tests: provider requests must stream, even without a
client-facing stream consumer.

Real-run bug: background tasks (scheduler-driven) built non-streaming
POSTs. A long completion — deep sub-agent reviews routinely exceed a
gateway edge timeout of ~100s — never returned response headers, the
gateway killed it with HTTP 524, and both same-provider retry attempts
524'd identically, failing the task. Streaming returns headers
immediately and sidesteps the edge timeout entirely.
"""

from __future__ import annotations

import sys
from typing import Any, Iterator

from zero.app.provider_adapter import ProviderAdapter
from zero.app.services import build_services
from zero.config import Settings
from zero.domain.execution import ExecutionId
from zero.domain.identity import UserId
from zero.domain.plans import PlanRevisionContent
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations

sys.path.insert(0, "/home/z/my-project/scripts/realrun")


class RecordingAdapter(ProviderAdapter):
    """Wraps an adapter and records which dispatch path each request took."""

    def __init__(self, inner: ProviderAdapter) -> None:
        self._inner = inner
        self.streamed: list[bool] = []

    @property
    def provider_name(self) -> str:
        return self._inner.provider_name

    def get_model(self, model_name: str):
        return self._inner.get_model(model_name)

    def send_request(self, request, *, cancel_event=None):
        self.streamed.append(False)
        return self._inner.send_request(request, cancel_event=cancel_event)

    def send_request_stream(self, request, *, cancel_event=None) -> Iterator[Any]:
        self.streamed.append(True)
        yield from self._inner.send_request_stream(request, cancel_event=cancel_event)


def _build_project_with_approved_plan(project_name: str):
    settings = Settings.load_for_test()
    database = Database(settings)
    apply_migrations(database)
    services = build_services(settings, database)
    owner = services.identity.create_user(display_name="Streaming owner")
    project = services.identity.create_project(owner_id=owner.id, name=project_name)
    services.interfaces.create_binding(
        project_id=project.id,
        actor_id=owner.id,
        platform="telegram",
        chat_id="9300",
        is_enabled=True,
    )
    event = services.plans.ingest_conversation_event(
        project_id=project.id,
        actor_id=owner.id,
        source="web",
        origin_kind="authenticated_human",
        content="Implement the streaming feature.",
    )
    plan = services.plans.create_plan(project_id=project.id, actor_id=owner.id)
    services.plans.propose_revision(
        plan_id=plan.id,
        project_id=project.id,
        actor_id=owner.id,
        content=PlanRevisionContent(
            objective="Implement the streaming feature",
            scope=("src",),
            constraints=(),
            acceptance_criteria=("A provider response is recorded",),
            risks=(),
            unresolved_questions=(),
            source_event_ids=(event.id,),
        ),
    )
    _approval, handoff = services.plans.approve_revision(
        plan_id=plan.id,
        project_id=project.id,
        actor_id=owner.id,
        expected_revision_number=1,
        idempotency_key=f"streaming-approval-{project_name}",
    )
    return services, project, owner, handoff


def test_main_task_provider_requests_stream_without_consumer() -> None:
    """A scheduler-driven task must dispatch as a streaming request even
    though no client-facing stream consumer is connected."""
    services, project, owner, _handoff = _build_project_with_approved_plan("streaming-main")
    recording = RecordingAdapter(services.providers._adapters["fake"])
    services.providers.register_adapter(recording)

    result = services.scheduler.run_once(
        project_id=project.id,
        actor_id=owner.id,
        lease_owner="streaming-test",
        provider="fake",
        model_name="fake-standard",
    )

    assert result.errors == ()
    assert result.tasks_run == 1
    assert recording.streamed == [True]


def test_delegated_subagent_request_streams() -> None:
    """The delegate tool's sub-agent request must also stream — this was
    the exact r5 failure (HTTP 524 on a non-streaming review request)."""
    services, project, owner, _handoff = _build_project_with_approved_plan("streaming-sub")
    recording = RecordingAdapter(services.providers._adapters["fake"])
    services.providers.register_adapter(recording)

    result = services.scheduler.run_once(
        project_id=project.id,
        actor_id=owner.id,
        lease_owner="streaming-test",
        provider="fake",
        model_name="fake-standard",
    )
    assert result.errors == ()

    execution = services.worker.list_project_executions(
        project_id=project.id, actor_id=owner.id
    )[0]
    payload = services.runtime._execute_delegation(
        call_arguments='{"objective": "Review the word-splitting rules"}',
        parent_allowed_tools=(),
        execution_id=ExecutionId(execution.id.value),
        project_id=project.id,
        actor_id=owner.id,
        provider="fake",
        model_name="fake-standard",
    )

    assert payload.get("status") != "error", payload
    assert recording.streamed == [True, True]


def test_stream_request_still_records_usage_and_completes_task() -> None:
    """Streaming must not change observable outcomes: the task completes
    with evidence and the usage tree is recorded."""
    services, project, owner, _handoff = _build_project_with_approved_plan("streaming-outcome")
    recording = RecordingAdapter(services.providers._adapters["fake"])
    services.providers.register_adapter(recording)

    result = services.scheduler.run_once(
        project_id=project.id,
        actor_id=owner.id,
        lease_owner="streaming-test",
        provider="fake",
        model_name="fake-standard",
    )

    assert result.errors == ()
    execution = services.worker.list_project_executions(
        project_id=project.id, actor_id=owner.id
    )[0]
    assert execution.state == "completed"
    usage = services.providers.get_usage_for_project(
        project.id, actor_id=owner.id, source="web"
    )
    assert usage is not None and usage.total_input_tokens >= 1
