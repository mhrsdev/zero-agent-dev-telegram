"""Regression tests: execution result messages must carry goal + task text.

Real-run bug: the Telegram result delivery for a failed execution was a
bare "Execution exec_xxx finished with state: failed." line with no plan
goal, no task titles, and no error, because the formatter only knew
about tasks that ran inside the current tick (and the nothing-succeeded
re-raise in ``run_ready_tasks`` discards them all).
"""

from __future__ import annotations

from types import SimpleNamespace

from zero.app.scheduler_service import SchedulerService
from zero.app.services import build_services
from zero.config import Settings
from zero.domain.execution import ExecutionId, TaskId
from zero.domain.identity import ProjectId, UserId
from zero.domain.plans import PlanRevisionContent
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations


def _authorization():
    class Authorization:
        def require_permission(self, **_kwargs):
            return None

    return Authorization()


def _delivery_recorder():
    class Delivery:
        def __init__(self) -> None:
            self.enqueued: list[str] = []

        @property
        def is_outbound_configured(self) -> bool:
            return False

        def list_enabled_bindings(self, _project_id):
            return [SimpleNamespace(id=SimpleNamespace(value="ib_1"))]

        def enqueue_execution_result(self, *, content, **_kwargs):
            self.enqueued.append(content)
            return SimpleNamespace(id=SimpleNamespace(value="idl_1"))

    return Delivery()


def _failed_execution_namespace() -> SimpleNamespace:
    execution_id = ExecutionId("exec_failed_ctx")
    return SimpleNamespace(
        id=execution_id,
        project_id=ProjectId("p_ctx"),
        plan_revision_id=SimpleNamespace(value="prv_1"),
        state="failed",
        blocker_reason="task failed; graph cannot proceed",
    )


def _task_namespace(task_id: str, state: str, objective: str, blocker: str | None = None):
    return SimpleNamespace(
        id=TaskId(task_id),
        execution_id=ExecutionId("exec_failed_ctx"),
        state=state,
        objective=objective,
        blocker_reason=blocker,
    )


def _build_scheduler(plans, worker, runtime, delivery) -> SchedulerService:
    return SchedulerService(
        plans=plans,
        worker=worker,
        runtime=runtime,
        authorization=_authorization(),
        result_delivery=delivery,
    )


def test_failed_execution_message_includes_goal_objectives_and_error() -> None:
    """The r5 data-loss path: zero tick-local results, yet the delivery
    must still name the goal, every task objective/state, and the error."""
    execution = _failed_execution_namespace()
    project_id = execution.project_id

    class Plans:
        def list_unclaimed_handoffs(self, _project, *, limit, **_kwargs):
            return []

        def get_revision(self, _revision_id, **_kwargs):
            return SimpleNamespace(
                content=SimpleNamespace(objective="Ship the textcase helper library")
            )

    class Worker:
        def list_project_executions(self, *, project_id, **_kwargs):
            return [execution]

        def get_execution(self, _execution_id, **_kwargs):
            return execution

        def list_tasks(self, _execution_id, **_kwargs):
            return [
                _task_namespace("task_a", "completed", "Inspect the repository layout"),
                _task_namespace("task_b", "completed", "Create textcase/convert.py"),
                _task_namespace(
                    "task_c", "failed", "Delegate a sub-agent review of the rules"
                ),
                _task_namespace(
                    "task_d",
                    "blocked",
                    "Reconcile the sub-agent findings",
                    blocker="dependency failed/blocked/cancelled",
                ),
            ]

        def list_attempts(self, task_id, **_kwargs):
            assert task_id == TaskId("task_c")
            return [
                SimpleNamespace(
                    attempt_number=1,
                    error_message="ProviderError: runtime execution failed",
                )
            ]

    class Runtime:
        # Simulates the nothing-succeeded re-raise: no results survive.
        def run_ready_tasks(self, **_kwargs):
            return []

    delivery = _delivery_recorder()
    scheduler = _build_scheduler(Plans(), Worker(), Runtime(), delivery)

    scheduler.run_once(
        project_id=project_id,
        actor_id=UserId("zu_owner"),
        lease_owner="scheduler-test",
        provider="fake",
        model_name="fake-standard",
    )

    assert len(delivery.enqueued) == 1
    content = delivery.enqueued[0]
    assert "Execution exec_failed_ctx finished with state: failed." in content
    assert "Blocker: task failed; graph cannot proceed" in content
    assert "Goal: Ship the textcase helper library" in content
    assert "- [completed] Inspect the repository layout" in content
    assert "- [completed] Create textcase/convert.py" in content
    assert "- [failed] Delegate a sub-agent review of the rules" in content
    assert "  error: ProviderError: runtime execution failed" in content
    assert "- [blocked] Reconcile the sub-agent findings" in content
    assert "  blocked: dependency failed/blocked/cancelled" in content


