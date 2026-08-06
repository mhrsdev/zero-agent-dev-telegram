"""Tests for enterprise persistent stores — DB-backed implementations."""
from __future__ import annotations

import asyncio
import pytest

from zero.core.scope import Scope
from zero.db import Database
from zero.db.sqlite_backend import InMemorySqliteBackend
from zero.security.approval import (
    ApprovalChoice,
    ApprovalRequest,
    ApprovalStatus,
)
from zero.security.session import (
    SessionExpiredError,
    SessionRevokedError,
    SessionStore,
)
from zero.stores.approval_store import DbApprovalStore, DbApprovalResolver
from zero.stores.session_store import DbSessionStore, DbSlidingWindowRateLimiter
from zero.stores.role_store import DbRoleStore, RoleScopeKind
from zero.stores.todo_store import DbTodoStore
from zero.stores.rate_limiter import DbRateLimiter
from zero.stores.conversation_store import DbConversationStore
from zero.core.permissions import Role


@pytest.fixture
async def db() -> Database:
    backend = InMemorySqliteBackend()
    d = Database(backend=backend)
    await d.start()
    yield d
    await d.stop()


@pytest.fixture
def dev_scope() -> Scope:
    return Scope.development(
        org_id="org_01HABC",
        workspace_id="ws_01HABC",
        project_id="prj_01HABC",
        group_id="grp_01HABC",
        topic_id=100,
    ).with_default_memory_scope()


@pytest.fixture
def personal_scope() -> Scope:
    return Scope.personal(user_id="usr_01HALICE").with_default_memory_scope()


# ---------------------------------------------------------------------- DbApprovalStore

class TestDbApprovalStore:
    @pytest.mark.asyncio
    async def test_create_and_get(self, db: Database, dev_scope: Scope) -> None:
        store = DbApprovalStore(db)
        req = ApprovalRequest(
            requester_id="usr_alice",
            action="merge_pr",
            scope=dev_scope,
        )
        await store.create_async(req)
        fetched = await store.get_async(req.id)
        assert fetched is not None
        assert fetched.requester_id == "usr_alice"
        assert fetched.action == "merge_pr"
        assert fetched.status is ApprovalStatus.PENDING

    @pytest.mark.asyncio
    async def test_list_pending(self, db: Database, dev_scope: Scope) -> None:
        store = DbApprovalStore(db)
        for i in range(3):
            await store.create_async(ApprovalRequest(
                requester_id=f"usr_user{i}",
                action="merge_pr",
                scope=dev_scope,
            ))
        pending = await store.list_pending_async(scope=dev_scope)
        assert len(pending) == 3

    @pytest.mark.asyncio
    async def test_resolve_persists(self, db: Database, dev_scope: Scope) -> None:
        store = DbApprovalStore(db)
        resolver = DbApprovalResolver(store)
        req = ApprovalRequest(
            requester_id="usr_alice",
            action="merge_pr",
            scope=dev_scope,
        )
        await store.create_async(req)
        result = await resolver.resolve_async(
            req.id, "usr_bob", ApprovalChoice.APPROVE,
        )
        assert result.status is ApprovalStatus.APPROVED

        # Verify it persisted.
        fetched = await store.get_async(req.id)
        assert fetched is not None
        assert fetched.status is ApprovalStatus.APPROVED
        assert fetched.approver_id == "usr_bob"


# ---------------------------------------------------------------------- DbSessionStore

