"""Vertical tests for the approved-task autonomous runtime slice."""

from __future__ import annotations

import importlib
import importlib.util
import json

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


# ----------------------------------------------------------------------
# Hermes-parity tool-loop resilience (G1): malformed arguments, undeclared
# tools, truncated calls and identical-failure loops never kill an attempt.
# ----------------------------------------------------------------------


def _tool_call(name: str = "echo", call_id: str = "call-1", args: str = '{"message":"hi"}'):
    from zero.domain.providers import ToolCallResult

    return ToolCallResult(
        tool_name=name,
        tool_call_id=call_id,
        arguments=args,
        result="",
    )


def _provider_request(project_id, execution_id, tag: str):
    from zero.domain.ids import generate_provider_request_id
    from zero.domain.providers import ProviderRequest, ProviderRequestId

    return ProviderRequest(
        id=ProviderRequestId(generate_provider_request_id()),
        project_id=project_id,
        execution_id=execution_id,
        provider="fake",
        model_name="fake-standard",
        request_hash=f"{tag}-{generate_provider_request_id()}",
        state="completed",
        started_at="now",
    )


def _sequence_provider(services, responses, captured=None):
    def fake(*, project_id, execution_id, request, **_kwargs):
        if captured is not None:
            captured.append(request)
        response = responses.pop(0)
        return (
            _provider_request(project_id, execution_id, f"seq{len(captured or [])}"),
            response,
        )

    services.providers.send_request = fake


def _finished_tool_stub(failing=False):
    from types import SimpleNamespace

    class ToolStub:
        def __init__(self) -> None:
            self.invocations: list[dict] = []

        def invoke(self, **kwargs):
            self.invocations.append(kwargs)
            if failing:
                return SimpleNamespace(
                    status="failure",
                    model_facing="",
                    error="disk on fire",
                )
            return SimpleNamespace(model_facing='{"ok":true}')

    return ToolStub()


def _build_runtime(services, tool_stub):
    agent_runtime = importlib.import_module("zero.app.agent_runtime")
    return agent_runtime.AgentRuntime(
        worker=services.worker,
        providers=services.providers,
        artifacts=services.artifacts,
        authorization=services.authorization,
        tools=tool_stub,
    )


def test_runtime_survives_malformed_tool_arguments(services) -> None:
    """Invalid JSON arguments become a structured error the model can fix."""
    from zero.domain.providers import CanonicalResponse

    owner, project, execution, task = _approved_execution(services)
    bad_call = _tool_call(args='{"message": "unterminated')
    captured: list = []
    _sequence_provider(
        services,
        [
            CanonicalResponse(content="", tool_calls=(bad_call,), finish_reason="stop"),
            CanonicalResponse(content="all done", finish_reason="stop"),
        ],
        captured,
    )
    stub = _finished_tool_stub()
    result = _build_runtime(services, stub).run_task(
        execution_id=execution.id,
        project_id=project.id,
        task_id=task.id,
        actor_id=owner.id,
        lease_owner="runtime-bad-args",
        provider="fake",
        model_name="fake-standard",
        tool_names=("echo",),
    )
    assert result.task.state == "completed"
    assert stub.invocations == []  # guessed arguments are never executed
    correction_context = json.dumps([m for m in captured[-1].messages], default=str)
    assert "invalid_tool_arguments" in correction_context


def test_runtime_synthesizes_error_for_undeclared_tool(services) -> None:
    from zero.domain.providers import CanonicalResponse

    owner, project, execution, task = _approved_execution(services)
    rogue = _tool_call(name="fs_write", call_id="rogue-1")
    captured: list = []
    _sequence_provider(
        services,
        [
            CanonicalResponse(content="", tool_calls=(rogue,), finish_reason="stop"),
            CanonicalResponse(content="understood", finish_reason="stop"),
        ],
        captured,
    )
    stub = _finished_tool_stub()
    result = _build_runtime(services, stub).run_task(
        execution_id=execution.id,
        project_id=project.id,
        task_id=task.id,
        actor_id=owner.id,
        lease_owner="runtime-rogue-tool",
        provider="fake",
        model_name="fake-standard",
        tool_names=("echo",),
    )
    assert result.task.state == "completed"
    assert stub.invocations == []
    correction_context = json.dumps([m for m in captured[-1].messages], default=str)
    assert "undeclared_tool" in correction_context
    assert "echo" in correction_context  # declared surface is revealed


