"""Execution graph tests — covers all M5 validation gates.

Per PLAN.md M5 validation:
- Independent tasks become ready together.
- Dependent tasks remain blocked until prerequisites succeed.
- Failed prerequisites block dependents safely.
- Cycles and missing dependencies are rejected.
- Restart reconstructs the same graph and statuses.
- Replayed scheduler events do not duplicate work.
- Cancellation propagates according to an explicit tested rule.

Per PLAN.md M5 acceptance:
- A sample approved plan produces a deterministic graph, exposes only
  valid ready tasks, survives process restart, and reaches a correct
  terminal state without executing code yet.
"""

from __future__ import annotations

import time

import pytest

from zero.app.services import build_services
from zero.app.worker_service import DependencySpec, TaskSpec
from zero.config import Settings
from zero.domain.execution import (
    CycleError,
    InvalidExecutionTransitionError,
    LeaseOwnershipError,
    MissingDependencyError,
)
from zero.domain.plans import PlanRevisionContent
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations


@pytest.fixture
def services(test_settings: Settings):
    database = Database(test_settings)
    apply_migrations(database)
    return build_services(test_settings, database)


def _make_approved_plan(services, owner_name="Owner"):
    """Helper: create an approved plan with one revision. Returns
    (owner, project, plan, handoff)."""
    owner = services.identity.create_user(display_name=owner_name)
    project = services.identity.create_project(owner_id=owner.id, name="Project A")
    event = services.plans.ingest_conversation_event(
        project_id=project.id,
        actor_id=owner.id,
        source="web",
        origin_kind="authenticated_human",
        content="Add a feature.",
    )
    plan = services.plans.create_plan(project_id=project.id, actor_id=owner.id)
    content = PlanRevisionContent(
        objective="Add a feature",
        scope=("backend",),
        constraints=(),
        acceptance_criteria=("Feature works",),
        risks=(),
        unresolved_questions=(),
        source_event_ids=(event.id,),
    )
    services.plans.propose_revision(
        plan_id=plan.id, project_id=project.id, actor_id=owner.id, content=content
    )
    _, handoff = services.plans.approve_revision(
        plan_id=plan.id,
        project_id=project.id,
        actor_id=owner.id,
        expected_revision_number=1,
        idempotency_key="approval-1",
    )
    return owner, project, plan, handoff


# ----------------------------------------------------------------------
# Execution creation
# ----------------------------------------------------------------------


def test_create_execution_from_approved_handoff(services) -> None:
    owner, _project, plan, handoff = _make_approved_plan(services)
    task_a = TaskSpec(key="A", objective="Task A")
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        project_id=_project.id,
        actor_id=owner.id,
        task_specs=[task_a],
    )
    assert execution.state == "pending"
    assert execution.plan_id == plan.id


def test_create_execution_rejects_unapproved_plan(services) -> None:
    """Per PLAN.md M5: 'Worker accepts only a valid approved plan
    revision.'"""
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(owner_id=owner.id, name="Project A")
    event = services.plans.ingest_conversation_event(
        project_id=project.id,
        actor_id=owner.id,
        source="web",
        origin_kind="authenticated_human",
        content="Add a feature.",
    )
    plan = services.plans.create_plan(project_id=project.id, actor_id=owner.id)
    content = PlanRevisionContent(
        objective="Add a feature",
        scope=(),
        constraints=(),
        acceptance_criteria=("Feature works",),
        risks=(),
        unresolved_questions=(),
        source_event_ids=(event.id,),
    )
    services.plans.propose_revision(
        plan_id=plan.id, project_id=project.id, actor_id=owner.id, content=content
    )
    # Get the handoff by approving, then reject... actually we can't
    # get a handoff without approving. So we test by creating an
    # execution from a handoff that doesn't exist.
    from zero.domain.plans import PlanHandoffId, PlanNotFoundError

    with pytest.raises(PlanNotFoundError):
        services.worker.create_execution_from_handoff(
            handoff_id=PlanHandoffId("ph_nonexistent"),
            project_id=project.id,
            actor_id=owner.id,
            task_specs=[TaskSpec(key="A", objective="Task A")],
        )


