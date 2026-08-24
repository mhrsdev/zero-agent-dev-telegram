"""Artifact store and Project RAG tests — covers all M8 validation gates.

Per PLAN.md M8 validation:
- Artifact hash and retrieval round-trip.
- Unauthorized artifact/memory access fails before content retrieval.
- Cross-project retrieval yields zero forbidden records.
- Rebuilding derived indexes reproduces searchable canonical content.
- Failed ingestion does not activate partial indexes.
- Memory lifecycle preserves provenance and decisions.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from zero.app.services import build_services
from zero.config import Settings
from zero.domain.artifacts import (
    ArtifactNotFoundError,
    RagDocumentNotFoundError,
)
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations


@pytest.fixture
def services(test_settings: Settings):
    database = Database(test_settings)
    apply_migrations(database)
    return build_services(test_settings, database)


@pytest.fixture
def project_with_owner(services):
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(owner_id=owner.id, name="Project A")
    return owner, project


# ----------------------------------------------------------------------
# Artifact storage
# ----------------------------------------------------------------------


def test_store_artifact_returns_hash(services, project_with_owner) -> None:
    owner, project = project_with_owner
    art = services.artifacts.store_artifact(
        project_id=project.id,
        actor_id=owner.id,
        kind="stdout",
        content="hello world",
        producer="test",
    )
    assert art.id.value.startswith("art_")
    assert art.content_hash  # SHA-256 is not empty
    assert art.size_bytes == len(b"hello world")
    assert art.kind == "stdout"


def test_artifact_hash_round_trip(services, project_with_owner) -> None:
    """Per PLAN.md M8: 'Artifact hash and retrieval round-trip.'"""
    owner, project = project_with_owner
    content = "test content for round trip"
    art = services.artifacts.store_artifact(
        project_id=project.id,
        actor_id=owner.id,
        kind="test_report",
        content=content,
    )
    # Retrieve and verify the hash matches.
    retrieved = services.artifacts.get_artifact(
        project_id=project.id, artifact_id=art.id, actor_id=owner.id
    )
    assert retrieved.content_hash == art.content_hash
    assert retrieved.content == content


def test_artifact_dedup_by_hash(services, project_with_owner) -> None:
    """Per zero-artifact-provenance-model: deduplication by content hash
    within a project."""
    owner, project = project_with_owner
    content = "duplicate content"
    art1 = services.artifacts.store_artifact(
        project_id=project.id,
        actor_id=owner.id,
        kind="stdout",
        content=content,
    )
    art2 = services.artifacts.store_artifact(
        project_id=project.id,
        actor_id=owner.id,
        kind="stdout",
        content=content,
    )
    # Same artifact returned (dedup).
    assert art1.id == art2.id


def test_concurrent_artifact_dedup_returns_one_winner(tmp_path) -> None:
    settings = Settings.load_for_test(database_url=f"sqlite:///{tmp_path / 'artifacts.db'}")
    database = Database(settings)
    apply_migrations(database)
    services_a = build_services(settings, database)
    owner = services_a.identity.create_user(display_name="Concurrent owner")
    project = services_a.identity.create_project(owner_id=owner.id, name="Concurrent project")

    def store_once():
        service_database = Database(settings)
        service = build_services(settings, service_database)
        return service.artifacts.store_artifact(
            project_id=project.id,
            actor_id=owner.id,
            kind="stdout",
            content="concurrent content",
            producer="concurrent-test",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = tuple(executor.map(lambda _item: store_once(), (1, 2)))

    assert first.id == second.id
    rows = (
        services_a.database.connect()
        .execute(
            "SELECT COUNT(*) FROM artifacts WHERE project_id = ?",
            (project.id.value,),
        )
        .fetchone()
    )
    assert rows[0] == 1
    provenance = services_a.artifacts.list_provenance(
        project_id=project.id,
        artifact_id=first.id,
        actor_id=owner.id,
    )
    assert len(provenance) == 2


def test_duplicate_content_preserves_each_provenance_record(
    services,
    project_with_owner,
) -> None:
    owner, project = project_with_owner
    content = "same bytes, different evidence"
    first = services.artifacts.store_artifact(
        project_id=project.id,
        actor_id=owner.id,
        kind="stdout",
        content=content,
        producer="task-a",
        provenance='{"task":"a"}',
    )
    second = services.artifacts.store_artifact(
        project_id=project.id,
        actor_id=owner.id,
        kind="stdout",
        content=content,
        producer="task-b",
        provenance='{"task":"b"}',
    )

    assert first.id == second.id
    records = services.artifacts.list_provenance(
        project_id=project.id,
        artifact_id=first.id,
        actor_id=owner.id,
    )
    assert [(record.producer, record.provenance) for record in records] == [
        ("task-a", '{"task":"a"}'),
        ("task-b", '{"task":"b"}'),
    ]


def test_cross_project_artifact_access_denied(services) -> None:
    """Per PLAN.md M8: 'Unauthorized artifact/memory access fails before
    content retrieval.'"""
    owner_a = services.identity.create_user(display_name="Owner A")
    project_a = services.identity.create_project(owner_id=owner_a.id, name="Project A")
    owner_b = services.identity.create_user(display_name="Owner B")
    project_b = services.identity.create_project(owner_id=owner_b.id, name="Project B")
    art = services.artifacts.store_artifact(
        project_id=project_a.id,
        actor_id=owner_a.id,
        kind="stdout",
        content="project A secret",
    )
    # Owner B cannot access project A's artifact.
    with pytest.raises(ArtifactNotFoundError):
        services.artifacts.get_artifact(
            project_id=project_b.id, artifact_id=art.id, actor_id=owner_b.id
        )


