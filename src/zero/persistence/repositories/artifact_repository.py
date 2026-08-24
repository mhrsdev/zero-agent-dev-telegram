"""Artifact and RAG repository — immutable artifacts, RAG documents, FTS index.

Per ``zero-artifact-provenance-model``:
- Artifacts are immutable (append-only triggers block UPDATE/DELETE).
- Content deduplication within a project (UNIQUE(project_id, content_hash)).
- Project-scoped: queries filter by project_id before content is loaded.

Per ``zero-context-memory``:
- Derived indexes (FTS5) are rebuildable from canonical rag_documents.
- Only 'approved' documents are indexed.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3

from zero.domain.artifacts import (
    Artifact,
    ArtifactId,
    ArtifactKind,
    ArtifactNotFoundError,
    ArtifactProvenance,
    ArtifactProvenanceId,
    RagDocument,
    RagDocumentId,
    RagDocumentNotFoundError,
    RagDocumentState,
)
from zero.domain.identity import ProjectId, UserId
from zero.persistence.connection import Database


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_FTS_SPECIAL = re.compile(r'["\'()*:^{}\[\]|,.\-]')


def _fts_safe_query(query: str) -> str:
    """Sanitize a user query for FTS5 MATCH.

    Strips FTS5 operator/syntax characters and quotes, then joins the
    remaining terms with OR. Returns an empty string when nothing
    searchable remains so callers can short-circuit instead of raising.
    """
    terms = [
        term
        for term in _FTS_SPECIAL.sub(" ", query or "").split()
        if term and term.upper() not in {"AND", "OR", "NOT", "NEAR"}
    ]
    return " OR ".join(terms)


def _row_to_artifact(row: sqlite3.Row) -> Artifact:
    return Artifact(
        id=ArtifactId(row["id"]),
        project_id=ProjectId(row["project_id"]),
        content_hash=row["content_hash"],
        kind=row["kind"],  # type: ignore[arg-type]
        media_type=row["media_type"],
        size_bytes=row["size_bytes"],
        content=row["content"],
        producer=row["producer"],
        provenance=row["provenance"],
        created_at=row["created_at"],
    )


def _row_to_artifact_provenance(row: sqlite3.Row) -> ArtifactProvenance:
    return ArtifactProvenance(
        id=ArtifactProvenanceId(row["id"]),
        project_id=ProjectId(row["project_id"]),
        artifact_id=ArtifactId(row["artifact_id"]),
        actor_id=UserId(row["actor_id"]),
        producer=row["producer"],
        provenance=row["provenance"],
        created_at=row["created_at"],
    )


def _row_to_rag_document(row: sqlite3.Row) -> RagDocument:
    return RagDocument(
        id=RagDocumentId(row["id"]),
        project_id=ProjectId(row["project_id"]),
        source_type=row["source_type"],  # type: ignore[arg-type]
        source_id=row["source_id"],
        title=row["title"],
        content=row["content"],
        content_hash=row["content_hash"],
        state=row["state"],  # type: ignore[arg-type]
        superseded_by=RagDocumentId(row["superseded_by"]) if row["superseded_by"] else None,
        index_version=row["index_version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class ArtifactRepository:
    """Database-backed artifact and RAG document repository."""

    def __init__(self, database: Database) -> None:
        self._database = database

    @property
    def database(self) -> Database:
        return self._database

    # ------------------------------------------------------------------
    # Artifacts
    # ------------------------------------------------------------------

    def insert_artifact(
        self,
        artifact: Artifact,
        *,
        commit: bool = True,
    ) -> None:
        """Insert an artifact. If an artifact with the same
        (project_id, content_hash) already exists, this is a no-op
        (idempotent deduplication)."""
        conn = self._database.connect()
        try:
            conn.execute(
                "INSERT INTO artifacts "
                "(id, project_id, content_hash, kind, media_type, "
                "size_bytes, content, producer, provenance) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    artifact.id.value,
                    artifact.project_id.value,
                    artifact.content_hash,
                    artifact.kind,
                    artifact.media_type,
                    artifact.size_bytes,
                    artifact.content,
                    artifact.producer,
                    artifact.provenance,
                ),
            )
            if commit:
                conn.commit()
        except sqlite3.IntegrityError as exc:
            if commit:
                conn.rollback()
            if "UNIQUE" in str(exc) and "content_hash" in str(exc):
                # Idempotent dedup: the same content already exists.
                return
            raise

    def get_artifact(
        self,
        project_id: ProjectId,
        artifact_id: ArtifactId,
    ) -> Artifact:
        """Return the artifact, scoped to the given project.

        Per ``zero-project-isolation-evidence`` §"Scope begins before
        access": the query filters by project_id before any row is
        loaded. A secret from another project is never returned even
        if its ID is guessed.
        """
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, content_hash, kind, media_type, "
            "size_bytes, content, producer, provenance, created_at "
            "FROM artifacts WHERE id = ? AND project_id = ?",
            (artifact_id.value, project_id.value),
        )
        row = cursor.fetchone()
        if row is None:
            raise ArtifactNotFoundError(f"Artifact {artifact_id} not found in project {project_id}")
        return _row_to_artifact(row)

    def get_artifact_by_hash(
        self,
        project_id: ProjectId,
        content_hash: str,
    ) -> Artifact | None:
        """Return the artifact with the given content hash in the
        project, or None."""
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, content_hash, kind, media_type, "
            "size_bytes, content, producer, provenance, created_at "
            "FROM artifacts WHERE project_id = ? AND content_hash = ?",
            (project_id.value, content_hash),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return _row_to_artifact(row)

    def list_artifacts_for_project(
        self,
        project_id: ProjectId,
        *,
        kind: ArtifactKind | None = None,
    ) -> list[Artifact]:
        conn = self._database.connect()
        if kind is not None:
            cursor = conn.execute(
                "SELECT id, project_id, content_hash, kind, media_type, "
                "size_bytes, content, producer, provenance, created_at "
                "FROM artifacts WHERE project_id = ? AND kind = ? "
                "ORDER BY created_at ASC",
                (project_id.value, kind),
            )
        else:
            cursor = conn.execute(
                "SELECT id, project_id, content_hash, kind, media_type, "
                "size_bytes, content, producer, provenance, created_at "
                "FROM artifacts WHERE project_id = ? "
                "ORDER BY created_at ASC",
                (project_id.value,),
            )
        return [_row_to_artifact(row) for row in cursor.fetchall()]

    def insert_provenance(
        self,
        record: ArtifactProvenance,
        *,
        commit: bool = True,
    ) -> None:
        conn = self._database.connect()
        conn.execute(
            "INSERT INTO artifact_provenance "
            "(id, project_id, artifact_id, actor_id, producer, provenance) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                record.id.value,
                record.project_id.value,
                record.artifact_id.value,
                record.actor_id.value,
                record.producer,
                record.provenance,
            ),
        )
        if commit:
            conn.commit()

    def list_provenance(
        self,
        project_id: ProjectId,
        artifact_id: ArtifactId,
    ) -> list[ArtifactProvenance]:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, artifact_id, actor_id, producer, provenance, created_at "
            "FROM artifact_provenance "
            "WHERE project_id = ? AND artifact_id = ? ORDER BY created_at ASC",
            (project_id.value, artifact_id.value),
        )
        return [_row_to_artifact_provenance(row) for row in cursor.fetchall()]

    def insert_rag_document(
        self,
        doc: RagDocument,
        *,
        commit: bool = True,
    ) -> None:
        conn = self._database.connect()
        try:
            conn.execute(
                "INSERT INTO rag_documents "
                "(id, project_id, source_type, source_id, title, content, "
                "content_hash, state) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    doc.id.value,
                    doc.project_id.value,
                    doc.source_type,
                    doc.source_id,
                    doc.title,
                    doc.content,
                    doc.content_hash,
                    doc.state,
                ),
            )
            if commit:
                conn.commit()
        except sqlite3.IntegrityError as exc:
            if commit:
                conn.rollback()
            from zero.domain.artifacts import RagDocumentAlreadyExistsError

            if "UNIQUE" in str(exc):
                raise RagDocumentAlreadyExistsError(
                    f"RAG document with source {doc.source_type}:"
                    f"{doc.source_id} already exists in project "
                    f"{doc.project_id}"
                ) from exc
            raise

    def get_rag_document(
        self,
        project_id: ProjectId,
        doc_id: RagDocumentId,
    ) -> RagDocument:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, source_type, source_id, title, content, "
            "content_hash, state, superseded_by, index_version, "
            "created_at, updated_at FROM rag_documents "
            "WHERE id = ? AND project_id = ?",
            (doc_id.value, project_id.value),
        )
        row = cursor.fetchone()
        if row is None:
            raise RagDocumentNotFoundError(
                f"RAG document {doc_id} not found in project {project_id}"
            )
        return _row_to_rag_document(row)

    def list_rag_documents_for_project(
        self,
        project_id: ProjectId,
        *,
        state: RagDocumentState | None = None,
    ) -> list[RagDocument]:
        conn = self._database.connect()
        if state is not None:
            cursor = conn.execute(
                "SELECT id, project_id, source_type, source_id, title, content, "
                "content_hash, state, superseded_by, index_version, "
                "created_at, updated_at FROM rag_documents "
                "WHERE project_id = ? AND state = ? "
                "ORDER BY created_at ASC",
                (project_id.value, state),
            )
        else:
            cursor = conn.execute(
                "SELECT id, project_id, source_type, source_id, title, content, "
                "content_hash, state, superseded_by, index_version, "
                "created_at, updated_at FROM rag_documents "
                "WHERE project_id = ? ORDER BY created_at ASC",
                (project_id.value,),
            )
        return [_row_to_rag_document(row) for row in cursor.fetchall()]

    def update_rag_document_state(
        self,
        doc_id: RagDocumentId,
        new_state: RagDocumentState,
        *,
        project_id: ProjectId | None = None,
        superseded_by: RagDocumentId | None = None,
        commit: bool = True,
    ) -> None:
        conn = self._database.connect()
        where = "id = ?"
        where_params: list[str] = [doc_id.value]
        if project_id is not None:
            where += " AND project_id = ?"
            where_params.append(project_id.value)
        if superseded_by is not None:
            cursor = conn.execute(
                "UPDATE rag_documents SET state = ?, superseded_by = ?, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                f"WHERE {where}",
                (new_state, superseded_by.value, *where_params),
            )
        else:
            cursor = conn.execute(
                "UPDATE rag_documents SET state = ?, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                f"WHERE {where}",
                (new_state, *where_params),
            )
        if cursor.rowcount == 0:
            scope = f" in project {project_id}" if project_id is not None else ""
            raise RagDocumentNotFoundError(f"RAG document {doc_id} not found{scope}")
        if commit:
            conn.commit()

    def set_rag_document_index_version(
        self,
        doc_id: RagDocumentId,
        index_version: int,
        *,
        commit: bool = True,
    ) -> None:
        conn = self._database.connect()
        cursor = conn.execute(
            "UPDATE rag_documents SET index_version = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE id = ?",
            (index_version, doc_id.value),
        )
        if cursor.rowcount == 0:
            raise RagDocumentNotFoundError(f"RAG document {doc_id} not found")
        if commit:
            conn.commit()

    # ------------------------------------------------------------------
    # RAG FTS index (rebuildable)
    # ------------------------------------------------------------------

    def index_rag_document(
        self,
        doc: RagDocument,
        *,
        commit: bool = True,
    ) -> None:
        """Add a document to the FTS index. Only called for 'approved'
        documents."""
        conn = self._database.connect()
        # Remove any existing index entry for this document (idempotent).
        conn.execute(
            "DELETE FROM rag_index_entries WHERE rag_document_id = ?",
            (doc.id.value,),
        )
        conn.execute(
            "INSERT INTO rag_index_entries "
            "(rag_document_id, project_id, title, content) "
            "VALUES (?, ?, ?, ?)",
            (doc.id.value, doc.project_id.value, doc.title, doc.content),
        )
        if commit:
            conn.commit()

    def remove_rag_document_from_index(
        self,
        doc_id: RagDocumentId,
        *,
        project_id: ProjectId | None = None,
        commit: bool = True,
    ) -> None:
        conn = self._database.connect()
        where = "rag_document_id = ?"
        where_params: list[str] = [doc_id.value]
        if project_id is not None:
            where += " AND project_id = ?"
            where_params.append(project_id.value)
        conn.execute(
            f"DELETE FROM rag_index_entries WHERE {where}",
            where_params,
        )
        if commit:
            conn.commit()

    def search_rag(
        self,
        project_id: ProjectId,
        query: str,
        *,
        limit: int = 20,
    ) -> list[tuple[RagDocument, float]]:
        """Search the RAG index for documents matching ``query``.

        Per ``zero-project-isolation-evidence`` §"Scope begins before
        access": the query filters by project_id before any row is
        loaded. Documents from other projects are never returned.

        The raw user query is never passed to FTS MATCH directly:
        operator/quote characters are stripped and the remaining terms
        are joined with OR so malformed input degrades to a plain term
        search instead of raising or silently returning nothing.

        Returns a list of (document, score) tuples, highest score first.
        """
        conn = self._database.connect()
        safe_query = _fts_safe_query(query)
        if not safe_query:
            return []
        # FTS5 MATCH query over sanitized terms. bm25() returns a score;
        # lower is better, so we negate it.
        cursor = conn.execute(
            "SELECT rag_document_id, bm25(rag_index_entries) as score "
            "FROM rag_index_entries "
            "WHERE project_id = ? AND rag_index_entries MATCH ? "
            "ORDER BY score ASC LIMIT ?",
            (project_id.value, safe_query, limit),
        )
        results: list[tuple[RagDocument, float]] = []
        for row in cursor.fetchall():
            doc_id = RagDocumentId(row["rag_document_id"])
            # Look up the full document (project-scoped).
            try:
                doc = self.get_rag_document(project_id, doc_id)
                # bm25 returns lower=better, so negate for "higher is
                # better".
                score = -row["score"]
                results.append((doc, score))
            except RagDocumentNotFoundError:
                # Index is out of sync; skip this entry.
                continue
        return results

    def rebuild_index(
        self,
        project_id: ProjectId,
    ) -> int:
        """Rebuild the FTS index for a project from canonical documents.

        Per ``zero-context-memory`` §"Indexes must be rebuildable from
        canonical records": derived search structures can be rebuilt
        from stored fact text and metadata.

        Returns the number of documents indexed.
        """
        conn = self._database.connect()
        # Clear all index entries for this project.
        conn.execute(
            "DELETE FROM rag_index_entries WHERE project_id = ?",
            (project_id.value,),
        )
        # Get all approved documents for this project.
        docs = self.list_rag_documents_for_project(project_id, state="approved")
        count = 0
        for doc in docs:
            conn.execute(
                "INSERT INTO rag_index_entries "
                "(rag_document_id, project_id, title, content) "
                "VALUES (?, ?, ?, ?)",
                (doc.id.value, doc.project_id.value, doc.title, doc.content),
            )
            # Update the index_version on the document.
            conn.execute(
                "UPDATE rag_documents SET index_version = 1, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE id = ?",
                (doc.id.value,),
            )
            count += 1
        conn.commit()
        return count

    def count_indexed_documents(self, project_id: ProjectId) -> int:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT COUNT(*) FROM rag_index_entries WHERE project_id = ?",
            (project_id.value,),
        )
        return int(cursor.fetchone()[0])
