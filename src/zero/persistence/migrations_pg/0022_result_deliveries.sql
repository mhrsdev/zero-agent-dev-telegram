-- GENERATED from 0022_result_deliveries.sql by scripts/gen_pg_migrations.py.
-- PostgreSQL dialect translation of the canonical SQLite schema.
-- Do not edit directly; re-run the generator instead.

-- Durable outbound execution-result delivery queue.
-- The queue is an intent boundary: scheduler completion never claims that a
-- provider message was sent. A separate delivery drain must claim, send,
-- record the external receipt, or preserve a retryable failure.

CREATE TABLE IF NOT EXISTS result_deliveries (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL REFERENCES projects(id),
    execution_id        TEXT NOT NULL REFERENCES executions(id),
    binding_id          TEXT NOT NULL REFERENCES interface_bindings(id),
    created_by          TEXT NOT NULL REFERENCES users(id),
    delivery_key        TEXT NOT NULL,
    content             TEXT NOT NULL,
    state               TEXT NOT NULL DEFAULT 'pending'
                        CHECK (state IN ('pending','processing','sent','failed')),
    attempt_count       INTEGER NOT NULL DEFAULT 0
                        CHECK (attempt_count >= 0),
    claim_token         TEXT,
    lease_expires_at    TEXT,
    external_message_id TEXT,
    last_error          TEXT,
    created_at          TEXT NOT NULL
                        DEFAULT (to_char(clock_timestamp(), 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')),
    updated_at          TEXT NOT NULL
                        DEFAULT (to_char(clock_timestamp(), 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')),
    UNIQUE (project_id, delivery_key)
);

CREATE INDEX IF NOT EXISTS idx_result_deliveries_pending
    ON result_deliveries(project_id, state, created_at);
CREATE INDEX IF NOT EXISTS idx_result_deliveries_execution
    ON result_deliveries(project_id, execution_id);