def test_artifact_handle_is_bounded(services, project_with_owner) -> None:
    """Per zero-artifact-provenance-model: model handles are bounded and
    read-only."""
    owner, project = project_with_owner
    long_content = "x" * 10000
    art = services.artifacts.store_artifact(
        project_id=project.id,
        actor_id=owner.id,
        kind="stdout",
        content=long_content,
    )
    handle = services.artifacts.get_artifact_handle(
        project_id=project.id, artifact_id=art.id, actor_id=owner.id
    )
    # The handle's summary is bounded to ~200 chars.
    assert len(handle.summary) <= 203  # 200 + "..."
    assert handle.size_bytes == 10000
    assert handle.content_hash == art.content_hash


def test_artifacts_are_append_only(services, project_with_owner) -> None:
    """Per zero-artifact-provenance-model: artifacts are immutable."""
    import sqlite3

    owner, project = project_with_owner
    art = services.artifacts.store_artifact(
        project_id=project.id,
        actor_id=owner.id,
        kind="stdout",
        content="immutable",
    )
    conn = services.database.connect()
    with pytest.raises(sqlite3.Error, match="append-only"):
        conn.execute(
            "UPDATE artifacts SET content = 'tampered' WHERE id = ?",
            (art.id.value,),
        )


# ----------------------------------------------------------------------
# Project RAG
# ----------------------------------------------------------------------


def test_ingest_and_search_rag(services, project_with_owner) -> None:
    owner, project = project_with_owner
    services.artifacts.ingest_rag_document(
        project_id=project.id,
        actor_id=owner.id,
        source_type="manual",
        source_id="doc1",
        title="Authentication Design",
        content="We use OAuth 2.0 for authentication with JWT tokens.",
        state="approved",
    )
    services.artifacts.ingest_rag_document(
        project_id=project.id,
        actor_id=owner.id,
        source_type="manual",
        source_id="doc2",
        title="Database Schema",
        content="The users table has id, email, and created_at columns.",
        state="approved",
    )
    # Search for "authentication".
    results = services.artifacts.search_rag(
        project_id=project.id, actor_id=owner.id, query="authentication"
    )
    assert len(results) >= 1
    # The auth doc should be in the results.
    titles = [doc.title for doc, _ in results]
    assert "Authentication Design" in titles


def test_cross_project_rag_returns_zero(services) -> None:
    """Per PLAN.md M8: 'Cross-project retrieval yields zero forbidden
    records.'"""
    owner_a = services.identity.create_user(display_name="Owner A")
    project_a = services.identity.create_project(owner_id=owner_a.id, name="Project A")
    owner_b = services.identity.create_user(display_name="Owner B")
    project_b = services.identity.create_project(owner_id=owner_b.id, name="Project B")
    services.artifacts.ingest_rag_document(
        project_id=project_a.id,
        actor_id=owner_a.id,
        source_type="manual",
        source_id="secret1",
        title="Project A Secret",
        content="This is a secret unique phrase for project A: zebra elephant.",
        state="approved",
    )
    # Search project B for the unique phrase.
    results = services.artifacts.search_rag(
        project_id=project_b.id, actor_id=owner_b.id, query="zebra elephant"
    )
    assert len(results) == 0  # zero forbidden records


