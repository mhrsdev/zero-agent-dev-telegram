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
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_artifact_provenance_artifact
    ON artifact_provenance(project_id, artifact_id, created_at);

CREATE TRIGGER IF NOT EXISTS artifact_provenance_project_match
BEFORE INSERT ON artifact_provenance
WHEN NOT EXISTS (
    SELECT 1 FROM artifacts
    WHERE artifacts.id = NEW.artifact_id
      AND artifacts.project_id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'artifact provenance project mismatch');
END;

CREATE TRIGGER IF NOT EXISTS artifact_provenance_no_update
BEFORE UPDATE ON artifact_provenance
BEGIN
    SELECT RAISE(ABORT, 'artifact provenance is immutable');
END;

CREATE TRIGGER IF NOT EXISTS artifact_provenance_no_delete
BEFORE DELETE ON artifact_provenance
BEGIN
    SELECT RAISE(ABORT, 'artifact provenance is immutable');
END;
