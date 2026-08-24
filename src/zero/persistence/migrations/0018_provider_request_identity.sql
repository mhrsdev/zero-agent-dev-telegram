-- Provider request identity and project-scoped deduplication.
--
-- ZERO_MIGRATION_FOREIGN_KEYS_OFF: this is a SQLite table rebuild.  The
-- existing provider_requests table has a global request_hash UNIQUE constraint
-- and no idempotency_key column.  Rebuild it so the durable schema matches the
-- provider service contract and logical project scope.

CREATE TABLE provider_requests_v2 (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id),
    execution_id    TEXT REFERENCES executions(id),
    provider        TEXT NOT NULL,
    model_name      TEXT NOT NULL,
    request_hash    TEXT NOT NULL,
    idempotency_key TEXT,
    state           TEXT NOT NULL DEFAULT 'pending'
                    CHECK (state IN ('pending','streaming','completed','failed','cancelled','unknown')),
    error_class     TEXT,
    error_message   TEXT,
    response_artifact_id TEXT REFERENCES artifacts(id),
    started_at      TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    completed_at    TEXT,
    UNIQUE (project_id, request_hash)
);

INSERT INTO provider_requests_v2
    (id, project_id, execution_id, provider, model_name, request_hash,
     idempotency_key, state, error_class, error_message,
     response_artifact_id, started_at, completed_at)
SELECT id, project_id, execution_id, provider, model_name, request_hash,
       NULL, state, error_class, error_message,
       response_artifact_id, started_at, completed_at
FROM provider_requests;

DROP TABLE provider_requests;
ALTER TABLE provider_requests_v2 RENAME TO provider_requests;

CREATE INDEX idx_provider_requests_project
    ON provider_requests(project_id);
CREATE INDEX idx_provider_requests_execution
    ON provider_requests(execution_id);
CREATE INDEX idx_provider_requests_state
    ON provider_requests(state);
CREATE UNIQUE INDEX uq_provider_requests_project_idempotency
    ON provider_requests(project_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
