-- Durable evidence produced by the automatic integration review test.
-- The evidence remains project/review/execution/worktree scoped even after
-- the isolated integration worktree is removed from disk.
CREATE TABLE IF NOT EXISTS integration_review_evidence (
    id                      TEXT PRIMARY KEY,
    project_id              TEXT NOT NULL REFERENCES projects(id),
    review_id               TEXT NOT NULL REFERENCES integration_reviews(id),
    execution_id            TEXT NOT NULL REFERENCES executions(id),
    integration_worktree_id TEXT NOT NULL REFERENCES integration_worktrees(id),
    worktree_path           TEXT NOT NULL,
    kind                    TEXT NOT NULL CHECK (kind IN ('test', 'preparation', 'failure')),
    command                 TEXT NOT NULL,
    args                    TEXT NOT NULL DEFAULT '[]',
    exit_code               INTEGER,
    timed_out               INTEGER NOT NULL DEFAULT 0 CHECK (timed_out IN (0, 1)),
    stdout                  TEXT NOT NULL DEFAULT '',
    stderr                  TEXT NOT NULL DEFAULT '',
    content_hash            TEXT NOT NULL,
    created_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (project_id, review_id, id)
);

CREATE INDEX IF NOT EXISTS idx_integration_review_evidence_review
    ON integration_review_evidence(project_id, review_id, created_at);
