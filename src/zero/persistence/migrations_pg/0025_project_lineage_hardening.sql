-- GENERATED from 0025_project_lineage_hardening.sql by scripts/gen_pg_migrations.py.
-- PostgreSQL dialect translation of the canonical SQLite schema.
-- Do not edit directly; re-run the generator instead.

-- Project-lineage hardening for denormalized SQLite rows.
--
-- SQLite foreign keys prove that referenced IDs exist, but they do not prove
-- that the referenced rows belong to the same project.  These triggers make
-- project ownership a database boundary for every remaining project-scoped
-- relationship.  Both INSERT and UPDATE are covered because a valid row can
-- otherwise be mutated into a mixed-project row after creation.

-- Dynamic agent topology -------------------------------------------------






-- Plan and callback graph ------------------------------------------------








-- Execution/context graph -------------------------------------------------








-- Agent/worktree evidence graph ------------------------------------------



-- Interface bindings and event identity ----------------------------------




-- Integration review/evidence graph --------------------------------------






-- Result delivery boundary -----------------------------------------------


-- Project-scoped documents -----------------------------------------------

CREATE OR REPLACE FUNCTION zero_agent_types_project_lineage_insert_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'agent type project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS agent_types_project_lineage_insert ON agent_types;
CREATE TRIGGER agent_types_project_lineage_insert
    BEFORE INSERT ON agent_types
    FOR EACH ROW EXECUTE FUNCTION zero_agent_types_project_lineage_insert_fn();

CREATE OR REPLACE FUNCTION zero_agent_types_project_lineage_update_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'agent type project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS agent_types_project_lineage_update ON agent_types;
CREATE TRIGGER agent_types_project_lineage_update
    BEFORE UPDATE OF project_id, superseded_by ON agent_types
    FOR EACH ROW EXECUTE FUNCTION zero_agent_types_project_lineage_update_fn();

CREATE OR REPLACE FUNCTION zero_agent_instances_project_lineage_insert_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'agent instance project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS agent_instances_project_lineage_insert ON agent_instances;
CREATE TRIGGER agent_instances_project_lineage_insert
    BEFORE INSERT ON agent_instances
    FOR EACH ROW EXECUTE FUNCTION zero_agent_instances_project_lineage_insert_fn();

CREATE OR REPLACE FUNCTION zero_agent_instances_project_lineage_update_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'agent instance project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS agent_instances_project_lineage_update ON agent_instances;
CREATE TRIGGER agent_instances_project_lineage_update
    BEFORE UPDATE OF project_id, agent_type_id, task_id ON agent_instances
    FOR EACH ROW EXECUTE FUNCTION zero_agent_instances_project_lineage_update_fn();

CREATE OR REPLACE FUNCTION zero_knowledge_records_project_lineage_insert_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'knowledge record project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS knowledge_records_project_lineage_insert ON knowledge_records;
CREATE TRIGGER knowledge_records_project_lineage_insert
    BEFORE INSERT ON knowledge_records
    FOR EACH ROW EXECUTE FUNCTION zero_knowledge_records_project_lineage_insert_fn();

CREATE OR REPLACE FUNCTION zero_knowledge_records_project_lineage_update_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'knowledge record project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS knowledge_records_project_lineage_update ON knowledge_records;
CREATE TRIGGER knowledge_records_project_lineage_update
    BEFORE UPDATE OF project_id, agent_type_id, superseded_by, migrated_from ON knowledge_records
    FOR EACH ROW EXECUTE FUNCTION zero_knowledge_records_project_lineage_update_fn();

CREATE OR REPLACE FUNCTION zero_plan_revisions_project_lineage_insert_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'plan revision project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS plan_revisions_project_lineage_insert ON plan_revisions;
CREATE TRIGGER plan_revisions_project_lineage_insert
    BEFORE INSERT ON plan_revisions
    FOR EACH ROW EXECUTE FUNCTION zero_plan_revisions_project_lineage_insert_fn();

CREATE OR REPLACE FUNCTION zero_plan_revisions_project_lineage_update_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'plan revision project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS plan_revisions_project_lineage_update ON plan_revisions;
CREATE TRIGGER plan_revisions_project_lineage_update
    BEFORE UPDATE OF project_id, plan_id ON plan_revisions
    FOR EACH ROW EXECUTE FUNCTION zero_plan_revisions_project_lineage_update_fn();

