-- Security hardening: opaque per-user access tokens and enforceable tool caps.

CREATE TABLE IF NOT EXISTS access_tokens (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash  TEXT NOT NULL UNIQUE,
    expires_at  TEXT NOT NULL,
    revoked_at  TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CHECK (revoked_at IS NULL OR revoked_at >= created_at)
);

CREATE INDEX IF NOT EXISTS idx_access_tokens_active
    ON access_tokens(token_hash, expires_at)
    WHERE revoked_at IS NULL;

ALTER TABLE tool_grants
    ADD COLUMN invocation_count INTEGER NOT NULL DEFAULT 0
    CHECK (invocation_count >= 0);
