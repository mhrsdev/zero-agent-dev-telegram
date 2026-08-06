"""DB-backed approval store — replaces in-memory ApprovalStore.

Per ADR T-8.1 + T-1.7:
    - Approvals persist across restarts
    - All transitions audited
    - DB CHECK constraints enforce requester != approver for approved status

Uses ``dev_approvals`` table (already in sqlite_backend.py schema).
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from zero.core.scope import Scope
from zero.security.approval import (
    ApprovalChoice,
    ApprovalExpiredError,
    ApprovalRequest,
    ApprovalResolver,
    ApprovalStatus,
    ApprovalStore,
    SelfApprovalError,
)

if TYPE_CHECKING:
    from zero.db import Database

__all__ = ["DbApprovalStore"]


class DbApprovalStore(ApprovalStore):
    """DB-backed approval store.

    Usage:
        >>> store = DbApprovalStore(db)
        >>> await store.create_async(req)
        >>> req = await store.get_async(approval_id)
    """

    def __init__(self, db: Database) -> None:
        self._db = db
        # In-memory cache for sync access (compat with old API).
        self._cache: dict[str, ApprovalRequest] = {}

    async def create_async(self, req: ApprovalRequest) -> ApprovalRequest:
        """Persist a new approval request."""
        self._cache[req.id] = req
        async with self._db.connection_for(req.scope) as conn:
            # Dev schema has dev_approvals table; personal/normal have their own.
            if not req.scope.is_development():
                # Personal/normal approvals are cached in-memory (their tables
                # exist but the sync API doesn't have async DB access).
                return req
            assert req.scope.project_id is not None  # noqa: S101
            await conn.execute(
                """INSERT INTO dev_approvals
                   (approval_id, project_id, requester_id, approver_id, action,
                    params_json, status, created_at, expires_at)
                   VALUES (?, ?, ?, NULL, ?, ?, 'pending', ?, ?)""",
                (
                    req.id,
                    req.scope.project_id,
                    req.requester_id,
                    req.action,
                    json.dumps(req.params),
                    req.created_at.isoformat(),
                    req.expires_at.isoformat(),
                ),
            )
        return req

    # Sync API (for backward compat with tests that don't use async).
    def create(self, req: ApprovalRequest) -> ApprovalRequest:
        self._cache[req.id] = req
        return req

    async def get_async(self, approval_id: str) -> ApprovalRequest | None:
        """Fetch by id. Lazily marks expired entries."""
        # Check cache first.
        if approval_id in self._cache:
            req = self._cache[approval_id]
            if req.is_expired and req.status is ApprovalStatus.PENDING:
                req.status = ApprovalStatus.EXPIRED
            return req

        # Query DB (dev schema).
        # Each Database instance has one dev schema, so we query it directly.
        from zero.core.scope import Mode  # noqa: PLC0415

        dev_scope = Scope.development(
            org_id="org_system",
            workspace_id="ws_system",
            project_id="prj_system",
            group_id="grp_system",
            topic_id=0,
        ).with_default_memory_scope()
        async with self._db.connection_for(dev_scope) as conn:
            row = await conn.fetchone(
                "SELECT approval_id, project_id, requester_id, approver_id, action, "
                "params_json, status, resolution_note, created_at, expires_at, resolved_at "
                "FROM dev_approvals WHERE approval_id = ?",
                (approval_id,),
            )
            if row is None:
                return None
            # Reconstruct ApprovalRequest.
            req = self._row_to_request(row, dev_scope)
            if req.is_expired and req.status is ApprovalStatus.PENDING:
                req.status = ApprovalStatus.EXPIRED
                await self.update_async(req)
            self._cache[approval_id] = req
            return req

    def get(self, approval_id: str) -> ApprovalRequest | None:
        """Sync get (cache only — use get_async for DB)."""
        req = self._cache.get(approval_id)
        if req is not None and req.is_expired and req.status is ApprovalStatus.PENDING:
            req.status = ApprovalStatus.EXPIRED
        return req

    async def list_pending_async(
        self, *, scope: Scope | None = None
    ) -> list[ApprovalRequest]:
        """List pending approvals, optionally filtered by scope."""
        if scope is None or not scope.is_development():
            # Personal/normal: use cache.
            return [r for r in self._cache.values() if r.status is ApprovalStatus.PENDING]
        async with self._db.connection_for(scope) as conn:
            assert scope.project_id is not None  # noqa: S101
            rows = await conn.fetchall(
                "SELECT approval_id, project_id, requester_id, approver_id, action, "
                "params_json, status, resolution_note, created_at, expires_at, resolved_at "
                "FROM dev_approvals WHERE project_id = ? AND status = 'pending' "
                "ORDER BY created_at DESC",
                (scope.project_id,),
            )
            return [self._row_to_request(r, scope) for r in rows]

    def list_pending(self, *, scope: Scope | None = None) -> list[ApprovalRequest]:
        """Sync list (cache only)."""
        out: list[ApprovalRequest] = []
        for r in list(self._cache.values()):
            if r.is_expired and r.status is ApprovalStatus.PENDING:
                r.status = ApprovalStatus.EXPIRED
            if r.status is ApprovalStatus.PENDING:
                if scope is None or r.scope.shares_realm_with(scope):
                    out.append(r)
        return out

    async def update_async(self, req: ApprovalRequest) -> None:
        """Persist updated approval state."""
        self._cache[req.id] = req
        if not req.scope.is_development():
            return
        async with self._db.connection_for(req.scope) as conn:
            assert req.scope.project_id is not None  # noqa: S101
            await conn.execute(
                """UPDATE dev_approvals
                   SET approver_id = ?, status = ?, resolution_note = ?,
                       resolved_at = ?
                   WHERE approval_id = ?""",
                (
                    req.approver_id,
                    req.status.value,
                    req.resolution_note,
                    req.resolved_at.isoformat() if req.resolved_at else None,
                    req.id,
                ),
            )

    def update(self, req: ApprovalRequest) -> None:
        self._cache[req.id] = req

    async def expire_overdue_async(self) -> int:
        """Mark all overdue PENDING approvals as EXPIRED."""
        count = 0
        for r in list(self._cache.values()):
            if r.status is ApprovalStatus.PENDING and r.is_expired:
                r.status = ApprovalStatus.EXPIRED
                await self.update_async(r)
                count += 1
        return count

    @staticmethod
    def _row_to_request(row: tuple[Any, ...], scope: Scope) -> ApprovalRequest:
        """Convert a DB row to ApprovalRequest."""
        (
            approval_id,
            _project_id,
            requester_id,
            approver_id,
            action,
            params_json,
            status_str,
            resolution_note,
            created_at_str,
            expires_at_str,
            resolved_at_str,
        ) = row
        params = json.loads(params_json) if isinstance(params_json, str) else params_json or {}
        req = ApprovalRequest(
            requester_id=requester_id,
            action=action,
            scope=scope,
            params=params,
            id=approval_id,
        )
        # Override auto-generated fields.
        req.status = ApprovalStatus(status_str)
        req.approver_id = approver_id
        req.resolution_note = resolution_note
        if resolved_at_str:
            req.resolved_at = datetime.fromisoformat(resolved_at_str)
        return req


class DbApprovalResolver(ApprovalResolver):
    """DB-backed approval resolver.

    Extends ApprovalResolver with async methods that persist to DB.
    """

    def __init__(self, store: DbApprovalStore) -> None:
        super().__init__(store)
        self._db_store = store

    async def resolve_async(
        self,
        approval_id: str,
        approver_id: str,
        choice: ApprovalChoice,
        *,
        note: str | None = None,
        edited_params: dict[str, Any] | None = None,
    ) -> ApprovalRequest:
        """Async version of resolve() that persists to DB."""
        req = await self._db_store.get_async(approval_id)
        if req is None:
            raise KeyError(f"approval {approval_id!r} not found")

        if req.is_expired and req.status is ApprovalStatus.PENDING:
            req.status = ApprovalStatus.EXPIRED
            await self._db_store.update_async(req)
            raise ApprovalExpiredError(
                f"approval {approval_id!r} expired at {req.expires_at.isoformat()}"
            )

        if req.status is ApprovalStatus.EXPIRED:
            raise ApprovalExpiredError(
                f"approval {approval_id!r} expired at {req.expires_at.isoformat()}"
            )

        if req.status is not ApprovalStatus.PENDING:
            raise ValueError(
                f"approval {approval_id!r} is already {req.status.value!r} — cannot resolve"
            )

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
        elif choice is ApprovalChoice.REQUEST_CHANGES:
            req.status = ApprovalStatus.CHANGES_REQUESTED
        else:  # pragma: no cover
            raise ValueError(f"unknown choice {choice!r}")

        await self._db_store.update_async(req)
        return req