CREATE OR REPLACE FUNCTION zero_plan_approvals_project_lineage_insert_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'plan approval project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS plan_approvals_project_lineage_insert ON plan_approvals;
CREATE TRIGGER plan_approvals_project_lineage_insert
    BEFORE INSERT ON plan_approvals
    FOR EACH ROW EXECUTE FUNCTION zero_plan_approvals_project_lineage_insert_fn();

CREATE OR REPLACE FUNCTION zero_plan_approvals_project_lineage_update_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'plan approval project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS plan_approvals_project_lineage_update ON plan_approvals;
CREATE TRIGGER plan_approvals_project_lineage_update
    BEFORE UPDATE OF project_id, plan_id, revision_id ON plan_approvals
    FOR EACH ROW EXECUTE FUNCTION zero_plan_approvals_project_lineage_update_fn();

CREATE OR REPLACE FUNCTION zero_plan_handoffs_project_lineage_insert_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'plan handoff project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS plan_handoffs_project_lineage_insert ON plan_handoffs;
CREATE TRIGGER plan_handoffs_project_lineage_insert
    BEFORE INSERT ON plan_handoffs
    FOR EACH ROW EXECUTE FUNCTION zero_plan_handoffs_project_lineage_insert_fn();

CREATE OR REPLACE FUNCTION zero_plan_handoffs_project_lineage_update_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'plan handoff project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS plan_handoffs_project_lineage_update ON plan_handoffs;
CREATE TRIGGER plan_handoffs_project_lineage_update
    BEFORE UPDATE OF project_id, plan_id, revision_id, execution_id ON plan_handoffs
    FOR EACH ROW EXECUTE FUNCTION zero_plan_handoffs_project_lineage_update_fn();

CREATE OR REPLACE FUNCTION zero_callback_tokens_project_lineage_insert_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'callback token project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS callback_tokens_project_lineage_insert ON callback_tokens;
CREATE TRIGGER callback_tokens_project_lineage_insert
    BEFORE INSERT ON callback_tokens
    FOR EACH ROW EXECUTE FUNCTION zero_callback_tokens_project_lineage_insert_fn();

CREATE OR REPLACE FUNCTION zero_callback_tokens_project_lineage_update_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'callback token project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS callback_tokens_project_lineage_update ON callback_tokens;
CREATE TRIGGER callback_tokens_project_lineage_update
    BEFORE UPDATE OF project_id, plan_id ON callback_tokens
    FOR EACH ROW EXECUTE FUNCTION zero_callback_tokens_project_lineage_update_fn();

CREATE OR REPLACE FUNCTION zero_execution_snapshots_project_lineage_insert_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'execution snapshot project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS execution_snapshots_project_lineage_insert ON execution_snapshots;
CREATE TRIGGER execution_snapshots_project_lineage_insert
    BEFORE INSERT ON execution_snapshots
    FOR EACH ROW EXECUTE FUNCTION zero_execution_snapshots_project_lineage_insert_fn();

CREATE OR REPLACE FUNCTION zero_execution_snapshots_project_lineage_update_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'execution snapshot project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS execution_snapshots_project_lineage_update ON execution_snapshots;
CREATE TRIGGER execution_snapshots_project_lineage_update
    BEFORE UPDATE OF project_id, execution_id ON execution_snapshots
    FOR EACH ROW EXECUTE FUNCTION zero_execution_snapshots_project_lineage_update_fn();

CREATE OR REPLACE FUNCTION zero_context_versions_project_lineage_insert_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'context version project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS context_versions_project_lineage_insert ON context_versions;
CREATE TRIGGER context_versions_project_lineage_insert
    BEFORE INSERT ON context_versions
    FOR EACH ROW EXECUTE FUNCTION zero_context_versions_project_lineage_insert_fn();

CREATE OR REPLACE FUNCTION zero_context_versions_project_lineage_update_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'context version project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS context_versions_project_lineage_update ON context_versions;
CREATE TRIGGER context_versions_project_lineage_update
    BEFORE UPDATE OF project_id, execution_id, transcript_artifact_id ON context_versions
    FOR EACH ROW EXECUTE FUNCTION zero_context_versions_project_lineage_update_fn();

CREATE OR REPLACE FUNCTION zero_context_injection_ledger_project_lineage_insert_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'context injection project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS context_injection_ledger_project_lineage_insert ON context_injection_ledger;
CREATE TRIGGER context_injection_ledger_project_lineage_insert
    BEFORE INSERT ON context_injection_ledger
    FOR EACH ROW EXECUTE FUNCTION zero_context_injection_ledger_project_lineage_insert_fn();

