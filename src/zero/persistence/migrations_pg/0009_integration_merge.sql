-- GENERATED from 0009_integration_merge.sql by scripts/gen_pg_migrations.py.
-- PostgreSQL dialect translation of the canonical SQLite schema.
-- Do not edit directly; re-run the generator instead.

-- Zero Develop — Milestone 11 schema: integration review and controlled merge.
--
-- This migration adds:
--   * integration_reviews — compatibility review records.
--   * merge_proposals — controlled merge proposals.
--   * integration_worktrees — combined test/integration workspaces.
--
-- Design invariants (per zero-agent-execution-lifecycle and
-- zero-planner-worker-contract):
--   * Integration review is a dynamic Sub Agent Type governed by the
--     same permissions and budgets.
--   * Review begins from diffs, touched contracts, dependencies,
--     schema/API/type/config changes, and test evidence.
--   * It does not reread the entire repository without evidence that
--     broad inspection is needed.
--   * Low-risk deterministic conflicts may be resolved by policy;
--     product decisions return to humans.
--   * Merge requires explicit authority and passing gates.
--   * Post-integration memory/RAG update only from accepted results.

-- ------------------------------------------------------------------
-- Integration reviews (compatibility review records)
-- ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS integration_reviews (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id),
    execution_id    TEXT NOT NULL REFERENCES executions(id),
    -- source_task_ids: JSON array of task IDs whose outputs are being
    -- reviewed for compatibility.
    source_task_ids TEXT NOT NULL,
    -- impact_set: JSON array of {file_path, change_type} derived from
    -- task outputs. Per PLAN.md M11: "Impact-set derivation from task
    -- outputs."
    impact_set      TEXT NOT NULL DEFAULT '[]',
    -- touched_contracts: JSON array of contracts/types/schemas/APIs
    -- that were changed.
    touched_contracts TEXT NOT NULL DEFAULT '[]',
    -- combined_test_result: the result of running combined tests in
    -- the integration worktree.
    combined_test_result TEXT
                    CHECK (combined_test_result IN ('pass','fail','not_run')),
    -- conflict_classification: none, low_risk, human_decision_required.
    conflict_classification TEXT NOT NULL DEFAULT 'none'
                    CHECK (conflict_classification IN ('none','low_risk','human_decision_required')),
    -- conflict_details: JSON array of conflict descriptions.
    conflict_details TEXT NOT NULL DEFAULT '[]',
    -- state: pending, reviewing, approved, rejected, human_decision_paused.
    state           TEXT NOT NULL DEFAULT 'pending'
                    CHECK (state IN ('pending','reviewing','approved','rejected','human_decision_paused')),
    -- integration_worktree_id: the worktree used for combined testing.
    integration_worktree_id TEXT,
    -- reviewed_by: the user who performed the review (or NULL if
    -- automated).
    reviewed_by     TEXT REFERENCES users(id),
    -- redacted_summary: safe summary for audit.
    redacted_summary TEXT,
    created_at      TEXT NOT NULL
                    DEFAULT (to_char(clock_timestamp(), 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')),
    updated_at      TEXT NOT NULL
                    DEFAULT (to_char(clock_timestamp(), 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'))
);

CREATE INDEX IF NOT EXISTS idx_integration_reviews_project
    ON integration_reviews(project_id);
CREATE INDEX IF NOT EXISTS idx_integration_reviews_execution
    ON integration_reviews(execution_id);
CREATE INDEX IF NOT EXISTS idx_integration_reviews_state
    ON integration_reviews(state);

-- ------------------------------------------------------------------
-- Merge proposals (controlled merge)
-- ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS merge_proposals (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id),
    integration_review_id TEXT NOT NULL REFERENCES integration_reviews(id),
    execution_id    TEXT NOT NULL REFERENCES executions(id),
    -- source_tasks: JSON array of task IDs included in the merge.
    source_tasks    TEXT NOT NULL,
    -- source_diffs: JSON array of artifact IDs containing diffs.
    source_diffs    TEXT NOT NULL DEFAULT '[]',
    -- checks_passed: whether all combined tests passed.
    checks_passed   INTEGER NOT NULL DEFAULT 0,
    -- risks: JSON array of risk descriptions.
    risks           TEXT NOT NULL DEFAULT '[]',
    -- state: proposed, approved, rejected, merged, cancelled.
    state           TEXT NOT NULL DEFAULT 'proposed'
                    CHECK (state IN ('proposed','approved','rejected','merged','cancelled')),
    -- approved_by: the user who approved the merge.
    approved_by     TEXT REFERENCES users(id),
    -- merged_at: when the merge was executed (NULL until merged).
    merged_at       TEXT,
    created_at      TEXT NOT NULL
                    DEFAULT (to_char(clock_timestamp(), 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')),
    updated_at      TEXT NOT NULL
                    DEFAULT (to_char(clock_timestamp(), 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'))
);

CREATE INDEX IF NOT EXISTS idx_merge_proposals_project
    ON merge_proposals(project_id);
CREATE INDEX IF NOT EXISTS idx_merge_proposals_execution
    ON merge_proposals(execution_id);
CREATE INDEX IF NOT EXISTS idx_merge_proposals_state
    ON merge_proposals(state);