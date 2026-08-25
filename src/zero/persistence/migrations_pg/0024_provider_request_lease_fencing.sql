-- GENERATED from 0024_provider_request_lease_fencing.sql by scripts/gen_pg_migrations.py.
-- PostgreSQL dialect translation of the canonical SQLite schema.
-- Do not edit directly; re-run the generator instead.

-- ZERO_MIGRATION_FOREIGN_KEYS_OFF
-- Add durable provider-request lease ownership and heartbeat fencing.
-- A request's started_at records history; lease_expires_at and heartbeat_at
-- determine whether a current worker still owns the active attempt.

DROP TRIGGER IF EXISTS trg_provider_request_execution_project_lineage;
DROP TRIGGER IF EXISTS trg_provider_request_execution_project_lineage_update;
DROP TRIGGER IF EXISTS trg_provider_request_artifact_project_lineage;
DROP TRIGGER IF EXISTS trg_provider_request_artifact_project_lineage_update;
DROP TRIGGER IF EXISTS trg_usage_provider_request_project_lineage;
DROP TRIGGER IF EXISTS trg_usage_provider_request_project_lineage_update;

CREATE TABLE provider_requests_v3 (
    id                    TEXT PRIMARY KEY,
    project_id            TEXT NOT NULL REFERENCES projects(id),
    execution_id          TEXT REFERENCES executions(id),
    provider              TEXT NOT NULL,
    model_name            TEXT NOT NULL,
    request_hash          TEXT NOT NULL,
    idempotency_key       TEXT,
    state                 TEXT NOT NULL DEFAULT 'pending'
                          CHECK (state IN ('pending','streaming','completed','failed','cancelled','unknown')),
    error_class           TEXT,
    error_message         TEXT,
    response_artifact_id  TEXT REFERENCES artifacts(id),
    started_at            TEXT NOT NULL
                          DEFAULT (to_char(clock_timestamp(), 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')),
    completed_at          TEXT,
    attempt_count         INTEGER NOT NULL DEFAULT 0
                          CHECK (attempt_count >= 0),
    claim_owner           TEXT,
    claim_token           TEXT,
    lease_expires_at      TEXT,
    heartbeat_at          TEXT,
    UNIQUE (project_id, request_hash)
);

INSERT INTO provider_requests_v3
    (id, project_id, execution_id, provider, model_name, request_hash,
     idempotency_key, state, error_class, error_message,
     response_artifact_id, started_at, completed_at, attempt_count,
     claim_owner, claim_token, lease_expires_at, heartbeat_at)
SELECT id, project_id, execution_id, provider, model_name, request_hash,
       idempotency_key, state, error_class, error_message,
       response_artifact_id, started_at, completed_at,
       CASE WHEN state IN ('pending','streaming') THEN 1 ELSE 0 END,
       NULL,
       NULL,
       CASE
           WHEN state IN ('pending','streaming')
           THEN to_char(to_timestamp(started_at, 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') + interval '300 seconds', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
           ELSE NULL
       END,
       CASE WHEN state IN ('pending','streaming') THEN started_at ELSE NULL END
FROM provider_requests;

DROP TABLE provider_requests;
ALTER TABLE provider_requests_v3 RENAME TO provider_requests;

CREATE INDEX idx_provider_requests_project
    ON provider_requests(project_id);
CREATE INDEX idx_provider_requests_execution
    ON provider_requests(execution_id);
CREATE INDEX idx_provider_requests_state
    ON provider_requests(state);
CREATE INDEX idx_provider_requests_active_lease
    ON provider_requests(state, lease_expires_at);
CREATE UNIQUE INDEX uq_provider_requests_project_idempotency
    ON provider_requests(project_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;






CREATE OR REPLACE FUNCTION zero_trg_provider_request_execution_project_lineage_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'provider request execution project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_provider_request_execution_project_lineage ON provider_requests;
CREATE TRIGGER trg_provider_request_execution_project_lineage
    BEFORE INSERT ON provider_requests
    FOR EACH ROW EXECUTE FUNCTION zero_trg_provider_request_execution_project_lineage_fn();

CREATE OR REPLACE FUNCTION zero_trg_provider_request_execution_project_lineage_update_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'provider request execution project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_provider_request_execution_project_lineage_update ON provider_requests;
CREATE TRIGGER trg_provider_request_execution_project_lineage_update
    BEFORE UPDATE OF project_id, execution_id ON provider_requests
    FOR EACH ROW EXECUTE FUNCTION zero_trg_provider_request_execution_project_lineage_update_fn();

CREATE OR REPLACE FUNCTION zero_trg_provider_request_artifact_project_lineage_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'provider request artifact project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_provider_request_artifact_project_lineage ON provider_requests;
CREATE TRIGGER trg_provider_request_artifact_project_lineage
    BEFORE INSERT ON provider_requests
    FOR EACH ROW EXECUTE FUNCTION zero_trg_provider_request_artifact_project_lineage_fn();

CREATE OR REPLACE FUNCTION zero_trg_provider_request_artifact_project_lineage_update_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'provider request artifact project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_provider_request_artifact_project_lineage_update ON provider_requests;
CREATE TRIGGER trg_provider_request_artifact_project_lineage_update
    BEFORE UPDATE OF project_id, response_artifact_id ON provider_requests
    FOR EACH ROW EXECUTE FUNCTION zero_trg_provider_request_artifact_project_lineage_update_fn();

CREATE OR REPLACE FUNCTION zero_trg_usage_provider_request_project_lineage_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'usage provider request project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_usage_provider_request_project_lineage ON usage_records;
CREATE TRIGGER trg_usage_provider_request_project_lineage
    BEFORE INSERT ON usage_records
    FOR EACH ROW EXECUTE FUNCTION zero_trg_usage_provider_request_project_lineage_fn();

CREATE OR REPLACE FUNCTION zero_trg_usage_provider_request_project_lineage_update_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'usage provider request project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_usage_provider_request_project_lineage_update ON usage_records;
CREATE TRIGGER trg_usage_provider_request_project_lineage_update
    BEFORE UPDATE OF project_id, provider_request_id ON usage_records
    FOR EACH ROW EXECUTE FUNCTION zero_trg_usage_provider_request_project_lineage_update_fn();
