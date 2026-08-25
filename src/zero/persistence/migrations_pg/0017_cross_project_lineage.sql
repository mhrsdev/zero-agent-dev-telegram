-- GENERATED from 0017_cross_project_lineage.sql by scripts/gen_pg_migrations.py.
-- PostgreSQL dialect translation of the canonical SQLite schema.
-- Do not edit directly; re-run the generator instead.

-- Cross-table lineage defense for denormalized project IDs.
-- SQLite foreign keys validate existence, while these triggers validate
-- that every referenced graph node belongs to the same project/lineage.















CREATE OR REPLACE FUNCTION zero_executions_project_lineage_insert_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'execution project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS executions_project_lineage_insert ON executions;
CREATE TRIGGER executions_project_lineage_insert
    BEFORE INSERT ON executions
    FOR EACH ROW EXECUTE FUNCTION zero_executions_project_lineage_insert_fn();

CREATE OR REPLACE FUNCTION zero_executions_project_lineage_update_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'execution project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS executions_project_lineage_update ON executions;
CREATE TRIGGER executions_project_lineage_update
    BEFORE UPDATE OF plan_id, plan_revision_id, plan_handoff_id, project_id ON executions
    FOR EACH ROW EXECUTE FUNCTION zero_executions_project_lineage_update_fn();

CREATE OR REPLACE FUNCTION zero_task_dependencies_project_lineage_insert_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'dependency project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS task_dependencies_project_lineage_insert ON task_dependencies;
CREATE TRIGGER task_dependencies_project_lineage_insert
    BEFORE INSERT ON task_dependencies
    FOR EACH ROW EXECUTE FUNCTION zero_task_dependencies_project_lineage_insert_fn();

CREATE OR REPLACE FUNCTION zero_task_dependencies_project_lineage_update_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'dependency project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS task_dependencies_project_lineage_update ON task_dependencies;
CREATE TRIGGER task_dependencies_project_lineage_update
    BEFORE UPDATE OF task_id, depends_on_task_id ON task_dependencies
    FOR EACH ROW EXECUTE FUNCTION zero_task_dependencies_project_lineage_update_fn();

CREATE OR REPLACE FUNCTION zero_command_runs_project_lineage_update_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'command run project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS command_runs_project_lineage_update ON command_runs;
CREATE TRIGGER command_runs_project_lineage_update
    BEFORE UPDATE OF worktree_id, task_id, project_id ON command_runs
    FOR EACH ROW EXECUTE FUNCTION zero_command_runs_project_lineage_update_fn();

CREATE OR REPLACE FUNCTION zero_task_artifacts_project_lineage_update_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'task artifact project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS task_artifacts_project_lineage_update ON task_artifacts;
CREATE TRIGGER task_artifacts_project_lineage_update
    BEFORE UPDATE OF worktree_id, task_id, command_run_id, project_id ON task_artifacts
    FOR EACH ROW EXECUTE FUNCTION zero_task_artifacts_project_lineage_update_fn();

CREATE OR REPLACE FUNCTION zero_task_artifacts_command_lineage_insert_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'task artifact command lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS task_artifacts_command_lineage_insert ON task_artifacts;
CREATE TRIGGER task_artifacts_command_lineage_insert
    BEFORE INSERT ON task_artifacts
    FOR EACH ROW EXECUTE FUNCTION zero_task_artifacts_command_lineage_insert_fn();

CREATE OR REPLACE FUNCTION zero_integration_reviews_project_lineage_insert_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'integration review project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS integration_reviews_project_lineage_insert ON integration_reviews;
CREATE TRIGGER integration_reviews_project_lineage_insert
    BEFORE INSERT ON integration_reviews
    FOR EACH ROW EXECUTE FUNCTION zero_integration_reviews_project_lineage_insert_fn();

CREATE OR REPLACE FUNCTION zero_integration_reviews_project_lineage_update_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'integration review project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS integration_reviews_project_lineage_update ON integration_reviews;
CREATE TRIGGER integration_reviews_project_lineage_update
    BEFORE UPDATE OF execution_id, project_id ON integration_reviews
    FOR EACH ROW EXECUTE FUNCTION zero_integration_reviews_project_lineage_update_fn();

CREATE OR REPLACE FUNCTION zero_merge_proposals_project_lineage_insert_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'merge proposal project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS merge_proposals_project_lineage_insert ON merge_proposals;
CREATE TRIGGER merge_proposals_project_lineage_insert
    BEFORE INSERT ON merge_proposals
    FOR EACH ROW EXECUTE FUNCTION zero_merge_proposals_project_lineage_insert_fn();

CREATE OR REPLACE FUNCTION zero_merge_proposals_project_lineage_update_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'merge proposal project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS merge_proposals_project_lineage_update ON merge_proposals;
CREATE TRIGGER merge_proposals_project_lineage_update
    BEFORE UPDATE OF integration_review_id, execution_id, project_id ON merge_proposals
    FOR EACH ROW EXECUTE FUNCTION zero_merge_proposals_project_lineage_update_fn();

CREATE OR REPLACE FUNCTION zero_integration_worktrees_project_lineage_insert_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'integration worktree project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS integration_worktrees_project_lineage_insert ON integration_worktrees;
CREATE TRIGGER integration_worktrees_project_lineage_insert
    BEFORE INSERT ON integration_worktrees
    FOR EACH ROW EXECUTE FUNCTION zero_integration_worktrees_project_lineage_insert_fn();

CREATE OR REPLACE FUNCTION zero_integration_worktrees_project_lineage_update_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'integration worktree project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS integration_worktrees_project_lineage_update ON integration_worktrees;
CREATE TRIGGER integration_worktrees_project_lineage_update
    BEFORE UPDATE OF execution_id, repository_id, project_id ON integration_worktrees
    FOR EACH ROW EXECUTE FUNCTION zero_integration_worktrees_project_lineage_update_fn();

CREATE OR REPLACE FUNCTION zero_integration_evidence_project_lineage_insert_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'integration evidence project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS integration_evidence_project_lineage_insert ON integration_evidence;
CREATE TRIGGER integration_evidence_project_lineage_insert
    BEFORE INSERT ON integration_evidence
    FOR EACH ROW EXECUTE FUNCTION zero_integration_evidence_project_lineage_insert_fn();

CREATE OR REPLACE FUNCTION zero_integration_evidence_project_lineage_update_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'integration evidence project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS integration_evidence_project_lineage_update ON integration_evidence;
CREATE TRIGGER integration_evidence_project_lineage_update
    BEFORE UPDATE OF proposal_id, execution_id, integration_worktree_id, project_id ON integration_evidence
    FOR EACH ROW EXECUTE FUNCTION zero_integration_evidence_project_lineage_update_fn();