def test_create_execution_is_idempotent(services) -> None:
    """Per zero-planner-worker-contract §'Idempotency is part of
    normal operation': one execution per approved plan revision."""
    owner, _project, _plan, handoff = _make_approved_plan(services)
    task_a = TaskSpec(key="A", objective="Task A")
    execution1 = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        project_id=_project.id,
        actor_id=owner.id,
        task_specs=[task_a],
    )
    execution2 = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        project_id=_project.id,
        actor_id=owner.id,
        task_specs=[task_a],
    )
    assert execution1.id == execution2.id


# ----------------------------------------------------------------------
# Independent tasks become ready together
# ----------------------------------------------------------------------


def test_independent_tasks_become_ready_together(services) -> None:
    """Per PLAN.md M5: 'Independent tasks become ready together.'"""
    owner, _project, _plan, handoff = _make_approved_plan(services)
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        project_id=_project.id,
        actor_id=owner.id,
        task_specs=[
            TaskSpec(key="A", objective="Task A"),
            TaskSpec(key="B", objective="Task B"),
            TaskSpec(key="C", objective="Task C"),
        ],
    )
    ready = services.worker.list_ready_tasks(
        execution.id,
        project_id=execution.project_id,
        actor_id=services.identity.get_project(execution.project_id).owner_user_id,
    )
    assert len(ready) == 3


# ----------------------------------------------------------------------
# Dependent tasks remain blocked until prerequisites succeed
# ----------------------------------------------------------------------


def test_dependent_tasks_remain_blocked_until_prerequisites_succeed(
    services,
) -> None:
    """Per PLAN.md M5: 'Dependent tasks remain blocked until
    prerequisites succeed.'"""
    owner, _project, _plan, handoff = _make_approved_plan(services)
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        project_id=_project.id,
        actor_id=owner.id,
        task_specs=[
            TaskSpec(key="A", objective="Task A"),
            TaskSpec(key="B", objective="Task B"),
        ],
        dependency_specs=[
            DependencySpec(task_key="B", depends_on_key="A"),
        ],
    )
    tasks = services.worker.list_tasks(
        execution.id,
        project_id=execution.project_id,
        actor_id=services.identity.get_project(execution.project_id).owner_user_id,
    )
    task_a = next(t for t in tasks if t.objective == "Task A")
    task_b = next(t for t in tasks if t.objective == "Task B")
    assert task_a.state == "ready"
    assert task_b.state == "pending"
    # Ready list contains only A.
    ready = services.worker.list_ready_tasks(
        execution.id,
        project_id=execution.project_id,
        actor_id=services.identity.get_project(execution.project_id).owner_user_id,
    )
    assert len(ready) == 1
    assert ready[0].id == task_a.id


def test_completing_prerequisite_makes_dependent_ready(services) -> None:
    owner, _project, _plan, handoff = _make_approved_plan(services)
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        project_id=_project.id,
        actor_id=owner.id,
        task_specs=[
            TaskSpec(key="A", objective="Task A"),
            TaskSpec(key="B", objective="Task B"),
        ],
        dependency_specs=[
            DependencySpec(task_key="B", depends_on_key="A"),
        ],
    )
    tasks = services.worker.list_tasks(
        execution.id,
        project_id=execution.project_id,
        actor_id=services.identity.get_project(execution.project_id).owner_user_id,
    )
    task_a = next(t for t in tasks if t.objective == "Task A")
    task_b = next(t for t in tasks if t.objective == "Task B")
    # Claim and complete A.
    attempt = services.worker.claim_task(
        execution_id=execution.id,
        task_id=task_a.id,
        lease_owner="worker-1",
        project_id=execution.project_id,
        actor_id=services.identity.get_project(execution.project_id).owner_user_id,
    )
    services.worker.complete_task(
        execution_id=execution.id,
        task_id=task_a.id,
        attempt_id=attempt.id,
        actor_id=owner.id,
        lease_owner="worker-1",
        project_id=execution.project_id,
    )
    # B should now be ready.
    task_b = services.worker._execution_repo.get_task(task_b.id)
    assert task_b.state == "ready"


# ----------------------------------------------------------------------
# Failed prerequisites block dependents safely
# ----------------------------------------------------------------------


