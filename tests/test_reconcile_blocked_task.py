"""Regression tests: operator reconciliation for blocked tasks.

Real-run bug: ``mark_provider_outcome_unknown`` blocks a task and pauses
the execution demanding "reconciliation required", but no service method
could perform that reconciliation — the ``blocked -> ready`` transition
was legal in the state machine yet unreachable, leaving every
unknown-outcome execution wedged forever.
"""

from __future__ import annotations

import pytest

from zero.app.services import build_services
from zero.app.worker_service import TaskSpec
from zero.config import Settings
from zero.domain.execution import (
    ExecutionId,
    InvalidTaskTransitionError,
    TaskId,
)
from zero.domain.plans import PlanRevisionContent
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations


def _make_execution_with_task(project_name: str):
    settings = Settings.load_for_test()
    database = Database(settings)
    apply_migrations(database)
    services = build_services(settings, database)
    owner = services.identity.create_user(display_name="Reconcile owner")
    project = services.identity.create_project(owner_id=owner.id, name=project_name)
    event = services.plans.ingest_conversation_event(
        project_id=project.id,
        actor_id=owner.id,
        source="web",
        origin_kind="authenticated_human",
        content="Implement the reconcile feature.",
    )
    plan = services.plans.create_plan(project_id=project.id, actor_id=owner.id)
    services.plans.propose_revision(
        plan_id=plan.id,
        project_id=project.id,
        actor_id=owner.id,
        content=PlanRevisionContent(
            objective="Implement the reconcile feature",
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
        idempotency_key=f"reconcile-approval-{project_name}",
    )
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        actor_id=owner.id,
        project_id=project.id,
        task_specs=[
            TaskSpec(
                key="implementation",
                objective="Implement the reconcile feature",
                permitted_scope=("src",),
                expected_evidence=("provider_response",),
            )
        ],
        dependency_specs=[],
    )
    return services, project, owner, execution


def test_blocked_task_can_be_reconciled_back_to_ready() -> None:
    services, project, owner, execution = _make_execution_with_task("reconcile-ready")
    task = services.worker.list_tasks(execution.id, project_id=project.id, actor_id=owner.id)[0]

    # Force the same durable state the unknown-outcome path produces.
    services.worker._execution_repo.update_task_state(
        task.id,
        "blocked",
        blocker_reason="provider outcome unknown; reconciliation required",
    )

    reconciled = services.worker.reconcile_blocked_task(
        execution_id=execution.id,
        project_id=project.id,
        task_id=task.id,
        actor_id=owner.id,
        source="web",
    )

    assert reconciled.state == "ready"
    assert reconciled.blocker_reason is None
    ready = services.worker.list_ready_tasks(
        execution.id, project_id=project.id, actor_id=owner.id
    )
    assert any(t.id == task.id for t in ready)

    events = services.audit.list_for_project(
        project_id=project.id, actor_id=owner.id, source="web"
    )
    assert any(event.operation == "task.reconcile" for event in events)


def test_reconcile_rejects_non_blocked_tasks() -> None:
    services, project, owner, execution = _make_execution_with_task("reconcile-reject")
    task = services.worker.list_tasks(execution.id, project_id=project.id, actor_id=owner.id)[0]

    with pytest.raises(InvalidTaskTransitionError):
        services.worker.reconcile_blocked_task(
            execution_id=execution.id,
            project_id=project.id,
            task_id=task.id,
            actor_id=owner.id,
            source="web",
        )


def test_completed_execution_has_no_stale_blocker() -> None:
    """Blocker hygiene: a paused execution (blocker set) that is later
    reconciled and finishes must be delivered as completed WITHOUT the
    stale pause-time blocker text."""
    services, project, owner, execution = _make_execution_with_task("reconcile-hygiene")
    task = services.worker.list_tasks(execution.id, project_id=project.id, actor_id=owner.id)[0]

    # Simulate the unknown-outcome durable state: task blocked, execution
    # paused with a blocker.
    services.worker._execution_repo.update_task_state(
        task.id,
        "blocked",
        blocker_reason="provider outcome unknown; reconciliation required",
    )
    services.worker._execution_repo.update_execution_state(
        execution.id,
        "paused",
        blocker_reason="task failed or blocked",
    )

    services.worker.reconcile_blocked_task(
        execution_id=execution.id,
        project_id=project.id,
        task_id=task.id,
        actor_id=owner.id,
        source="web",
    )

    result = services.scheduler.run_once(
        project_id=project.id,
        actor_id=owner.id,
        lease_owner="reconcile-hygiene",
        provider="fake",
        model_name="fake-standard",
    )
    assert result.errors == ()

    final = services.worker.get_execution(execution.id, project_id=project.id, actor_id=owner.id)
    assert final.state == "completed"
    assert final.blocker_reason is None
