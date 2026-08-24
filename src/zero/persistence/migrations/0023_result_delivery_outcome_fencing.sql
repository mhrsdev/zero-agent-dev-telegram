-- ZERO_MIGRATION_FOREIGN_KEYS_OFF
-- Add explicit ambiguous-outcome and delayed-retry semantics to the durable
-- result-delivery outbox. A worker that loses the provider response cannot
-- safely replay a message without an operator/provider reconciliation step.

CREATE TABLE result_deliveries_v2 (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL REFERENCES projects(id),
    execution_id        TEXT NOT NULL REFERENCES executions(id),
    binding_id          TEXT NOT NULL REFERENCES interface_bindings(id),
    created_by          TEXT NOT NULL REFERENCES users(id),
    delivery_key        TEXT NOT NULL,
    content             TEXT NOT NULL,
    state               TEXT NOT NULL DEFAULT 'pending'
                        CHECK (state IN ('pending','processing','sent','failed','unknown')),
    attempt_count       INTEGER NOT NULL DEFAULT 0
                        CHECK (attempt_count >= 0),
    claim_token         TEXT,
    lease_expires_at    TEXT,
    next_attempt_at     TEXT NOT NULL
                        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    external_message_id TEXT,
    last_error          TEXT,
    created_at          TEXT NOT NULL
                        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at          TEXT NOT NULL
                        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (project_id, delivery_key)
);

INSERT INTO result_deliveries_v2
    (id, project_id, execution_id, binding_id, created_by, delivery_key,
     content, state, attempt_count, claim_token, lease_expires_at,
     next_attempt_at, external_message_id, last_error, created_at, updated_at)
SELECT id, project_id, execution_id, binding_id, created_by, delivery_key,
       content, state, attempt_count, claim_token, lease_expires_at,
       created_at, external_message_id, last_error, created_at, updated_at
FROM result_deliveries;

DROP TABLE result_deliveries;
ALTER TABLE result_deliveries_v2 RENAME TO result_deliveries;

CREATE INDEX idx_result_deliveries_pending
    ON result_deliveries(project_id, state, next_attempt_at, created_at);
CREATE INDEX idx_result_deliveries_execution
    ON result_deliveries(project_id, execution_id);
