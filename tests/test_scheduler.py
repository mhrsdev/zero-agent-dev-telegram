from __future__ import annotations

from threading import Event
from types import SimpleNamespace
from typing import Any, cast

from zero.app.scheduler_service import SchedulerService, SchedulerTickResult
from zero.app.services import build_services
from zero.config import Settings
from zero.domain.execution import ExecutionId, TaskId
from zero.domain.identity import ProjectId, UserId
from zero.domain.plans import PlanRevisionContent
from zero.domain.worktrees import RepositoryId
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations


def test_scheduler_claims_approved_handoff_and_drains_ready_task() -> None:
    settings = Settings.load_for_test()
    database = Database(settings)
    apply_migrations(database)
    services = build_services(settings, database)
    owner = services.identity.create_user(display_name="Scheduler owner")
    project = services.identity.create_project(owner_id=owner.id, name="Scheduler project")
    services.interfaces.create_binding(
        project_id=project.id,
        actor_id=owner.id,
        platform="telegram",
        chat_id="9100",
        is_enabled=True,
    )
    event = services.plans.ingest_conversation_event(
        project_id=project.id,
        actor_id=owner.id,
        source="web",
        origin_kind="authenticated_human",
        content="Implement the feature.",
    )
    plan = services.plans.create_plan(project_id=project.id, actor_id=owner.id)
    services.plans.propose_revision(
        plan_id=plan.id,
        project_id=project.id,
        actor_id=owner.id,
        content=PlanRevisionContent(
            objective="Implement the feature",
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
        idempotency_key="scheduler-approval",
    )

    result = services.scheduler.run_once(
        project_id=project.id,
        actor_id=owner.id,
        lease_owner="scheduler-test",
        provider="fake",
        model_name="fake-standard",
    )

    assert result.handoffs_claimed == 1
    assert result.tasks_run == 1
    assert result.errors == ()
    execution = services.worker._execution_repo.get_execution_for_revision(handoff.revision_id)
    assert execution is not None
    assert execution.state == "completed"
    pending_deliveries = services.result_delivery.list_pending(project.id)
    assert len(pending_deliveries) == 1
    assert pending_deliveries[0].execution_id == execution.id.value


def test_scheduler_requests_automatic_combined_test_after_review() -> None:
    project_id = ProjectId("p_scheduler")
    actor_id = UserId("zu_scheduler")
    execution_id = ExecutionId("exec_scheduler")
    task_id = TaskId("task_scheduler")
    repository_id = RepositoryId("repo_scheduler")
    calls: list[tuple[str, tuple[str, ...]]] = []
    proposal_calls: list[tuple[str, tuple[str, ...]]] = []

    class Authorization:
        def require_permission(self, **_kwargs):
            return None

    class Plans:
        def list_unclaimed_handoffs(self, _project, *, limit, **_kwargs):
            return []

    class Worker:
        def list_project_executions(self, *, project_id, **_kwargs):
            return [SimpleNamespace(id=execution_id, state="pending")]

        def get_execution(self, _execution_id, **_kwargs):
            return SimpleNamespace(id=execution_id, project_id=project_id, state="completed")

        def list_tasks(self, _execution_id, **_kwargs):
            return [SimpleNamespace(id=task_id, state="completed")]

    class Runtime:
        def run_ready_tasks(self, **_kwargs):
            return []

    class Integration:
        def list_reviews(self, _execution_id, **_kwargs):
            return []

        def create_review(self, **_kwargs):
            return SimpleNamespace(id=SimpleNamespace(value="irev_scheduler"))

        def run_combined_tests(self, *, command, args, **_kwargs):
            calls.append((command, args))
            return SimpleNamespace(
                id=SimpleNamespace(value="irev_scheduler"),
                state="approved",
            )

        def create_merge_proposal(self, *, source_tasks, **_kwargs):
            proposal_calls.append(("create", tuple(task.value for task in source_tasks)))
            return SimpleNamespace(
                id=SimpleNamespace(value="mp_scheduler"),
                integration_review_id=SimpleNamespace(value="irev_scheduler"),
            )

        def list_proposals(self, _execution_id, **_kwargs):
            return []

    scheduler = SchedulerService(
        plans=Plans(),
        worker=Worker(),
        runtime=Runtime(),
        authorization=Authorization(),
        integration=Integration(),
    )
    result = scheduler.run_once(
        project_id=project_id,
        actor_id=actor_id,
        lease_owner="scheduler-test",
        provider="fake",
        model_name="fake-standard",
        repository_id=repository_id,
        combined_test_command="sh",
        combined_test_args=("-c", "true"),
    )

    assert result.integration_review_ids == ("irev_scheduler",)
    assert result.merge_proposal_ids == ("mp_scheduler",)
    assert calls == [("sh", ("-c", "true"))]
    assert proposal_calls == [("create", ("task_scheduler",))]


def test_scheduler_run_forever_stops_after_supervised_tick() -> None:
    stop_event = Event()
    calls: list[dict[str, object]] = []

    class OneTickScheduler(SchedulerService):
        def run_once(self, **kwargs):
            calls.append(kwargs)
            stop_event.set()
            return SchedulerTickResult(
                handoffs_claimed=0,
                executions_seen=0,
                tasks_run=0,
                task_results=(),
                integration_review_ids=(),
                merge_proposal_ids=(),
                errors=(),
            )

    ticks: list[SchedulerTickResult] = []
    OneTickScheduler(
        plans=cast(Any, None),
        worker=cast(Any, None),
        runtime=cast(Any, None),
        authorization=cast(Any, None),
    ).run_forever(
        project_id=ProjectId("p_forever"),
        actor_id=UserId("zu_forever"),
        lease_owner="scheduler-forever",
        provider="fake",
        model_name="fake-standard",
        combined_test_command="sh",
        combined_test_args=("-c", "true"),
        interval_seconds=0.1,
        stop_event=stop_event,
        on_tick=ticks.append,
    )

    assert len(calls) == 1
    assert calls[0]["combined_test_command"] == "sh"
    assert calls[0]["combined_test_args"] == ("-c", "true")
    assert len(ticks) == 1