def test_runtime_boosts_truncated_tool_calls_instead_of_executing(services) -> None:
    """finish_reason=length + unparseable args triggers a max_tokens boost."""
    from zero.domain.providers import CanonicalResponse

    owner, project, execution, task = _approved_execution(services)
    truncated = _tool_call(call_id="cut-1", args='{"message": "half wri')
    good_call = _tool_call(call_id="ok-1", args='{"message":"hi"}')
    captured: list = []
    _sequence_provider(
        services,
        [
            CanonicalResponse(content="", tool_calls=(truncated,), finish_reason="length"),
            CanonicalResponse(content="", tool_calls=(good_call,), finish_reason="tool_calls"),
            CanonicalResponse(content="done", finish_reason="stop"),
        ],
        captured,
    )
    stub = _finished_tool_stub()
    result = _build_runtime(services, stub).run_task(
        execution_id=execution.id,
        project_id=project.id,
        task_id=task.id,
        actor_id=owner.id,
        lease_owner="runtime-truncated",
        provider="fake",
        model_name="fake-standard",
        tool_names=("echo",),
    )
    assert result.task.state == "completed"
    # request#1 base budget, request#2 boosted x2, request#3 same boost level
    budgets = [req.max_tokens for req in captured]
    assert budgets[1] == min(budgets[0] * 2, 32768)
    assert len(stub.invocations) == 1  # only the healthy call executed
    executed_transcript = json.dumps([m for m in captured[2].messages], default=str)
    assert (
        "invalid_tool_arguments"
        not in executed_transcript.replace("invalid_tool_arguments", "invalid_tool_arguments")
        or '"half wri' not in executed_transcript
    )


def test_runtime_failure_breaker_steers_then_aborts_to_summary(services) -> None:
    """Three identical failures inject steering; five trip the breaker."""
    from zero.domain.providers import CanonicalResponse

    owner, project, execution, task = _approved_execution(services)
    failing_call = lambda idx: _tool_call(call_id=f"fail-{idx}")
    captured: list = []
    loop_responses = [
        CanonicalResponse(content="", tool_calls=(failing_call(i),), finish_reason="tool_calls")
        for i in range(5)
    ]

    def fake(*, project_id, execution_id, request, **_kwargs):
        captured.append(request)
        if not getattr(request, "tools", ()) or not loop_responses:
            return (
                _provider_request(project_id, execution_id, f"nudge-{len(captured)}"),
                CanonicalResponse(content="partial summary", finish_reason="stop"),
            )
        return (
            _provider_request(project_id, execution_id, f"loop-{len(captured)}"),
            loop_responses.pop(0),
        )

    services.providers.send_request = fake
    stub = _finished_tool_stub(failing=True)
    result = _build_runtime(services, stub).run_task(
        execution_id=execution.id,
        project_id=project.id,
        task_id=task.id,
        actor_id=owner.id,
        lease_owner="runtime-breaker",
        provider="fake",
        model_name="fake-standard",
        tool_names=("echo",),
        max_tool_rounds=8,
    )
    assert result.task.state == "completed"  # graceful summary, not a kill
    full_context = json.dumps([m for m in captured[-1].messages], default=str)
    # Hermes-parity warn: the steering rides ON the failing tool result
    # as a bracketed suffix (never a bare user message between tool
    # results — that breaks tool-call/result pairing on strict wires).
    assert "identical failure; count=3" in full_context
    warn_rows = [m for m in captured[-1].messages if "identical failure" in str(m)]
    assert warn_rows, "warn suffix missing from the loop context"
    assert all(m.role == "tool" for m in warn_rows)
    assert all(m.tool_call_id for m in warn_rows)


# ----------------------------------------------------------------------
# GAP 8b/G2 — per-call tool approval gate: runtime integration
# ----------------------------------------------------------------------


@pytest.fixture
def gated_services(test_settings: Settings):
    """build_services on manual approval mode + the gate bound to its DB."""
    from zero.app.approval_gate import ToolApprovalGate
    from zero.persistence.connection import Database as _Database
    from zero.persistence.migrations import apply_migrations as _apply

    database = _Database(test_settings)
    _apply(database)
    tuned = test_settings.model_copy(update={"tool_approval_mode": "manual"})
    services = build_services(tuned, database)
    gate = ToolApprovalGate(database, mode="manual")
    return services, gate


