"""Unit tests for zero.security.approval — ADR T-8.1."""
from __future__ import annotations

import pytest
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


@pytest.fixture
def dev_scope() -> Scope:
    return Scope.development(
        org_id="org_01HABC",
        workspace_id="ws_01HABC",
        project_id="prj_01HABC",
        group_id="grp_01HABC",
        topic_id=100,
    )


@pytest.fixture
def store() -> ApprovalStore:
    return ApprovalStore()


@pytest.fixture
def resolver(store: ApprovalStore) -> ApprovalResolver:
    return ApprovalResolver(store)


class TestApprovalRequest:
    def test_basic_construction(self, dev_scope: Scope) -> None:
        req = ApprovalRequest(
            requester_id="usr_alice",
            action="merge_pr",
            scope=dev_scope,
        )
        assert req.status is ApprovalStatus.PENDING
        assert req.expires_at > req.created_at
        assert req.is_expired is False

    def test_expiry_detection(self, dev_scope: Scope) -> None:
        req = ApprovalRequest(
            requester_id="usr_alice",
            action="merge_pr",
            scope=dev_scope,
            timeout_seconds=0,  # immediately expires
        )
        # Force expiry by sleeping a tick
        import time
        time.sleep(0.01)
        assert req.is_expired is True


class TestApprovalResolver:
    @pytest.mark.asyncio
    async def test_approve(
        self, store: ApprovalStore, resolver: ApprovalResolver, dev_scope: Scope
    ) -> None:
        req = ApprovalRequest(
            requester_id="usr_alice",
            action="merge_pr",
            scope=dev_scope,
        )
        store.create(req)
        result = resolver.resolve(req.id, "usr_bob", ApprovalChoice.APPROVE)
        assert result.status is ApprovalStatus.APPROVED
        assert result.approver_id == "usr_bob"

    @pytest.mark.asyncio
    async def test_reject(
        self, store: ApprovalStore, resolver: ApprovalResolver, dev_scope: Scope
    ) -> None:
        req = ApprovalRequest(
            requester_id="usr_alice",
            action="merge_pr",
            scope=dev_scope,
        )
        store.create(req)
        result = resolver.resolve(req.id, "usr_bob", ApprovalChoice.REJECT, note="code not ready")
        assert result.status is ApprovalStatus.REJECTED
        assert result.resolution_note == "code not ready"

    @pytest.mark.asyncio
    async def test_self_approval_blocked(
        self, store: ApprovalStore, resolver: ApprovalResolver, dev_scope: Scope
    ) -> None:
        """T-8.1 acceptance: requester cannot self-approve."""
        req = ApprovalRequest(
            requester_id="usr_alice",
            action="merge_pr",
            scope=dev_scope,
        )
        store.create(req)
        with pytest.raises(SelfApprovalError):
            resolver.resolve(req.id, "usr_alice", ApprovalChoice.APPROVE)

    @pytest.mark.asyncio
    async def test_expired_auto_rejects(
        self, store: ApprovalStore, resolver: ApprovalResolver, dev_scope: Scope
    ) -> None:
        """T-8.1 acceptance: expired = auto-reject (not implicit approve)."""
        req = ApprovalRequest(
            requester_id="usr_alice",
            action="merge_pr",
            scope=dev_scope,
            timeout_seconds=0,
        )
        store.create(req)
        import time
        time.sleep(0.01)
        with pytest.raises(ApprovalExpiredError):
            resolver.resolve(req.id, "usr_bob", ApprovalChoice.APPROVE)
        # Confirm status was set to EXPIRED
        assert store.get(req.id).status is ApprovalStatus.EXPIRED

    @pytest.mark.asyncio
    async def test_edit_then_reapprove(
        self, store: ApprovalStore, resolver: ApprovalResolver, dev_scope: Scope
    ) -> None:
        """Edit allows param change, then requires re-approval."""
        req = ApprovalRequest(
            requester_id="usr_alice",
            action="sandbox_exec",
            scope=dev_scope,
            params={"command": "rm -rf /"},
        )
        store.create(req)
        # Bob edits to a safer command
        result = resolver.resolve(
            req.id, "usr_bob", ApprovalChoice.EDIT,
            edited_params={"command": "ls /tmp"},
        )
        assert result.status is ApprovalStatus.EDITED
        assert result.edited_params == {"command": "ls /tmp"}
        # Original params unchanged yet
        assert result.params == {"command": "rm -rf /"}

        # Apply edit → back to PENDING
        reapplied = resolver.apply_edit(req.id)
        assert reapplied.status is ApprovalStatus.PENDING
        assert reapplied.params == {"command": "ls /tmp"}
        assert reapplied.edited_params is None

        # Now someone (not the requester) can approve
        final = resolver.resolve(req.id, "usr_charlie", ApprovalChoice.APPROVE)
        assert final.status is ApprovalStatus.APPROVED

    @pytest.mark.asyncio
    async def test_request_changes(
        self, store: ApprovalStore, resolver: ApprovalResolver, dev_scope: Scope
    ) -> None:
        req = ApprovalRequest(
            requester_id="usr_alice",
            action="promote_fact",
            scope=dev_scope,
        )
        store.create(req)
        result = resolver.resolve(
            req.id, "usr_bob", ApprovalChoice.REQUEST_CHANGES,
            note="please add source",
        )
        assert result.status is ApprovalStatus.CHANGES_REQUESTED
        assert result.resolution_note == "please add source"

    @pytest.mark.asyncio
    async def test_cannot_resolve_already_resolved(
        self, store: ApprovalStore, resolver: ApprovalResolver, dev_scope: Scope
    ) -> None:
        req = ApprovalRequest(
            requester_id="usr_alice",
            action="merge_pr",
            scope=dev_scope,
        )
        store.create(req)
        resolver.resolve(req.id, "usr_bob", ApprovalChoice.APPROVE)
        with pytest.raises(ValueError):
            resolver.resolve(req.id, "usr_bob", ApprovalChoice.APPROVE)

    @pytest.mark.asyncio
    async def test_edit_requires_edited_params(
        self, store: ApprovalStore, resolver: ApprovalResolver, dev_scope: Scope
    ) -> None:
        req = ApprovalRequest(
            requester_id="usr_alice",
            action="sandbox_exec",
            scope=dev_scope,
            params={"command": "ls"},
        )
        store.create(req)
        with pytest.raises(ValueError):
            resolver.resolve(req.id, "usr_bob", ApprovalChoice.EDIT)

    @pytest.mark.asyncio
    async def test_expire_overdue(
        self, store: ApprovalStore, resolver: ApprovalResolver, dev_scope: Scope
    ) -> None:
        """Resolver can bulk-expire overdue PENDING approvals."""
        req = ApprovalRequest(
            requester_id="usr_alice",
            action="merge_pr",
            scope=dev_scope,
            timeout_seconds=0,
        )
        store.create(req)
        import time
        time.sleep(0.01)
        count = resolver.expire_overdue()
        assert count == 1
        assert store.get(req.id).status is ApprovalStatus.EXPIRED
