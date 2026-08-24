"""Vertical tests for the approved-task autonomous runtime slice."""

from __future__ import annotations

import importlib
import importlib.util

import pytest

from zero.app.services import build_services
from zero.app.worker_service import TaskSpec
from zero.config import Settings
from zero.domain.plans import PlanRevisionContent
from zero.domain.providers import ProviderCancelledError, ProviderUnknownOutcomeError
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations


def _approved_execution(
    services,
    *,
    expected_evidence: tuple[str, ...] = ("provider_response",),
    objective: str = "Produce a provider response",
):
    owner = services.identity.create_user(display_name="Runtime owner")
    project = services.identity.create_project(owner_id=owner.id, name="Runtime")
    event = services.plans.ingest_conversation_event(
        project_id=project.id,
        actor_id=owner.id,
        source="web",
        origin_kind="authenticated_human",
        content="Run the approved task.",
    )
    plan = services.plans.create_plan(project_id=project.id, actor_id=owner.id)
    services.plans.propose_revision(
        plan_id=plan.id,
        project_id=project.id,
        actor_id=owner.id,
        content=PlanRevisionContent(
            objective=objective,
            scope=("backend",),
            constraints=(),
            acceptance_criteria=("A provider response is durably recorded",),
            risks=(),
            unresolved_questions=(),
            source_event_ids=(event.id,),
        ),
    )
    _, handoff = services.plans.approve_revision(
        plan_id=plan.id,
        project_id=project.id,
        actor_id=owner.id,
        expected_revision_number=1,
        idempotency_key="runtime-approval",
    )
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        project_id=project.id,
        actor_id=owner.id,
        task_specs=[
            TaskSpec(
                key="runtime-task",
                objective=objective,
                permitted_scope=("backend",),
                expected_evidence=expected_evidence,
            )
        ],
    )
    task = services.worker.list_tasks(
        execution.id,
        project_id=execution.project_id,
        actor_id=services.identity.get_project(execution.project_id).owner_user_id,
    )[0]
    return owner, project, execution, task


@pytest.fixture
def services(test_settings: Settings):
    database = Database(test_settings)
    apply_migrations(database)
    return build_services(test_settings, database)


def test_runtime_preserves_unknown_provider_outcome_for_reconciliation(
    services,
    monkeypatch,
) -> None:
    owner, project, execution, task = _approved_execution(services)
    runtime = importlib.import_module("zero.app.agent_runtime").AgentRuntime(
        worker=services.worker,
        providers=services.providers,
        artifacts=services.artifacts,
        authorization=services.authorization,
    )

    def unknown_outcome(*args, **kwargs):
        raise ProviderUnknownOutcomeError("provider may have accepted request")

    monkeypatch.setattr(services.providers, "send_request", unknown_outcome)
    with pytest.raises(ProviderUnknownOutcomeError):
        runtime.run_task(
            execution_id=execution.id,
            project_id=project.id,
            task_id=task.id,
            actor_id=owner.id,
            lease_owner="runtime-worker-unknown",
            provider="fake",
            model_name="fake-standard",
        )

    final_task = services.worker.list_tasks(
        execution.id,
        project_id=project.id,
        actor_id=owner.id,
    )[0]
    final_attempt = services.worker.list_attempts(
        task.id,
        project_id=project.id,
        actor_id=owner.id,
    )[-1]
    assert final_task.state == "blocked"
    assert "unknown" in (final_task.blocker_reason or "").lower()
    assert final_attempt.state == "unknown"


def test_runtime_propagates_worker_cancellation_event(services, monkeypatch) -> None:
    owner, project, execution, task = _approved_execution(services)
    runtime = importlib.import_module("zero.app.agent_runtime").AgentRuntime(
        worker=services.worker,
        providers=services.providers,
        artifacts=services.artifacts,
        authorization=services.authorization,
    )
    observed: dict[str, object] = {}

    def cancelled_provider(*args, **kwargs):
        observed["cancel_event"] = kwargs.get("cancel_event")
        raise ProviderCancelledError("cancelled")

    monkeypatch.setattr(services.providers, "send_request", cancelled_provider)
    # Capture before the run: terminal executions evict their
    # process-local event (bounded-map hygiene), so a post-run fetch
    # returns a fresh object by design.
    cancel_event_before = services.worker.get_cancellation_event(execution.id)
    with pytest.raises(ProviderCancelledError):
        runtime.run_task(
            execution_id=execution.id,
            project_id=project.id,
            task_id=task.id,
            actor_id=owner.id,
            lease_owner="runtime-worker-cancel",
            provider="fake",
            model_name="fake-standard",
        )

    assert observed["cancel_event"] is cancel_event_before


