"""Audit event repository — append-only.

Per ``zero-control-plane-trust`` §"Audit is evidence, not a transcript
dump": the audit trail is durable authority evidence and must not be
silently mutated. The database enforces append-only behavior with
triggers that block UPDATE and DELETE; this repository exposes only
insert and read methods.
"""

from __future__ import annotations

import sqlite3

from zero.domain.audit import AuditEvent, AuditEventId, looks_sensitive
from zero.domain.identity import ProjectId, UserId
from zero.persistence.connection import Database


def _row_to_event(row: sqlite3.Row | tuple) -> AuditEvent:
    project_id = row["project_id"]
    actor_id = row["actor_id"]
    return AuditEvent(
        id=AuditEventId(row["id"]),
        project_id=ProjectId(project_id) if project_id else None,
        actor_id=UserId(actor_id) if actor_id else None,
        source=row["source"],  # type: ignore[arg-type]
        operation=row["operation"],
        target_type=row["target_type"],
        target_id=row["target_id"],
        result=row["result"],  # type: ignore[arg-type]
        correlation_id=row["correlation_id"],
        redacted_summary=row["redacted_summary"],
        created_at=row["created_at"],
    )


class AuditRepository:
    """Database-backed, append-only audit event repository."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def insert(self, event: AuditEvent, *, commit: bool = True) -> None:
        """Append a new audit event.

        Per ``zero-control-plane-trust`` §"Audit is evidence, not a
        transcript dump": the event carries stable identifiers and
        compact before/after state, not raw payloads. We defensively
        check the summary for sensitive-looking content before
        storing.
        """
        if event.redacted_summary and looks_sensitive(event.redacted_summary):
            # Defensive check; the primary control is careful
            # construction at the call site. We strip the summary
            # rather than failing the audit write — audit must be
            # durable even when summaries are imperfect.
            event = AuditEvent(
                id=event.id,
                project_id=event.project_id,
                actor_id=event.actor_id,
                source=event.source,
                operation=event.operation,
                target_type=event.target_type,
                target_id=event.target_id,
                result=event.result,
                correlation_id=event.correlation_id,
                redacted_summary="[REDACTED: sensitive content detected]",
                created_at=event.created_at,
            )
        conn = self._database.connect()
        try:
            conn.execute(
                "INSERT INTO audit_events "
                "(id, project_id, actor_id, source, operation, target_type, "
                "target_id, result, correlation_id, redacted_summary) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.id.value,
                    event.project_id.value if event.project_id else None,
                    event.actor_id.value if event.actor_id else None,
                    event.source,
                    event.operation,
                    event.target_type,
                    event.target_id,
                    event.result,
                    event.correlation_id,
                    event.redacted_summary,
                ),
            )
            if commit:
                conn.commit()
        except sqlite3.IntegrityError:
            if commit:
                conn.rollback()
            raise

    def get_by_id(self, event_id: AuditEventId) -> AuditEvent | None:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, actor_id, source, operation, target_type, "
            "target_id, result, correlation_id, redacted_summary, created_at "
            "FROM audit_events WHERE id = ?",
            (event_id.value,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return _row_to_event(row)

    def list_for_project(
        self,
        project_id: ProjectId,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AuditEvent]:
        """List audit events for a project, newest first.

        Per ``zero-project-isolation-evidence`` §"Scope begins before
        access": the query filters by ``project_id`` before any row is
        loaded. Events from other projects are never returned even if
        the caller guesses an ID.
        """
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, actor_id, source, operation, target_type, "
            "target_id, result, correlation_id, redacted_summary, created_at "
            "FROM audit_events WHERE project_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (project_id.value, limit, offset),
        )
        return [_row_to_event(row) for row in cursor.fetchall()]

    def list_for_actor(
        self,
        actor_id: UserId,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AuditEvent]:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, actor_id, source, operation, target_type, "
            "target_id, result, correlation_id, redacted_summary, created_at "
            "FROM audit_events WHERE actor_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (actor_id.value, limit, offset),
        )
        return [_row_to_event(row) for row in cursor.fetchall()]

    def list_for_correlation(self, correlation_id: str) -> list[AuditEvent]:
        """List all events sharing a correlation ID, oldest first.

        Per ``zero-observability-evidence`` §"One correlation spine
        connects evidence": a correlation ID links related events
        (e.g. an execution ID linking plan approval, task creation,
        and tool invocation events).
        """
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, actor_id, source, operation, target_type, "
            "target_id, result, correlation_id, redacted_summary, created_at "
            "FROM audit_events WHERE correlation_id = ? "
            "ORDER BY created_at ASC, id ASC",
            (correlation_id,),
        )
        return [_row_to_event(row) for row in cursor.fetchall()]
