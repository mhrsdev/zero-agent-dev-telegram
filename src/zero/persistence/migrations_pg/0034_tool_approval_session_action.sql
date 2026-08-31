-- GENERATED from 0034_tool_approval_session_action.sql by scripts/gen_pg_migrations.py.
-- PostgreSQL dialect translation of the canonical SQLite schema.
-- Do not edit directly; re-run the generator instead.

-- ZERO_MIGRATION_FOREIGN_KEYS_OFF
-- Fix 15 (2026-08-31, Hermes approval-card parity): the Telegram card
-- offered allow once / allow always / deny but NO session-grain action,
-- so the gate's ``session`` grain (in-process reuse for the rest of the
-- execution) was reachable only through the REST surface. Hermes ships
-- a 2x2 keyboard (once / session / always / deny) for exactly this.
--
-- SQLite cannot ALTER a CHECK constraint, so the table is rebuilt with
-- the widened action vocabulary. Rows and indexes are preserved.
CREATE TABLE tool_approval_tokens_v2 (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    -- approval_id: the tool_approval_decisions row this token resolves.
    approval_id TEXT NOT NULL,
    -- action: allow once / allow session (execution-scoped grant) /
    -- allow always (durable grant) / deny.
    action TEXT NOT NULL CHECK (action IN ('allow_once', 'allow_session', 'allow_always', 'deny')),
    -- expires_at: old tokens cannot be used (same 24h TTL semantics as
    -- plan callback tokens).
    expires_at TEXT NOT NULL,
    -- used_at: one-shot consumption marker.
    used_at TEXT,
    created_by TEXT REFERENCES users(id),
    created_at TEXT NOT NULL
);

INSERT INTO tool_approval_tokens_v2
    (id, project_id, approval_id, action, expires_at, used_at, created_by, created_at)
SELECT
    id, project_id, approval_id, action, expires_at, used_at, created_by, created_at
FROM tool_approval_tokens;

DROP TABLE tool_approval_tokens;
ALTER TABLE tool_approval_tokens_v2 RENAME TO tool_approval_tokens;

CREATE INDEX idx_tool_approval_tokens_approval
    ON tool_approval_tokens(approval_id);
CREATE INDEX idx_tool_approval_tokens_unused
    ON tool_approval_tokens(project_id) WHERE used_at IS NULL;