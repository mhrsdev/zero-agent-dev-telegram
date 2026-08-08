-- Zero Develop — Milestone 8 schema: artifact store, Project RAG, memory lifecycle.
--
-- This migration adds:
--   * artifacts — immutable artifact storage with hash and metadata.
--   * rag_documents — canonical project knowledge records ingested
--     into Project RAG from approved sources.
--   * rag_index_entries — rebuildable lexical retrieval index entries
--     (FTS5 virtual table) derived from rag_documents.
--
-- Design invariants (per zero-artifact-provenance-model and
-- zero-context-memory):
--   * Canonical records are project-scoped, authorized, versioned, and
--     provenance-linked.
--   * Full evidence is separate from model-facing rendering.
--   * Derived indexes are rebuildable.
--   * Provider cache, embeddings, and summaries are not canonical truth.
--   * Artifact hash and retrieval round-trip.
--   * Unauthorized artifact/memory access fails before content retrieval.
--   * Cross-project retrieval yields zero forbidden records.

-- ------------------------------------------------------------------
-- Artifacts (immutable storage with hash and metadata)
-- ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS artifacts (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id),
    -- content_hash is a SHA-256 of the content. UNIQUE per project so
    -- identical content within a project deduplicates, but the same
    -- content in different projects remains isolated.
    content_hash    TEXT NOT NULL,
    kind            TEXT NOT NULL
                    CHECK (kind IN ('stdout','stderr','diff','test_report',
                                    'exit_status','transcript','compaction_segment',
                                    'source_snapshot','other')),
    media_type      TEXT NOT NULL DEFAULT 'text/plain',
    size_bytes      INTEGER NOT NULL,
    -- content is stored as TEXT. For large outputs, a future migration
    -- can move this to a file-backed store; the schema stays the same.
    content         TEXT NOT NULL,
    -- producer: what produced this artifact (e.g. task_id, tool_name).
    producer        TEXT,
    -- provenance: JSON document with source event IDs, revision refs.
    provenance      TEXT,
    created_at      TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (project_id, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_artifacts_project
    ON artifacts(project_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_kind
    ON artifacts(project_id, kind);

-- Artifacts are immutable: block UPDATE and DELETE.
CREATE TRIGGER IF NOT EXISTS artifacts_no_update
    BEFORE UPDATE ON artifacts
    FOR EACH ROW
    BEGIN
        SELECT RAISE(ABORT, 'artifacts is append-only');
    END;

CREATE TRIGGER IF NOT EXISTS artifacts_no_delete
    BEFORE DELETE ON artifacts
    FOR EACH ROW
    BEGIN
        SELECT RAISE(ABORT, 'artifacts is append-only');
    END;

-- ------------------------------------------------------------------
-- RAG documents (canonical project knowledge from approved sources)
-- ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS rag_documents (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id),
    -- source_type: what kind of source produced this document.
    source_type     TEXT NOT NULL
                    CHECK (source_type IN ('plan_revision','task_result',
                                           'knowledge_record','artifact',
                                           'manual')),
    -- source_id: the ID of the source (e.g. plan revision ID, task ID).
    source_id       TEXT NOT NULL,
    -- title: a short human-readable title for the document.
    title           TEXT NOT NULL,
    -- content: the full text content of the document.
    content         TEXT NOT NULL,
    -- content_hash: SHA-256 of content for integrity and dedup.
    content_hash    TEXT NOT NULL,
    -- state: candidate, approved, superseded, archived.
    -- Only 'approved' documents are indexed.
    state           TEXT NOT NULL DEFAULT 'candidate'
                    CHECK (state IN ('candidate','approved','superseded','archived')),
    -- superseded_by: the ID of the document that supersedes this one.
    superseded_by   TEXT REFERENCES rag_documents(id),
    -- index_version: the version of the derived index entry. NULL
    -- means not yet indexed. Incremented on reindex.
    index_version   INTEGER,
    created_at      TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at      TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (project_id, source_type, source_id)
);

CREATE INDEX IF NOT EXISTS idx_rag_docs_project
    ON rag_documents(project_id);
CREATE INDEX IF NOT EXISTS idx_rag_docs_state
    ON rag_documents(project_id, state);

-- ------------------------------------------------------------------
-- RAG index entries (rebuildable lexical retrieval, FTS5)
-- ------------------------------------------------------------------

-- FTS5 virtual table for full-text search. This is a derived index;
-- it can be dropped and rebuilt from rag_documents at any time.
CREATE VIRTUAL TABLE IF NOT EXISTS rag_index_entries USING fts5(
    rag_document_id UNINDEXED,
    project_id UNINDEXED,
    title,
    content,
    tokenize = 'porter unicode61'
);

-- ------------------------------------------------------------------
-- Context versions (for M9 compaction lifecycle)
-- ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS context_versions (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id),
    execution_id    TEXT NOT NULL REFERENCES executions(id),
    -- version: incremented for each new context version.
    version         INTEGER NOT NULL,
    -- active: 1 if this is the active context for the execution, 0
    -- otherwise. Exactly one active version per execution.
    active          INTEGER NOT NULL DEFAULT 0,
    -- system_message: the immutable system/security policy text.
    system_message  TEXT NOT NULL,
    -- user_prefix: project and agent identity text.
    user_prefix     TEXT NOT NULL,
    -- plan_contract: the current plan and task contract text.
    plan_contract   TEXT NOT NULL DEFAULT '',
    -- execution_snapshot: typed execution state (JSON) that survives
    -- compaction. Per zero-context-memory §9: compaction summary is
    -- NOT the sole copy of plan/task IDs, worktree IDs, etc.
    execution_snapshot TEXT NOT NULL DEFAULT '{}',
    -- retrieved_context: the rendered retrieval output (JSON array of
    -- {source, content, token_count} objects).
    retrieved_context TEXT NOT NULL DEFAULT '[]',
    -- conversation_tail: recent valid exchange messages (JSON array).
    conversation_tail TEXT NOT NULL DEFAULT '[]',
    -- compaction_summary: the summary text if this context was
    -- produced by compaction. NULL for non-compacted contexts.
    compaction_summary TEXT,
    -- transcript_artifact_id: the artifact containing the full
    -- pre-compaction transcript. NULL for non-compacted contexts.
    transcript_artifact_id TEXT REFERENCES artifacts(id),
    -- token_count: estimated tokens in this context.
    token_count     INTEGER NOT NULL DEFAULT 0,
    -- created_at: when this context version was created.
    created_at      TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (execution_id, version)
);