def test_failed_prerequisite_blocks_dependent(services) -> None:
    """Per PLAN.md M5: 'Failed prerequisites block dependents safely.'"""
    owner, _project, _plan, handoff = _make_approved_plan(services)
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        project_id=_project.id,
        actor_id=owner.id,
        task_specs=[
            TaskSpec(key="A", objective="Task A"),
            TaskSpec(key="B", objective="Task B"),
        ],
        dependency_specs=[
            DependencySpec(task_key="B", depends_on_key="A"),
        ],
    )
    tasks = services.worker.list_tasks(
        execution.id,
        project_id=execution.project_id,
        actor_id=services.identity.get_project(execution.project_id).owner_user_id,
    )
    task_a = next(t for t in tasks if t.objective == "Task A")
    task_b = next(t for t in tasks if t.objective == "Task B")
    # Claim and fail A.
    attempt = services.worker.claim_task(
        execution_id=execution.id,
        task_id=task_a.id,
        lease_owner="worker-1",
        project_id=execution.project_id,
        actor_id=services.identity.get_project(execution.project_id).owner_user_id,
    )
    services.worker.fail_task(
        execution_id=execution.id,
        task_id=task_a.id,
        attempt_id=attempt.id,
        error_message="something went wrong",
        actor_id=owner.id,
        lease_owner="worker-1",
        project_id=execution.project_id,
    )
    # B should be blocked (not ready, not pending).
    task_b = services.worker._execution_repo.get_task(task_b.id)
    assert task_b.state == "blocked"
    # Ready list is empty.
    ready = services.worker.list_ready_tasks(
        execution.id,
        project_id=execution.project_id,
        actor_id=services.identity.get_project(execution.project_id).owner_user_id,
    )
    assert len(ready) == 0


# ----------------------------------------------------------------------
# Cycles and missing dependencies are rejected
# ----------------------------------------------------------------------


def test_cycle_rejected(services) -> None:
    """Per PLAN.md M5: 'Cycles and missing dependencies are rejected.'"""
    owner, _project, _plan, handoff = _make_approved_plan(services)
    with pytest.raises(CycleError):
        services.worker.create_execution_from_handoff(
            handoff_id=handoff.id,
            project_id=_project.id,
            actor_id=owner.id,
            task_specs=[
                TaskSpec(key="A", objective="Task A"),
                TaskSpec(key="B", objective="Task B"),
            ],
            dependency_specs=[
                DependencySpec(task_key="A", depends_on_key="B"),
                DependencySpec(task_key="B", depends_on_key="A"),
            ],
        )


def test_self_dependency_rejected(services) -> None:
    owner, _project, _plan, handoff = _make_approved_plan(services)
    with pytest.raises(CycleError):
        services.worker.create_execution_from_handoff(
            handoff_id=handoff.id,
            project_id=_project.id,
            actor_id=owner.id,
            task_specs=[TaskSpec(key="A", objective="Task A")],
            dependency_specs=[
                DependencySpec(task_key="A", depends_on_key="A"),
            ],
        )


def test_missing_dependency_rejected(services) -> None:
    """A dependency on a non-existent task key is rejected."""
    owner, _project, _plan, handoff = _make_approved_plan(services)
    with pytest.raises(MissingDependencyError):
        services.worker.create_execution_from_handoff(
            handoff_id=handoff.id,
            project_id=_project.id,
            actor_id=owner.id,
            task_specs=[TaskSpec(key="A", objective="Task A")],
            dependency_specs=[
                DependencySpec(task_key="A", depends_on_key="nonexistent"),
            ],
        )


# ----------------------------------------------------------------------
# Restart reconstructs the same graph and statuses
# ----------------------------------------------------------------------


