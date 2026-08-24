-- Zero security/state-integrity remediation.
-- This migration is intentionally named for this remediation stream so it
-- does not collide with generic hardening migrations from other streams.

ALTER TABLE tasks ADD COLUMN completion_evidence TEXT NOT NULL DEFAULT '[]';

-- Denormalized project IDs must agree with their canonical parent records.
CREATE TRIGGER IF NOT EXISTS tasks_project_lineage_insert
BEFORE INSERT ON tasks
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1 FROM executions
    WHERE executions.id = NEW.execution_id
      AND executions.project_id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'task execution/project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS tasks_project_lineage_update
BEFORE UPDATE OF execution_id, project_id ON tasks
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1 FROM executions
    WHERE executions.id = NEW.execution_id
      AND executions.project_id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'task execution/project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS task_attempts_project_lineage_insert
BEFORE INSERT ON task_attempts
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1 FROM tasks
    WHERE tasks.id = NEW.task_id
      AND tasks.project_id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'attempt task/project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS task_attempts_project_lineage_update
BEFORE UPDATE OF task_id, project_id ON task_attempts
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1 FROM tasks
    WHERE tasks.id = NEW.task_id
      AND tasks.project_id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'attempt task/project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS worktrees_project_lineage_insert
BEFORE INSERT ON worktrees
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1
    FROM repositories r
    JOIN executions e ON e.id = NEW.execution_id
    JOIN tasks t ON t.id = NEW.task_id
    WHERE r.id = NEW.repository_id
      AND r.project_id = NEW.project_id
      AND e.project_id = NEW.project_id
      AND t.execution_id = NEW.execution_id
      AND t.project_id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'worktree project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS worktrees_project_lineage_update
BEFORE UPDATE OF repository_id, execution_id, task_id, project_id ON worktrees
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1
    FROM repositories r
    JOIN executions e ON e.id = NEW.execution_id
    JOIN tasks t ON t.id = NEW.task_id
    WHERE r.id = NEW.repository_id
      AND r.project_id = NEW.project_id
      AND e.project_id = NEW.project_id
      AND t.execution_id = NEW.execution_id
      AND t.project_id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'worktree project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS command_runs_project_lineage_insert
BEFORE INSERT ON command_runs
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1 FROM worktrees w
    JOIN tasks t ON t.id = NEW.task_id
    WHERE w.id = NEW.worktree_id
      AND w.project_id = NEW.project_id
      AND w.task_id = NEW.task_id
      AND t.project_id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'command run project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS task_artifacts_project_lineage_insert
BEFORE INSERT ON task_artifacts
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1 FROM worktrees w
    JOIN tasks t ON t.id = NEW.task_id
    WHERE w.id = NEW.worktree_id
      AND w.project_id = NEW.project_id
      AND w.task_id = NEW.task_id
      AND t.project_id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'task artifact project lineage mismatch');
END;