def test_completed_execution_message_keeps_tick_response_content() -> None:
    """Tick-local response bodies remain in the message when present."""
    execution = _failed_execution_namespace()
    execution.state = "completed"
    execution.blocker_reason = None
    # During the drain the execution is still runnable; by delivery time
    # the durable state reads back as completed (same id).
    runnable = _failed_execution_namespace()
    runnable.state = "pending"
    runnable.blocker_reason = None
    project_id = execution.project_id
    task_id = TaskId("task_done")

    class Plans:
        def list_unclaimed_handoffs(self, _project, *, limit, **_kwargs):
            return []

        def get_revision(self, _revision_id, **_kwargs):
            return SimpleNamespace(content=SimpleNamespace(objective="Ship the helper"))

    class Worker:
        def list_project_executions(self, *, project_id, **_kwargs):
            return [runnable]

        def get_execution(self, _execution_id, **_kwargs):
            return execution

        def list_tasks(self, _execution_id, **_kwargs):
            return [_task_namespace("task_done", "completed", "Create the module")]

        def list_attempts(self, _task_id, **_kwargs):
            return []

    class Runtime:
        def run_ready_tasks(self, **_kwargs):
            return [
                SimpleNamespace(
                    task=SimpleNamespace(execution_id=execution.id, id=task_id),
                    response=SimpleNamespace(content="Added the helper module."),
                )
            ]

    delivery = _delivery_recorder()
    scheduler = _build_scheduler(Plans(), Worker(), Runtime(), delivery)

    scheduler.run_once(
        project_id=project_id,
        actor_id=UserId("zu_owner"),
        lease_owner="scheduler-test",
        provider="fake",
        model_name="fake-standard",
    )

    assert len(delivery.enqueued) == 1
    content = delivery.enqueued[0]
    assert "Goal: Ship the helper" in content
    assert "- [completed] Create the module" in content
    assert f"Result of task {task_id.value}: Added the helper module." in content


def test_message_degrades_gracefully_when_enrichment_fails() -> None:
    """Durable-state reads are advisory: failures must never break the
    delivery or the scheduler tick."""

    class Plans:
        def list_unclaimed_handoffs(self, _project, *, limit, **_kwargs):
            return []

        def get_revision(self, _revision_id, **_kwargs):
            raise RuntimeError("revision store unavailable")

    class Worker:
        def list_project_executions(self, *, project_id, **_kwargs):
            return [_failed_execution_namespace()]

        def get_execution(self, _execution_id, **_kwargs):
            return _failed_execution_namespace()

        def list_tasks(self, _execution_id, **_kwargs):
            raise RuntimeError("task store unavailable")

    class Runtime:
        def run_ready_tasks(self, **_kwargs):
            return []

    delivery = _delivery_recorder()
    scheduler = _build_scheduler(
        Plans(), Worker(), Runtime(), delivery
    )

    result = scheduler.run_once(
        project_id=ProjectId("p_ctx"),
        actor_id=UserId("zu_owner"),
        lease_owner="scheduler-test",
        provider="fake",
        model_name="fake-standard",
    )

    assert result.errors == ()
    assert len(delivery.enqueued) == 1
    content = delivery.enqueued[0]
    assert "finished with state: failed." in content
    assert "Blocker: task failed; graph cannot proceed" in content


def test_real_services_completed_delivery_includes_goal_and_task_line() -> None:
    """End-to-end with the real composition root: the pending delivery
    for a completed execution names the plan goal and the task."""
    settings = Settings.load_for_test()
    database = Database(settings)
    apply_migrations(database)
    services = build_services(settings, database)
    owner = services.identity.create_user(display_name="Delivery owner")
    project = services.identity.create_project(owner_id=owner.id, name="Delivery project")
    services.interfaces.create_binding(
        project_id=project.id,
        actor_id=owner.id,
        platform="telegram",
        chat_id="9200",
        is_enabled=True,
    )
    event = services.plans.ingest_conversation_event(
        project_id=project.id,
        actor_id=owner.id,
        source="web",
        origin_kind="authenticated_human",
        content="Implement the delivery feature.",
    )
    plan = services.plans.create_plan(project_id=project.id, actor_id=owner.id)
    services.plans.propose_revision(
        plan_id=plan.id,
        project_id=project.id,
        actor_id=owner.id,
        content=PlanRevisionContent(
            objective="Implement the delivery feature",
            scope=("src",),
            constraints=(),
            acceptance_criteria=("A provider response is recorded",),
            risks=(),
            unresolved_questions=(),
            source_event_ids=(event.id,),
        ),
    )
    services.plans.approve_revision(
        plan_id=plan.id,
        project_id=project.id,
        actor_id=owner.id,
        expected_revision_number=1,
        idempotency_key="delivery-approval",
    )

    result = services.scheduler.run_once(
        project_id=project.id,
        actor_id=owner.id,
        lease_owner="scheduler-test",
        provider="fake",
        model_name="fake-standard",
    )

    assert result.errors == ()
    pending = services.result_delivery.list_pending(project.id)
    assert len(pending) == 1
    content = pending[0].content
    assert "Goal: Implement the delivery feature" in content
    assert "- [completed] Implement the delivery feature" in content