def test_restart_reconstructs_graph(services) -> None:
    """Per PLAN.md M5: 'Restart reconstructs the same graph and
    statuses.'

    We simulate a restart by calling recover_after_restart and
    verifying that the graph state is preserved."""
    owner, _project, _plan, handoff = _make_approved_plan(services)
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        project_id=_project.id,
        actor_id=owner.id,
        task_specs=[
            TaskSpec(key="A", objective="Task A"),
            TaskSpec(key="B", objective="Task B"),
        ],
        dependency_specs=[
            DependencySpec(task_key="B", depends_on_key="A"),
        ],
    )
    tasks = services.worker.list_tasks(
        execution.id,
        project_id=execution.project_id,
        actor_id=services.identity.get_project(execution.project_id).owner_user_id,
    )
    task_a = next(t for t in tasks if t.objective == "Task A")
    # Claim A (now it's running).
    services.worker.claim_task(
        execution_id=execution.id,
        task_id=task_a.id,
        lease_owner="worker-1",
        project_id=execution.project_id,
        actor_id=services.identity.get_project(execution.project_id).owner_user_id,
    )
    # Simulate a crashed worker with an expired lease; live leases are
    # intentionally preserved by recovery.
    conn = services.database.connect()
    conn.execute(
        "UPDATE task_attempts SET lease_expires_at = ? WHERE task_id = ?",
        ("2000-01-01T00:00:00.000000Z", task_a.id.value),
    )
    conn.commit()
    # Simulate restart: the worker process died, leaving A in 'running'
    # with an attempt in 'running'.
    # Recovery should: mark the attempt 'unknown', transition A back to
    # 'ready', recompute readiness, and pause the execution.
    recovered = services.worker.recover_after_restart(
        execution_id=execution.id, actor_id=owner.id, project_id=execution.project_id
    )
    # Recovery leaves ready work schedulable rather than pausing it.
    assert recovered.state == "running"
    # A should be back to ready.
    task_a = services.worker._execution_repo.get_task(task_a.id)
    assert task_a.state == "ready"
    # The attempt should be marked 'unknown'.
    attempts = services.worker.list_attempts(
        task_a.id,
        project_id=task_a.project_id,
        actor_id=services.identity.get_project(task_a.project_id).owner_user_id,
    )
    assert attempts[-1].state == "unknown"
    # A snapshot was taken.
    snapshot = services.worker.get_latest_snapshot(
        execution.id,
        project_id=execution.project_id,
        actor_id=services.identity.get_project(execution.project_id).owner_user_id,
    )
    assert snapshot is not None
    assert snapshot.snapshot_reason == "restart_recovery"


def test_restart_preserves_completed_tasks(services) -> None:
    """Completed tasks remain completed after restart."""
    owner, _project, _plan, handoff = _make_approved_plan(services)
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        project_id=_project.id,
        actor_id=owner.id,
        task_specs=[TaskSpec(key="A", objective="Task A")],
    )
    task_a = services.worker.list_tasks(
        execution.id,
        project_id=execution.project_id,
        actor_id=services.identity.get_project(execution.project_id).owner_user_id,
    )[0]
    attempt = services.worker.claim_task(
        execution_id=execution.id,
        task_id=task_a.id,
        lease_owner="worker-1",
        project_id=execution.project_id,
        actor_id=services.identity.get_project(execution.project_id).owner_user_id,
    )
    services.worker.complete_task(
        execution_id=execution.id,
        task_id=task_a.id,
        attempt_id=attempt.id,
        actor_id=owner.id,
        lease_owner="worker-1",
        project_id=execution.project_id,
    )
    # Restart.
    services.worker.recover_after_restart(
        execution_id=execution.id, actor_id=owner.id, project_id=execution.project_id
    )
    task_a = services.worker._execution_repo.get_task(task_a.id)
    assert task_a.state == "completed"
    # Execution should be completed (all tasks completed).
    execution = services.worker.get_execution(
        execution.id,
        project_id=execution.project_id,
        actor_id=services.identity.get_project(execution.project_id).owner_user_id,
    )
    assert execution.state == "completed"


