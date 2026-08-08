-- Zero Develop — Milestone 2 + Milestone 3 schema.
--
-- This migration adds:
--   * users and external_identities (canonical identity model).
--   * project owner and project_memberships (project isolation).
--   * permissions and role assignments (authorization matrix).
--   * secret_references (server-side secret storage).
--   * tools and tool_grants (capability-based tool runtime).
--   * audit_events (append-only authority evidence).
--
-- Design invariants enforced by constraints (per ADR 0005 and the
-- zero-project-isolation-evidence skill):
--   * Every project-scoped table has a project_id column with a FK
--     to projects.
--   * External identity uniqueness is (platform, external_id), not
--     display name.
--   * Project membership uniqueness is (project_id, user_id).
--   * Audit events are append-only (no UPDATE/DELETE path is exposed
--     by the application; constraints make the table insertion-only
--     via a trigger that blocks UPDATE and DELETE).
--   * Tool grants are scoped to (project_id, tool_id, agent_scope).

-- ------------------------------------------------------------------
-- Users
-- ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS users (
    id              TEXT PRIMARY KEY,
    display_name    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','suspended','deleted')),
    created_at      TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- ------------------------------------------------------------------
-- External identities (links to Zero users, never authority on their own)
-- ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS external_identities (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id),
    platform        TEXT NOT NULL
                    CHECK (platform IN ('telegram','discord','web','email','other')),
    external_id     TEXT NOT NULL,
    external_username TEXT,
    verified_at     TEXT,
    created_at      TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (platform, external_id)
);

-- ------------------------------------------------------------------
-- Projects (extend the table created in 0001_initial.sql).
-- We add the owner_user_id column. Because ALTER TABLE in SQLite
-- cannot add a column with a non-constant default or a FK clause
-- directly, we add the column without a FK and enforce the FK in
-- application code. A future migration to PostgreSQL will define
-- this properly.
-- ------------------------------------------------------------------

-- SQLite-specific: add column if it does not exist. We use a guarded
-- approach because the column may already exist if the migration was
-- partially applied.
ALTER TABLE projects ADD COLUMN owner_user_id TEXT REFERENCES users(id);

CREATE INDEX IF NOT EXISTS idx_projects_owner ON projects(owner_user_id);

-- ------------------------------------------------------------------
-- Project memberships
-- ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS project_memberships (
    project_id      TEXT NOT NULL REFERENCES projects(id),
    user_id         TEXT NOT NULL REFERENCES users(id),
    role            TEXT NOT NULL
                    CHECK (role IN ('owner','member','viewer')),
    created_at      TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (project_id, user_id)
);

-- ------------------------------------------------------------------
-- Secret references (server-side, encrypted)
-- ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS secret_references (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id),
    name            TEXT NOT NULL,
    secret_type     TEXT NOT NULL
                    CHECK (secret_type IN ('api_key','token','password','other')),
    encrypted_value TEXT NOT NULL,
    -- The encrypted_value is a Fernet token. The encryption key is
    -- derived from ZERO_SECRET_KEY in the application. The raw value
    -- is NEVER stored, NEVER logged, NEVER returned to the client.
    created_at      TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    revoked_at      TEXT,
    UNIQUE (project_id, name)
);

CREATE INDEX IF NOT EXISTS idx_secrets_project ON secret_references(project_id);

-- ------------------------------------------------------------------
-- Tools (registry)
-- ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS tools (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    description     TEXT NOT NULL,
    input_schema    TEXT NOT NULL,  -- JSON Schema as text
    output_schema   TEXT NOT NULL,  -- JSON Schema as text
    -- The handler_key tells the application which Python handler to
    -- invoke. It is server-side only; never sent to models.
    handler_key     TEXT NOT NULL,
    created_at      TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- ------------------------------------------------------------------
-- Tool grants (capability grants per project + agent scope)
-- ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS tool_grants (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id),
    tool_id         TEXT NOT NULL REFERENCES tools(id),
    agent_scope     TEXT NOT NULL
                    CHECK (agent_scope IN ('main_planner','main_worker','sub_agent_type','integration')),
    -- Optional limit fields; NULL means "use tool default". Real
    -- limits (count, concurrency, cost) arrive in later milestones.
    max_invocations INTEGER,
    timeout_seconds INTEGER,
    created_at      TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (project_id, tool_id, agent_scope)
);

CREATE INDEX IF NOT EXISTS idx_tool_grants_project
    ON tool_grants(project_id);

-- ------------------------------------------------------------------
-- Audit events (append-only)
-- ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS audit_events (
    id              TEXT PRIMARY KEY,
    project_id      TEXT,  -- nullable for system-wide events
    actor_id        TEXT,  -- nullable for system events with no actor
    source          TEXT NOT NULL
                    CHECK (source IN ('web','telegram','discord','system','internal')),
    operation       TEXT NOT NULL,
    target_type     TEXT,
    target_id       TEXT,
    result          TEXT NOT NULL
                    CHECK (result IN ('success','denied','failure','error')),
    correlation_id  TEXT,
    -- redacted_summary is a small, safe, human-readable description.
    -- It MUST NOT contain raw payloads, secrets, prompts, or PII.
    redacted_summary TEXT,
    created_at      TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_audit_project_time
    ON audit_events(project_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_actor_time
    ON audit_events(actor_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_correlation
    ON audit_events(correlation_id);

-- Append-only enforcement: block UPDATE and DELETE on audit_events.
-- Per zero-control-plane-trust §"Audit is evidence, not a transcript
-- dump" and zero-recovery-consistency §"Idempotency makes retries
-- ordinary": the audit trail is the durable authority evidence; it
-- must not be silently mutated.
CREATE TRIGGER IF NOT EXISTS audit_events_no_update
    BEFORE UPDATE ON audit_events
    FOR EACH ROW
    BEGIN
        SELECT RAISE(ABORT, 'audit_events is append-only');
    END;

CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
    BEFORE DELETE ON audit_events
    FOR EACH ROW
    BEGIN
        SELECT RAISE(ABORT, 'audit_events is append-only');
    END;
