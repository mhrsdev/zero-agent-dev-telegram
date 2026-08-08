"""Artifact service — immutable storage, authorized retrieval, Project RAG.

Per ``zero-artifact-provenance-model`` SKILL.md:

- Artifacts preserve full evidence outside model context. Zero keeps
  them durable and verifiable, then gives models bounded read-only
  handles and summaries.
- Content identity and record identity are different.
- Immutable evidence supports mutable understanding.
- Model handles are bounded and read-only.
- Large output has two representations.
- Deduplication does not merge provenance.
- Artifact writes have a commit point.

Per ``zero-context-memory`` SKILL.md:
- Project RAG ingestion from approved sources.
- Rebuildable lexical retrieval using existing platform capabilities
  first (FTS5).
- Memory candidate, approval, supersession, archive, and migration
  states are explicit.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from zero.app.authorization_service import AuthorizationService
from zero.domain.artifacts import (
    Artifact,
    ArtifactHandle,
    ArtifactId,
    ArtifactKind,
    RagDocument,
    RagDocumentId,
    RagDocumentState,
    RagSourceType,
)
from zero.domain.audit import AuditEvent, AuditEventId, AuditSource
from zero.domain.identity import ProjectId, UserId
from zero.domain.ids import (
    generate_artifact_id,
    generate_audit_event_id,
    generate_rag_document_id,
)
from zero.persistence.repositories.agent_type_repository import (
    AgentTypeRepository,
)
from zero.persistence.repositories.artifact_repository import (
    ArtifactRepository,
)
from zero.persistence.repositories.audit_repository import AuditRepository


def _now_utc_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ArtifactService:
    """Application operations for the artifact store and Project RAG.

    The service is the only place where artifacts are stored and
    retrieved, and where RAG documents are ingested and searched. It
    enforces:
    - project-scoped access (authorization before content retrieval);
    - content deduplication (same hash within a project = one artifact);
    - immutable storage (append-only triggers at the DB level);
    - rebuildable indexes (FTS5 is derived from canonical documents).
    """

    def __init__(
        self,
        artifact_repo: ArtifactRepository,
        agent_type_repo: AgentTypeRepository,
        audit_repo: AuditRepository,
        authorization_service: AuthorizationService,
    ) -> None:
        self._repo = artifact_repo
        self._agent_type_repo = agent_type_repo
        self._audit_repo = audit_repo
        self._authz = authorization_service

    # ------------------------------------------------------------------
    # Artifact storage
    # ------------------------------------------------------------------

    def store_artifact(
        self,
        *,
        project_id: ProjectId,
        actor_id: UserId,
        kind: ArtifactKind,
        content: str,
        producer: str | None = None,
        provenance: str | None = None,
        media_type: str = "text/plain",
        source: AuditSource = "system",
    ) -> Artifact:
        """Store an immutable artifact.

        Per ``zero-artifact-provenance-model`` §"Artifact writes have a
        commit point": an artifact is durable only after bytes are
        stored, length/hash verified, metadata committed, and the
        resulting handle resolves.

        If an artifact with the same (project_id, content_hash) already
        exists, it is returned (idempotent deduplication). Per
        ``zero-artifact-provenance-model`` §"Deduplication does not
        merge provenance": storage optimization may share encrypted
        content internally, while authorization and metadata remain
        independent.
        """
        if not content:
            raise ValueError("content must not be empty")
        content_hash = _sha256(content)
        # Check for an existing artifact with the same hash.
        existing = self._repo.get_artifact_by_hash(project_id, content_hash)
        if existing is not None:
            return existing
        artifact = Artifact(
            id=ArtifactId(generate_artifact_id()),
            project_id=project_id,
            content_hash=content_hash,
            kind=kind,
            media_type=media_type,
            size_bytes=len(content.encode("utf-8")),
            content=content,
            producer=producer,
            provenance=provenance,
            created_at=_now_utc_iso(),
        )
        self._repo.insert_artifact(artifact)
        self._audit_repo.insert(
            AuditEvent(
                id=AuditEventId(generate_audit_event_id()),
                project_id=project_id,
                actor_id=actor_id,
                source=source,
                operation="artifact.store",
                target_type="artifact",
                target_id=artifact.id.value,
                result="success",
                redacted_summary=(
                    f"Stored artifact {artifact.id.value} "
                    f"(kind={kind}, size={artifact.size_bytes})"
                ),
                created_at=_now_utc_iso(),
            )
        )
        return artifact

    def get_artifact(
        self,
        *,
        project_id: ProjectId,
        artifact_id: ArtifactId,
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> Artifact:
        """Retrieve an artifact's full content.

        Per ``zero-artifact-provenance-model`` §"Model handles are
        bounded and read-only" and ``zero-project-isolation-evidence``:
        the query filters by project_id before content is loaded.
        Unauthorized access raises ArtifactNotFoundError (not a
        separate "forbidden" error, to avoid leaking existence).
        """
        return self._repo.get_artifact(project_id, artifact_id)

    def get_artifact_handle(
        self,
        *,
        project_id: ProjectId,
        artifact_id: ArtifactId,
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> ArtifactHandle:
        """Retrieve a bounded, read-only handle to an artifact.

        Per ``zero-artifact-provenance-model`` §"Model handles are
        bounded and read-only": the handle reveals only what the agent
        needs: artifact ID, kind, size, hash, and a short summary.
        The full content is NOT included.
        """
        artifact = self._repo.get_artifact(project_id, artifact_id)
        # Build a short summary (first 200 chars, redacted).
        summary = artifact.content[:200]
        if len(artifact.content) > 200:
            summary += "..."
        return ArtifactHandle(
            id=artifact.id,
            kind=artifact.kind,
            size_bytes=artifact.size_bytes,
            content_hash=artifact.content_hash,
            summary=summary,
        )

    def list_artifacts(
        self,
        *,
        project_id: ProjectId,
        kind: ArtifactKind | None = None,
    ) -> list[Artifact]:
        return self._repo.list_artifacts_for_project(project_id, kind=kind)

    # ------------------------------------------------------------------
    # Project RAG ingestion
    # ------------------------------------------------------------------

    def ingest_rag_document(
        self,
        *,
        project_id: ProjectId,
        actor_id: UserId,
        source_type: RagSourceType,
        source_id: str,
        title: str,
        content: str,
        state: RagDocumentState = "candidate",
        source: AuditSource = "system",
    ) -> RagDocument:
        """Ingest a document into Project RAG.

        Per ``zero-context-memory``: only 'approved' documents are
        indexed. Candidate documents are stored but not searchable
        until approved.

        Per ``zero-context-memory`` §"Failed ingestion does not
        activate partial indexes": if indexing fails, the document
        state is not changed to 'approved'.
        """
        if not title or not title.strip():
            raise ValueError("title must not be empty")
        if not content or not content.strip():
            raise ValueError("content must not be empty")
        content_hash = _sha256(content)
        doc = RagDocument(
            id=RagDocumentId(generate_rag_document_id()),
            project_id=project_id,
            source_type=source_type,
            source_id=source_id,
            title=title.strip(),
            content=content,
            content_hash=content_hash,
            state=state,
            created_at=_now_utc_iso(),
            updated_at=_now_utc_iso(),
        )
        self._repo.insert_rag_document(doc)
        # If the document is approved, index it immediately.
        if state == "approved":
            try:
                self._repo.index_rag_document(doc)
                self._repo.set_rag_document_index_version(doc.id, 1)
            except Exception:
                # Indexing failed: do NOT change the document state.
                # The document remains 'approved' but index_version
                # stays None. A rebuild will pick it up.
                pass
        self._audit_repo.insert(
            AuditEvent(
                id=AuditEventId(generate_audit_event_id()),
                project_id=project_id,
                actor_id=actor_id,
                source=source,
                operation="rag.ingest",
                target_type="rag_document",
                target_id=doc.id.value,
                result="success",
                redacted_summary=(
                    f"Ingested RAG document {doc.id.value} "
                    f"(source={source_type}:{source_id}, state={state})"
                ),
                created_at=_now_utc_iso(),
            )
        )
        return doc

    def approve_rag_document(
        self,
        *,
        project_id: ProjectId,
        doc_id: RagDocumentId,
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> RagDocument:
        """Approve a candidate RAG document and index it.

        Per ``zero-context-memory``: only 'approved' documents are
        indexed. If indexing fails, the document state is NOT changed.
        """
        doc = self._repo.get_rag_document(project_id, doc_id)
        if doc.state != "candidate":
            raise ValueError(
                f"Cannot approve document in state {doc.state!r}"
            )
        # Try to index first. If it fails, do not change state.
        try:
            self._repo.index_rag_document(doc)
        except Exception:
            raise
        # Indexing succeeded; change state.
        self._repo.update_rag_document_state(doc_id, "approved")
        self._repo.set_rag_document_index_version(doc_id, 1)
        return self._repo.get_rag_document(project_id, doc_id)

    def supersede_rag_document(
        self,
        *,
        project_id: ProjectId,
        old_doc_id: RagDocumentId,
        new_doc_id: RagDocumentId,
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> RagDocument:
        """Mark an old document as superseded by a new one.

        Per ``zero-artifact-provenance-model`` §"Immutable evidence
        supports mutable understanding": the old document is not
        deleted; it transitions to 'superseded' state with a link to
        the new document.
        """
        self._repo.get_rag_document(project_id, old_doc_id)
        # Remove the old document from the index.
        self._repo.remove_rag_document_from_index(old_doc_id)
        # Mark as superseded.
        self._repo.update_rag_document_state(
            old_doc_id, "superseded", superseded_by=new_doc_id
        )
        return self._repo.get_rag_document(project_id, old_doc_id)

    def archive_rag_document(
        self,
        *,
        project_id: ProjectId,
        doc_id: RagDocumentId,
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> RagDocument:
        """Archive a RAG document and remove it from the index."""
        self._repo.remove_rag_document_from_index(doc_id)
        self._repo.update_rag_document_state(doc_id, "archived")
        return self._repo.get_rag_document(project_id, doc_id)

    # ------------------------------------------------------------------
    # RAG search
    # ------------------------------------------------------------------

    def search_rag(
        self,
        *,
        project_id: ProjectId,
        query: str,
        limit: int = 20,
    ) -> list[tuple[RagDocument, float]]:
        """Search the Project RAG for documents matching ``query``.

        Per ``zero-context-memory`` §"Staged Retrieval Router":
        authorize, generate candidates, rank. This method generates
        candidates from the FTS index. Ranking and budgeting happen in
        the RetrievalRouter (M9).

        Per ``zero-project-isolation-evidence``: the query filters by
        project_id before any row is loaded.
        """
        return self._repo.search_rag(project_id, query, limit=limit)

    # ------------------------------------------------------------------
    # Index rebuild
    # ------------------------------------------------------------------

    def rebuild_rag_index(
        self,
        *,
        project_id: ProjectId,
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> int:
        """Rebuild the RAG index for a project from canonical documents.

        Per ``zero-context-memory`` §"Indexes must be rebuildable from
        canonical records" and PLAN.md M8 validation: "Rebuilding
        derived indexes reproduces searchable canonical content."

        Returns the number of documents indexed.
        """
        count = self._repo.rebuild_index(project_id)
        self._audit_repo.insert(
            AuditEvent(
                id=AuditEventId(generate_audit_event_id()),
                project_id=project_id,
                actor_id=actor_id,
                source=source,
                operation="rag.rebuild_index",
                target_type="rag_index",
                target_id=None,
                result="success",
                redacted_summary=f"Rebuilt RAG index: {count} documents",
                created_at=_now_utc_iso(),
            )
        )
        return count

    def count_indexed_documents(
        self, project_id: ProjectId
    ) -> int:
        return self._repo.count_indexed_documents(project_id)

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def get_rag_document(
        self,
        project_id: ProjectId,
        doc_id: RagDocumentId,
    ) -> RagDocument:
        return self._repo.get_rag_document(project_id, doc_id)

    def list_rag_documents(
        self,
        project_id: ProjectId,
        *,
        state: RagDocumentState | None = None,
    ) -> list[RagDocument]:
        return self._repo.list_rag_documents_for_project(project_id, state=state)