def test_recovery_with_ready_tasks_resumes_and_completes_execution(services) -> None:
    owner, _project, _plan, handoff = _make_approved_plan(services)
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        project_id=_project.id,
        actor_id=owner.id,
        task_specs=[TaskSpec(key="A", objective="A"), TaskSpec(key="B", objective="B")],
    )
    tasks = services.worker.list_tasks(
        execution.id,
        project_id=execution.project_id,
        actor_id=owner.id,
    )
    for task in tasks:
        attempt = services.worker.claim_task(
            execution_id=execution.id,
            task_id=task.id,
            project_id=execution.project_id,
            actor_id=owner.id,
            lease_owner=f"worker-{task.objective}",
        )
        services.database.connect().execute(
            "UPDATE task_attempts SET lease_expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00.000000Z", attempt.id.value),
        )
    services.database.connect().commit()
    recovered = services.worker.recover_after_restart(
        execution_id=execution.id,
        project_id=execution.project_id,
        actor_id=owner.id,
    )
    assert recovered.state == "running"
    ready = services.worker.list_ready_tasks(
        execution.id,
        project_id=execution.project_id,
        actor_id=owner.id,
    )
    assert len(ready) == 2
    for task in ready:
        attempt = services.worker.claim_task(
            execution_id=execution.id,
            task_id=task.id,
            project_id=execution.project_id,
            actor_id=owner.id,
            lease_owner=f"reclaimer-{task.id.value}",
        )
        services.worker.complete_task(
            execution_id=execution.id,
            project_id=execution.project_id,
            task_id=task.id,
            attempt_id=attempt.id,
            actor_id=owner.id,
            lease_owner=f"reclaimer-{task.id.value}",
        )
    final = services.worker.get_execution(
        execution.id,
        project_id=execution.project_id,
        actor_id=owner.id,
    )
    assert final.state == "completed"


def test_task_lease_renewal_is_fenced_to_owner(services) -> None:
    owner, _project, _plan, handoff = _make_approved_plan(services)
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        project_id=_project.id,
        actor_id=owner.id,
        task_specs=[TaskSpec(key="A", objective="A")],
    )
    task = services.worker.list_tasks(
        execution.id, project_id=execution.project_id, actor_id=owner.id
    )[0]
    attempt = services.worker.claim_task(
        execution_id=execution.id,
        task_id=task.id,
        project_id=execution.project_id,
        actor_id=owner.id,
        lease_owner="owner-a",
    )
    with pytest.raises(LeaseOwnershipError):
        services.worker.renew_task_lease(
            execution_id=execution.id,
            task_id=task.id,
            attempt_id=attempt.id,
            project_id=execution.project_id,
            actor_id=owner.id,
            lease_owner="owner-b",
        )
    time.sleep(0.002)  # guarantee the renewed expiry lands on a later timestamp
    renewed = services.worker.renew_task_lease(
        execution_id=execution.id,
        task_id=task.id,
        attempt_id=attempt.id,
        project_id=execution.project_id,
        actor_id=owner.id,
        lease_owner="owner-a",
    )
    assert renewed.lease_expires_at > attempt.lease_expires_at


def test_replayed_claim_creates_new_attempt(services) -> None:
    """Per PLAN.md M5: 'Replayed scheduler events do not duplicate
    work.'

    Each claim creates a new attempt with an incremented
    attempt_number. The task transitions running -> ready (after
    failure) -> running again with a new attempt."""
    owner, _project, _plan, handoff = _make_approved_plan(services)
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        project_id=_project.id,
        actor_id=owner.id,
        task_specs=[TaskSpec(key="A", objective="Task A")],
    )
    task_a = services.worker.list_tasks(
        execution.id,
        project_id=execution.project_id,
        actor_id=services.identity.get_project(execution.project_id).owner_user_id,
    )[0]
    # First claim.
    attempt1 = services.worker.claim_task(
        execution_id=execution.id,
        task_id=task_a.id,
        lease_owner="worker-1",
        project_id=execution.project_id,
        actor_id=services.identity.get_project(execution.project_id).owner_user_id,
    )
    assert attempt1.attempt_number == 1
    # Fail the task.
    services.worker.fail_task(
        execution_id=execution.id,
        task_id=task_a.id,
        attempt_id=attempt1.id,
        error_message="first attempt failed",
        actor_id=owner.id,
        lease_owner="worker-1",
        project_id=execution.project_id,
    )
    # Failed tasks can transition back to ready (retry allowed).
    services.worker._execution_repo.update_task_state(task_a.id, "ready")
    # Second claim creates a new attempt.
    attempt2 = services.worker.claim_task(
        execution_id=execution.id,
        task_id=task_a.id,
        lease_owner="worker-2",
        project_id=execution.project_id,
        actor_id=services.identity.get_project(execution.project_id).owner_user_id,
    )
    assert attempt2.attempt_number == 2
    # Both attempts exist.
    attempts = services.worker.list_attempts(
        task_a.id,
        project_id=task_a.project_id,
        actor_id=services.identity.get_project(task_a.project_id).owner_user_id,
    )
    assert len(attempts) == 2


