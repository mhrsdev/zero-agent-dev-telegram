-- GENERATED from 0026_project_ownership_and_legacy_provider_recovery.sql by scripts/gen_pg_migrations.py.
-- PostgreSQL dialect translation of the canonical SQLite schema.
-- Do not edit directly; re-run the generator instead.

-- Preserve migration checksums while hardening project ownership and
-- normalizing active provider rows created before lease fencing.
-- Project ownership is a root invariant: denormalized project_id values are
-- immutable rather than movable across projects through direct SQL.





-- A pre-fencing streaming row has no durable owner or token that can fence
-- the old worker. Treating it as unknown is safer than replaying it as if the
-- provider call definitely did not happen. Pending rows were never dispatched
-- and may be claimed normally after their legacy lease fields are cleared.
UPDATE provider_requests
SET state = 'unknown',
    error_class = 'unknown_outcome',
    error_message = 'provider request was active before lease fencing',
    completed_at = COALESCE(completed_at, to_char(clock_timestamp(), 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')),
    claim_owner = NULL,
    claim_token = NULL,
    lease_expires_at = NULL,
    heartbeat_at = NULL
WHERE state = 'streaming' AND claim_token IS NULL;

UPDATE provider_requests
SET claim_owner = NULL,
    claim_token = NULL,
    lease_expires_at = NULL,
    heartbeat_at = NULL
WHERE state = 'pending';
CREATE OR REPLACE FUNCTION zero_plans_project_ownership_immutable_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'project ownership is immutable';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS plans_project_ownership_immutable ON plans;
CREATE TRIGGER plans_project_ownership_immutable
    BEFORE UPDATE OF project_id ON plans
    FOR EACH ROW EXECUTE FUNCTION zero_plans_project_ownership_immutable_fn();

CREATE OR REPLACE FUNCTION zero_agent_types_project_ownership_immutable_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'project ownership is immutable';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS agent_types_project_ownership_immutable ON agent_types;
CREATE TRIGGER agent_types_project_ownership_immutable
    BEFORE UPDATE OF project_id ON agent_types
    FOR EACH ROW EXECUTE FUNCTION zero_agent_types_project_ownership_immutable_fn();

CREATE OR REPLACE FUNCTION zero_knowledge_records_project_ownership_immutable_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'project ownership is immutable';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS knowledge_records_project_ownership_immutable ON knowledge_records;
CREATE TRIGGER knowledge_records_project_ownership_immutable
    BEFORE UPDATE OF project_id ON knowledge_records
    FOR EACH ROW EXECUTE FUNCTION zero_knowledge_records_project_ownership_immutable_fn();

CREATE OR REPLACE FUNCTION zero_rag_documents_project_ownership_immutable_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'project ownership is immutable';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS rag_documents_project_ownership_immutable ON rag_documents;
CREATE TRIGGER rag_documents_project_ownership_immutable
    BEFORE UPDATE OF project_id ON rag_documents
    FOR EACH ROW EXECUTE FUNCTION zero_rag_documents_project_ownership_immutable_fn();
