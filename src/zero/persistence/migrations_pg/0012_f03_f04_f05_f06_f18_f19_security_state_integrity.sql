-- GENERATED from 0012_f03_f04_f05_f06_f18_f19_security_state_integrity.sql by scripts/gen_pg_migrations.py.
-- PostgreSQL dialect translation of the canonical SQLite schema.
-- Do not edit directly; re-run the generator instead.

-- Zero security/state-integrity remediation.
-- This migration is intentionally named for this remediation stream so it
-- does not collide with generic hardening migrations from other streams.

ALTER TABLE tasks ADD COLUMN completion_evidence TEXT NOT NULL DEFAULT '[]';

-- Denormalized project IDs must agree with their canonical parent records.







CREATE OR REPLACE FUNCTION zero_tasks_project_lineage_insert_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'task execution/project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS tasks_project_lineage_insert ON tasks;
CREATE TRIGGER tasks_project_lineage_insert
    BEFORE INSERT ON tasks
    FOR EACH ROW WHEN NOT EXISTS ( SELECT 1 FROM executions WHERE executions.id = NEW.execution_id AND executions.project_id = NEW.project_id ) EXECUTE FUNCTION zero_tasks_project_lineage_insert_fn();

CREATE OR REPLACE FUNCTION zero_tasks_project_lineage_update_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'task execution/project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS tasks_project_lineage_update ON tasks;
CREATE TRIGGER tasks_project_lineage_update
    BEFORE UPDATE OF execution_id, project_id ON tasks
    FOR EACH ROW WHEN NOT EXISTS ( SELECT 1 FROM executions WHERE executions.id = NEW.execution_id AND executions.project_id = NEW.project_id ) EXECUTE FUNCTION zero_tasks_project_lineage_update_fn();

CREATE OR REPLACE FUNCTION zero_task_attempts_project_lineage_insert_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'attempt task/project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS task_attempts_project_lineage_insert ON task_attempts;
CREATE TRIGGER task_attempts_project_lineage_insert
    BEFORE INSERT ON task_attempts
    FOR EACH ROW WHEN NOT EXISTS ( SELECT 1 FROM tasks WHERE tasks.id = NEW.task_id AND tasks.project_id = NEW.project_id ) EXECUTE FUNCTION zero_task_attempts_project_lineage_insert_fn();

CREATE OR REPLACE FUNCTION zero_task_attempts_project_lineage_update_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'attempt task/project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS task_attempts_project_lineage_update ON task_attempts;
CREATE TRIGGER task_attempts_project_lineage_update
    BEFORE UPDATE OF task_id, project_id ON task_attempts
    FOR EACH ROW WHEN NOT EXISTS ( SELECT 1 FROM tasks WHERE tasks.id = NEW.task_id AND tasks.project_id = NEW.project_id ) EXECUTE FUNCTION zero_task_attempts_project_lineage_update_fn();

CREATE OR REPLACE FUNCTION zero_worktrees_project_lineage_insert_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'worktree project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS worktrees_project_lineage_insert ON worktrees;
CREATE TRIGGER worktrees_project_lineage_insert
    BEFORE INSERT ON worktrees
    FOR EACH ROW WHEN NOT EXISTS ( SELECT 1 FROM repositories r JOIN executions e ON e.id = NEW.execution_id JOIN tasks t ON t.id = NEW.task_id WHERE r.id = NEW.repository_id AND r.project_id = NEW.project_id AND e.project_id = NEW.project_id AND t.execution_id = NEW.execution_id AND t.project_id = NEW.project_id ) EXECUTE FUNCTION zero_worktrees_project_lineage_insert_fn();

CREATE OR REPLACE FUNCTION zero_worktrees_project_lineage_update_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'worktree project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS worktrees_project_lineage_update ON worktrees;
CREATE TRIGGER worktrees_project_lineage_update
    BEFORE UPDATE OF repository_id, execution_id, task_id, project_id ON worktrees
    FOR EACH ROW WHEN NOT EXISTS ( SELECT 1 FROM repositories r JOIN executions e ON e.id = NEW.execution_id JOIN tasks t ON t.id = NEW.task_id WHERE r.id = NEW.repository_id AND r.project_id = NEW.project_id AND e.project_id = NEW.project_id AND t.execution_id = NEW.execution_id AND t.project_id = NEW.project_id ) EXECUTE FUNCTION zero_worktrees_project_lineage_update_fn();

CREATE OR REPLACE FUNCTION zero_command_runs_project_lineage_insert_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'command run project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS command_runs_project_lineage_insert ON command_runs;
CREATE TRIGGER command_runs_project_lineage_insert
    BEFORE INSERT ON command_runs
    FOR EACH ROW WHEN NOT EXISTS ( SELECT 1 FROM worktrees w JOIN tasks t ON t.id = NEW.task_id WHERE w.id = NEW.worktree_id AND w.project_id = NEW.project_id AND w.task_id = NEW.task_id AND t.project_id = NEW.project_id ) EXECUTE FUNCTION zero_command_runs_project_lineage_insert_fn();

CREATE OR REPLACE FUNCTION zero_task_artifacts_project_lineage_insert_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'task artifact project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS task_artifacts_project_lineage_insert ON task_artifacts;
CREATE TRIGGER task_artifacts_project_lineage_insert
    BEFORE INSERT ON task_artifacts
    FOR EACH ROW WHEN NOT EXISTS ( SELECT 1 FROM worktrees w JOIN tasks t ON t.id = NEW.task_id WHERE w.id = NEW.worktree_id AND w.project_id = NEW.project_id AND w.task_id = NEW.task_id AND t.project_id = NEW.project_id ) EXECUTE FUNCTION zero_task_artifacts_project_lineage_insert_fn();