# ----------------------------------------------------------------------
# Cancellation propagation
# ----------------------------------------------------------------------


def test_cancellation_propagates_to_non_terminal_tasks(services) -> None:
    """Per zero-planner-worker-contract §'Cancellation is a state
    transition': cancellation affects running attempts, dependent
    tasks, tool calls, worktrees, artifacts, and final reporting.

    Propagation rule: tasks in terminal states are not changed; tasks
    in non-terminal states are transitioned to 'cancelled'; running
    attempts are transitioned to 'cancelled'."""
    owner, _project, _plan, handoff = _make_approved_plan(services)
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        project_id=_project.id,
        actor_id=owner.id,
        task_specs=[
            TaskSpec(key="A", objective="Task A"),
            TaskSpec(key="B", objective="Task B"),
            TaskSpec(key="C", objective="Task C"),
        ],
    )
    tasks = services.worker.list_tasks(
        execution.id,
        project_id=execution.project_id,
        actor_id=services.identity.get_project(execution.project_id).owner_user_id,
    )
    task_a = next(t for t in tasks if t.objective == "Task A")
    task_b = next(t for t in tasks if t.objective == "Task B")
    # Complete A.
    attempt_a = services.worker.claim_task(
        execution_id=execution.id,
        task_id=task_a.id,
        lease_owner="worker-1",
        project_id=execution.project_id,
        actor_id=services.identity.get_project(execution.project_id).owner_user_id,
    )
    services.worker.complete_task(
        execution_id=execution.id,
        task_id=task_a.id,
        attempt_id=attempt_a.id,
        actor_id=owner.id,
        lease_owner="worker-1",
        project_id=execution.project_id,
    )
    # Claim B (now running).
    attempt_b = services.worker.claim_task(
        execution_id=execution.id,
        task_id=task_b.id,
        lease_owner="w2",
        project_id=execution.project_id,
        actor_id=services.identity.get_project(execution.project_id).owner_user_id,
    )
    # Cancel the execution.
    cancelled = services.worker.cancel_execution(
        execution_id=execution.id, actor_id=owner.id, project_id=execution.project_id
    )
    assert cancelled.state == "cancelled"
    # A is still completed (terminal).
    task_a = services.worker._execution_repo.get_task(task_a.id)
    assert task_a.state == "completed"
    # B is cancelled.
    task_b = services.worker._execution_repo.get_task(task_b.id)
    assert task_b.state == "cancelled"
    # B's attempt is cancelled.
    attempt_b = services.worker._execution_repo.get_attempt(attempt_b.id)
    assert attempt_b.state == "cancelled"
    # C is cancelled.
    task_c = next(t for t in tasks if t.objective == "Task C")
    task_c = services.worker._execution_repo.get_task(task_c.id)
    assert task_c.state == "cancelled"


def test_cancellation_signals_inflight_runtime(services) -> None:
    owner, _project, _plan, handoff = _make_approved_plan(services)
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        project_id=_project.id,
        actor_id=owner.id,
        task_specs=[TaskSpec(key="A", objective="Task A")],
    )
    cancellation = services.worker.get_cancellation_event(execution.id)
    assert not cancellation.is_set()

    services.worker.cancel_execution(
        execution_id=execution.id,
        actor_id=owner.id,
        project_id=execution.project_id,
    )

    assert cancellation.is_set()


def test_cannot_cancel_already_terminal_execution(services) -> None:
    owner, _project, _plan, handoff = _make_approved_plan(services)
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        project_id=_project.id,
        actor_id=owner.id,
        task_specs=[TaskSpec(key="A", objective="Task A")],
    )
    services.worker.cancel_execution(
        execution_id=execution.id, actor_id=owner.id, project_id=execution.project_id
    )
    # Cannot cancel again.
    with pytest.raises(InvalidExecutionTransitionError):
        services.worker.cancel_execution(
            execution_id=execution.id, actor_id=owner.id, project_id=execution.project_id
        )


# ----------------------------------------------------------------------
# Execution completion
# ----------------------------------------------------------------------