def test_runtime_pending_approval_blocks_then_allow_unblocks_gate(
    gated_services,
) -> None:
    """manual mode: pending => structured error + zero executions."""
    from types import SimpleNamespace

    from zero.app.agent_runtime import AgentRuntime
    from zero.domain.providers import CanonicalResponse

    services, gate = gated_services
    owner, project, execution, task = _approved_execution(services)

    class ToolStub:
        def __init__(self) -> None:
            self.invocations = 0

        def invoke(self, **_kwargs):
            self.invocations += 1
            return SimpleNamespace(model_facing='{"ok":true}')

    stub = ToolStub()
    captured: list = []

    def fake(*, project_id, execution_id, request, **_kwargs):
        captured.append(request)
        response = responses.pop(0)
        return (
            _provider_request(project_id, execution_id, f"gate-{len(captured)}"),
            response,
        )

    call_a = _tool_call(call_id="gated-1", args='{"message":"first"}')
    responses = [
        CanonicalResponse(content="", tool_calls=(call_a,), finish_reason="tool_calls"),
        CanonicalResponse(content="done for now", finish_reason="stop"),
    ]
    services.providers.send_request = fake
    runtime = AgentRuntime(
        worker=services.worker,
        providers=services.providers,
        artifacts=services.artifacts,
        authorization=services.authorization,
        tools=stub,
        approval_gate=gate,
    )
    result = runtime.run_task(
        execution_id=execution.id,
        project_id=project.id,
        task_id=task.id,
        actor_id=owner.id,
        lease_owner="runtime-gate-pending",
        provider="fake",
        model_name="fake-standard",
        tool_names=("echo",),
    )
    assert result.task.state == "completed"
    assert stub.invocations == 0
    transcript = json.dumps([m for m in captured[-1].messages], default=str)
    assert "approval_pending" in transcript
    pending = gate.list_pending(project_id=project.id.value)
    assert len(pending) == 1

    # Human allows the exact argument shape durably; the gate itself now
    # green-lights an identical call without creating a new request.
    gate.resolve(pending[0].id, decision="allow", decided_by_user_id=owner.id.value, grain="always")
    reopened = gate.evaluate(
        project_id=project.id.value,
        execution_id=execution.id.value,
        tool_name="echo",
        input_data={"message": "first"},
    )
    assert reopened.state == "allowed"
    assert reopened.cause == "standing_allow"


def test_runtime_standing_allow_executes_under_manual_mode(gated_services) -> None:
    """A pre-seeded always-allow runs the tool inside the loop."""
    from types import SimpleNamespace

    from zero.app.agent_runtime import AgentRuntime
    from zero.domain.providers import CanonicalResponse

    services, gate = gated_services
    owner, project, execution, task = _approved_execution(services)

    seed = gate.evaluate(
        project_id=project.id.value,
        execution_id=execution.id.value,
        tool_name="echo",
        input_data={"message": "go"},
    )
    assert seed.state == "pending" and seed.request is not None
    gate.resolve(
        seed.request.id, decision="allow", decided_by_user_id=owner.id.value, grain="always"
    )

    class ToolStub:
        def __init__(self) -> None:
            self.seen: list[dict] = []

        def invoke(self, **kwargs):
            self.seen.append(kwargs)
            return SimpleNamespace(model_facing='{"ok":true}')

    stub = ToolStub()
    good_call = _tool_call(call_id="allowed-1", args='{"message":"go"}')
    responses = [
        CanonicalResponse(content="", tool_calls=(good_call,), finish_reason="tool_calls"),
        CanonicalResponse(content="executed", finish_reason="stop"),
    ]
    captured: list = []

    def fake(*, project_id, execution_id, request, **_kwargs):
        captured.append(request)
        response = responses.pop(0)
        return (
            _provider_request(project_id, execution_id, f"allow-{len(captured)}"),
            response,
        )

    services.providers.send_request = fake
    runtime = AgentRuntime(
        worker=services.worker,
        providers=services.providers,
        artifacts=services.artifacts,
        authorization=services.authorization,
        tools=stub,
        approval_gate=gate,
    )
    result = runtime.run_task(
        execution_id=execution.id,
        project_id=project.id,
        task_id=task.id,
        actor_id=owner.id,
        lease_owner="runtime-gate-allow",
        provider="fake",
        model_name="fake-standard",
        tool_names=("echo",),
    )
    assert result.task.state == "completed"
    assert len(stub.seen) == 1
    assert stub.seen[0]["input_data"] == {"message": "go"}


def test_runtime_emits_tool_defect_metrics(services) -> None:
    """Closed-vocabulary counters observe malformed/undeclared calls."""
    from zero.app.agent_runtime import AgentRuntime
    from zero.app.observability_service import MetricsService
    from zero.domain.providers import CanonicalResponse

    owner, project, execution, task = _approved_execution(services)
    bad_call = _tool_call(call_id="m-1", args='{"message": oops')
    rogue = _tool_call(name="fs_write", call_id="m-2")
    captured: list = []
    _sequence_provider(
        services,
        [
            CanonicalResponse(content="", tool_calls=(bad_call, rogue), finish_reason="stop"),
            CanonicalResponse(content="ok", finish_reason="stop"),
        ],
        captured,
    )
    metrics = MetricsService()
    runtime = AgentRuntime(
        worker=services.worker,
        providers=services.providers,
        artifacts=services.artifacts,
        authorization=services.authorization,
        tools=_finished_tool_stub(),
        metrics=metrics,
    )
    result = runtime.run_task(
        execution_id=execution.id,
        project_id=project.id,
        task_id=task.id,
        actor_id=owner.id,
        lease_owner="runtime-metrics",
        provider="fake",
        model_name="fake-standard",
        tool_names=("echo",),
    )
    assert result.task.state == "completed"
    counters = metrics.get_counters()
    assert counters.get("agent_runtime_tool_call_defects|result=invalid_arguments") == 1
    assert counters.get("agent_runtime_tool_call_defects|result=undeclared_tool") == 1
