# ADR 0017 — Immutable Artifact Store with Content Deduplication

- Status: ACCEPTED
- Date: 2026-08-08
- Milestone: 8 (Artifact Store, Persistent Agent Memory, Project RAG)
- Skills applied: `zero-artifact-provenance-model`, `zero-context-memory`,
  `zero-project-isolation-evidence`

## Context

`PLAN.md` §13 (Milestone 8) requires:
- Immutable artifact storage with hash and metadata.
- Read-only artifact references.
- Canonical project knowledge records.
- Project RAG ingestion from approved sources.
- Rebuildable lexical retrieval using existing platform capabilities
  first.
- Topology snapshot and migration integration.

`zero-artifact-provenance-model` §"Content identity and record identity
are different": "A content hash identifies bytes. An artifact record
identifies why those bytes exist, who may access them, and how they
relate to Zero state."

`zero-artifact-provenance-model` §"Deduplication does not merge
provenance": "Identical bytes may be stored once physically while
retaining separate logical artifact records for different projects or
executions. Cross-project content deduplication must never create
cross-project access."

## Decision

Adopt an immutable artifact store with:

1. **Content hash deduplication within a project**: `UNIQUE(project_id,
   content_hash)` ensures that identical content within a project is
   stored once. The same content in different projects remains isolated
   (separate rows, separate authorization).

2. **Append-only enforcement**: SQLite triggers block `UPDATE` and
   `DELETE` on the `artifacts` table. Artifacts are immutable once
   stored.

3. **Model-facing handles**: `get_artifact_handle` returns a bounded
   `ArtifactHandle` with artifact ID, kind, size, hash, and a 200-char
   summary. The full content is NOT included in the handle. Models see
   the handle; the full content is retrieved only when explicitly needed
   and authorized.

4. **Project-scoped queries**: all retrieval queries filter by
   `project_id` before content is loaded. A guessed artifact ID from
   another project returns `ArtifactNotFoundError`, not the content.

5. **Provenance**: each artifact carries a `producer` (what created it)
   and `provenance` (JSON with source event IDs, revision refs). This
   forms a provenance graph that can answer "where did this artifact
   come from?"

6. **Artifact kinds**: stdout, stderr, diff, test_report, exit_status,
   transcript, compaction_segment, source_snapshot, other. Each kind
   has different retention implications (to be exercised in M14).

## Rejected alternatives

- **Vector database for RAG**: explicitly rejected by PLAN.md M8: "Do
  not add a vector database until holdout evidence shows existing
  database search and project structure are insufficient." We use
  SQLite FTS5 for lexical retrieval, which is sufficient for the
  current vertical slice.
- **File-backed storage**: deferred. Content is stored as TEXT in the
  database. A future migration can move large content to files; the
  schema stays the same.
- **Separate artifact service**: rejected. The artifact store is part
  of the control plane; it uses the same database, authorization, and
  audit as every other service.
- **Mutable artifacts**: explicitly rejected by
  `zero-artifact-provenance-model` §"Immutable evidence supports
  mutable understanding". The interpretation may improve while the
  source bytes remain unchanged.

## Consequences

- Artifacts are durable, verifiable, and immutable.
- Content deduplication saves storage without breaking project
  isolation.
- Models see bounded handles, not full content.
- Cross-project artifact access is impossible through the service layer.
- The provenance graph supports audit and debugging.
