-- GENERATED from 0015_task_artifact_immutability.sql by scripts/gen_pg_migrations.py.
-- PostgreSQL dialect translation of the canonical SQLite schema.
-- Do not edit directly; re-run the generator instead.

-- Task artifacts are canonical execution evidence.  They are append-only;
-- application code verifies SHA-256 while these triggers protect the record
-- against direct SQL mutation and deletion.


CREATE OR REPLACE FUNCTION zero_task_artifacts_no_update_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'task_artifacts are immutable';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS task_artifacts_no_update ON task_artifacts;
CREATE TRIGGER task_artifacts_no_update
    BEFORE UPDATE ON task_artifacts
    FOR EACH ROW EXECUTE FUNCTION zero_task_artifacts_no_update_fn();

CREATE OR REPLACE FUNCTION zero_task_artifacts_no_delete_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'task_artifacts are immutable';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS task_artifacts_no_delete ON task_artifacts;
CREATE TRIGGER task_artifacts_no_delete
    BEFORE DELETE ON task_artifacts
    FOR EACH ROW EXECUTE FUNCTION zero_task_artifacts_no_delete_fn();