CREATE OR REPLACE FUNCTION zero_context_injection_ledger_project_lineage_update_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'context injection project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS context_injection_ledger_project_lineage_update ON context_injection_ledger;
CREATE TRIGGER context_injection_ledger_project_lineage_update
    BEFORE UPDATE OF project_id, execution_id ON context_injection_ledger
    FOR EACH ROW EXECUTE FUNCTION zero_context_injection_ledger_project_lineage_update_fn();

CREATE OR REPLACE FUNCTION zero_compaction_records_project_lineage_insert_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'compaction record project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS compaction_records_project_lineage_insert ON compaction_records;
CREATE TRIGGER compaction_records_project_lineage_insert
    BEFORE INSERT ON compaction_records
    FOR EACH ROW EXECUTE FUNCTION zero_compaction_records_project_lineage_insert_fn();

CREATE OR REPLACE FUNCTION zero_compaction_records_project_lineage_update_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'compaction record project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS compaction_records_project_lineage_update ON compaction_records;
CREATE TRIGGER compaction_records_project_lineage_update
    BEFORE UPDATE OF project_id, execution_id, memory_delta_artifact_id, transcript_artifact_id ON compaction_records
    FOR EACH ROW EXECUTE FUNCTION zero_compaction_records_project_lineage_update_fn();

CREATE OR REPLACE FUNCTION zero_command_runs_project_lineage_insert_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'command run project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS command_runs_project_lineage_insert ON command_runs;
CREATE TRIGGER command_runs_project_lineage_insert
    BEFORE INSERT ON command_runs
    FOR EACH ROW EXECUTE FUNCTION zero_command_runs_project_lineage_insert_fn();

CREATE OR REPLACE FUNCTION zero_tasks_agent_type_project_lineage_insert_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'task agent type project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS tasks_agent_type_project_lineage_insert ON tasks;
CREATE TRIGGER tasks_agent_type_project_lineage_insert
    BEFORE INSERT ON tasks
    FOR EACH ROW EXECUTE FUNCTION zero_tasks_agent_type_project_lineage_insert_fn();

CREATE OR REPLACE FUNCTION zero_tasks_agent_type_project_lineage_update_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'task agent type project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS tasks_agent_type_project_lineage_update ON tasks;
CREATE TRIGGER tasks_agent_type_project_lineage_update
    BEFORE UPDATE OF project_id, agent_type_id ON tasks
    FOR EACH ROW EXECUTE FUNCTION zero_tasks_agent_type_project_lineage_update_fn();

CREATE OR REPLACE FUNCTION zero_interface_event_claims_binding_lineage_insert_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'interface event claim binding lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS interface_event_claims_binding_lineage_insert ON interface_event_claims;
CREATE TRIGGER interface_event_claims_binding_lineage_insert
    BEFORE INSERT ON interface_event_claims
    FOR EACH ROW EXECUTE FUNCTION zero_interface_event_claims_binding_lineage_insert_fn();

CREATE OR REPLACE FUNCTION zero_interface_event_claims_binding_lineage_update_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'interface event claim binding lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS interface_event_claims_binding_lineage_update ON interface_event_claims;
CREATE TRIGGER interface_event_claims_binding_lineage_update
    BEFORE UPDATE OF platform, binding_scope, binding_id ON interface_event_claims
    FOR EACH ROW EXECUTE FUNCTION zero_interface_event_claims_binding_lineage_update_fn();

CREATE OR REPLACE FUNCTION zero_interface_event_log_project_lineage_insert_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'interface event project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS interface_event_log_project_lineage_insert ON interface_event_log;
CREATE TRIGGER interface_event_log_project_lineage_insert
    BEFORE INSERT ON interface_event_log
    FOR EACH ROW EXECUTE FUNCTION zero_interface_event_log_project_lineage_insert_fn();

CREATE OR REPLACE FUNCTION zero_interface_event_log_project_lineage_update_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'interface event project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS interface_event_log_project_lineage_update ON interface_event_log;
CREATE TRIGGER interface_event_log_project_lineage_update
    BEFORE UPDATE OF project_id, platform, binding_scope, binding_id ON interface_event_log
    FOR EACH ROW EXECUTE FUNCTION zero_interface_event_log_project_lineage_update_fn();