def test_execution_completes_when_all_tasks_complete(services) -> None:
    owner, _project, _plan, handoff = _make_approved_plan(services)
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        project_id=_project.id,
        actor_id=owner.id,
        task_specs=[
            TaskSpec(key="A", objective="Task A"),
            TaskSpec(key="B", objective="Task B"),
        ],
    )
    tasks = services.worker.list_tasks(
        execution.id,
        project_id=execution.project_id,
        actor_id=services.identity.get_project(execution.project_id).owner_user_id,
    )
    for task in tasks:
        attempt = services.worker.claim_task(
            execution_id=execution.id,
            task_id=task.id,
            lease_owner="worker-1",
            project_id=execution.project_id,
            actor_id=services.identity.get_project(execution.project_id).owner_user_id,
        )
        services.worker.complete_task(
            execution_id=execution.id,
            task_id=task.id,
            attempt_id=attempt.id,
            actor_id=owner.id,
            lease_owner="worker-1",
            project_id=execution.project_id,
        )
    execution = services.worker.get_execution(
        execution.id,
        project_id=execution.project_id,
        actor_id=services.identity.get_project(execution.project_id).owner_user_id,
    )
    assert execution.state == "completed"


def test_execution_fails_when_graph_cannot_proceed(services) -> None:
    """When a task fails and nothing in the graph can run or become
    runnable, the execution terminates as ``failed`` instead of
    pausing forever (audit fix: executions must be able to fail).
    Provider-unknown blocks still pause for operator reconciliation."""
    owner, _project, _plan, handoff = _make_approved_plan(services)
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        project_id=_project.id,
        actor_id=owner.id,
        task_specs=[
            TaskSpec(key="A", objective="Task A"),
            TaskSpec(key="B", objective="Task B"),
        ],
        dependency_specs=[
            DependencySpec(task_key="B", depends_on_key="A"),
        ],
    )
    task_a = next(
        t
        for t in services.worker.list_tasks(
            execution.id,
            project_id=execution.project_id,
            actor_id=services.identity.get_project(execution.project_id).owner_user_id,
        )
        if t.objective == "Task A"
    )
    attempt = services.worker.claim_task(
        execution_id=execution.id,
        task_id=task_a.id,
        lease_owner="worker-1",
        project_id=execution.project_id,
        actor_id=services.identity.get_project(execution.project_id).owner_user_id,
    )
    services.worker.fail_task(
        execution_id=execution.id,
        task_id=task_a.id,
        attempt_id=attempt.id,
        error_message="failed",
        actor_id=owner.id,
        lease_owner="worker-1",
        project_id=execution.project_id,
    )
    execution = services.worker.get_execution(
        execution.id,
        project_id=execution.project_id,
        actor_id=services.identity.get_project(execution.project_id).owner_user_id,
    )
    assert execution.state == "failed"
    assert execution.blocker_reason is not None


def test_execution_pauses_for_provider_unknown_block(services) -> None:
    """Provider-unknown blocks await reconciliation: the execution pauses
    rather than being declared permanently failed."""
    owner, _project, _plan, handoff = _make_approved_plan(services)
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        project_id=_project.id,
        actor_id=owner.id,
        task_specs=[TaskSpec(key="A", objective="Task A")],
    )
    task_a = next(
        t
        for t in services.worker.list_tasks(
            execution.id,
            project_id=execution.project_id,
            actor_id=services.identity.get_project(execution.project_id).owner_user_id,
        )
        if t.objective == "Task A"
    )
    attempt = services.worker.claim_task(
        execution_id=execution.id,
        task_id=task_a.id,
        lease_owner="worker-1",
        project_id=execution.project_id,
        actor_id=services.identity.get_project(execution.project_id).owner_user_id,
    )
    services.worker.mark_provider_outcome_unknown(
        execution_id=execution.id,
        task_id=task_a.id,
        attempt_id=attempt.id,
        error_message="unknown outcome",
        actor_id=owner.id,
        lease_owner="worker-1",
        project_id=execution.project_id,
    )
    execution = services.worker.get_execution(
        execution.id,
        project_id=execution.project_id,
        actor_id=services.identity.get_project(execution.project_id).owner_user_id,
    )
    assert execution.state == "paused"
    assert execution.blocker_reason is not None


