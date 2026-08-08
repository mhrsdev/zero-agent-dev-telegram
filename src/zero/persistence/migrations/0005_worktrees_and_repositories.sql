-- Zero Develop — Milestone 6 schema: isolated execution with branches and worktrees.
--
-- This migration adds:
--   * repositories — registered target repositories for coding tasks.
--   * worktrees — isolated working trees (one per task attempt).
--   * command_runs — scoped, time-bounded command invocations.
--   * task_artifacts — captured stdout/stderr/diff/test evidence.
--
-- Design invariants (per zero-agent-execution-lifecycle and
-- zero-recovery-consistency):
--   * Every coding task receives an isolated branch and working tree.
--   * The target repository and base revision are explicit.
--   * Commands are scoped, time-bounded, and audited.
--   * A task returns diff, checks, artifacts, and status.
--   * No task pushes, merges, or deploys without explicit authority.
--   * Cleanup never deletes an unknown path, mount, active workspace,
--     or uncommitted human work.

-- ------------------------------------------------------------------
-- Repositories (registered target repositories)
-- ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS repositories (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id),
    name            TEXT NOT NULL,
    -- local_path is the absolute filesystem path to the bare or
    -- working clone that worktrees will be created from. Validated
    -- at registration time.
    local_path      TEXT NOT NULL,
    -- default_base_revision is the revision to branch from when no
    -- explicit base is provided. May be a branch name, tag, or SHA.
    default_base_revision TEXT,
    created_at      TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (project_id, name)
);

CREATE INDEX IF NOT EXISTS idx_repositories_project
    ON repositories(project_id);

-- ------------------------------------------------------------------
-- Worktrees (isolated working trees, one per task attempt)
-- ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS worktrees (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id),
    repository_id   TEXT NOT NULL REFERENCES repositories(id),
    -- execution_id and task_id link the worktree to the execution graph.
    execution_id    TEXT NOT NULL REFERENCES executions(id),
    task_id         TEXT NOT NULL REFERENCES tasks(id),
    -- branch_name is the git branch created for this worktree.
    branch_name     TEXT NOT NULL,
    -- worktree_path is the absolute filesystem path to the worktree.
    worktree_path   TEXT NOT NULL,
    -- base_revision is the immutable revision the branch was created from.
    base_revision   TEXT NOT NULL,
    -- state: allocated, active, interrupted, succeeded, failed,
    -- cancelled, cleanup_eligible, removed.
    state           TEXT NOT NULL DEFAULT 'allocated'
                    CHECK (state IN ('allocated','active','interrupted','succeeded','failed','cancelled','cleanup_eligible','removed')),
    -- cleanup_eligible_at: when the worktree became eligible for
    -- cleanup. NULL until cleanup is safe.
    cleanup_eligible_at TEXT,
    created_at      TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at      TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_worktrees_project ON worktrees(project_id);
CREATE INDEX IF NOT EXISTS idx_worktrees_execution ON worktrees(execution_id);
CREATE INDEX IF NOT EXISTS idx_worktrees_task ON worktrees(task_id);
CREATE INDEX IF NOT EXISTS idx_worktrees_state ON worktrees(state);
-- One active worktree per task at a time.
CREATE UNIQUE INDEX IF NOT EXISTS idx_worktrees_task_active
    ON worktrees(task_id) WHERE state IN ('allocated','active','interrupted');

-- ------------------------------------------------------------------
-- Command runs (scoped, time-bounded command invocations)
-- ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS command_runs (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id),
    worktree_id     TEXT NOT NULL REFERENCES worktrees(id),
    task_id         TEXT NOT NULL REFERENCES tasks(id),
    -- command is the executable name (e.g. "python", "pytest", "git").
    command         TEXT NOT NULL,
    -- args is a JSON array of string arguments.
    args            TEXT NOT NULL DEFAULT '[]',
    -- exit_code is NULL while running; integer when complete.
    exit_code       INTEGER,
    -- timed_out is TRUE if the command exceeded its timeout.
    timed_out       INTEGER NOT NULL DEFAULT 0,
    -- timeout_seconds is the per-command timeout.
    timeout_seconds INTEGER NOT NULL DEFAULT 300,
    started_at      TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    completed_at    TEXT,
    -- state: running, completed, timed_out, cancelled, unknown.
    state           TEXT NOT NULL DEFAULT 'running'
                    CHECK (state IN ('running','completed','timed_out','cancelled','unknown'))
);

CREATE INDEX IF NOT EXISTS idx_command_runs_worktree
    ON command_runs(worktree_id);
CREATE INDEX IF NOT EXISTS idx_command_runs_task
    ON command_runs(task_id);
CREATE INDEX IF NOT EXISTS idx_command_runs_state
    ON command_runs(state);

-- ------------------------------------------------------------------
-- Task artifacts (captured stdout/stderr/diff/test evidence)
-- ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS task_artifacts (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id),
    worktree_id     TEXT NOT NULL REFERENCES worktrees(id),
    task_id         TEXT NOT NULL REFERENCES tasks(id),
    command_run_id  TEXT REFERENCES command_runs(id),
    -- kind: stdout, stderr, diff, test_report, exit_status, other.
    kind            TEXT NOT NULL
                    CHECK (kind IN ('stdout','stderr','diff','test_report','exit_status','other')),
    -- content is the captured text. For large outputs, this will be
    -- replaced by an artifact store reference in M8.
    content         TEXT NOT NULL,
    -- content_hash is a SHA-256 of the content for integrity.
    content_hash    TEXT NOT NULL,
    created_at      TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_task_artifacts_worktree
    ON task_artifacts(worktree_id);
CREATE INDEX IF NOT EXISTS idx_task_artifacts_task
    ON task_artifacts(task_id);
CREATE INDEX IF NOT EXISTS idx_task_artifacts_kind
    ON task_artifacts(kind);
