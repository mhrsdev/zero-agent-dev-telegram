"""Regression tests for the Pass-3 lifecycle fixes.

- N1: failed tasks with remaining retry budget pause the execution
  ("awaiting automatic retry") instead of terminally failing it, so the
  scheduler can requeue and reclaim them.
- N2: dependency-blocked tasks return to ready/pending once their
  dependencies recover.
- N4: failure/cancellation recording tolerates an expired-but-owned
  lease (no more zombie running attempts).
- N9: a lost streaming provider lease classifies as unknown_outcome.
- N6: a graph ending [completed.., cancelled] terminates the execution
  instead of leaving it running forever.
- N7: completion racing cancellation is never reported as success.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from zero.app.services import build_services
from zero.app.worker_service import DependencySpec, TaskSpec
from zero.config import Settings
from zero.domain.providers import ProviderRequestStateError
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations


def _make_services(task_max_attempts: int = 0):
    settings = Settings.load_for_test(task_max_attempts=task_max_attempts)
    database = Database(settings)
    apply_migrations(database)
    return build_services(settings, database)


def _approved_two_task_execution(services, *, key_b: str | None = None):
    from zero.domain.plans import PlanRevisionContent

    owner = services.identity.create_user(display_name="Lifecycle owner")
    project = services.identity.create_project(owner_id=owner.id, name="Lifecycle")
    event = services.plans.ingest_conversation_event(
        project_id=project.id,
        actor_id=owner.id,
        source="web",
        origin_kind="authenticated_human",
        content="Run the plan.",
    )
    plan = services.plans.create_plan(project_id=project.id, actor_id=owner.id)
    services.plans.propose_revision(
        plan_id=plan.id,
        project_id=project.id,
        actor_id=owner.id,
        content=PlanRevisionContent(
            objective="Lifecycle objective",
            scope=(),
            constraints=(),
            acceptance_criteria=("Done",),
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
        idempotency_key="lifecycle-approval",
    )
    specs = [TaskSpec(key="A", objective="Task A")]
    deps: list[DependencySpec] = []
    if key_b is not None:
        specs.append(TaskSpec(key=key_b, objective=f"Task {key_b}"))
        deps.append(DependencySpec(task_key=key_b, depends_on_key="A"))
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        project_id=project.id,
        actor_id=owner.id,
        task_specs=specs,
        dependency_specs=deps,
    )
    tasks = {
        t.objective: t
        for t in services.worker.list_tasks(execution.id, project_id=project.id, actor_id=owner.id)
    }
    return owner, project, execution, tasks


def _claim(services, owner, execution, task, *, lease_owner="w1", lease_seconds=300):
    return services.worker.claim_task(
        execution_id=execution.id,
        task_id=task.id,
        project_id=execution.project_id,
        actor_id=owner.id,
        lease_owner=lease_owner,
        lease_duration_seconds=lease_seconds,
    )


# ----------------------------------------------------------------------
# N1: retry-aware execution pausing + requeue round-trip
# ----------------------------------------------------------------------


def test_retry_budget_pauses_execution_and_requeue_completes() -> None:
    services = _make_services(task_max_attempts=2)
    owner, project, execution, tasks = _approved_two_task_execution(services)
    task_a = tasks["Task A"]

    attempt = _claim(services, owner, execution, task_a)
    services.worker.fail_task(
        execution_id=execution.id,
        task_id=task_a.id,
        attempt_id=attempt.id,
        error_message="transient failure",
        actor_id=owner.id,
        lease_owner="w1",
        project_id=execution.project_id,
    )
    current = services.worker.get_execution(execution.id, project_id=project.id, actor_id=owner.id)
    # With retry budget left, the execution must NOT be terminal-failed;
    # it pauses for the scheduler's requeue pass.
    assert current.state == "paused"
    assert "awaiting automatic" in (current.blocker_reason or "")

    services.worker.requeue_failed_task(
        execution_id=execution.id,
        project_id=project.id,
        task_id=task_a.id,
        actor_id=owner.id,
    )
    reclaimed_attempt = _claim(services, owner, execution, task_a, lease_owner="w2")
    completed = services.worker.complete_task(
        execution_id=execution.id,
        task_id=task_a.id,
        attempt_id=reclaimed_attempt.id,
        actor_id=owner.id,
        lease_owner="w2",
        project_id=execution.project_id,
    )
    assert completed.state == "completed"
    final = services.worker.get_execution(execution.id, project_id=project.id, actor_id=owner.id)
    assert final.state == "completed"


def test_exhausted_retry_budget_still_fails_execution() -> None:
    services = _make_services(task_max_attempts=1)
    owner, project, execution, tasks = _approved_two_task_execution(services)
    task_a = tasks["Task A"]
    attempt = _claim(services, owner, execution, task_a)
    services.worker.fail_task(
        execution_id=execution.id,
        task_id=task_a.id,
        attempt_id=attempt.id,
        error_message="permanent",
        actor_id=owner.id,
        lease_owner="w1",
        project_id=execution.project_id,
    )
    current = services.worker.get_execution(execution.id, project_id=project.id, actor_id=owner.id)
    assert current.state == "failed"


# ----------------------------------------------------------------------
# N2: blocked dependents recover when dependencies are retried
# ----------------------------------------------------------------------


def test_blocked_dependent_recovers_after_dependency_retry() -> None:
    services = _make_services(task_max_attempts=2)
    owner, project, execution, tasks = _approved_two_task_execution(services, key_b="B")
    task_a, task_b = tasks["Task A"], tasks["Task B"]

    attempt = _claim(services, owner, execution, task_a)
    services.worker.fail_task(
        execution_id=execution.id,
        task_id=task_a.id,
        attempt_id=attempt.id,
        error_message="flaky",
        actor_id=owner.id,
        lease_owner="w1",
        project_id=execution.project_id,
    )

    def state_of(task):
        return services.worker.list_tasks(execution.id, project_id=project.id, actor_id=owner.id)[
            [
                t.objective
                for t in services.worker.list_tasks(
                    execution.id, project_id=project.id, actor_id=owner.id
                )
            ].index(task.objective)
        ]

    blocked_b = state_of(task_b)
    assert blocked_b.state == "blocked"

    services.worker.requeue_failed_task(
        execution_id=execution.id,
        project_id=project.id,
        task_id=task_a.id,
        actor_id=owner.id,
    )
    retried = _claim(services, owner, execution, task_a, lease_owner="w2")
    services.worker.complete_task(
        execution_id=execution.id,
        task_id=task_a.id,
        attempt_id=retried.id,
        actor_id=owner.id,
        lease_owner="w2",
        project_id=execution.project_id,
    )
    revived = state_of(task_b)
    assert revived.state == "ready", (
        "dependency-blocked task must revive once its dependency completes"
    )


# ----------------------------------------------------------------------
# N4: expired-but-owned lease still records terminal failure
# ----------------------------------------------------------------------


def test_expired_lease_failure_is_recorded_not_zombied() -> None:
    services = _make_services()
    owner, _project, execution, tasks = _approved_two_task_execution(services)
    task_a = tasks["Task A"]
    attempt = _claim(services, owner, execution, task_a, lease_seconds=1)
    time.sleep(1.15)  # let the fencing clock expire
    failed = services.worker.fail_task(
        execution_id=execution.id,
        task_id=task_a.id,
        attempt_id=attempt.id,
        error_message="lease expired mid-run",
        actor_id=owner.id,
        lease_owner="w1",
        project_id=execution.project_id,
    )
    assert failed.state == "failed"


def test_completion_still_requires_live_lease() -> None:
    services = _make_services()
    owner, _project, execution, tasks = _approved_two_task_execution(services)
    task_a = tasks["Task A"]
    attempt = _claim(services, owner, execution, task_a, lease_seconds=1)
    time.sleep(1.15)
    from zero.domain.artifacts import ArtifactId
    from zero.domain.execution import LeaseOwnershipError

    with pytest.raises(LeaseOwnershipError):
        services.worker.complete_task(
            execution_id=execution.id,
            task_id=task_a.id,
            attempt_id=attempt.id,
            actor_id=owner.id,
            lease_owner="w1",
            evidence=(),
            evidence_artifact_ids=(ArtifactId("art_x"),),
            project_id=execution.project_id,
        )


# ----------------------------------------------------------------------
# N9: lost streaming lease -> unknown_outcome
# ----------------------------------------------------------------------


def test_lost_stream_lease_classifies_unknown_outcome() -> None:
    services = _make_services()
    error = ProviderRequestStateError("provider request lease was lost")
    assert services.providers._classify_error(error) == "unknown_outcome"


# ----------------------------------------------------------------------
# N6: cancelled-terminal graphs terminate the execution
# ----------------------------------------------------------------------


def test_cancelled_task_terminates_otherwise_done_graph() -> None:
    services = _make_services()
    owner, project, execution, tasks = _approved_two_task_execution(services)
    task_a = tasks["Task A"]
    attempt = _claim(services, owner, execution, task_a)
    services.worker.cancel_task(
        execution_id=execution.id,
        project_id=project.id,
        task_id=task_a.id,
        attempt_id=attempt.id,
        actor_id=owner.id,
        lease_owner="w1",
    )
    final = services.worker.get_execution(execution.id, project_id=project.id, actor_id=owner.id)
    assert final.state in {"failed", "cancelled"}
    assert final.state != "running"


# ----------------------------------------------------------------------
# N7: completion losing a race to cancellation is not reported success
# ----------------------------------------------------------------------


def test_runtime_refuses_success_when_completion_returns_cancelled() -> None:
    services = _make_services()
    owner, _project, execution, tasks = _approved_two_task_execution(services)
    task_a = tasks["Task A"]

    real_complete = services.worker.complete_task
    sentinel = SimpleNamespace(state="cancelled")

    def racing_complete(*args, **kwargs):
        # Simulate cancel_execution winning the race between evidence
        # acceptance and durable completion.
        kwargs = dict(kwargs)
        kwargs["evidence"] = ()
        kwargs["evidence_artifact_ids"] = ()
        real_complete(*args, **kwargs)
        return sentinel

    services.worker.complete_task = racing_complete  # type: ignore[assignment]

    from zero.domain.execution import ExecutionId as ExecId
    from zero.domain.identity import UserId as UId

    runtime = services.runtime
    assert runtime is not None
    with pytest.raises(Exception) as exc_info:
        runtime.run_task(
            execution_id=ExecId(execution.id.value),
            project_id=execution.project_id,
            task_id=task_a.id,
            actor_id=UId(owner.id.value),
            lease_owner="race-runner",
            provider="fake",
            model_name="fake-standard",
            source="system",
        )
    assert "instead of completed" in str(exc_info.value)