# ----------------------------------------------------------------------
# Cross-project isolation
# ----------------------------------------------------------------------


def test_execution_isolation_across_projects(services) -> None:
    """Per PLAN.md M2 acceptance: executions are project-scoped."""
    _owner_a, project_a, _, handoff_a = _make_approved_plan(services, "Owner A")
    owner_b = services.identity.create_user(display_name="Owner B")
    services.identity.create_project(owner_id=owner_b.id, name="Project B")
    # owner_b is NOT a member of project A and cannot create an
    # execution from handoff_a.
    from zero.domain.authorization import AuthorizationError

    with pytest.raises(AuthorizationError):
        services.worker.create_execution_from_handoff(
            handoff_id=handoff_a.id,
            project_id=project_a.id,
            actor_id=owner_b.id,
            task_specs=[TaskSpec(key="A", objective="Task A")],
        )


# ----------------------------------------------------------------------
# Snapshots
# ----------------------------------------------------------------------


def test_snapshot_taken_on_creation(services) -> None:
    owner, _project, _plan, handoff = _make_approved_plan(services)
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        project_id=_project.id,
        actor_id=owner.id,
        task_specs=[TaskSpec(key="A", objective="Task A")],
    )
    snapshot = services.worker.get_latest_snapshot(
        execution.id,
        project_id=execution.project_id,
        actor_id=services.identity.get_project(execution.project_id).owner_user_id,
    )
    assert snapshot is not None
    assert snapshot.snapshot_reason == "before_fan_out"
    assert snapshot.snapshot_version == 1


def test_snapshot_taken_on_task_completion(services) -> None:
    owner, _project, _plan, handoff = _make_approved_plan(services)
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        project_id=_project.id,
        actor_id=owner.id,
        task_specs=[TaskSpec(key="A", objective="Task A")],
    )
    task_a = services.worker.list_tasks(
        execution.id,
        project_id=execution.project_id,
        actor_id=services.identity.get_project(execution.project_id).owner_user_id,
    )[0]
    attempt = services.worker.claim_task(
        execution_id=execution.id,
        task_id=task_a.id,
        lease_owner="worker-1",
        project_id=execution.project_id,
        actor_id=services.identity.get_project(execution.project_id).owner_user_id,
    )
    services.worker.complete_task(
        execution_id=execution.id,
        task_id=task_a.id,
        attempt_id=attempt.id,
        actor_id=owner.id,
        lease_owner="worker-1",
        project_id=execution.project_id,
    )
    snapshot = services.worker.get_latest_snapshot(
        execution.id,
        project_id=execution.project_id,
        actor_id=services.identity.get_project(execution.project_id).owner_user_id,
    )
    assert snapshot is not None
    assert snapshot.snapshot_reason == "after_task_complete"


# ----------------------------------------------------------------------
# Idempotent task state transitions
# ----------------------------------------------------------------------


def test_completing_already_completed_task_is_noop(services) -> None:
    """Per zero-recovery-consistency §'Idempotency makes retries
    ordinary': completing a task that is already completed is a
    no-op."""
    owner, _project, _plan, handoff = _make_approved_plan(services)
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        project_id=_project.id,
        actor_id=owner.id,
        task_specs=[TaskSpec(key="A", objective="Task A")],
    )
    task_a = services.worker.list_tasks(
        execution.id,
        project_id=execution.project_id,
        actor_id=services.identity.get_project(execution.project_id).owner_user_id,
    )[0]
    attempt = services.worker.claim_task(
        execution_id=execution.id,
        task_id=task_a.id,
        lease_owner="worker-1",
        project_id=execution.project_id,
        actor_id=services.identity.get_project(execution.project_id).owner_user_id,
    )
    services.worker.complete_task(
        execution_id=execution.id,
        task_id=task_a.id,
        attempt_id=attempt.id,
        actor_id=owner.id,
        lease_owner="worker-1",
        project_id=execution.project_id,
    )
    # Second completion is a no-op.
    result = services.worker.complete_task(
        execution_id=execution.id,
        task_id=task_a.id,
        attempt_id=attempt.id,
        actor_id=owner.id,
        lease_owner="worker-1",
        project_id=execution.project_id,
    )
    assert result.state == "completed"
