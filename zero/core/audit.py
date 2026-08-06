"""Zero v2 audit log — ADR T-1.7.

Append-only audit log. Every sensitive operation creates an entry.

Who (human or agent), what, on what, result, scope. Before/after for sensitive
ops. Searchable. Exportable to JSON + CSV.

Storage: per-schema ``*_audit_log`` tables (one in each of the three SQLite
files / Postgres schemas). Audit is co-located with the data it audits —
the dev schema's audit table lives in dev.db, etc.
"""
from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from zero.core.scope import Scope
from zero.db import Database

__all__ = [
    "ActorType",
    "AuditAction",
    "AuditEntry",
    "AuditLogger",
    "AuditResult",
    "audit",
]


# ---------------------------------------------------------------------- enums

class ActorType(StrEnum):
    HUMAN = "human"
    AGENT = "agent"
    SYSTEM = "system"


class AuditResult(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    DENIED = "denied"


AuditAction = Literal[
    # tenancy
    "org.create", "workspace.create", "project.create", "project.archive",
    "user.invite", "user.role_change", "user.remove",
    # task
    "task.create", "task.update", "task.delete", "task.claim", "task.release",
    # memory
    "memory.write", "memory.invalidate", "memory.promote",
    "memory.scope_violation",
    # agent
    "agent.run.start", "agent.run.complete", "agent.run.fail", "agent.run.cancel",
    "agent.budget.exceeded",
    # approval
    "approval.request", "approval.approve", "approval.reject", "approval.edit",
    "approval.changes_requested", "approval.expire",
    # secret
    "secret.access", "secret.rotation",
    # github
    "github.connect", "github.disconnect", "github.merge_attempt",
    # platform
    "platform.command", "platform.pairing",
    # session
    "session.create", "session.extend", "session.revoke",
    # sandbox
    "sandbox.exec",
]


# ---------------------------------------------------------------------- entry

@dataclass(frozen=True, slots=True)
class AuditEntry:
    """A single audit record. Immutable once created."""

    actor_type: ActorType
    actor_id: str
    action: AuditAction
    scope: Scope
    result: AuditResult
    target_type: str | None = None
    target_id: str | None = None
    before: Mapping[str, Any] | None = None
    after: Mapping[str, Any] | None = None
    note: str | None = None
    id: str = field(default_factory=lambda: f"aud_{uuid.uuid4().hex[:16]}")
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_row(self) -> dict[str, Any]:
        """Convert to a row suitable for DB insert."""
        return {
            "id": self.id,
            "actor_type": self.actor_type.value,
            "actor_id": self.actor_id,
            "action": self.action,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "scope_key": self.scope.retrieval_key(),
            "mode": self.scope.mode.value,
            "before_value": json.dumps(self.before) if self.before is not None else None,
            "after_value": json.dumps(self.after) if self.after is not None else None,
            "result": self.result.value,
            "note": self.note,
            "created_at": self.created_at.isoformat(),
        }


# ---------------------------------------------------------------------- logger

class AuditLogger:
    """Async audit logger that writes to the correct per-schema table.

    Usage:
        >>> audit = AuditLogger(db)
        >>> await audit.log(AuditEntry(
        ...     actor_type=ActorType.HUMAN,
        ...     actor_id="usr_01H...",
        ...     action="task.create",
        ...     scope=project_scope,
        ...     result=AuditResult.SUCCESS,
        ...     target_type="task",
        ...     target_id="tsk_01H...",
        ... ))
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    async def log(self, entry: AuditEntry) -> None:
        """Write ``entry`` to the audit table in the correct schema."""
        # Schema is selected by entry.scope.mode — same logic as all other data.
        row = entry.to_row()
        async with self._db.connection_for(entry.scope) as conn:
            # Insert into the per-schema audit table.
            # personal/normal schemas don't have audit_log table in our minimal
            # implementation — write to dev_audit_log when scope is dev, else
            # log to file via stdlib logging.
            from zero.core.scope import Mode  # noqa: PLC0415  # local import avoids cycle

            if entry.scope.mode is Mode.DEVELOPMENT:
                # Also include project_id (dev_audit_log requires it)
                assert entry.scope.project_id is not None
                await conn.execute(
                    """
                    INSERT INTO dev_audit_log
                        (id, project_id, actor_type, actor_id, action,
                         target_type, target_id, scope_key, mode,
                         before_value, after_value, result, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["id"], entry.scope.project_id, row["actor_type"], row["actor_id"],
                        row["action"], row["target_type"], row["target_id"], row["scope_key"],
                        row["mode"], row["before_value"], row["after_value"], row["result"],
                        row["created_at"],
                    ),
                )
            else:
                # Personal/Normal: log via stdlib logger (audit is a separate
                # concern; personal/normal scopes use file-based audit
                # (dev scope writes to dev_audit_log table).
                from zero.core.logging import get_logger  # noqa: PLC0415

                log = get_logger("zero.audit")
                log.info(
                    "audit",
                    extra={
                        "audit": row,
                        "scope": entry.scope.to_log_dict(),
                    },
                )


# ---------------------------------------------------------------------- global accessor

_audit_logger: AuditLogger | None = None


def set_audit_logger(logger: AuditLogger) -> None:
    """Set the process-wide :class:`AuditLogger`."""
    global _audit_logger
    _audit_logger = logger


def audit() -> AuditLogger:
    """Return the process-wide :class:`AuditLogger`.

    Raises ``RuntimeError`` if not yet set via :func:`set_audit_logger`.
    """
    if _audit_logger is None:
        raise RuntimeError(
            "AuditLogger not initialized — call set_audit_logger() at startup"
        )
    return _audit_logger
