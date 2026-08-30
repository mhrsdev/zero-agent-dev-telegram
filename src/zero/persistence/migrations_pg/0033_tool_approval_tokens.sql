-- GENERATED from 0033_tool_approval_tokens.sql by scripts/gen_pg_migrations.py.
-- PostgreSQL dialect translation of the canonical SQLite schema.
-- Do not edit directly; re-run the generator instead.

-- Tool approval inline buttons (Hermes parity, 2026-08-31).
--
-- Hermes resolves per-call tool approvals from the messaging surface
-- itself: the approval prompt carries inline buttons (once / session /
-- always / deny) whose callback ids are opaque references. Zero had the
-- durable gate + REST resolve endpoint but NO messaging surface, so a
-- manual-mode deployment could only be unblocked through the HTTP API
-- or the /approvals listing.
--
-- This table mirrors callback_tokens for tool approvals: the Telegram
-- card carries opaque token ids as callback_data; the server resolves
-- the CURRENT gate state and permission when the button is pressed
-- ("UI controls are not authority"), consumes the token one-shot, and
-- answers the press with the outcome toast.
CREATE TABLE tool_approval_tokens (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    -- approval_id: the tool_approval_decisions row this token resolves.
    approval_id TEXT NOT NULL,
    -- action: allow once / allow always (durable grant) / deny.
    action TEXT NOT NULL CHECK (action IN ('allow_once', 'allow_always', 'deny')),
    -- expires_at: old tokens cannot be used (same 24h TTL semantics as
    -- plan callback tokens).
    expires_at TEXT NOT NULL,
    -- used_at: one-shot consumption marker.
    used_at TEXT,
    created_by TEXT REFERENCES users(id),
    created_at TEXT NOT NULL
);

CREATE INDEX idx_tool_approval_tokens_approval
    ON tool_approval_tokens(approval_id);
CREATE INDEX idx_tool_approval_tokens_unused
    ON tool_approval_tokens(project_id) WHERE used_at IS NULL;