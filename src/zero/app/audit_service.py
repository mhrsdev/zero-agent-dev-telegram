"""Audit service — append-only audit event recording.

Per ``zero-control-plane-trust`` §"Audit is evidence, not a transcript
dump": An audit event explains who caused what transition, in which
project, through which interface, against which revision, and with
what result. It normally does not need the raw conversation, source
file, prompt, tool output, or secret.

Per ``zero-control-plane-trust`` §"Atomicity follows the business
fact": Operations that represent one fact should not leave half-facts.
This service provides :meth:`record_concurrently` for callers that
need an audit event to be persisted atomically with the business
operation it describes.
"""

from __future__ import annotations

from datetime import UTC, datetime

from zero.app.authorization_service import AuthorizationService
from zero.domain.audit import (
    AuditEvent,
    AuditEventId,
    AuditSource,
)
from zero.domain.identity import ProjectId, UserId
from zero.domain.ids import generate_audit_event_id
from zero.persistence.repositories.audit_repository import AuditRepository


def _now_utc_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class AuditService:
    """Application operations for audit events.

    This is the canonical way to record an audit event. HTTP handlers
    and future adapters call this service rather than touching the
    repository directly.
    """

    def __init__(
        self,
        audit_repo: AuditRepository,
        authorization: AuthorizationService,
    ) -> None:
        self._audit_repo = audit_repo
        self._authorization = authorization

    def record(
        self,
        *,
        project_id: ProjectId | None,
        actor_id: UserId | None,
        source: AuditSource,
        operation: str,
        target_type: str | None = None,
        target_id: str | None = None,
        result: str = "success",
        correlation_id: str | None = None,
        redacted_summary: str | None = None,
    ) -> AuditEvent:
        """Record an audit event.

        The ``redacted_summary`` MUST NOT contain raw payloads, secrets,
        prompts, tool output, PII, or credentials. The repository
        performs a defensive scan and replaces sensitive-looking
        summaries with a placeholder, but the primary control is
        careful construction at the call site.
        """
        event = AuditEvent(
            id=AuditEventId(generate_audit_event_id()),
            project_id=project_id,
            actor_id=actor_id,
            source=source,
            operation=operation,
            target_type=target_type,
            target_id=target_id,
            result=result,  # type: ignore[arg-type]
            correlation_id=correlation_id,
            redacted_summary=redacted_summary,
            created_at=_now_utc_iso(),
        )
        self._audit_repo.insert(event)
        return event

    def list_for_project(
        self,
        *,
        project_id: ProjectId,
        actor_id: UserId,
        limit: int = 100,
        offset: int = 0,
        source: AuditSource = "system",
    ) -> list[AuditEvent]:
        """List audit events for a project, newest first.

        Per ``zero-project-isolation-evidence`` §"Scope begins before
        access": the repository filters by ``project_id`` before any
        row is loaded.
        """
        self._authorization.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="audit.view",
            source=source,
        )
        return self._audit_repo.list_for_project(project_id, limit=limit, offset=offset)

    def list_for_actor(
        self,
        *,
        actor_id: UserId,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AuditEvent]:
        return self._audit_repo.list_for_actor(actor_id, limit=limit, offset=offset)

    def list_for_correlation(self, correlation_id: str) -> list[AuditEvent]:
        return self._audit_repo.list_for_correlation(correlation_id)
