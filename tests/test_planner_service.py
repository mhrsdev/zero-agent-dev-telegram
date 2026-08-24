from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from zero.app.planner_service import PlannerService
from zero.app.services import build_services
from zero.config import Settings
from zero.domain.providers import CanonicalResponse
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations


def test_planner_turns_actionable_human_event_into_proposed_revision() -> None:
    settings = Settings.load_for_test()
    database = Database(settings)
    apply_migrations(database)
    services = build_services(settings, database)
    owner = services.identity.create_user(display_name="Planner owner")
    project = services.identity.create_project(owner_id=owner.id, name="Planner project")
    event = services.plans.ingest_conversation_event(
        project_id=project.id,
        actor_id=owner.id,
        source="web",
        origin_kind="authenticated_human",
        content="Please add a health endpoint and tests.",
    )

    def provider(**_kwargs):
        return (
            SimpleNamespace(id=SimpleNamespace(value="preq_planner_test")),
            CanonicalResponse(
                content=json.dumps(
                    {
                        "actionable": True,
                        "objective": "Add a health endpoint and tests",
                        "scope": ["src", "tests"],
                        "constraints": ["Do not expose secrets"],
                        "acceptance_criteria": ["health endpoint returns 200", "tests pass"],
                        "risks": [],
                        "unresolved_questions": [],
                    }
                )
            ),
        )

    services.providers.send_request = provider
    planner = PlannerService(services.plans, services.providers)
    revision = planner.propose_from_event(
        event_id=event.id,
        actor_id=owner.id,
        provider="fake",
        model_name="fake-standard",
    )

    assert revision is not None
    assert revision.state == "proposed"
    assert revision.content.source_event_ids == (event.id,)
    assert (
        services.plans.list_plans(project_id=project.id, actor_id=owner.id)[0].current_state
        == "proposed"
    )


def test_planner_rejects_non_actionable_or_malformed_model_output() -> None:
    settings = Settings.load_for_test()
    database = Database(settings)
    apply_migrations(database)
    services = build_services(settings, database)
    owner = services.identity.create_user(display_name="Planner owner")
    project = services.identity.create_project(owner_id=owner.id, name="Planner project")
    event = services.plans.ingest_conversation_event(
        project_id=project.id,
        actor_id=owner.id,
        source="web",
        origin_kind="authenticated_human",
        content="Thanks, that is useful context.",
    )

    services.providers.send_request = lambda **_kwargs: (
        SimpleNamespace(id=SimpleNamespace(value="preq_planner_test")),
        CanonicalResponse(content="not json"),
    )
    planner = PlannerService(services.plans, services.providers)
    with pytest.raises(Exception, match="valid JSON"):
        planner.propose_from_event(
            event_id=event.id,
            actor_id=owner.id,
            provider="fake",
            model_name="fake-standard",
        )
    assert services.plans.list_plans(project_id=project.id, actor_id=owner.id) == []