CREATE INDEX IF NOT EXISTS idx_context_versions_execution
    ON context_versions(execution_id, version);
CREATE UNIQUE INDEX IF NOT EXISTS idx_context_versions_active
    ON context_versions(execution_id) WHERE active = 1;

-- ------------------------------------------------------------------
-- Context injection ledger (selected/omitted records)
-- ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS context_injection_ledger (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id),
    execution_id    TEXT NOT NULL REFERENCES executions(id),
    context_version INTEGER NOT NULL,
    -- selected: JSON array of {source, record_id, token_count} for
    -- records injected into the context.
    selected        TEXT NOT NULL DEFAULT '[]',
    -- omitted: JSON array of {source, record_id, reason} for records
    -- that were candidates but were omitted.
    omitted         TEXT NOT NULL DEFAULT '[]',
    -- total_candidates: total number of candidate records considered.
    total_candidates INTEGER NOT NULL DEFAULT 0,
    -- total_tokens: total tokens in the selected records.
    total_tokens    INTEGER NOT NULL DEFAULT 0,
    -- budget_tokens: the token budget that was in effect.
    budget_tokens   INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_injection_ledger_execution
    ON context_injection_ledger(execution_id, context_version);

-- ------------------------------------------------------------------
-- Compaction records (durable compaction lifecycle)
-- ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS compaction_records (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id),
    execution_id    TEXT NOT NULL REFERENCES executions(id),
    -- source_context_version: the context version before compaction.
    source_context_version INTEGER NOT NULL,
    -- target_context_version: the new context version after compaction.
    target_context_version INTEGER NOT NULL,
    -- source_event_range: JSON {start_event_id, end_event_id} of the
    -- events that were compacted.
    source_event_range TEXT NOT NULL,
    -- memory_delta_artifact_id: artifact containing accepted memory
    -- deltas flushed before compaction. NULL if no deltas.
    memory_delta_artifact_id TEXT REFERENCES artifacts(id),
    -- transcript_artifact_id: artifact containing the full transcript.
    transcript_artifact_id TEXT REFERENCES artifacts(id),
    -- summary: the compaction summary text.
    summary         TEXT NOT NULL,
    -- fit_rung: which rung of the degradation ladder was used.
    fit_rung        TEXT NOT NULL,
    -- state: pre_flush, fit, summary_validated, committed, activated,
    -- failed, no_thrash_blocked.
    state           TEXT NOT NULL DEFAULT 'pre_flush'
                    CHECK (state IN ('pre_flush','fit','summary_validated',
                                     'committed','activated','failed',
                                     'no_thrash_blocked')),
    -- no_thrash_count: number of consecutive compactions that did not
    -- reclaim meaningful space. Used to detect thrashing.
    no_thrash_count INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (execution_id, target_context_version)
);

CREATE INDEX IF NOT EXISTS idx_compaction_records_execution
    ON compaction_records(execution_id);
