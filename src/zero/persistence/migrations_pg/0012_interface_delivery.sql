-- GENERATED from 0012_interface_delivery.sql by scripts/gen_pg_migrations.py.
-- PostgreSQL dialect translation of the canonical SQLite schema.
-- Do not edit directly; re-run the generator instead.

-- Interface delivery hardening (Gate D, kept separate from operations).
--
-- Durable claims make webhook redelivery safe even when the domain side
-- effect is slower than the transport acknowledgement.  Cursors are
-- scoped by adapter instance/binding and are stored as text to preserve
-- provider-wide integer ranges.

CREATE TABLE IF NOT EXISTS interface_event_claims (
    platform            TEXT NOT NULL
                        CHECK (platform IN ('telegram','discord','other')),
    external_event_id   TEXT NOT NULL,
    state               TEXT NOT NULL DEFAULT 'processing'
                        CHECK (state IN ('processing','succeeded','failed')),
    attempt_count       INTEGER NOT NULL DEFAULT 1
                        CHECK (attempt_count > 0),
    claimed_at          TEXT NOT NULL
                        DEFAULT (to_char(clock_timestamp(), 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')),
    completed_at        TEXT,
    PRIMARY KEY (platform, external_event_id)
);

CREATE INDEX IF NOT EXISTS idx_interface_event_claims_retry
    ON interface_event_claims(platform, state, claimed_at);

CREATE TABLE IF NOT EXISTS interface_cursors (
    platform            TEXT NOT NULL
                        CHECK (platform IN ('telegram','discord','other')),
    scope_key           TEXT NOT NULL,
    cursor              TEXT NOT NULL,
    updated_at          TEXT NOT NULL
                        DEFAULT (to_char(clock_timestamp(), 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')),
    PRIMARY KEY (platform, scope_key)
);

-- SQLite UNIQUE constraints treat NULL values as distinct.  These partial
-- indexes enforce one general-chat binding while retaining topic scopes.
CREATE UNIQUE INDEX IF NOT EXISTS uq_interface_bindings_general
    ON interface_bindings(platform, chat_id)
    WHERE topic_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_interface_bindings_topic
    ON interface_bindings(platform, chat_id, topic_id)
    WHERE topic_id IS NOT NULL;