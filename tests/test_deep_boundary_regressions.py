"""Regression tests for the remaining cross-project execution boundaries."""

from __future__ import annotations

import pytest

from zero.app.services import build_services
from zero.app.worker_service import TaskSpec
from zero.config import Settings
from zero.domain.authorization import AuthorizationError
from zero.domain.plans import PlanRevisionContent
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations


@pytest.fixture
def services(test_settings: Settings):
    database = Database(test_settings)
    apply_migrations(database)
    return build_services(test_settings, database)


def _make_execution(services):
    owner = services.identity.create_user(display_name="Boundary owner")
    project = services.identity.create_project(owner_id=owner.id, name="Boundary project")
    event = services.plans.ingest_conversation_event(
        project_id=project.id,
        actor_id=owner.id,
        source="web",
        origin_kind="authenticated_human",
        content="Create a task.",
    )
    plan = services.plans.create_plan(project_id=project.id, actor_id=owner.id)
    content = PlanRevisionContent(
        objective="Create a task",
        scope=("src",),
        constraints=(),
        acceptance_criteria=("The task is complete",),
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
        idempotency_key="boundary-approval",
    )
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        project_id=project.id,
        actor_id=owner.id,
        task_specs=[TaskSpec(key="task", objective="Do the task")],
    )
    return owner, project, execution


def test_worker_reads_require_explicit_authorized_project_scope(services) -> None:
    owner, project, execution = _make_execution(services)
    outsider = services.identity.create_user(display_name="Boundary outsider")

    with pytest.raises(AuthorizationError):
        services.worker.list_tasks(
            execution.id,
            project_id=project.id,
            actor_id=outsider.id,
        )

    assert services.worker.list_tasks(
        execution.id,
        project_id=project.id,
        actor_id=owner.id,
    )

    with pytest.raises(AuthorizationError):
        services.worker.list_tasks(execution.id)


def test_plan_reads_require_authorized_project_scope(services) -> None:
    owner, project, _execution = _make_execution(services)
    outsider = services.identity.create_user(display_name="Plan outsider")
    plan = services.plans.list_plans_for_project(
        project.id,
        actor_id=owner.id,
    )[0]

    with pytest.raises(AuthorizationError):
        services.plans.get_plan(
            plan.id,
            project_id=project.id,
            actor_id=outsider.id,
        )


def test_worker_claim_requires_authorized_actor_before_task_lookup(services) -> None:
    owner, project, execution = _make_execution(services)
    task = services.worker.list_tasks(
        execution.id,
        project_id=project.id,
        actor_id=owner.id,
    )[0]
    outsider = services.identity.create_user(display_name="Claim outsider")

    with pytest.raises(AuthorizationError):
        services.worker.claim_task(
            execution_id=execution.id,
            task_id=task.id,
            project_id=project.id,
            actor_id=outsider.id,
            lease_owner="outsider-worker",
        )


def test_conversation_events_redact_json_style_credentials_before_persistence(services) -> None:
    owner = services.identity.create_user(display_name="Conversation owner")
    project = services.identity.create_project(owner_id=owner.id, name="Conversation project")

    event = services.plans.ingest_conversation_event(
        project_id=project.id,
        actor_id=owner.id,
        source="web",
        origin_kind="authenticated_human",
        content='Please use {"token": "synthetic-conversation-credential"}.',
    )

    stored = services.plans.list_conversation_events(
        project_id=project.id,
        actor_id=owner.id,
    )
    saved = next(item for item in stored if item.id == event.id)
    assert "synthetic-conversation-credential" not in saved.content
    assert "[REDACTED]" in saved.content