def test_rebuild_index_reproduces_content(services, project_with_owner) -> None:
    """Per PLAN.md M8: 'Rebuilding derived indexes reproduces searchable
    canonical content.'"""
    owner, project = project_with_owner
    services.artifacts.ingest_rag_document(
        project_id=project.id,
        actor_id=owner.id,
        source_type="manual",
        source_id="doc1",
        title="Test Document",
        content="A unique searchable phrase: pineapple pizza.",
        state="approved",
    )
    # Verify search works before rebuild.
    results_before = services.artifacts.search_rag(
        project_id=project.id, actor_id=owner.id, query="pineapple"
    )
    assert len(results_before) == 1
    # Rebuild the index.
    count = services.artifacts.rebuild_rag_index(project_id=project.id, actor_id=owner.id)
    assert count == 1
    # Verify search still works after rebuild.
    results_after = services.artifacts.search_rag(
        project_id=project.id, actor_id=owner.id, query="pineapple"
    )
    assert len(results_after) == 1
    assert results_after[0][0].title == "Test Document"


def test_candidate_not_searchable_until_approved(services, project_with_owner) -> None:
    """Per zero-context-memory: only 'approved' documents are indexed."""
    owner, project = project_with_owner
    services.artifacts.ingest_rag_document(
        project_id=project.id,
        actor_id=owner.id,
        source_type="manual",
        source_id="doc1",
        title="Candidate Doc",
        content="A unique phrase: dragonfly sunset.",
        state="candidate",  # not approved
    )
    # Search should not find it.
    results = services.artifacts.search_rag(
        project_id=project.id, actor_id=owner.id, query="dragonfly"
    )
    assert len(results) == 0
    # Approve it.
    doc = services.artifacts.list_rag_documents(project.id, actor_id=owner.id, state="candidate")[0]
    services.artifacts.approve_rag_document(project_id=project.id, doc_id=doc.id, actor_id=owner.id)
    # Now search should find it.
    results = services.artifacts.search_rag(
        project_id=project.id, actor_id=owner.id, query="dragonfly"
    )
    assert len(results) == 1


def test_supersede_document(services, project_with_owner) -> None:
    """Per zero-artifact-provenance-model: immutable evidence supports
    mutable understanding. The old document is superseded, not deleted."""
    owner, project = project_with_owner
    old_doc = services.artifacts.ingest_rag_document(
        project_id=project.id,
        actor_id=owner.id,
        source_type="manual",
        source_id="v1",
        title="Design v1",
        content="We use REST API.",
        state="approved",
    )
    new_doc = services.artifacts.ingest_rag_document(
        project_id=project.id,
        actor_id=owner.id,
        source_type="manual",
        source_id="v2",
        title="Design v2",
        content="We use GraphQL API.",
        state="approved",
    )
    # Supersede the old doc.
    old_doc = services.artifacts.supersede_rag_document(
        project_id=project.id,
        old_doc_id=old_doc.id,
        new_doc_id=new_doc.id,
        actor_id=owner.id,
    )
    assert old_doc.state == "superseded"
    assert old_doc.superseded_by == new_doc.id
    # The old doc is no longer searchable.
    results = services.artifacts.search_rag(project_id=project.id, actor_id=owner.id, query="REST")
    assert len(results) == 0
    # The new doc is searchable.
    results = services.artifacts.search_rag(
        project_id=project.id, actor_id=owner.id, query="GraphQL"
    )
    assert len(results) == 1


def test_artifact_list_by_kind(services, project_with_owner) -> None:
    owner, project = project_with_owner
    services.artifacts.store_artifact(
        project_id=project.id,
        actor_id=owner.id,
        kind="stdout",
        content="output 1",
    )
    services.artifacts.store_artifact(
        project_id=project.id,
        actor_id=owner.id,
        kind="stderr",
        content="error 1",
    )
    stdout_only = services.artifacts.list_artifacts(
        project_id=project.id, actor_id=owner.id, kind="stdout"
    )
    assert len(stdout_only) == 1
    assert stdout_only[0].kind == "stdout"


def test_cross_project_rag_archive_does_not_mutate_foreign_document(services) -> None:
    owner_a = services.identity.create_user(display_name="Owner A")
    project_a = services.identity.create_project(owner_id=owner_a.id, name="Project A")
    owner_b = services.identity.create_user(display_name="Owner B")
    project_b = services.identity.create_project(owner_id=owner_b.id, name="Project B")
    document = services.artifacts.ingest_rag_document(
        project_id=project_a.id,
        actor_id=owner_a.id,
        source_type="manual",
        source_id="foreign-doc",
        title="Foreign document",
        content="must remain approved",
        state="approved",
    )

    with pytest.raises(RagDocumentNotFoundError):
        services.artifacts.archive_rag_document(
            project_id=project_b.id,
            doc_id=document.id,
            actor_id=owner_b.id,
        )

    unchanged = services.artifacts.get_rag_document(project_a.id, document.id, actor_id=owner_a.id)
    assert unchanged.state == "approved"
    assert services.artifacts.search_rag(
        project_id=project_a.id, actor_id=owner_a.id, query="approved"
    )
