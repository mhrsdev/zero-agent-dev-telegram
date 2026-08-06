"""Tests for DbMemoryStore — DB-backed memory with TF-IDF semantic search."""
from __future__ import annotations

import pytest

from zero.core.scope import Scope
from zero.db import Database
from zero.db.sqlite_backend import InMemorySqliteBackend
from zero.memory.db_store import DbMemoryStore, TfidfIndex
from zero.memory.entry import MemoryEntry, MemoryKind, MemorySource


@pytest.fixture
async def db() -> Database:
    backend = InMemorySqliteBackend()
    d = Database(backend=backend)
    await d.start()
    # Seed dev schema with org, workspace, project for FK constraints.
    dev_seed_scope = Scope.development(
        org_id="org_01HABC", workspace_id="ws_01HABC",
        project_id="prj_01HABC", group_id="grp_01HABC", topic_id=100,
    ).with_default_memory_scope()
    async with d.connection_for(dev_seed_scope) as conn:
        await conn.execute(
            "INSERT INTO dev_orgs (org_id, display_name, owner_type, owner_id) VALUES (?, ?, ?, ?)",
            ("org_01HABC", "Test Org", "user", "usr_test"),
        )
        await conn.execute(
            "INSERT INTO dev_workspaces (workspace_id, org_id, display_name, is_default) VALUES (?, ?, ?, ?)",
            ("ws_01HABC", "org_01HABC", "Default", True),
        )
        await conn.execute(
            "INSERT INTO dev_projects (project_id, workspace_id, display_name) VALUES (?, ?, ?)",
            ("prj_01HABC", "ws_01HABC", "Test Project"),
        )
    # Seed personal schema with user.
    async with d.connection_for(Scope.personal(user_id="usr_01HALICE").with_default_memory_scope()) as conn:
        await conn.execute(
            "INSERT INTO personal_users (user_id, display_name) VALUES (?, ?)",
            ("usr_01HALICE", "Alice"),
        )
    yield d
    await d.stop()


@pytest.fixture
def dev_scope() -> Scope:
    return Scope.development(
        org_id="org_01HABC", workspace_id="ws_01HABC",
        project_id="prj_01HABC", group_id="grp_01HABC", topic_id=100,
    ).with_default_memory_scope()


@pytest.fixture
def personal_scope() -> Scope:
    return Scope.personal(user_id="usr_01HALICE").with_default_memory_scope()


class TestTfidfIndex:
    def test_add_and_score(self) -> None:
        idx = TfidfIndex()
        idx.add("doc1", "Python is a programming language")
        idx.add("doc2", "JavaScript is also a programming language")
        idx.add("doc3", "Cooking recipes for Italian food")

        # "programming" should score higher in doc1/doc2 than doc3.
        score1 = idx.score("doc1", "programming language")
        score3 = idx.score("doc3", "programming language")
        assert score1 > score3
        assert score1 > 0

    def test_remove(self) -> None:
        idx = TfidfIndex()
        idx.add("doc1", "hello world")
        assert idx.score("doc1", "hello") > 0
        idx.remove("doc1")
        assert idx.score("doc1", "hello") == 0

    def test_empty_query(self) -> None:
        idx = TfidfIndex()
        idx.add("doc1", "hello world")
        assert idx.score("doc1", "") == 0

    def test_no_overlap(self) -> None:
        idx = TfidfIndex()
        idx.add("doc1", "hello world")
        assert idx.score("doc1", "cooking recipes") == 0


