-- GENERATED from 0011_security_hardening.sql by scripts/gen_pg_migrations.py.
-- PostgreSQL dialect translation of the canonical SQLite schema.
-- Do not edit directly; re-run the generator instead.

-- Security hardening: opaque per-user access tokens and enforceable tool caps.

CREATE TABLE IF NOT EXISTS access_tokens (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash  TEXT NOT NULL UNIQUE,
    expires_at  TEXT NOT NULL,
    revoked_at  TEXT,
    created_at  TEXT NOT NULL DEFAULT (to_char(clock_timestamp(), 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')),
    CHECK (revoked_at IS NULL OR revoked_at >= created_at)
);

CREATE INDEX IF NOT EXISTS idx_access_tokens_active
    ON access_tokens(token_hash, expires_at)
    WHERE revoked_at IS NULL;

ALTER TABLE tool_grants
    ADD COLUMN invocation_count INTEGER NOT NULL DEFAULT 0
    CHECK (invocation_count >= 0);