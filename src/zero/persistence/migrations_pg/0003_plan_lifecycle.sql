-- GENERATED from 0003_plan_lifecycle.sql by scripts/gen_pg_migrations.py.
-- PostgreSQL dialect translation of the canonical SQLite schema.
-- Do not edit directly; re-run the generator instead.

-- Zero Develop — Milestone 4 schema: plan lifecycle and Main Planner.
--
-- This migration adds:
--   * conversation_events — interface-neutral intake for human discussion.
--   * plans — a plan (one per objective), project-scoped.
--   * plan_revisions — immutable revisions of a plan's content.
--   * plan_approvals — immutable approval evidence tied to a revision.
--   * plan_handoffs — the single handoff record produced when an
--     authorized user approves a revision (one per approved revision).
--
-- Design invariants (per zero-planner-worker-contract and
-- zero-control-plane-trust):
--   * Plans are versioned proposals. Editing produces a new revision;
--     it does not retroactively change what was approved.
--   * Approval names a specific revision. Stale approvals fail safely.
--   * Rejection produces no runnable handoff.
--   * History remains inspectable.
--   * Duplicate delivery is idempotent.
--   * One handoff per approved revision (UNIQUE constraint).

-- ------------------------------------------------------------------
-- Conversation events (interface-neutral intake)
-- ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS conversation_events (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id),
    actor_id        TEXT NOT NULL REFERENCES users(id),
    source          TEXT NOT NULL
                    CHECK (source IN ('web','telegram','discord','system','internal')),
    -- external_event_id is the transport idempotency key (e.g.
    -- Telegram update_id). UNIQUE per (source, external_event_id) so
    -- duplicate delivery is idempotent.
    external_event_id TEXT,
    -- origin_kind classifies the event structurally (per
    -- zero-context-memory §7). role=user alone never proves human
    -- intent; this field distinguishes real human turns from
    -- synthetic runtime messages.
    origin_kind     TEXT NOT NULL
                    CHECK (origin_kind IN ('authenticated_human','planner_injection','system_reminder','compaction_carrier','tool_result','auto_continue')),
    content         TEXT NOT NULL,
    -- The raw payload is stored as a protected artifact reference
    -- (deferred to M8); for now we store the content text only.
    created_at      TEXT NOT NULL
                    DEFAULT (to_char(clock_timestamp(), 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')),
    UNIQUE (source, external_event_id)
);

CREATE INDEX IF NOT EXISTS idx_conv_project_time
    ON conversation_events(project_id, created_at);
CREATE INDEX IF NOT EXISTS idx_conv_actor_time
    ON conversation_events(actor_id, created_at);

-- ------------------------------------------------------------------
-- Plans (one per objective, project-scoped)
-- ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS plans (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id),
    -- current_state is the live state of the plan: draft, proposed,
    -- approved, rejected, superseded, archived.
    current_state   TEXT NOT NULL DEFAULT 'draft'
                    CHECK (current_state IN ('draft','proposed','approved','rejected','superseded','archived')),
    -- current_revision_number is the latest revision number for this
    -- plan. 0 means no revision has been proposed yet.
    current_revision_number INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
                    DEFAULT (to_char(clock_timestamp(), 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')),
    updated_at      TEXT NOT NULL
                    DEFAULT (to_char(clock_timestamp(), 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'))
);

CREATE INDEX IF NOT EXISTS idx_plans_project ON plans(project_id);

-- ------------------------------------------------------------------
-- Plan revisions (immutable)
-- ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS plan_revisions (
    id              TEXT PRIMARY KEY,
    plan_id         TEXT NOT NULL REFERENCES plans(id),
    project_id      TEXT NOT NULL REFERENCES projects(id),
    revision_number INTEGER NOT NULL,
    -- The revision content is structured typed data (per
    -- zero-planner-worker-contract §"Model output becomes data only
    -- after validation"). We store it as JSON text.
    objective       TEXT NOT NULL,
    scope           TEXT NOT NULL,        -- JSON array of strings
    constraints     TEXT NOT NULL,        -- JSON array of strings
    acceptance_criteria TEXT NOT NULL,    -- JSON array of strings
    risks           TEXT NOT NULL,        -- JSON array of strings
    unresolved_questions TEXT NOT NULL,   -- JSON array of strings
    source_event_ids TEXT NOT NULL,       -- JSON array of conversation_event IDs
    -- proposed_by is the user who triggered the proposal (the Main
    -- Planner acts on behalf of an authorized human).
    proposed_by     TEXT NOT NULL REFERENCES users(id),
    state           TEXT NOT NULL DEFAULT 'proposed'
                    CHECK (state IN ('proposed','approved','rejected','superseded')),
    created_at      TEXT NOT NULL
                    DEFAULT (to_char(clock_timestamp(), 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')),
    UNIQUE (plan_id, revision_number)
);

CREATE INDEX IF NOT EXISTS idx_plan_revisions_plan
    ON plan_revisions(plan_id, revision_number);
CREATE INDEX IF NOT EXISTS idx_plan_revisions_project
    ON plan_revisions(project_id);

-- Append-only enforcement for plan_revisions: revisions are immutable
-- once stored. The only field that changes is `state`, and that
-- transition is recorded as a separate approval/rejection event.
-- We DO allow UPDATE on `state` (the state transition is a legitimate
-- business fact), but we block UPDATE on the content fields.
-- SQLite triggers cannot easily distinguish fields, so we rely on the
-- application layer to never update content fields. A future
-- migration to PostgreSQL can add per-field UPDATE triggers.

-- ------------------------------------------------------------------
-- Plan approvals (immutable evidence)
-- ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS plan_approvals (
    id              TEXT PRIMARY KEY,
    plan_id         TEXT NOT NULL REFERENCES plans(id),
    revision_id     TEXT NOT NULL REFERENCES plan_revisions(id),
    project_id      TEXT NOT NULL REFERENCES projects(id),
    -- approved_by is the authorized human who approved this revision.
    approved_by     TEXT NOT NULL REFERENCES users(id),
    source          TEXT NOT NULL
                    CHECK (source IN ('web','telegram','discord','system','internal')),
    result          TEXT NOT NULL
                    CHECK (result IN ('approved','rejected')),
    -- idempotency_key makes duplicate delivery idempotent. The same
    -- (revision_id, approved_by, result, idempotency_key) tuple
    -- returns the same approval record.
    idempotency_key TEXT NOT NULL,
    redacted_reason TEXT,
    created_at      TEXT NOT NULL
                    DEFAULT (to_char(clock_timestamp(), 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')),
    -- One approval per (revision_id, result) — approving twice is
    -- idempotent; rejecting after approval is a separate transition
    -- that is not allowed (the plan is already approved).
    UNIQUE (revision_id, result, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_plan_approvals_plan
    ON plan_approvals(plan_id);
CREATE INDEX IF NOT EXISTS idx_plan_approvals_project
    ON plan_approvals(project_id);

-- Append-only: approvals are immutable evidence.


-- ------------------------------------------------------------------
-- Plan handoffs (the single immutable handoff record per approved revision)
-- ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS plan_handoffs (
    id              TEXT PRIMARY KEY,
    plan_id         TEXT NOT NULL REFERENCES plans(id),
    revision_id     TEXT NOT NULL REFERENCES plan_revisions(id),
    project_id      TEXT NOT NULL REFERENCES projects(id),
    approved_by     TEXT NOT NULL REFERENCES users(id),
    -- execution_id is set when the Worker creates an execution from
    -- this handoff. NULL means the handoff has not yet been picked up
    -- by the Worker.
    execution_id    TEXT,
    created_at      TEXT NOT NULL
                    DEFAULT (to_char(clock_timestamp(), 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')),
    -- ONE handoff per approved revision. This is the critical
    -- invariant: duplicate approval events produce one handoff, not
    -- many. Enforced by UNIQUE(revision_id).
    UNIQUE (revision_id)
);

CREATE INDEX IF NOT EXISTS idx_plan_handoffs_project
    ON plan_handoffs(project_id);
CREATE INDEX IF NOT EXISTS idx_plan_handoffs_execution
    ON plan_handoffs(execution_id);
CREATE OR REPLACE FUNCTION zero_plan_approvals_no_update_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'plan_approvals is append-only';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS plan_approvals_no_update ON plan_approvals;
CREATE TRIGGER plan_approvals_no_update
    BEFORE UPDATE ON plan_approvals
    FOR EACH ROW EXECUTE FUNCTION zero_plan_approvals_no_update_fn();

CREATE OR REPLACE FUNCTION zero_plan_approvals_no_delete_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'plan_approvals is append-only';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS plan_approvals_no_delete ON plan_approvals;
CREATE TRIGGER plan_approvals_no_delete
    BEFORE DELETE ON plan_approvals
    FOR EACH ROW EXECUTE FUNCTION zero_plan_approvals_no_delete_fn();
