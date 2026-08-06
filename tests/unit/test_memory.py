"""Unit tests for zero.memory — Phase 6 acceptance criteria."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from zero.core.scope import Scope
from zero.memory.entry import MemoryEntry, MemoryKind, MemorySource
from zero.memory.fact_promotion import FactPromoter, FactPromotionError
from zero.memory.store import MemoryStore


@pytest.fixture
def personal_scope() -> Scope:
    return Scope.personal(user_id="usr_01HALICE").with_default_memory_scope()


@pytest.fixture
def normal_scope() -> Scope:
    return Scope.normal(group_id="grp_01HABC", topic_id=100).with_default_memory_scope()


@pytest.fixture
def dev_scope() -> Scope:
    return Scope.development(
        org_id="org_01HABC", workspace_id="ws_01HABC",
        project_id="prj_01HABC", group_id="grp_01HABC", topic_id=200,
    ).with_default_memory_scope()


@pytest.fixture
def dev_scope_other() -> Scope:
    return Scope.development(
        org_id="org_02HABC", workspace_id="ws_02HABC",
        project_id="prj_02HABC", group_id="grp_02HABC", topic_id=300,
    ).with_default_memory_scope()


# ---------------------------------------------------------------------- MemoryEntry invariants

class TestMemoryEntry:
    def test_basic_construction(self, dev_scope: Scope) -> None:
        e = MemoryEntry(
            scope=dev_scope,
            kind=MemoryKind.SEMANTIC,
            content="The API rate limit is 100 req/min",
            source=MemorySource(type="message", ref="msg_01HABC"),
            created_by="usr_01HALICE",
        )
        assert e.kind is MemoryKind.SEMANTIC
        assert e.is_valid

    def test_fact_requires_approved_by(self, dev_scope: Scope) -> None:
        """T-6.4 acceptance: no code path creates a Fact without approved_by."""
        with pytest.raises(ValueError, match="requires approved_by"):
            MemoryEntry(
                scope=dev_scope,
                kind=MemoryKind.FACT,
                content="The sky is blue",
                source=MemorySource(type="message", ref="msg_01HABC"),
                created_by="usr_01HALICE",
                # approved_by=None
            )

    def test_decision_requires_approved_by(self, dev_scope: Scope) -> None:
        with pytest.raises(ValueError, match="requires approved_by"):
            MemoryEntry(
                scope=dev_scope,
                kind=MemoryKind.DECISION,
                content="Use PostgreSQL",
                source=MemorySource(type="message", ref="msg_01HABC"),
                created_by="usr_01HALICE",
            )

    def test_normal_mode_forbids_fact(self, normal_scope: Scope) -> None:
        """T-4.11 acceptance: NORMAL mode forbids kind=fact."""
        with pytest.raises(ValueError, match="NORMAL mode forbids"):
            MemoryEntry(
                scope=normal_scope,
                kind=MemoryKind.FACT,
                content="some fact",
                source=MemorySource(type="message", ref="msg_01HABC"),
                created_by="usr_01HALICE",
                approved_by="usr_01HALICE",  # even with approver, NORMAL forbids
            )

    def test_normal_mode_forbids_decision(self, normal_scope: Scope) -> None:
        with pytest.raises(ValueError, match="NORMAL mode forbids"):
            MemoryEntry(
                scope=normal_scope,
                kind=MemoryKind.DECISION,
                content="some decision",
                source=MemorySource(type="message", ref="msg_01HABC"),
                created_by="usr_01HALICE",
                approved_by="usr_01HALICE",
            )

    def test_fact_with_approval_ok(self, dev_scope: Scope) -> None:
        e = MemoryEntry(
            scope=dev_scope,
            kind=MemoryKind.FACT,
            content="The API rate limit is 100 req/min",
            source=MemorySource(type="promotion", ref="promoted_from:mem_xxx"),
            created_by="usr_01HALICE",
            approved_by="usr_01HBOB",
        )
        assert e.kind is MemoryKind.FACT
        assert e.approved_by == "usr_01HBOB"

    def test_scratch_auto_expires(self, dev_scope: Scope) -> None:
        """Scratch kind requires expiry (auto-set if missing)."""
        e = MemoryEntry(
            scope=dev_scope,
            kind=MemoryKind.SCRATCH,
            content="working note",
            source=MemorySource(type="message", ref="msg_01HABC"),
            created_by="usr_01HALICE",
            topic_id=200,
        )
        assert e.expires_at is not None
        assert e.expires_at > datetime.now(UTC)

    def test_source_must_not_be_empty(self, dev_scope: Scope) -> None:
        with pytest.raises(ValueError, match="must both be non-empty"):
            MemoryEntry(
                scope=dev_scope,
                kind=MemoryKind.SEMANTIC,
                content="x",
                source=MemorySource(type="", ref=""),
                created_by="usr_01HALICE",
            )


# ---------------------------------------------------------------------- MemoryStore scope-bound retrieval

class TestMemoryStoreRetrieval:
    def test_retrieve_only_returns_scope_matches(
        self,
        personal_scope: Scope,
        normal_scope: Scope,
        dev_scope: Scope,
    ) -> None:
        store = MemoryStore()
        store.store(MemoryEntry(
            scope=personal_scope, kind=MemoryKind.SEMANTIC,
            content="personal secret", source=MemorySource(type="t", ref="1"),
            created_by="usr_01HALICE",
        ))
        store.store(MemoryEntry(
            scope=normal_scope, kind=MemoryKind.SEMANTIC,
            content="normal secret", source=MemorySource(type="t", ref="2"),
            created_by="usr_01HALICE",
        ))
        store.store(MemoryEntry(
            scope=dev_scope, kind=MemoryKind.SEMANTIC,
            content="dev secret", source=MemorySource(type="t", ref="3"),
            created_by="usr_01HALICE",
        ))

        # Personal retrieval only returns personal
        results = store.retrieve(personal_scope, query="secret")
        contents = [r.entry.content for r in results]
        assert "personal secret" in contents
        assert "normal secret" not in contents
        assert "dev secret" not in contents

    def test_personal_never_in_dev(
        self,
        personal_scope: Scope,
        dev_scope: Scope,
    ) -> None:
        """T-6.5 acceptance: in DEVELOPMENT mode, NO personal record ever returned."""
        store = MemoryStore()
        store.store(MemoryEntry(
            scope=personal_scope, kind=MemoryKind.SEMANTIC,
            content="user's personal secret", source=MemorySource(type="t", ref="1"),
            created_by="usr_01HALICE",
        ))
        # Try to retrieve from dev scope — should return nothing.
        results = store.retrieve(dev_scope, query="user's personal secret")
        assert len(results) == 0

    def test_normal_group_isolation(
        self,
        normal_scope: Scope,
        dev_scope_other: Scope,
    ) -> None:
        """Two different normal groups must not see each other's memory."""
        store = MemoryStore()
        store.store(MemoryEntry(
            scope=normal_scope, kind=MemoryKind.SEMANTIC,
            content="group A topic", source=MemorySource(type="t", ref="1"),
            created_by="usr_01HALICE",
        ))
        # Different group → different scope_key
        other_normal = Scope.normal(group_id="grp_OTHER", topic_id=999).with_default_memory_scope()
        results = store.retrieve(other_normal, query="group A topic")
        assert len(results) == 0

    def test_dev_project_isolation(
        self,
        dev_scope: Scope,
        dev_scope_other: Scope,
    ) -> None:
        """Two different dev projects must not see each other's memory."""
        store = MemoryStore()
        store.store(MemoryEntry(
            scope=dev_scope, kind=MemoryKind.SEMANTIC,
            content="project A fact", source=MemorySource(type="t", ref="1"),
            created_by="usr_01HALICE",
        ))
        results = store.retrieve(dev_scope_other, query="project A fact")
        assert len(results) == 0

    def test_retrieval_priority_fact_first(self, dev_scope: Scope) -> None:
        """T-6.5: Fact > Decision > Semantic > Episodic > Scratch."""
        store = MemoryStore()
        store.store(MemoryEntry(
            scope=dev_scope, kind=MemoryKind.SCRATCH,
            content="the answer is 42", source=MemorySource(type="t", ref="1"),
            created_by="usr_01HALICE", topic_id=200,
        ))
        store.store(MemoryEntry(
            scope=dev_scope, kind=MemoryKind.SEMANTIC,
            content="the answer is 42", source=MemorySource(type="t", ref="2"),
            created_by="usr_01HALICE",
        ))
        store.store(MemoryEntry(
            scope=dev_scope, kind=MemoryKind.FACT,
            content="the answer is 42", source=MemorySource(type="promotion", ref="3"),
            created_by="usr_01HALICE", approved_by="usr_01HBOB",
        ))
        results = store.retrieve(dev_scope, query="answer 42", limit=10)
        # Fact should come first
        assert results[0].entry.kind is MemoryKind.FACT

    def test_invalidated_excluded(self, dev_scope: Scope) -> None:
        store = MemoryStore()
        entry = MemoryEntry(
            scope=dev_scope, kind=MemoryKind.SEMANTIC,
            content="old fact", source=MemorySource(type="t", ref="1"),
            created_by="usr_01HALICE",
        )
        store.store(entry)
        store.invalidate(entry.id, invalidated_by="usr_01HBOB", reason="outdated")
        results = store.retrieve(dev_scope, query="old fact")
        assert len(results) == 0

    def test_token_budget_enforced(self, dev_scope: Scope) -> None:
        store = MemoryStore()
        # Add many entries
        for i in range(20):
            store.store(MemoryEntry(
                scope=dev_scope, kind=MemoryKind.SEMANTIC,
                content=f"item {i} " * 100,  # ~700 chars each
                source=MemorySource(type="t", ref=f"{i}"),
                created_by="usr_01HALICE",
            ))
        # Limit to ~1000 tokens (~4000 chars)
        results = store.retrieve(dev_scope, query="item", max_tokens=1000)
        total_chars = sum(len(r.entry.content) for r in results)
        assert total_chars <= 4500  # some headroom

    def test_include_kinds_filter(self, dev_scope: Scope) -> None:
        store = MemoryStore()
        store.store(MemoryEntry(
            scope=dev_scope, kind=MemoryKind.SEMANTIC,
            content="semantic", source=MemorySource(type="t", ref="1"),
            created_by="usr_01HALICE",
        ))
        store.store(MemoryEntry(
            scope=dev_scope, kind=MemoryKind.EPISODIC,
            content="episodic", source=MemorySource(type="t", ref="2"),
            created_by="usr_01HALICE",
        ))
        results = store.retrieve(
            dev_scope, query="semantic OR episodic",
            include_kinds=frozenset({MemoryKind.SEMANTIC}),
        )
        assert all(r.entry.kind is MemoryKind.SEMANTIC for r in results)

    def test_export_scope(self, dev_scope: Scope) -> None:
        store = MemoryStore()
        store.store(MemoryEntry(
            scope=dev_scope, kind=MemoryKind.SEMANTIC,
            content="x", source=MemorySource(type="t", ref="1"),
            created_by="usr_01HALICE",
        ))
        store.store(MemoryEntry(
            scope=Scope.normal(group_id="grp_OTHER", topic_id=999).with_default_memory_scope(),
            kind=MemoryKind.SEMANTIC,
            content="y", source=MemorySource(type="t", ref="2"),
            created_by="usr_01HALICE",
        ))
        exported = store.export_scope(dev_scope)
        assert len(exported) == 1
        assert exported[0].content == "x"


