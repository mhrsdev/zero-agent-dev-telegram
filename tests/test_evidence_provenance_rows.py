"""Regression: evidence validation must honor per-store provenance rows.

Real run (2026-08-28): store_artifact deduplicates by content hash, so a
task that produced a byte-identical diff to an EARLIER attempt received
the earlier artifact row, whose `provenance` COLUMN carried the earlier
task/attempt identity. `_validate_evidence_artifacts` read only that
column and failed the task with "evidence artifact … does not belong to
task …". The artifact-provenance model explicitly keeps provenance
independent of content dedup: every store appends its own
``artifact_provenance`` row. Validation now accepts an artifact when any
provenance row matches the validating task/attempt.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from zero.domain.execution import TaskAttemptId


@pytest.fixture()
def evidence_world(test_settings):
    from zero.app.services import build_services
    from zero.persistence.connection import Database
    from zero.persistence.migrations import apply_migrations

    database = Database(test_settings)
    apply_migrations(database)
    return build_services(test_settings, database)


def _make_task(services):
    from zero.app.worker_service import TaskSpec
    from zero.domain.plans import PlanRevisionContent

    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(owner_id=owner.id, name="P")
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
        acceptance_criteria=("Works",),
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
        idempotency_key="k1",
    )
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        project_id=project.id,
        actor_id=owner.id,
        task_specs=[TaskSpec(key="A", objective="Do it")],
    )
    task = services.worker.list_tasks(
        execution.id, project_id=project.id, actor_id=owner.id
    )[0]
    # Attempts are created when a task is claimed; the validator only
    # compares attempt_id VALUES inside provenance JSON, so a synthetic
    # id keeps this test focused on the matching logic.
    attempt = SimpleNamespace(id=TaskAttemptId("att_test_attempt_1"))
    return owner, project, execution, task, attempt


def _provenance(execution, task, attempt_id, label):
    return json.dumps(
        {
            "execution_id": execution.id.value,
            "task_id": task.id.value,
            "attempt_id": attempt_id,
            "evidence_labels": [label],
        },
        sort_keys=True,
    )


def test_deduped_artifact_validates_for_later_attempt(evidence_world):
    """Same content stored by a later attempt must validate for it."""
    services = evidence_world
    owner, project, execution, task, attempt = _make_task(services)
    attempt_id = attempt.id.value if attempt is not None else "att_manual"

    diff_payload = "--- Status (includes untracked) ---\n?? pkg/\n"

    # Attempt 1 (a previous run) stored the identical diff first.
    first = services.artifacts.store_artifact(
        project_id=project.id,
        actor_id=owner.id,
        kind="diff",
        content=diff_payload,
        producer="agent-runtime:earlier",
        provenance=_provenance(execution, task, "att_earlier", "diff"),
        source="system",
    )
    # This attempt stores the SAME bytes: dedup returns `first`...
    second = services.artifacts.store_artifact(
        project_id=project.id,
        actor_id=owner.id,
        kind="diff",
        content=diff_payload,
        producer="agent-runtime:current",
        provenance=_provenance(execution, task, attempt_id, "diff"),
        source="system",
    )
    assert second.id == first.id, "precondition: content dedup reuses the row"
    # ...but the per-store provenance row exists.
    rows = services.artifacts.list_provenance(
        project_id=project.id, artifact_id=second.id, actor_id=owner.id, source="system"
    )
    assert any(
        json.loads(row.provenance or "{}").get("attempt_id") == attempt_id
        for row in rows
    ), "store_artifact must append a provenance row for the current store"

    # Validation passes for the current attempt thanks to the rows.
    validated = services.worker._validate_evidence_artifacts(
        task=task,
        attempt_id=attempt.id,
        evidence_artifact_ids=(second.id,),
    )
    assert validated == (second.id.value,)


def test_foreign_artifact_still_rejected(evidence_world):
    """An artifact never stored for this task/attempt remains invalid."""
    services = evidence_world
    owner, project, execution, task, attempt = _make_task(services)
    foreign = services.artifacts.store_artifact(
        project_id=project.id,
        actor_id=owner.id,
        kind="diff",
        content="unrelated content from another task entirely",
        producer="agent-runtime:other",
        provenance=_provenance(execution, task, "att_other", "diff"),
        source="system",
    )
    from zero.app.worker_service import MissingEvidenceError

    with pytest.raises(MissingEvidenceError, match="does not belong"):
        services.worker._validate_evidence_artifacts(
            task=task,
            attempt_id=attempt.id,
            evidence_artifact_ids=(foreign.id,),
        )
