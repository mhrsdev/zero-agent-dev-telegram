-- Zero Develop — Milestone 7 schema: dynamic Sub Agent Type lifecycle.
--
-- This migration adds:
--   * agent_types — project-specific Sub Agent Type definitions.
--   * agent_type_versions — versioned topology snapshots (for lossless
--     split/merge/retire with rollback).
--   * agent_instances — runtime instances of a type.
--   * knowledge_records — agent-type-scoped memory (the records that
--     must be preserved through split/merge/retire).
--   * topology_snapshots — frozen topology state for rollback.
--
-- Design invariants (per zero-agent-execution-lifecycle and
-- zero-context-memory):
--   * Main roles (Planner, Worker) are fixed. Sub Agent Types are
--     project-specific and dynamic.
--   * Type responsibility, memory scope, tool rights, model policy,
--     context budget, and concurrency limit are explicit.
--   * Instances share accepted type knowledge but not task-local
--     scratch context.
--   * Split, merge, retirement, and role changes are lossless and
--     reversible.
--   * Never hard-delete source topology or memory as part of evolution.

-- ------------------------------------------------------------------
-- Agent types (project-specific Sub Agent Types)
-- ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS agent_types (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id),
    name            TEXT NOT NULL,
    -- responsibility: what this type owns.
    responsibility  TEXT NOT NULL,
    -- memory_scope: what knowledge this type manages (text description).
    memory_scope    TEXT NOT NULL,
    -- permitted_tools: JSON array of tool IDs this type may invoke.
    permitted_tools TEXT NOT NULL DEFAULT '[]',
    -- model_policy: which model/provider to use (JSON object; empty
    -- means "use project default").
    model_policy    TEXT NOT NULL DEFAULT '{}',
    -- context_budget_tokens: max context tokens for instances.
    context_budget_tokens INTEGER NOT NULL DEFAULT 100000,
    -- max_concurrent_instances: how many instances may run at once.
    max_concurrent_instances INTEGER NOT NULL DEFAULT 1,
    -- state: active, archived, retired.
    state           TEXT NOT NULL DEFAULT 'active'
                    CHECK (state IN ('active','archived','retired')),
    -- version: incremented on each modification; used for optimistic
    -- concurrency and topology versioning.
    version         INTEGER NOT NULL DEFAULT 1,
    -- superseded_by: if this type was split/merged into another, the
    -- ID of the successor. NULL if active or retired without successor.
    superseded_by   TEXT REFERENCES agent_types(id),
    created_at      TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at      TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (project_id, name)
);

CREATE INDEX IF NOT EXISTS idx_agent_types_project
    ON agent_types(project_id);
CREATE INDEX IF NOT EXISTS idx_agent_types_state
    ON agent_types(state);

-- ------------------------------------------------------------------
-- Agent instances (runtime instances of a type)
-- ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS agent_instances (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id),
    agent_type_id   TEXT NOT NULL REFERENCES agent_types(id),
    -- task_id: the task this instance is assigned to (NULL when idle).
    task_id         TEXT REFERENCES tasks(id),
    -- state: idle, running, completed, failed, cancelled.
    state           TEXT NOT NULL DEFAULT 'idle'
                    CHECK (state IN ('idle','running','completed','failed','cancelled')),
    created_at      TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at      TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_agent_instances_type
    ON agent_instances(agent_type_id);
CREATE INDEX IF NOT EXISTS idx_agent_instances_state
    ON agent_instances(state);

-- ------------------------------------------------------------------
-- Knowledge records (agent-type-scoped memory)
-- ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS knowledge_records (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id),
    -- agent_type_id: the type that owns this record. NULL means
    -- project-wide (Project RAG, added in M8).
    agent_type_id   TEXT REFERENCES agent_types(id),
    -- kind: decision, fact, constraint, contract, failure, other.
    kind            TEXT NOT NULL
                    CHECK (kind IN ('decision','fact','constraint','contract','failure','other')),
    -- content: the knowledge text.
    content         TEXT NOT NULL,
    -- content_hash: SHA-256 of content for integrity.
    content_hash    TEXT NOT NULL,
    -- provenance: where this record came from (e.g. task ID, plan
    -- revision ID, external source).
    provenance      TEXT,
    -- state: candidate, approved, superseded, archived.
    state           TEXT NOT NULL DEFAULT 'approved'
                    CHECK (state IN ('candidate','approved','superseded','archived')),
    -- superseded_by: if this record was superseded by another, the ID.
    superseded_by   TEXT REFERENCES knowledge_records(id),
    -- migrated_from: if this record was migrated from another type
    -- (split/merge), the original record ID. This is the provenance
    -- link required by PLAN.md M7.
    migrated_from   TEXT REFERENCES knowledge_records(id),
    created_at      TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at      TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_knowledge_project
    ON knowledge_records(project_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_type
    ON knowledge_records(agent_type_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_state
    ON knowledge_records(state);

-- ------------------------------------------------------------------
-- Topology snapshots (frozen topology state for rollback)
-- ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS topology_snapshots (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id),
    -- snapshot_version: incremented for each new snapshot.
    snapshot_version INTEGER NOT NULL,
    -- reason: why the snapshot was taken (e.g. "before_split",
    -- "before_merge", "before_retire", "rollback").
    reason          TEXT NOT NULL,
    -- topology_state: JSON document capturing all agent types, their
    -- versions, states, and knowledge record counts at snapshot time.
    topology_state  TEXT NOT NULL,
    created_at      TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (project_id, snapshot_version)
);

CREATE INDEX IF NOT EXISTS idx_topology_snapshots_project
    ON topology_snapshots(project_id, snapshot_version);
