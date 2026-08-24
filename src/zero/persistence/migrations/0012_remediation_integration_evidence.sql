-- Zero Develop — coordinated remediation schema for integration evidence.
-- Keep this migration distinct; release branches may renumber it.
--
-- This migration adds durable lineage for real Git integration workspaces,
-- command/check/commit evidence, and the resulting target/rollback refs.

CREATE TABLE IF NOT EXISTS integration_worktrees (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id),
    execution_id    TEXT NOT NULL REFERENCES executions(id),
    repository_id   TEXT NOT NULL REFERENCES repositories(id),
    worktree_path   TEXT NOT NULL,
    branch_name     TEXT NOT NULL,
    base_revision   TEXT NOT NULL,
    target_revision TEXT,
    state           TEXT NOT NULL DEFAULT 'created'
                    CHECK (state IN ('created','prepared','checks_passed','merged','failed','removed')),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_integration_worktrees_project
    ON integration_worktrees(project_id);
CREATE INDEX IF NOT EXISTS idx_integration_worktrees_execution
    ON integration_worktrees(execution_id);

CREATE TABLE IF NOT EXISTS integration_evidence (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id),
    execution_id    TEXT NOT NULL REFERENCES executions(id),
    proposal_id     TEXT NOT NULL REFERENCES merge_proposals(id),
    integration_worktree_id TEXT REFERENCES integration_worktrees(id),
    kind            TEXT NOT NULL CHECK (kind IN ('command','test','commit','merge','rollback')),
    command         TEXT,
    args            TEXT NOT NULL DEFAULT '[]',
    exit_code       INTEGER,
    content         TEXT NOT NULL DEFAULT '',
    content_hash    TEXT NOT NULL,
    ref_name        TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_integration_evidence_proposal
    ON integration_evidence(proposal_id);

ALTER TABLE merge_proposals ADD COLUMN integration_worktree_id TEXT;
ALTER TABLE merge_proposals ADD COLUMN target_revision TEXT;
ALTER TABLE merge_proposals ADD COLUMN rollback_revision TEXT;
ALTER TABLE merge_proposals ADD COLUMN evidence_ids TEXT NOT NULL DEFAULT '[]';