CREATE OR REPLACE FUNCTION zero_integration_reviews_worktree_lineage_insert_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'integration review worktree lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS integration_reviews_worktree_lineage_insert ON integration_reviews;
CREATE TRIGGER integration_reviews_worktree_lineage_insert
    BEFORE INSERT ON integration_reviews
    FOR EACH ROW EXECUTE FUNCTION zero_integration_reviews_worktree_lineage_insert_fn();

CREATE OR REPLACE FUNCTION zero_integration_reviews_worktree_lineage_update_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'integration review worktree lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS integration_reviews_worktree_lineage_update ON integration_reviews;
CREATE TRIGGER integration_reviews_worktree_lineage_update
    BEFORE UPDATE OF project_id, execution_id, integration_worktree_id ON integration_reviews
    FOR EACH ROW EXECUTE FUNCTION zero_integration_reviews_worktree_lineage_update_fn();

CREATE OR REPLACE FUNCTION zero_integration_review_evidence_project_lineage_insert_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'integration review evidence project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS integration_review_evidence_project_lineage_insert ON integration_review_evidence;
CREATE TRIGGER integration_review_evidence_project_lineage_insert
    BEFORE INSERT ON integration_review_evidence
    FOR EACH ROW EXECUTE FUNCTION zero_integration_review_evidence_project_lineage_insert_fn();

CREATE OR REPLACE FUNCTION zero_integration_review_evidence_project_lineage_update_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'integration review evidence project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS integration_review_evidence_project_lineage_update ON integration_review_evidence;
CREATE TRIGGER integration_review_evidence_project_lineage_update
    BEFORE UPDATE OF project_id, review_id, execution_id, integration_worktree_id ON integration_review_evidence
    FOR EACH ROW EXECUTE FUNCTION zero_integration_review_evidence_project_lineage_update_fn();

CREATE OR REPLACE FUNCTION zero_merge_proposals_worktree_lineage_insert_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'merge proposal worktree lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS merge_proposals_worktree_lineage_insert ON merge_proposals;
CREATE TRIGGER merge_proposals_worktree_lineage_insert
    BEFORE INSERT ON merge_proposals
    FOR EACH ROW EXECUTE FUNCTION zero_merge_proposals_worktree_lineage_insert_fn();

CREATE OR REPLACE FUNCTION zero_merge_proposals_worktree_lineage_update_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'merge proposal worktree lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS merge_proposals_worktree_lineage_update ON merge_proposals;
CREATE TRIGGER merge_proposals_worktree_lineage_update
    BEFORE UPDATE OF project_id, execution_id, integration_worktree_id ON merge_proposals
    FOR EACH ROW EXECUTE FUNCTION zero_merge_proposals_worktree_lineage_update_fn();

CREATE OR REPLACE FUNCTION zero_result_deliveries_project_lineage_insert_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'result delivery project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS result_deliveries_project_lineage_insert ON result_deliveries;
CREATE TRIGGER result_deliveries_project_lineage_insert
    BEFORE INSERT ON result_deliveries
    FOR EACH ROW EXECUTE FUNCTION zero_result_deliveries_project_lineage_insert_fn();

CREATE OR REPLACE FUNCTION zero_result_deliveries_project_lineage_update_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'result delivery project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS result_deliveries_project_lineage_update ON result_deliveries;
CREATE TRIGGER result_deliveries_project_lineage_update
    BEFORE UPDATE OF project_id, execution_id, binding_id ON result_deliveries
    FOR EACH ROW EXECUTE FUNCTION zero_result_deliveries_project_lineage_update_fn();

CREATE OR REPLACE FUNCTION zero_rag_documents_project_lineage_insert_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'RAG document project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS rag_documents_project_lineage_insert ON rag_documents;
CREATE TRIGGER rag_documents_project_lineage_insert
    BEFORE INSERT ON rag_documents
    FOR EACH ROW EXECUTE FUNCTION zero_rag_documents_project_lineage_insert_fn();

CREATE OR REPLACE FUNCTION zero_rag_documents_project_lineage_update_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'RAG document project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS rag_documents_project_lineage_update ON rag_documents;
CREATE TRIGGER rag_documents_project_lineage_update
    BEFORE UPDATE OF project_id, superseded_by ON rag_documents
    FOR EACH ROW EXECUTE FUNCTION zero_rag_documents_project_lineage_update_fn();
