"""Audit domain types.

Per ``zero-control-plane-trust`` §"Audit is evidence, not a transcript
dump": An audit event explains who caused what transition, in which
project, through which interface, against which revision, and with
what result. It normally does not need the raw conversation, source
file, prompt, tool output, or secret.

Per ``zero-observability-evidence`` §"Audit explains authority": Audit
records answer who caused a protected transition and under which
policy. Operational logs answer what the process experienced. Audit
should not disappear with log rotation, and logging every function
call does not create an audit trail.

Per ``zero-control-plane-trust`` §"Atomicity follows the business
fact": Operations that represent one fact should not leave half-facts.
Approval plus its audit evidence, membership change plus revocation
effects, or topology activation plus lineage should either become
durable together or remain unapplied.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from zero.domain.identity import ProjectId, UserId

#: Prefix for Audit Event IDs.
AUDIT_EVENT_ID_PREFIX = "aud_"

AuditSource = Literal["web", "telegram", "discord", "system", "internal"]
AuditResult = Literal["success", "denied", "failure", "error"]


@dataclass(frozen=True)
class AuditEventId:
    """Stable server-issued ID for an audit event."""

    value: str

    def __post_init__(self) -> None:
        if not self.value or not isinstance(self.value, str):
            raise ValueError("AuditEventId must be a non-empty string")
        if not self.value.startswith(AUDIT_EVENT_ID_PREFIX):
            raise ValueError(
                f"AuditEventId must start with "
                f"{AUDIT_EVENT_ID_PREFIX!r}; got {self.value!r}"
            )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class AuditEvent:
    """A single audit event.

    Attributes:
        id: stable server-issued ID.
        project_id: the project this event concerns. ``None`` for
            system-wide events (e.g. user creation).
        actor_id: the user who caused the event. ``None`` for system
            events with no actor.
        source: the interface that originated the event (web,
            telegram, ...).
        operation: a stable, low-cardinality operation identifier
            (e.g. ``"plan.approve"``, ``"member.add"``).
        target_type: the kind of resource targeted (e.g. ``"plan"``,
            ``"membership"``). ``None`` if not applicable.
        target_id: the stable ID of the targeted resource. ``None``
            if not applicable.
        result: the outcome of the operation.
        correlation_id: an optional ID linking related events (e.g.
            an execution ID linking plan approval, task creation, and
            tool invocation events).
        redacted_summary: a small, safe, human-readable description.
            MUST NOT contain raw payloads, secrets, prompts, tool
            output, PII, or credentials. When in doubt, omit.
        created_at: ISO-8601 timestamp.
    """

    id: AuditEventId
    project_id: ProjectId | None
    actor_id: UserId | None
    source: AuditSource
    operation: str
    target_type: str | None = None
    target_id: str | None = None
    result: AuditResult = "success"
    correlation_id: str | None = None
    redacted_summary: str | None = None
    created_at: str = ""


# ----------------------------------------------------------------------
# Sensitive patterns to redact from summaries
# ----------------------------------------------------------------------

#: Substrings that, if found in a redacted_summary, indicate the
#: summary may be leaking sensitive content. The audit service uses
#: this list as a defensive check; summaries should be constructed
#: carefully in the first place, not scanned after the fact.
SENSITIVE_SUMMARY_PATTERNS: tuple[str, ...] = (
    "sk-",            # common API key prefix
    "Bearer ",        # HTTP auth header
    "password=",      # query-string password
    "secret=",        # query-string secret
    "token=",         # query-string token
    "api_key=",       # query-string api key
)


def looks_sensitive(text: str) -> bool:
    """Return True if ``text`` contains a sensitive-looking substring.

    Per ``zero-control-plane-trust`` §"Audit is evidence, not a
    transcript dump": audit events MUST NOT contain raw credentials.
    This helper is a defensive check; the primary control is careful
    construction of summaries.
    """
    if not text:
        return False
    lowered = text.lower()
    return any(pattern.lower() in lowered for pattern in SENSITIVE_SUMMARY_PATTERNS)


# ----------------------------------------------------------------------
# Typed failures
# ----------------------------------------------------------------------


class AuditError(RuntimeError):
    """Base class for audit-domain typed failures."""


class AuditAppendOnlyError(AuditError):
    """An attempt was made to mutate or delete an audit event.

    Per ``zero-control-plane-trust`` §"Audit is evidence, not a
    transcript dump" and ``zero-recovery-consistency`` §"Idempotency
    makes retries ordinary": the audit trail is durable authority
    evidence and must not be silently mutated. The database enforces
    this with triggers; this error is raised if the application
    attempts a mutation.
    """


class AuditSensitiveContentError(AuditError):
    """An audit event's summary contained sensitive-looking content.

    This is a defensive check; the primary control is careful
    construction of summaries at the call site.
    """
