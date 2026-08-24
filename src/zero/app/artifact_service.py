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
import logging
import sqlite3
from datetime import UTC, datetime

from zero.app.authorization_service import AuthorizationService
from zero.domain.artifacts import (
    Artifact,
    ArtifactHandle,
    ArtifactId,
    ArtifactKind,
    ArtifactProvenance,
    ArtifactProvenanceId,
    IndexRebuildError,
    RagDocument,
    RagDocumentId,
    RagDocumentState,
    RagSourceType,
)
from zero.domain.audit import AuditEvent, AuditEventId, AuditSource, looks_sensitive
from zero.domain.identity import ProjectId, UserId
from zero.domain.ids import (
    generate_artifact_id,
    generate_artifact_provenance_id,
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

logger = logging.getLogger(__name__)


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
        commit: bool = True,
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
        # Artifact contents are project evidence.  Authorize before
        # validating or deduplicating the payload so an unauthorized caller
        # cannot use this method as a write oracle.
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="execution.start",
            source=source,
        )
        if not content:
            raise ValueError("content must not be empty")
        content_hash = _sha256(content)
        from contextlib import nullcontext

        transaction = self._repo.database.transaction() if commit else nullcontext()
        with transaction:
            # The transaction begins before the deduplication read.  On a
            # file-backed SQLite database this serializes competing writers,
            # so the second caller observes the first caller's committed
            # artifact instead of constructing a loser ID and provenance row.
            existing = self._repo.get_artifact_by_hash(project_id, content_hash)
            if existing is None:
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
                self._repo.insert_artifact(artifact, commit=False)
                is_new_artifact = True
            else:
                artifact = existing
                is_new_artifact = False
            self._repo.insert_provenance(
                ArtifactProvenance(
                    id=ArtifactProvenanceId(generate_artifact_provenance_id()),
                    project_id=project_id,
                    artifact_id=artifact.id,
                    actor_id=actor_id,
                    producer=producer,
                    provenance=provenance,
                    created_at=_now_utc_iso(),
                ),
                commit=False,
            )
            if is_new_artifact:
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
                            f"Stored artifact {artifact.id.value} (kind={kind}, size={artifact.size_bytes})"
                        ),
                        created_at=_now_utc_iso(),
                    ),
                    commit=False,
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
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="execution.view_diffs",
            source=source,
        )
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
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="execution.view_diffs",
            source=source,
        )
        artifact = self._repo.get_artifact(project_id, artifact_id)
        # Build a short summary and redact sensitive-looking content.
        summary = artifact.content[:200]
        if len(artifact.content) > 200:
            summary += "..."
        if looks_sensitive(summary):
            summary = "[REDACTED: sensitive content detected]"
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
        actor_id: UserId,
        kind: ArtifactKind | None = None,
        source: AuditSource = "system",
    ) -> list[Artifact]:
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="execution.view_diffs",
            source=source,
        )
        return self._repo.list_artifacts_for_project(project_id, kind=kind)

    def list_provenance(
        self,
        *,
        project_id: ProjectId,
        artifact_id: ArtifactId,
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> list[ArtifactProvenance]:
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="execution.view_diffs",
            source=source,
        )
        return self._repo.list_provenance(project_id, artifact_id)

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
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="agent.manage",
            source=source,
        )
        if not title or not title.strip():
            raise ValueError("title must not be empty")
        if not content or not content.strip():
            raise ValueError("content must not be empty")
        content_hash = _sha256(content)
        requested_state = state
        # An approved document is not authoritative until its derived index
        # entry and index version commit.  Persist it as a candidate first so
        # an indexing failure cannot create the contradictory state
        # ``approved + index_version=NULL``.
        persisted_state = "candidate" if requested_state == "approved" else requested_state
        doc = RagDocument(
            id=RagDocumentId(generate_rag_document_id()),
            project_id=project_id,
            source_type=source_type,
            source_id=source_id,
            title=title.strip(),
            content=content,
            content_hash=content_hash,
            state=persisted_state,
            created_at=_now_utc_iso(),
            updated_at=_now_utc_iso(),
        )
        self._repo.insert_rag_document(doc)
        index_error: Exception | None = None
        if requested_state == "approved":
            try:
                with self._repo.database.transaction():
                    self._repo.index_rag_document(doc, commit=False)
                    self._repo.update_rag_document_state(doc.id, "approved", commit=False)
                    self._repo.set_rag_document_index_version(doc.id, 1, commit=False)
            except (IndexRebuildError, OSError, sqlite3.Error, ValueError) as exc:
                index_error = exc
                # A failed index attempt may have partially inserted a derived
                # row if a custom index backend committed before failing.  The
                # canonical document remains a candidate and is retriable.
                try:
                    self._repo.remove_rag_document_from_index(doc.id)
                except (IndexRebuildError, OSError, sqlite3.Error) as cleanup_exc:
                    logger.debug(
                        "RAG index cleanup failed: %s",
                        type(cleanup_exc).__name__,
                    )
        self._audit_repo.insert(
            AuditEvent(
                id=AuditEventId(generate_audit_event_id()),
                project_id=project_id,
                actor_id=actor_id,
                source=source,
                operation="rag.ingest",
                target_type="rag_document",
                target_id=doc.id.value,
                result="failure" if index_error is not None else "success",
                redacted_summary=(
                    f"Ingested RAG document {doc.id.value} "
                    f"(requested_state={requested_state}, "
                    f"state={'candidate' if index_error else requested_state})"
                ),
                created_at=_now_utc_iso(),
            )
        )
        return self._repo.get_rag_document(project_id, doc.id)

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
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="agent.manage",
            source=source,
        )
        doc = self._repo.get_rag_document(project_id, doc_id)
        if doc.state != "candidate":
            raise ValueError(f"Cannot approve document in state {doc.state!r}")
        # Indexing and activation are one commit point.  A failure leaves
        # the candidate untouched and never exposes a searchable partial
        # document.
        with self._repo.database.transaction():
            self._repo.index_rag_document(doc, commit=False)
            self._repo.update_rag_document_state(doc_id, "approved", commit=False)
            self._repo.set_rag_document_index_version(doc_id, 1, commit=False)
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
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="agent.manage",
            source=source,
        )
        self._repo.get_rag_document(project_id, old_doc_id)
        # Remove the old document from the index.
        self._repo.remove_rag_document_from_index(old_doc_id)
        # Mark as superseded.
        self._repo.update_rag_document_state(old_doc_id, "superseded", superseded_by=new_doc_id)
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
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="agent.manage",
            source=source,
        )
        self._repo.get_rag_document(project_id, doc_id)
        self._repo.remove_rag_document_from_index(doc_id, project_id=project_id)
        self._repo.update_rag_document_state(doc_id, "archived", project_id=project_id)
        return self._repo.get_rag_document(project_id, doc_id)

    # ------------------------------------------------------------------
    # RAG search
    # ------------------------------------------------------------------

    def search_rag(
        self,
        *,
        project_id: ProjectId,
        actor_id: UserId,
        query: str,
        limit: int = 20,
        source: AuditSource = "system",
    ) -> list[tuple[RagDocument, float]]:
        """Search the Project RAG for documents matching ``query``.

        Per ``zero-context-memory`` §"Staged Retrieval Router":
        authorize, generate candidates, rank. This method generates
        candidates from the FTS index. Ranking and budgeting happen in
        the RetrievalRouter (M9).

        Per ``zero-project-isolation-evidence``: the query filters by
        project_id before any row is loaded.
        """
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="execution.view_diffs",
            source=source,
        )
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
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
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="agent.manage",
            source=source,
        )
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

    def count_indexed_documents(self, project_id: ProjectId) -> int:
        return self._repo.count_indexed_documents(project_id)

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def get_rag_document(
        self,
        project_id: ProjectId,
        doc_id: RagDocumentId,
        *,
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> RagDocument:
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="execution.view_diffs",
            source=source,
        )
        return self._repo.get_rag_document(project_id, doc_id)

    def list_rag_documents(
        self,
        project_id: ProjectId,
        *,
        actor_id: UserId,
        state: RagDocumentState | None = None,
        source: AuditSource = "system",
    ) -> list[RagDocument]:
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="execution.view_diffs",
            source=source,
        )
        return self._repo.list_rag_documents_for_project(project_id, state=state)
