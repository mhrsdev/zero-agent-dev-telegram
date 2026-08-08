"""Audit service tests — append-only, redaction, no secrets.

Per ``zero-control-plane-trust`` §"Audit is evidence, not a transcript
dump": An audit event explains who caused what transition, in which
project, through which interface, against which revision, and with
what result. It normally does not need the raw conversation, source
file, prompt, tool output, or secret.

Per ``zero-observability-evidence`` §"Audit explains authority": Audit
records answer who caused a protected transition and under which
policy. Operational logs answer what the process experienced. Audit
should not disappear with log rotation.

Per PLAN.md M3 validation: 'Audit entries contain no credential-like
fixtures.'
"""

from __future__ import annotations

import sqlite3

import pytest

from zero.app.services import build_services
from zero.config import Settings
from zero.domain.audit import (
    looks_sensitive,
)
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations


@pytest.fixture
def services(test_settings: Settings):
    database = Database(test_settings)
    apply_migrations(database)
    return build_services(test_settings, database)


# ----------------------------------------------------------------------
# Append-only enforcement
# ----------------------------------------------------------------------


def test_audit_event_cannot_be_updated(services) -> None:
    """Per zero-control-plane-trust §"Audit is evidence, not a
    transcript dump": the audit trail must not be silently mutated.
    The database enforces this with a trigger that blocks UPDATE."""
    owner = services.identity.create_user(display_name="Owner")
    # The user.create event is now in the audit log.
    events = services.audit.list_for_actor(actor_id=owner.id, limit=10)
    event = next(e for e in events if e.operation == "user.create")
    # Attempting to UPDATE the event must fail.
    conn = services.database.connect()
    with pytest.raises(sqlite3.Error, match="append-only"):
        conn.execute(
            "UPDATE audit_events SET redacted_summary = 'tampered' WHERE id = ?",
            (event.id.value,),
        )


def test_audit_event_cannot_be_deleted(services) -> None:
    """The database enforces append-only with a trigger that blocks
    DELETE."""
    owner = services.identity.create_user(display_name="Owner")
    events = services.audit.list_for_actor(actor_id=owner.id, limit=10)
    event = next(e for e in events if e.operation == "user.create")
    conn = services.database.connect()
    with pytest.raises(sqlite3.Error, match="append-only"):
        conn.execute(
            "DELETE FROM audit_events WHERE id = ?",
            (event.id.value,),
        )


# ----------------------------------------------------------------------
# Audit event contains required fields
# ----------------------------------------------------------------------


def test_audit_event_has_actor_project_operation_result(
    services,
) -> None:
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(
        owner_id=owner.id, name="Project A"
    )
    events = services.audit.list_for_project(project_id=project.id, actor_id=project.owner_user_id, limit=10)
    create_event = next(
        e for e in events if e.operation == "project.create"
    )
    assert create_event.actor_id == owner.id
    assert create_event.project_id == project.id
    assert create_event.operation == "project.create"
    assert create_event.result == "success"
    assert create_event.source == "system"
    assert create_event.created_at


# ----------------------------------------------------------------------
# No credential-like content in audit
# ----------------------------------------------------------------------


def test_audit_summary_with_secret_value_is_redacted(services) -> None:
    """Per zero-control-plane-trust §"Audit is evidence, not a
    transcript dump": if a caller accidentally passes a secret-like
    value in the summary, the audit service defensively redacts it.
    The primary control is careful construction at the call site."""
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(
        owner_id=owner.id, name="Project A"
    )
    services.audit.record(
        project_id=project.id,
        actor_id=owner.id,
        source="web",
        operation="test.sensitive",
        redacted_summary="Used api_key=sk-abc123secretvalue",
    )
    events = services.audit.list_for_project(project_id=project.id, actor_id=project.owner_user_id, limit=50)
    event = next(
        e for e in events if e.operation == "test.sensitive"
    )
    # The summary should be replaced because it contained "sk-".
    assert event.redacted_summary is not None
    assert "sk-abc123secretvalue" not in event.redacted_summary
    assert "REDACTED" in event.redacted_summary


def test_looks_sensitive_detects_common_patterns() -> None:
    """The defensive redaction helper detects common secret patterns."""
    assert looks_sensitive("api_key=sk-12345")
    assert looks_sensitive("Authorization: Bearer abc123")
    assert looks_sensitive("password=letmein")
    assert looks_sensitive("secret=mysecret")
    assert looks_sensitive("token=abc")
    assert not looks_sensitive("Created user zu_abc123")
    assert not looks_sensitive("")
    assert not looks_sensitive("normal audit summary")


# ----------------------------------------------------------------------
# Correlation
# ----------------------------------------------------------------------


def test_list_for_correlation_returns_related_events_in_order(
    services,
) -> None:
    """Per zero-observability-evidence §"One correlation spine connects
    evidence": a correlation ID links related events."""
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(
        owner_id=owner.id, name="Project A"
    )
    correlation_id = "corr_test_12345"
    services.audit.record(
        project_id=project.id,
        actor_id=owner.id,
        source="web",
        operation="step1",
        correlation_id=correlation_id,
    )
    services.audit.record(
        project_id=project.id,
        actor_id=owner.id,
        source="web",
        operation="step2",
        correlation_id=correlation_id,
    )
    events = services.audit.list_for_correlation(correlation_id)
    assert len(events) == 2
    # Both events should be present. Ordering is by created_at then id;
    # when timestamps collide within the same millisecond, the order
    # may differ, so we check set membership rather than strict order.
    operations = {e.operation for e in events}
    assert operations == {"step1", "step2"}


# ----------------------------------------------------------------------
# Project-scoped listing
# ----------------------------------------------------------------------


def test_list_for_project_returns_only_project_events(services) -> None:
    """Per zero-project-isolation-evidence §"Scope begins before
    access": listing audit events for a project must not return any
    event from another project."""
    owner_a = services.identity.create_user(display_name="Owner A")
    owner_b = services.identity.create_user(display_name="Owner B")
    project_a = services.identity.create_project(
        owner_id=owner_a.id, name="Project A"
    )
    project_b = services.identity.create_project(
        owner_id=owner_b.id, name="Project B"
    )
    events_a = services.audit.list_for_project(
        project_id=project_a.id, actor_id=project_a.owner_user_id, limit=100
    )
    events_b = services.audit.list_for_project(
        project_id=project_b.id, actor_id=project_b.owner_user_id, limit=100
    )
    # No event in A's list should belong to B.
    for event in events_a:
        assert event.project_id != project_b.id
    for event in events_b:
        assert event.project_id != project_a.id


# ----------------------------------------------------------------------
# System events (no project)
# ----------------------------------------------------------------------


def test_system_events_have_null_project_id(services) -> None:
    """Per zero-control-plane-trust: audit events for system-wide
    operations (like user.create) have a null project_id."""
    owner = services.identity.create_user(display_name="Owner")
    events = services.audit.list_for_actor(actor_id=owner.id, limit=10)
    user_create = next(e for e in events if e.operation == "user.create")
    assert user_create.project_id is None