class TestDbSessionStore:
    @pytest.mark.asyncio
    async def test_create_and_lookup(self, db: Database, dev_scope: Scope) -> None:
        store = DbSessionStore(db)
        session, token = await store.create_async(
            user_id="usr_alice", scope=dev_scope,
        )
        assert session.user_id == "usr_alice"
        assert token.startswith("zs_")

        looked_up = await store.lookup_async(token)
        assert looked_up.id == session.id
        assert looked_up.user_id == "usr_alice"

    @pytest.mark.asyncio
    async def test_revoke(self, db: Database, dev_scope: Scope) -> None:
        store = DbSessionStore(db)
        session, token = await store.create_async(
            user_id="usr_alice", scope=dev_scope,
        )
        assert await store.revoke_async(session.id, reason="manual") is True
        with pytest.raises(SessionRevokedError):
            await store.lookup_async(token)

    @pytest.mark.asyncio
    async def test_revoke_all_for_user(self, db: Database, dev_scope: Scope) -> None:
        store = DbSessionStore(db)
        s1, _ = await store.create_async(user_id="usr_alice", scope=dev_scope)
        s2, _ = await store.create_async(user_id="usr_alice", scope=dev_scope)
        count = await store.revoke_all_for_user_async("usr_alice")
        assert count >= 2

    @pytest.mark.asyncio
    async def test_expired_raises(self, db: Database, dev_scope: Scope) -> None:
        store = DbSessionStore(db)
        # Create with ttl=0 (expires immediately).
        _, token = await store.create_async(
            user_id="usr_alice", scope=dev_scope, ttl_seconds=0,
        )
        # Wait so datetime.now() is definitely past expires_at.
        import time
        time.sleep(0.2)
        with pytest.raises((SessionExpiredError, Exception)):
            await store.lookup_async(token)

    @pytest.mark.asyncio
    async def test_token_not_in_log(self, db: Database, dev_scope: Scope) -> None:
        """Token hash must NEVER appear in log output."""
        store = DbSessionStore(db)
        session, token = await store.create_async(
            user_id="usr_alice", scope=dev_scope,
        )
        d = session.to_log_dict()
        assert session.token_hash not in str(d)
        assert token not in str(d)


# ---------------------------------------------------------------------- DbRateLimiter

class TestDbRateLimiter:
    @pytest.mark.asyncio
    async def test_allows_under_limit(self, db: Database) -> None:
        limiter = DbRateLimiter(db)
        for _ in range(5):
            assert await limiter.check_and_increment("test_bucket", max_count=10) is True

    @pytest.mark.asyncio
    async def test_blocks_over_limit(self, db: Database) -> None:
        limiter = DbRateLimiter(db)
        for _ in range(3):
            assert await limiter.check_and_increment("blocked_bucket", max_count=3) is True
        # 4th should be blocked.
        assert await limiter.check_and_increment("blocked_bucket", max_count=3) is False

    @pytest.mark.asyncio
    async def test_reset(self, db: Database) -> None:
        limiter = DbRateLimiter(db)
        for _ in range(5):
            await limiter.check_and_increment("reset_bucket", max_count=100)
        count = await limiter.reset("reset_bucket")
        assert count > 0
        # Counter should be 0 now.
        current = await limiter.get_current_count("reset_bucket")
        assert current == 0


# ---------------------------------------------------------------------- DbRoleStore

class TestDbRoleStore:
    @pytest.mark.asyncio
    async def test_grant_and_lookup(self, db: Database, dev_scope: Scope) -> None:
        store = DbRoleStore(db)
        await store.grant_role_async(
            user_id="usr_alice",
            role=Role.MAINTAINER,
            scope_kind=RoleScopeKind.PROJECT,
            scope_id=dev_scope.project_id or "",
            granted_by="usr_admin",
        )
        role = await store.get_role_for_scope_async(
            user_id="usr_alice", scope=dev_scope,
        )
        assert role is Role.MAINTAINER

    @pytest.mark.asyncio
    async def test_no_binding_returns_agent(self, db: Database, dev_scope: Scope) -> None:
        store = DbRoleStore(db)
        role = await store.get_role_for_scope_async(
            user_id="usr_unknown", scope=dev_scope,
        )
        assert role is Role.AGENT

    @pytest.mark.asyncio
    async def test_personal_scope_returns_personal_user(
        self, db: Database, personal_scope: Scope
    ) -> None:
        store = DbRoleStore(db)
        role = await store.get_role_for_scope_async(
            user_id="usr_alice", scope=personal_scope,
        )
        assert role is Role.PERSONAL_USER

    @pytest.mark.asyncio
    async def test_revoke(self, db: Database, dev_scope: Scope) -> None:
        store = DbRoleStore(db)
        await store.grant_role_async(
            user_id="usr_alice",
            role=Role.MAINTAINER,
            scope_kind=RoleScopeKind.PROJECT,
            scope_id=dev_scope.project_id or "",
            granted_by="usr_admin",
        )
        await store.revoke_role_async(
            user_id="usr_alice",
            scope_kind=RoleScopeKind.PROJECT,
            scope_id=dev_scope.project_id or "",
            revoked_by="usr_admin",
        )
        role = await store.get_role_for_scope_async(
            user_id="usr_alice", scope=dev_scope,
        )
        # Revoked → no binding → AGENT.
        assert role is Role.AGENT


