"""Artifact store and Project RAG domain types.

Per ``zero-artifact-provenance-model`` SKILL.md:

- Artifacts preserve full evidence outside model context. Diffs, logs,
  transcripts, test reports, source snapshots, compaction segments, and
  generated outputs can be large or sensitive. Zero keeps them durable
  and verifiable, then gives models bounded read-only handles and
  summaries.
- An artifact store is not a dumping directory. Its value comes from
  project scope, immutable identity, hashes, provenance, retention, and
  controlled access.
- Content identity and record identity are different: a content hash
  identifies bytes; an artifact record identifies why those bytes exist,
  who may access them, and how they relate to Zero state.
- Immutable evidence supports mutable understanding: the interpretation
  may improve while the source bytes remain unchanged.
- Provenance forms a graph.
- Model handles are bounded and read-only.
- Large output has two representations: canonical artifact (full) and
  model rendering (bounded).
- Deduplication does not merge provenance.
- Retention follows evidence value and risk.
- Artifact writes have a commit point.

Per ``zero-context-memory`` SKILL.md §"Non-negotiable invariants":
- Canonical records are project-scoped, authorized, versioned, and
  provenance-linked.
- Full evidence is separate from model-facing rendering.
- Derived indexes are rebuildable.
- Provider cache, embeddings, and summaries are not canonical truth.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from zero.domain.identity import ProjectId

#: Prefix for Artifact IDs.
ARTIFACT_ID_PREFIX = "art_"
#: Prefix for RAG Document IDs.
RAG_DOCUMENT_ID_PREFIX = "rag_"

# ----------------------------------------------------------------------
# Artifact types
# ----------------------------------------------------------------------

ArtifactKind = Literal[
    "stdout",
    "stderr",
    "diff",
    "test_report",
    "exit_status",
    "transcript",
    "compaction_segment",
    "source_snapshot",
    "other",
]


@dataclass(frozen=True)
class ArtifactId:
    """Stable server-issued ID for an artifact."""

    value: str

    def __post_init__(self) -> None:
        if not self.value or not isinstance(self.value, str):
            raise ValueError("ArtifactId must be a non-empty string")
        if not self.value.startswith(ARTIFACT_ID_PREFIX):
            raise ValueError(
                f"ArtifactId must start with "
                f"{ARTIFACT_ID_PREFIX!r}; got {self.value!r}"
            )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class Artifact:
    """An immutable artifact with hash and metadata.

    Per ``zero-artifact-provenance-model``: artifacts preserve full
    evidence outside model context. They are project-scoped, immutable
    (append-only triggers block UPDATE/DELETE), and content-deduplicated
    within a project.

    Attributes:
        id: stable server-issued ID.
        project_id: the project this artifact belongs to.
        content_hash: SHA-256 of the content. UNIQUE per project.
        kind: the artifact kind (stdout, diff, transcript, etc.).
        media_type: MIME type (default text/plain).
        size_bytes: size of the content in bytes.
        content: the full content text.
        producer: what produced this artifact (e.g. task_id, tool_name).
        provenance: JSON document with source event IDs, revision refs.
        created_at: ISO-8601 timestamp.
    """

    id: ArtifactId
    project_id: ProjectId
    content_hash: str
    kind: ArtifactKind
    media_type: str = "text/plain"
    size_bytes: int = 0
    content: str = ""
    producer: str | None = None
    provenance: str | None = None
    created_at: str = ""


@dataclass(frozen=True)
class ArtifactHandle:
    """A bounded, read-only model-facing handle to an artifact.

    Per ``zero-artifact-provenance-model`` §"Model handles are bounded
    and read-only": a model-facing pointer reveals only what the agent
    needs: artifact ID, kind, short description, size, hash, and an
    authorized read operation. Raw storage paths and writable URLs
    create avoidable risk.

    Attributes:
        id: the artifact ID.
        kind: the artifact kind.
        size_bytes: size of the content.
        content_hash: SHA-256 of the content (for verification).
        summary: a short, redacted description suitable for model context.
    """

    id: ArtifactId
    kind: ArtifactKind
    size_bytes: int
    content_hash: str
    summary: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id.value,
            "kind": self.kind,
            "size_bytes": self.size_bytes,
            "content_hash": self.content_hash,
            "summary": self.summary,
        }


# ----------------------------------------------------------------------
# RAG document types
# ----------------------------------------------------------------------

RagSourceType = Literal[
    "plan_revision", "task_result", "knowledge_record", "artifact", "manual"
]

RagDocumentState = Literal[
    "candidate", "approved", "superseded", "archived"
]


@dataclass(frozen=True)
class RagDocumentId:
    """Stable server-issued ID for a RAG document."""

    value: str

    def __post_init__(self) -> None:
        if not self.value or not isinstance(self.value, str):
            raise ValueError("RagDocumentId must be a non-empty string")
        if not self.value.startswith(RAG_DOCUMENT_ID_PREFIX):
            raise ValueError(
                f"RagDocumentId must start with "
                f"{RAG_DOCUMENT_ID_PREFIX!r}; got {self.value!r}"
            )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class RagDocument:
    """A canonical project knowledge record ingested into Project RAG.

    Per ``zero-context-memory``: canonical records are project-scoped,
    authorized, versioned, and provenance-linked. Derived indexes (the
    FTS5 table) are rebuildable from these canonical documents.

    Attributes:
        id: stable server-issued ID.
        project_id: the project (denormalized for fast scoping).
        source_type: what kind of source produced this document.
        source_id: the ID of the source (e.g. plan revision ID, task ID).
        title: short human-readable title.
        content: full text content.
        content_hash: SHA-256 of content for integrity and dedup.
        state: candidate, approved, superseded, archived. Only
            'approved' documents are indexed.
        superseded_by: the ID of the document that supersedes this one.
        index_version: the version of the derived index entry. None
            means not yet indexed.
        created_at: ISO-8601 timestamp.
        updated_at: ISO-8601 timestamp.
    """

    id: RagDocumentId
    project_id: ProjectId
    source_type: RagSourceType
    source_id: str
    title: str
    content: str
    content_hash: str
    state: RagDocumentState = "candidate"
    superseded_by: RagDocumentId | None = None
    index_version: int | None = None
    created_at: str = ""
    updated_at: str = ""


# ----------------------------------------------------------------------
# Typed failures
# ----------------------------------------------------------------------


class ArtifactError(RuntimeError):
    """Base class for artifact-domain typed failures."""


class ArtifactNotFoundError(ArtifactError):
    pass


class RagError(RuntimeError):
    """Base class for RAG-domain typed failures."""


class RagDocumentNotFoundError(RagError):
    pass


class RagDocumentAlreadyExistsError(RagError):
    pass


class UnauthorizedArtifactAccessError(ArtifactError):
    """An attempt was made to access an artifact from another project.

    Per ``zero-artifact-provenance-model`` §"Deduplication does not
    merge provenance" and ``zero-project-isolation-evidence``:
    cross-project access must return zero forbidden records.
    """


class IndexRebuildError(RagError):
    """The derived index could not be rebuilt."""


class CompactionError(RuntimeError):
    """Base class for compaction-domain typed failures."""


class CompactionThrashError(CompactionError):
    """Repeated compaction without meaningful reclaimed space.

    Per ``zero-context-memory`` §"Compaction lifecycle" and
    ``zero-claude-token-economics`` §"Prune deterministically before
    summarizing": a no-thrash guard stops repeated compaction without
    meaningful reclaimed space and surfaces the oversized source.
    """
