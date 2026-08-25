-- GENERATED from 0016_artifact_provenance.sql by scripts/gen_pg_migrations.py.
-- PostgreSQL dialect translation of the canonical SQLite schema.
-- Do not edit directly; re-run the generator instead.

-- A content-deduplicated artifact can have multiple independent
-- producer/provenance occurrences.  These records preserve that graph
-- without mutating canonical artifact bytes or metadata.

CREATE TABLE IF NOT EXISTS artifact_provenance (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id),
    artifact_id     TEXT NOT NULL REFERENCES artifacts(id),
    actor_id        TEXT NOT NULL REFERENCES users(id),
    producer        TEXT,
    provenance      TEXT,
    created_at      TEXT NOT NULL
                    DEFAULT (to_char(clock_timestamp(), 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'))
);

CREATE INDEX IF NOT EXISTS idx_artifact_provenance_artifact
    ON artifact_provenance(project_id, artifact_id, created_at);



CREATE OR REPLACE FUNCTION zero_artifact_provenance_project_match_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'artifact provenance project mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS artifact_provenance_project_match ON artifact_provenance;
CREATE TRIGGER artifact_provenance_project_match
    BEFORE INSERT ON artifact_provenance
    FOR EACH ROW EXECUTE FUNCTION zero_artifact_provenance_project_match_fn();

CREATE OR REPLACE FUNCTION zero_artifact_provenance_no_update_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'artifact provenance is immutable';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS artifact_provenance_no_update ON artifact_provenance;
CREATE TRIGGER artifact_provenance_no_update
    BEFORE UPDATE ON artifact_provenance
    FOR EACH ROW EXECUTE FUNCTION zero_artifact_provenance_no_update_fn();

CREATE OR REPLACE FUNCTION zero_artifact_provenance_no_delete_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'artifact provenance is immutable';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS artifact_provenance_no_delete ON artifact_provenance;
CREATE TRIGGER artifact_provenance_no_delete
    BEFORE DELETE ON artifact_provenance
    FOR EACH ROW EXECUTE FUNCTION zero_artifact_provenance_no_delete_fn();