# ---------------------------------------------------------------------- DbTodoStore

class TestDbTodoStore:
    @pytest.mark.asyncio
    async def test_add_and_list(self, db: Database, dev_scope: Scope) -> None:
        store = DbTodoStore(db)
        await store.add_async(scope=dev_scope, text="task 1", created_by="usr_alice")
        await store.add_async(scope=dev_scope, text="task 2", created_by="usr_alice")
        items = await store.list_async(scope=dev_scope)
        assert len(items) == 2
        assert items[0].item_text == "task 1"
        assert items[1].item_text == "task 2"

    @pytest.mark.asyncio
    async def test_complete(self, db: Database, dev_scope: Scope) -> None:
        store = DbTodoStore(db)
        await store.add_async(scope=dev_scope, text="task", created_by="usr_alice")
        completed = await store.complete_async(scope=dev_scope, index=1)
        assert completed is not None
        assert completed.completed is True
        assert completed.completed_at is not None

    @pytest.mark.asyncio
    async def test_remove(self, db: Database, dev_scope: Scope) -> None:
        store = DbTodoStore(db)
        await store.add_async(scope=dev_scope, text="task", created_by="usr_alice")
        removed = await store.remove_async(scope=dev_scope, index=1)
        assert removed is not None
        assert removed.item_text == "task"
        # List should be empty.
        items = await store.list_async(scope=dev_scope)
        assert len(items) == 0


# ---------------------------------------------------------------------- DbConversationStore

class TestDbConversationStore:
    @pytest.mark.asyncio
    async def test_create_and_append(self, db: Database, dev_scope: Scope) -> None:
        store = DbConversationStore(db)
        session = await store.get_or_create_session_async(
            scope=dev_scope,
            external_chat_id="123",
            topic_id=100,
            user_id="usr_alice",
        )
        assert session.scope_key == dev_scope.retrieval_key()

        # Append messages.
        await store.append_message_async(
            scope=dev_scope, session=session,
            role="user", content="hello",
        )
        await store.append_message_async(
            scope=dev_scope, session=session,
            role="assistant", content="hi there",
        )

        history = await store.get_history_async(scope=dev_scope, session=session)
        assert len(history) == 2
        assert history[0].role == "user"
        assert history[0].content == "hello"
        assert history[1].role == "assistant"

    @pytest.mark.asyncio
    async def test_session_reuse(self, db: Database, dev_scope: Scope) -> None:
        store = DbConversationStore(db)
        s1 = await store.get_or_create_session_async(
            scope=dev_scope, external_chat_id="123", topic_id=100, user_id="usr_alice",
        )
        s2 = await store.get_or_create_session_async(
            scope=dev_scope, external_chat_id="123", topic_id=100, user_id="usr_alice",
        )
        assert s1.session_id == s2.session_id  # same session

    @pytest.mark.asyncio
    async def test_scope_change_creates_new_session(
        self, db: Database, dev_scope: Scope
    ) -> None:
        """Per T-4.18: scope change = new session."""
        store = DbConversationStore(db)
        s1 = await store.get_or_create_session_async(
            scope=dev_scope, external_chat_id="123", topic_id=100, user_id="usr_alice",
        )
        # Different scope (different project).
        dev_scope_2 = Scope.development(
            org_id="org_02HABC", workspace_id="ws_02HABC",
            project_id="prj_02HABC", group_id="grp_02HABC", topic_id=100,
        ).with_default_memory_scope()
        s2 = await store.get_or_create_session_async(
            scope=dev_scope_2, external_chat_id="123", topic_id=100, user_id="usr_alice",
        )
        assert s1.session_id != s2.session_id  # different sessions
