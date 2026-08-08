-- Zero Develop — Milestone 5 schema: Main Worker and durable execution graph.
--
-- This migration adds:
--   * executions — one execution per approved plan revision.
--   * tasks — nodes in the execution graph.
--   * task_dependencies — edges (A must complete before B).
--   * task_attempts — individual attempts to run a task (for retries).
--   * execution_snapshots — durable restart-safe state.
--
-- Design invariants (per zero-planner-worker-contract and
-- zero-recovery-consistency):
--   * Worker accepts only a valid approved plan revision.
--   * Task state is typed and durable, not held only in model context.
--   * Dependencies determine readiness and concurrency.
--   * Retries and duplicate events are idempotent.
--   * Human-decision conflicts pause rather than being guessed away.
--   * One execution per approved plan revision (UNIQUE constraint).

-- ------------------------------------------------------------------
-- Executions (one per approved plan revision)
-- ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS executions (
    id              TEXT PRIMARY KEY,
    plan_id         TEXT NOT NULL REFERENCES plans(id),
    plan_revision_id TEXT NOT NULL REFERENCES plan_revisions(id),
    plan_handoff_id TEXT NOT NULL REFERENCES plan_handoffs(id),
    project_id      TEXT NOT NULL REFERENCES projects(id),
    -- state: pending (created, not started), running, paused,
    -- completed, failed, cancelled.
    state           TEXT NOT NULL DEFAULT 'pending'
                    CHECK (state IN ('pending','running','paused','completed','failed','cancelled')),
    -- blocker_reason is set when the execution is paused waiting for
    -- a human decision (per PLAN.md M5: "Human-decision conflicts
    -- pause rather than being guessed away").
    blocker_reason  TEXT,
    -- idempotency_key makes duplicate execution creation idempotent.
    -- Per zero-planner-worker-contract §"Idempotency is part of
    -- normal operation": "one execution per approved plan revision".
    idempotency_key TEXT NOT NULL,
    created_at      TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at      TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    -- ONE execution per plan revision. This is the critical invariant.
    UNIQUE (plan_revision_id)
);

CREATE INDEX IF NOT EXISTS idx_executions_project ON executions(project_id);
CREATE INDEX IF NOT EXISTS idx_executions_plan ON executions(plan_id);
CREATE INDEX IF NOT EXISTS idx_executions_state ON executions(state);

-- ------------------------------------------------------------------
-- Tasks (nodes in the execution graph)
-- ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    execution_id    TEXT NOT NULL REFERENCES executions(id),
    project_id      TEXT NOT NULL REFERENCES projects(id),
    -- objective: what this task accomplishes (tied to plan acceptance).
    objective       TEXT NOT NULL,
    -- permitted_scope: what the task is allowed to touch (JSON array
    -- of strings, e.g. file paths or domain areas).
    permitted_scope TEXT NOT NULL,
    -- expected_evidence: what the task must produce (JSON array).
    expected_evidence TEXT NOT NULL,
    -- state: pending (waiting for deps), ready (deps met, not
    -- started), running, completed, failed, blocked, cancelled.
    state           TEXT NOT NULL DEFAULT 'pending'
                    CHECK (state IN ('pending','ready','running','completed','failed','blocked','cancelled')),
    -- blocker_reason is set when the task is blocked waiting for a
    -- human decision.
    blocker_reason  TEXT,
    -- agent_type_id is set when the Worker assigns a Sub Agent Type
    -- to this task (M7). NULL until then.
    agent_type_id   TEXT,
    -- terminal_state_set_at: timestamp when the task reached a
    -- terminal state (completed/failed/cancelled). NULL otherwise.
    terminal_state_set_at TEXT,
    created_at      TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at      TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_tasks_execution ON tasks(execution_id);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);

-- ------------------------------------------------------------------
-- Task dependencies (edges in the execution graph)
-- ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS task_dependencies (
    -- task_id depends on depends_on_task_id. depends_on_task_id must
    -- complete before task_id can become ready.
    task_id         TEXT NOT NULL REFERENCES tasks(id),
    depends_on_task_id TEXT NOT NULL REFERENCES tasks(id),
    created_at      TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (task_id, depends_on_task_id),
    CHECK (task_id != depends_on_task_id)
);

CREATE INDEX IF NOT EXISTS idx_task_deps_task ON task_dependencies(task_id);
CREATE INDEX IF NOT EXISTS idx_task_deps_depends_on
    ON task_dependencies(depends_on_task_id);

-- ------------------------------------------------------------------
-- Task attempts (individual run attempts, for retries)
-- ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS task_attempts (
    id              TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL REFERENCES tasks(id),
    project_id      TEXT NOT NULL REFERENCES projects(id),
    attempt_number  INTEGER NOT NULL,
    -- state: running, succeeded, failed, cancelled, unknown.
    state           TEXT NOT NULL DEFAULT 'running'
                    CHECK (state IN ('running','succeeded','failed','cancelled','unknown')),
    -- error_message is set on failure. MUST NOT contain secrets.
    error_message   TEXT,
    -- lease_owner: the worker that currently owns this attempt. NULL
    -- when no worker owns it (e.g. after completion).
    lease_owner     TEXT,
    -- lease_expires_at: when the lease expires. An expired lease does
    -- NOT prove failure; it proves that current ownership is absent.
    -- Per zero-recovery-consistency §"Leases distinguish ownership
    -- from history".
    lease_expires_at TEXT,
    started_at      TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    completed_at    TEXT,
    UNIQUE (task_id, attempt_number)
);

CREATE INDEX IF NOT EXISTS idx_task_attempts_task
    ON task_attempts(task_id, attempt_number);
CREATE INDEX IF NOT EXISTS idx_task_attempts_state
    ON task_attempts(state);

-- ------------------------------------------------------------------
-- Execution snapshots (durable restart-safe state)
-- ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS execution_snapshots (
    id              TEXT PRIMARY KEY,
    execution_id    TEXT NOT NULL REFERENCES executions(id),
    project_id      TEXT NOT NULL REFERENCES projects(id),
    -- snapshot_version: incremented for each new snapshot. The
    -- highest version is the current restart-safe state.
    snapshot_version INTEGER NOT NULL,
    -- graph_state: a JSON document capturing the full task graph
    -- state (task IDs, states, dependencies, terminal evidence
    -- references). This is what the Worker reconstructs from on
    -- restart.
    graph_state     TEXT NOT NULL,
    -- snapshot_reason: why the snapshot was taken (e.g. "before_fan_out",
    -- "before_transition", "restart_recovery").
    snapshot_reason TEXT NOT NULL,
    created_at      TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (execution_id, snapshot_version)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_execution
    ON execution_snapshots(execution_id, snapshot_version);