# ---------------------------------------------------------------------- FactPromoter

class TestFactPromoter:
    @pytest.mark.asyncio
    async def test_promote_to_fact(
        self,
        dev_scope: Scope,
    ) -> None:
        from zero.core.audit import ActorType, AuditLogger, set_audit_logger
        from zero.core.permissions import PermissionContext, Role
        from zero.db import Database
        from zero.db.sqlite_backend import InMemorySqliteBackend

        # Set up audit logger
        backend = InMemorySqliteBackend()
        db = Database(backend=backend)
        await db.start()
        set_audit_logger(AuditLogger(db))

        store = MemoryStore()
        promoter = FactPromoter(memory_store=store)

        # Source entry
        source = MemoryEntry(
            scope=dev_scope, kind=MemoryKind.SEMANTIC,
            content="API rate limit is 100 req/min",
            source=MemorySource(type="message", ref="msg_01HABC"),
            created_by="usr_01HALICE",
        )
        store.store(source)

        # Permission context for a maintainer
        ctx = PermissionContext(
            actor_id="usr_01HBOB",
            actor_type=ActorType.HUMAN,
            scope=dev_scope,
            role=Role.MAINTAINER,
        )

        # Promote
        fact = await promoter.promote(
            source_entry=source,
            approved_by="usr_01HBOB",
            permission_ctx=ctx,
        )
        assert fact.kind is MemoryKind.FACT
        assert fact.approved_by == "usr_01HBOB"

        # Source should be invalidated
        assert source.invalidated_at is not None

        await db.stop()

    @pytest.mark.asyncio
    async def test_promote_without_permission_rejected(
        self,
        dev_scope: Scope,
    ) -> None:
        from zero.core.audit import ActorType, AuditLogger, set_audit_logger
        from zero.core.permissions import PermissionContext, PermissionDenied, Role
        from zero.db import Database
        from zero.db.sqlite_backend import InMemorySqliteBackend

        backend = InMemorySqliteBackend()
        db = Database(backend=backend)
        await db.start()
        set_audit_logger(AuditLogger(db))

        store = MemoryStore()
        promoter = FactPromoter(memory_store=store)

        source = MemoryEntry(
            scope=dev_scope, kind=MemoryKind.SEMANTIC,
            content="x", source=MemorySource(type="t", ref="1"),
            created_by="usr_01HALICE",
        )
        store.store(source)

        # Developer role — does not have promote_fact permission
        ctx = PermissionContext(
            actor_id="usr_01HALICE",
            actor_type=ActorType.HUMAN,
            scope=dev_scope,
            role=Role.DEVELOPER,
        )

        with pytest.raises(PermissionDenied):
            await promoter.promote(
                source_entry=source,
                approved_by="usr_01HALICE",
                permission_ctx=ctx,
            )

        await db.stop()

    @pytest.mark.asyncio
    async def test_promote_in_normal_scope_rejected(
        self,
        normal_scope: Scope,
    ) -> None:
        """Facts are project-scoped — cannot promote in NORMAL mode."""
        from zero.core.audit import ActorType, AuditLogger, set_audit_logger
        from zero.core.permissions import PermissionContext, Role
        from zero.db import Database
        from zero.db.sqlite_backend import InMemorySqliteBackend

        backend = InMemorySqliteBackend()
        db = Database(backend=backend)
        await db.start()
        set_audit_logger(AuditLogger(db))

        store = MemoryStore()
        promoter = FactPromoter(memory_store=store)

        source = MemoryEntry(
            scope=normal_scope, kind=MemoryKind.SEMANTIC,
            content="x", source=MemorySource(type="t", ref="1"),
            created_by="usr_01HALICE",
        )
        store.store(source)

        ctx = PermissionContext(
            actor_id="usr_01HBOB",
            actor_type=ActorType.HUMAN,
            scope=normal_scope,
            role=Role.MAINTAINER,
        )

        with pytest.raises(FactPromotionError, match="DEVELOPMENT scope"):
            await promoter.promote(
                source_entry=source,
                approved_by="usr_01HBOB",
                permission_ctx=ctx,
            )

        await db.stop()