def test_runtime_does_not_accept_response_after_durable_cancellation(services, monkeypatch) -> None:
    owner, project, execution, task = _approved_execution(services)
    runtime = importlib.import_module("zero.app.agent_runtime").AgentRuntime(
        worker=services.worker,
        providers=services.providers,
        artifacts=services.artifacts,
        authorization=services.authorization,
    )
    original_send = services.providers.send_request

    def send_then_cancel(**kwargs):
        result = original_send(**kwargs)
        services.worker.cancel_execution(
            execution_id=execution.id,
            project_id=project.id,
            actor_id=owner.id,
        )
        return result

    monkeypatch.setattr(services.providers, "send_request", send_then_cancel)
    with pytest.raises(ProviderCancelledError):
        runtime.run_task(
            execution_id=execution.id,
            project_id=project.id,
            task_id=task.id,
            actor_id=owner.id,
            lease_owner="runtime-worker-cancel-after-response",
            provider="fake",
            model_name="fake-standard",
        )

    final_task = services.worker.list_tasks(
        execution.id,
        project_id=project.id,
        actor_id=owner.id,
    )[0]
    assert final_task.state == "cancelled"
    assert not any(
        artifact.producer == f"agent-runtime:{task.id.value}"
        for artifact in services.artifacts.list_artifacts(
            project_id=project.id,
            actor_id=owner.id,
        )
    )


def test_runtime_claims_calls_persists_evidence_and_completes(services) -> None:
    owner, _project, execution, task = _approved_execution(services)
    assert importlib.util.find_spec("zero.app.agent_runtime") is not None
    agent_runtime = importlib.import_module("zero.app.agent_runtime")
    runtime_class = getattr(agent_runtime, "AgentRuntime", None)
    assert runtime_class is not None
    runtime = runtime_class(
        worker=services.worker,
        providers=services.providers,
        artifacts=services.artifacts,
        authorization=services.authorization,
    )

    result = runtime.run_task(
        execution_id=execution.id,
        project_id=_project.id,
        task_id=task.id,
        actor_id=owner.id,
        lease_owner="runtime-worker-1",
        provider="fake",
        model_name="fake-standard",
    )

    assert result.task.state == "completed"
    assert result.attempt.state == "succeeded"
    assert result.evidence_artifact_id is not None
    artifacts = services.artifacts.list_artifacts(
        project_id=task.project_id,
        actor_id=owner.id,
    )
    assert any(artifact.id == result.evidence_artifact_id for artifact in artifacts)
    assert (
        services.worker.list_attempts(
            task.id,
            project_id=task.project_id,
            actor_id=services.identity.get_project(task.project_id).owner_user_id,
        )[0].lease_owner
        == "runtime-worker-1"
    )


def test_runtime_does_not_complete_with_unresolved_tool_calls(services) -> None:
    from types import SimpleNamespace

    from zero.app.agent_runtime import AgentRuntime, RuntimeToolError
    from zero.domain.ids import generate_provider_request_id
    from zero.domain.providers import (
        CanonicalResponse,
        ProviderRequest,
        ProviderRequestId,
        ToolCallResult,
    )

    owner, project, execution, task = _approved_execution(services)

    class ToolStub:
        def invoke(self, **_kwargs):
            return SimpleNamespace(model_facing='{"ok":true}')

    def looping_provider(*, project_id, execution_id, request, **_kwargs):
        call = ToolCallResult(
            tool_name="echo",
            tool_call_id="loop-call",
            arguments='{"message":"loop"}',
            result="",
        )
        return (
            ProviderRequest(
                id=ProviderRequestId(generate_provider_request_id()),
                project_id=project_id,
                execution_id=execution_id,
                provider="fake",
                model_name="fake-standard",
                request_hash=f"loop-{len(request.messages)}",
                state="completed",
                started_at="now",
            ),
            CanonicalResponse(content="again", tool_calls=(call,), finish_reason="tool_calls"),
        )

    services.providers.send_request = looping_provider
    runtime = AgentRuntime(
        worker=services.worker,
        providers=services.providers,
        artifacts=services.artifacts,
        authorization=services.authorization,
        tools=ToolStub(),
    )
    with pytest.raises(RuntimeToolError, match="unresolved calls"):
        runtime.run_task(
            execution_id=execution.id,
            project_id=project.id,
            task_id=task.id,
            actor_id=owner.id,
            lease_owner="runtime-loop-worker",
            provider="fake",
            model_name="fake-standard",
            tool_names=("echo",),
            max_tool_rounds=1,
        )
    final_task = services.worker.list_tasks(
        execution.id,
        project_id=project.id,
        actor_id=owner.id,
    )[0]
    assert final_task.state == "failed"
