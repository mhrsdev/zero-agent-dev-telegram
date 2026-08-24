-- Task artifacts are canonical execution evidence.  They are append-only;
-- application code verifies SHA-256 while these triggers protect the record
-- against direct SQL mutation and deletion.

CREATE TRIGGER IF NOT EXISTS task_artifacts_no_update
BEFORE UPDATE ON task_artifacts
BEGIN
    SELECT RAISE(ABORT, 'task_artifacts are immutable');
END;

CREATE TRIGGER IF NOT EXISTS task_artifacts_no_delete
BEFORE DELETE ON task_artifacts
BEGIN
    SELECT RAISE(ABORT, 'task_artifacts are immutable');
END;