class TestDbMemoryStore:
    @pytest.mark.asyncio
    async def test_store_and_retrieve(self, db: Database, dev_scope: Scope) -> None:
        store = DbMemoryStore(db)
        entry = MemoryEntry(
            scope=dev_scope, kind=MemoryKind.SEMANTIC,
            content="Python is a programming language",
            source=MemorySource(type="test", ref="1"),
            created_by="usr_test",
        )
        await store.store(entry)

        results = await store.retrieve(dev_scope, "programming language")
        assert len(results) == 1
        assert "Python" in results[0].entry.content

    @pytest.mark.asyncio
    async def test_scope_isolation(self, db: Database, dev_scope: Scope, personal_scope: Scope) -> None:
        """Personal memory never retrieved in dev scope."""
        store = DbMemoryStore(db)
        await store.store(MemoryEntry(
            scope=personal_scope, kind=MemoryKind.SEMANTIC,
            content="personal secret",
            source=MemorySource(type="test", ref="1"),
            created_by="usr_test",
        ))
        await store.store(MemoryEntry(
            scope=dev_scope, kind=MemoryKind.SEMANTIC,
            content="project fact",
            source=MemorySource(type="test", ref="2"),
            created_by="usr_test",
        ))

        # Dev scope retrieval should NOT return personal.
        dev_results = await store.retrieve(dev_scope, "secret")
        assert len(dev_results) == 0

        # Personal scope retrieval should return personal.
        personal_results = await store.retrieve(personal_scope, "secret")
        assert len(personal_results) == 1
        assert "personal secret" in personal_results[0].entry.content

    @pytest.mark.asyncio
    async def test_tfidf_ranking(self, db: Database, dev_scope: Scope) -> None:
        """TF-IDF ranks more relevant entries higher."""
        store = DbMemoryStore(db)
        await store.store(MemoryEntry(
            scope=dev_scope, kind=MemoryKind.SEMANTIC,
            content="Python programming tutorial",
            source=MemorySource(type="test", ref="1"),
            created_by="usr_test",
        ))
        await store.store(MemoryEntry(
            scope=dev_scope, kind=MemoryKind.SEMANTIC,
            content="Cooking Italian pasta recipes",
            source=MemorySource(type="test", ref="2"),
            created_by="usr_test",
        ))

        results = await store.retrieve(dev_scope, "Python programming")
        assert len(results) >= 1
        # Most relevant should be the Python one.
        assert "Python" in results[0].entry.content

    @pytest.mark.asyncio
    async def test_invalidate(self, db: Database, dev_scope: Scope) -> None:
        store = DbMemoryStore(db)
        entry = MemoryEntry(
            scope=dev_scope, kind=MemoryKind.SEMANTIC,
            content="temporary fact",
            source=MemorySource(type="test", ref="1"),
            created_by="usr_test",
        )
        await store.store(entry)

        # Should be retrievable.
        results = await store.retrieve(dev_scope, "temporary")
        assert len(results) == 1

        # Invalidate.
        await store.invalidate(entry.id, invalidated_by="usr_test", reason="outdated", scope=dev_scope)

        # Should NOT be retrievable.
        results = await store.retrieve(dev_scope, "temporary")
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_priority_ordering(self, db: Database, dev_scope: Scope) -> None:
        """Fact > Decision > Semantic > Episodic in retrieval priority."""
        store = DbMemoryStore(db)
        await store.store(MemoryEntry(
            scope=dev_scope, kind=MemoryKind.SEMANTIC,
            content="the answer is 42",
            source=MemorySource(type="test", ref="1"),
            created_by="usr_test",
        ))
        await store.store(MemoryEntry(
            scope=dev_scope, kind=MemoryKind.FACT,
            content="the answer is 42",
            source=MemorySource(type="promotion", ref="2"),
            created_by="usr_test",
            approved_by="usr_maintainer",
        ))

        results = await store.retrieve(dev_scope, "answer 42")
        assert len(results) >= 1
        # Fact should come first.
        assert results[0].entry.kind is MemoryKind.FACT

    @pytest.mark.asyncio
    async def test_export_scope(self, db: Database, dev_scope: Scope) -> None:
        store = DbMemoryStore(db)
        await store.store(MemoryEntry(
            scope=dev_scope, kind=MemoryKind.SEMANTIC,
            content="entry 1",
            source=MemorySource(type="test", ref="1"),
            created_by="usr_test",
        ))
        await store.store(MemoryEntry(
            scope=dev_scope, kind=MemoryKind.SEMANTIC,
            content="entry 2",
            source=MemorySource(type="test", ref="2"),
            created_by="usr_test",
        ))
        exported = await store.export_scope(dev_scope)
        assert len(exported) == 2

    @pytest.mark.asyncio
    async def test_persists_across_restart(self, db: Database, dev_scope: Scope) -> None:
        """Data persists in DB even after the store object is destroyed."""
        store1 = DbMemoryStore(db)
        await store1.store(MemoryEntry(
            scope=dev_scope, kind=MemoryKind.SEMANTIC,
            content="persistent fact",
            source=MemorySource(type="test", ref="1"),
            created_by="usr_test",
        ))

        # Create a new store instance (simulates restart).
        store2 = DbMemoryStore(db)
        results = await store2.retrieve(dev_scope, "persistent")
        assert len(results) == 1
        assert "persistent fact" in results[0].entry.content
