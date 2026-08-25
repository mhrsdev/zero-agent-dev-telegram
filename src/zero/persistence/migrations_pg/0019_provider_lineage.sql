-- GENERATED from 0019_provider_lineage.sql by scripts/gen_pg_migrations.py.
-- PostgreSQL dialect translation of the canonical SQLite schema.
-- Do not edit directly; re-run the generator instead.

-- Provider request/usage project-lineage enforcement.
-- Single-column foreign keys prove existence, not ownership. These triggers
-- reject confused-deputy rows even when a caller has valid IDs from another
-- project.








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

CREATE OR REPLACE FUNCTION zero_trg_usage_execution_project_lineage_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'usage execution project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_usage_execution_project_lineage ON usage_records;
CREATE TRIGGER trg_usage_execution_project_lineage
    BEFORE INSERT ON usage_records
    FOR EACH ROW EXECUTE FUNCTION zero_trg_usage_execution_project_lineage_fn();

CREATE OR REPLACE FUNCTION zero_trg_usage_execution_project_lineage_update_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'usage execution project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_usage_execution_project_lineage_update ON usage_records;
CREATE TRIGGER trg_usage_execution_project_lineage_update
    BEFORE UPDATE OF project_id, execution_id ON usage_records
    FOR EACH ROW EXECUTE FUNCTION zero_trg_usage_execution_project_lineage_update_fn();
