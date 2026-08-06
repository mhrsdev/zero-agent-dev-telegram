"""Zero v2 approval workflow — ADR T-8.1.

Four responses:
    - ``Approve``            — go ahead with current params
    - ``Reject``             — deny, do not retry
    - ``Edit``               — modify params, then re-approval required
    - ``Request Changes``    — ask requester to revise

Rules (T-8.1 acceptance):
    - Requester cannot self-approve (DB CHECK in dev_approvals).
    - Expired = auto-reject (not implicit approve).
    - ``Edit`` allows param change, then requires re-approval.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from zero.core.scope import Scope

__all__ = [
    "ApprovalChoice",
    "ApprovalExpiredError",
    "ApprovalRequest",
    "ApprovalResolver",
    "ApprovalStatus",
    "ApprovalStore",
    "SelfApprovalError",
]


# ---------------------------------------------------------------------- enums

class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EDITED = "edited"             # params changed; needs re-approval
    CHANGES_REQUESTED = "changes_requested"  # requester must revise
    EXPIRED = "expired"


class ApprovalChoice(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    EDIT = "edit"                  # returns EDITED status
    REQUEST_CHANGES = "request_changes"


# ---------------------------------------------------------------------- errors

class ApprovalExpiredError(Exception):
    """Raised when trying to resolve an expired approval."""


class SelfApprovalError(Exception):
    """Raised when requester attempts to approve their own request."""


# ---------------------------------------------------------------------- request

@dataclass(slots=True)
class ApprovalRequest:
    """A single approval request."""

    requester_id: str
    action: str                    # e.g. "merge_pr", "promote_fact", "sandbox_exec"
    scope: Scope
    params: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: int = 300     # 5 min default
    id: str = field(default_factory=lambda: f"apv_{uuid.uuid4().hex[:16]}")
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime = field(init=False)
    status: ApprovalStatus = ApprovalStatus.PENDING
    approver_id: str | None = None
    resolved_at: datetime | None = None
    resolution_note: str | None = None
    # For Edit: the modified params (status=EDITED, original_params moved here)
    edited_params: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        self.expires_at = self.created_at + timedelta(seconds=self.timeout_seconds)

    @property
    def is_expired(self) -> bool:
        return datetime.now(UTC) > self.expires_at and self.status is ApprovalStatus.PENDING

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "action": self.action,
            "scope": self.scope.retrieval_key(),
            "requester_id": self.requester_id,
            "status": self.status.value,
            "expires_at": self.expires_at.isoformat(),
            "approver_id": self.approver_id,
        }


# ---------------------------------------------------------------------- store

class ApprovalStore:
    """In-memory approval store (base class).

    For production use, prefer :class:`zero.stores.approval_store.DbApprovalStore`
    which persists to the ``dev_approvals`` / ``normal_approvals`` / ``personal_approvals``
    tables with full audit trail.
    """

    def __init__(self) -> None:
        self._requests: dict[str, ApprovalRequest] = {}

    def create(self, req: ApprovalRequest) -> ApprovalRequest:
        self._requests[req.id] = req
        return req

    def get(self, approval_id: str) -> ApprovalRequest | None:
        req = self._requests.get(approval_id)
        if req is None:
            return None
        # Lazily mark as expired.
        if req.is_expired and req.status is ApprovalStatus.PENDING:
            req.status = ApprovalStatus.EXPIRED
        return req

    def list_pending(self, *, scope: Scope | None = None) -> list[ApprovalRequest]:
        out: list[ApprovalRequest] = []
        for r in self._requests.values():
            if r.is_expired and r.status is ApprovalStatus.PENDING:
                r.status = ApprovalStatus.EXPIRED
            if r.status is ApprovalStatus.PENDING:
                if scope is None or r.scope.shares_realm_with(scope):
                    out.append(r)
        return out

    def update(self, req: ApprovalRequest) -> None:
        self._requests[req.id] = req


# ---------------------------------------------------------------------- resolver

class ApprovalResolver:
    """Resolves approval requests with the four-choice workflow.

    Enforces:
        - Requester cannot self-approve (raises :class:`SelfApprovalError`)
        - Expired approvals auto-reject (raises :class:`ApprovalExpiredError`)
        - ``Edit`` puts request back into PENDING with new params
        - ``Request Changes`` puts request back into PENDING with note
    """

    def __init__(self, store: ApprovalStore) -> None:
        self._store = store

    def resolve(
        self,
        approval_id: str,
        approver_id: str,
        choice: ApprovalChoice,
        *,
        note: str | None = None,
        edited_params: dict[str, Any] | None = None,
    ) -> ApprovalRequest:
        """Resolve an approval request. Raises on invalid operations."""
        # Get the raw request WITHOUT lazy expiry side-effects.
        req = self._store._requests.get(approval_id)
        if req is None:
            raise KeyError(f"approval {approval_id!r} not found")

        # Lazy expiry check — must come BEFORE the status check below.
        # If the request was PENDING but is now expired, mark it and raise.
        if req.is_expired and req.status is ApprovalStatus.PENDING:
            req.status = ApprovalStatus.EXPIRED
            self._store.update(req)
            raise ApprovalExpiredError(
                f"approval {approval_id!r} expired at {req.expires_at.isoformat()}"
            )

        if req.status is ApprovalStatus.EXPIRED:
            # Already marked expired (by an earlier call to store.get()).
            raise ApprovalExpiredError(
                f"approval {approval_id!r} expired at {req.expires_at.isoformat()}"
            )

        if req.status is not ApprovalStatus.PENDING:
            raise ValueError(
                f"approval {approval_id!r} is already {req.status.value!r} — cannot resolve"
            )

        # Self-approval check (T-8.1 acceptance criterion).
        if choice is ApprovalChoice.APPROVE and approver_id == req.requester_id:
            raise SelfApprovalError(
                f"approver {approver_id!r} is the requester — cannot self-approve"
            )

        req.approver_id = approver_id
        req.resolved_at = datetime.now(UTC)
        req.resolution_note = note

        if choice is ApprovalChoice.APPROVE:
            req.status = ApprovalStatus.APPROVED
        elif choice is ApprovalChoice.REJECT:
            req.status = ApprovalStatus.REJECTED
        elif choice is ApprovalChoice.EDIT:
            if edited_params is None:
                raise ValueError("Edit choice requires edited_params")
            req.edited_params = edited_params
            req.status = ApprovalStatus.EDITED
            # Put back into PENDING with new params for re-approval
            # — see ApprovalResolver.apply_edit()
        elif choice is ApprovalChoice.REQUEST_CHANGES:
            req.status = ApprovalStatus.CHANGES_REQUESTED
        else:  # pragma: no cover  # exhaustive enum
            raise ValueError(f"unknown choice {choice!r}")

        self._store.update(req)
        return req

    def apply_edit(self, approval_id: str) -> ApprovalRequest:
        """Transition an EDITED request back to PENDING with the new params.

        The requester must explicitly accept the edit before re-approval is
        sought (this is the "param change then re-approval" rule).
        """
        req = self._store.get(approval_id)
        if req is None:
            raise KeyError(f"approval {approval_id!r} not found")
        if req.status is not ApprovalStatus.EDITED:
            raise ValueError(
                f"approval {approval_id!r} is {req.status.value!r} — must be EDITED to apply"
            )
        if req.edited_params is None:  # pragma: no cover  # invariant
            raise ValueError("edited_params is None — cannot apply edit")

        # Apply edited params, put back to PENDING.
        req.params = req.edited_params
        req.edited_params = None
        req.status = ApprovalStatus.PENDING
        req.approver_id = None
        req.resolved_at = None
        req.resolution_note = None
        # Reset timeout (give a fresh window for re-approval).
        req.expires_at = datetime.now(UTC) + timedelta(seconds=req.timeout_seconds)

        self._store.update(req)
        return req

    def expire_overdue(self) -> int:
        """Mark all overdue PENDING approvals as EXPIRED. Returns count."""
        count = 0
        # Scan ALL requests directly (not via list_pending which filters out
        # already-lazily-expired ones).
        for r in self._store._requests.values():
            if r.status is ApprovalStatus.PENDING and r.is_expired:
                r.status = ApprovalStatus.EXPIRED
                self._store.update(r)
                count += 1
        return count
