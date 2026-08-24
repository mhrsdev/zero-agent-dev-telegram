-- Project-lineage hardening for denormalized SQLite rows.
--
-- SQLite foreign keys prove that referenced IDs exist, but they do not prove
-- that the referenced rows belong to the same project.  These triggers make
-- project ownership a database boundary for every remaining project-scoped
-- relationship.  Both INSERT and UPDATE are covered because a valid row can
-- otherwise be mutated into a mixed-project row after creation.

-- Dynamic agent topology -------------------------------------------------
CREATE TRIGGER IF NOT EXISTS agent_types_project_lineage_insert
BEFORE INSERT ON agent_types
WHEN NEW.superseded_by IS NOT NULL
 AND NOT EXISTS (
    SELECT 1 FROM agent_types successor
    WHERE successor.id = NEW.superseded_by
      AND successor.project_id = NEW.project_id
 )
BEGIN
    SELECT RAISE(ABORT, 'agent type project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS agent_types_project_lineage_update
BEFORE UPDATE OF project_id, superseded_by ON agent_types
WHEN NEW.superseded_by IS NOT NULL
 AND NOT EXISTS (
    SELECT 1 FROM agent_types successor
    WHERE successor.id = NEW.superseded_by
      AND successor.project_id = NEW.project_id
 )
BEGIN
    SELECT RAISE(ABORT, 'agent type project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS agent_instances_project_lineage_insert
BEFORE INSERT ON agent_instances
WHEN NOT EXISTS (
    SELECT 1 FROM agent_types
    WHERE agent_types.id = NEW.agent_type_id
      AND agent_types.project_id = NEW.project_id
 )
OR (
    NEW.task_id IS NOT NULL
    AND NOT EXISTS (
        SELECT 1 FROM tasks
        WHERE tasks.id = NEW.task_id
          AND tasks.project_id = NEW.project_id
    )
)
BEGIN
    SELECT RAISE(ABORT, 'agent instance project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS agent_instances_project_lineage_update
BEFORE UPDATE OF project_id, agent_type_id, task_id ON agent_instances
WHEN NOT EXISTS (
    SELECT 1 FROM agent_types
    WHERE agent_types.id = NEW.agent_type_id
      AND agent_types.project_id = NEW.project_id
 )
OR (
    NEW.task_id IS NOT NULL
    AND NOT EXISTS (
        SELECT 1 FROM tasks
        WHERE tasks.id = NEW.task_id
          AND tasks.project_id = NEW.project_id
    )
)
BEGIN
    SELECT RAISE(ABORT, 'agent instance project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS knowledge_records_project_lineage_insert
BEFORE INSERT ON knowledge_records
WHEN (
    NEW.agent_type_id IS NOT NULL
    AND NOT EXISTS (
        SELECT 1 FROM agent_types
        WHERE agent_types.id = NEW.agent_type_id
          AND agent_types.project_id = NEW.project_id
    )
)
OR (
    NEW.superseded_by IS NOT NULL
    AND NOT EXISTS (
        SELECT 1 FROM knowledge_records successor
        WHERE successor.id = NEW.superseded_by
          AND successor.project_id = NEW.project_id
    )
)
OR (
    NEW.migrated_from IS NOT NULL
    AND NOT EXISTS (
        SELECT 1 FROM knowledge_records source
        WHERE source.id = NEW.migrated_from
          AND source.project_id = NEW.project_id
    )
)
BEGIN
    SELECT RAISE(ABORT, 'knowledge record project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS knowledge_records_project_lineage_update
BEFORE UPDATE OF project_id, agent_type_id, superseded_by, migrated_from ON knowledge_records
WHEN (
    NEW.agent_type_id IS NOT NULL
    AND NOT EXISTS (
        SELECT 1 FROM agent_types
        WHERE agent_types.id = NEW.agent_type_id
          AND agent_types.project_id = NEW.project_id
    )
)
OR (
    NEW.superseded_by IS NOT NULL
    AND NOT EXISTS (
        SELECT 1 FROM knowledge_records successor
        WHERE successor.id = NEW.superseded_by
          AND successor.project_id = NEW.project_id
    )
)
OR (
    NEW.migrated_from IS NOT NULL
    AND NOT EXISTS (
        SELECT 1 FROM knowledge_records source
        WHERE source.id = NEW.migrated_from
          AND source.project_id = NEW.project_id
    )
)
BEGIN
    SELECT RAISE(ABORT, 'knowledge record project lineage mismatch');
END;

-- Plan and callback graph ------------------------------------------------
CREATE TRIGGER IF NOT EXISTS plan_revisions_project_lineage_insert
BEFORE INSERT ON plan_revisions
WHEN NOT EXISTS (
    SELECT 1 FROM plans
    WHERE plans.id = NEW.plan_id
      AND plans.project_id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'plan revision project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS plan_revisions_project_lineage_update
BEFORE UPDATE OF project_id, plan_id ON plan_revisions
WHEN NOT EXISTS (
    SELECT 1 FROM plans
    WHERE plans.id = NEW.plan_id
      AND plans.project_id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'plan revision project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS plan_approvals_project_lineage_insert
BEFORE INSERT ON plan_approvals
WHEN NOT EXISTS (
    SELECT 1
    FROM plans
    JOIN plan_revisions ON plan_revisions.id = NEW.revision_id
    WHERE plans.id = NEW.plan_id
      AND plans.project_id = NEW.project_id
      AND plan_revisions.plan_id = plans.id
      AND plan_revisions.project_id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'plan approval project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS plan_approvals_project_lineage_update
BEFORE UPDATE OF project_id, plan_id, revision_id ON plan_approvals
WHEN NOT EXISTS (
    SELECT 1
    FROM plans
    JOIN plan_revisions ON plan_revisions.id = NEW.revision_id
    WHERE plans.id = NEW.plan_id
      AND plans.project_id = NEW.project_id
      AND plan_revisions.plan_id = plans.id
      AND plan_revisions.project_id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'plan approval project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS plan_handoffs_project_lineage_insert
BEFORE INSERT ON plan_handoffs
WHEN NOT EXISTS (
    SELECT 1
    FROM plans
    JOIN plan_revisions ON plan_revisions.id = NEW.revision_id
    WHERE plans.id = NEW.plan_id
      AND plans.project_id = NEW.project_id
      AND plan_revisions.plan_id = plans.id
      AND plan_revisions.project_id = NEW.project_id
)
OR (
    NEW.execution_id IS NOT NULL
    AND NOT EXISTS (
        SELECT 1 FROM executions
        WHERE executions.id = NEW.execution_id
          AND executions.project_id = NEW.project_id
    )
)
BEGIN
    SELECT RAISE(ABORT, 'plan handoff project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS plan_handoffs_project_lineage_update
BEFORE UPDATE OF project_id, plan_id, revision_id, execution_id ON plan_handoffs
WHEN NOT EXISTS (
    SELECT 1
    FROM plans
    JOIN plan_revisions ON plan_revisions.id = NEW.revision_id
    WHERE plans.id = NEW.plan_id
      AND plans.project_id = NEW.project_id
      AND plan_revisions.plan_id = plans.id
      AND plan_revisions.project_id = NEW.project_id
)
OR (
    NEW.execution_id IS NOT NULL
    AND NOT EXISTS (
        SELECT 1 FROM executions
        WHERE executions.id = NEW.execution_id
          AND executions.project_id = NEW.project_id
    )
)
BEGIN
    SELECT RAISE(ABORT, 'plan handoff project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS callback_tokens_project_lineage_insert
BEFORE INSERT ON callback_tokens
WHEN NOT EXISTS (
    SELECT 1 FROM plans
    WHERE plans.id = NEW.plan_id
      AND plans.project_id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'callback token project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS callback_tokens_project_lineage_update
BEFORE UPDATE OF project_id, plan_id ON callback_tokens
WHEN NOT EXISTS (
    SELECT 1 FROM plans
    WHERE plans.id = NEW.plan_id
      AND plans.project_id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'callback token project lineage mismatch');
END;

-- Execution/context graph -------------------------------------------------
CREATE TRIGGER IF NOT EXISTS execution_snapshots_project_lineage_insert
BEFORE INSERT ON execution_snapshots
WHEN NOT EXISTS (
    SELECT 1 FROM executions
    WHERE executions.id = NEW.execution_id
      AND executions.project_id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'execution snapshot project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS execution_snapshots_project_lineage_update
BEFORE UPDATE OF project_id, execution_id ON execution_snapshots
WHEN NOT EXISTS (
    SELECT 1 FROM executions
    WHERE executions.id = NEW.execution_id
      AND executions.project_id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'execution snapshot project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS context_versions_project_lineage_insert
BEFORE INSERT ON context_versions
WHEN NOT EXISTS (
    SELECT 1 FROM executions
    WHERE executions.id = NEW.execution_id
      AND executions.project_id = NEW.project_id
)
OR (
    NEW.transcript_artifact_id IS NOT NULL
    AND NOT EXISTS (
        SELECT 1 FROM artifacts
        WHERE artifacts.id = NEW.transcript_artifact_id
          AND artifacts.project_id = NEW.project_id
    )
)
BEGIN
    SELECT RAISE(ABORT, 'context version project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS context_versions_project_lineage_update
BEFORE UPDATE OF project_id, execution_id, transcript_artifact_id ON context_versions
WHEN NOT EXISTS (
    SELECT 1 FROM executions
    WHERE executions.id = NEW.execution_id
      AND executions.project_id = NEW.project_id
)
OR (
    NEW.transcript_artifact_id IS NOT NULL
    AND NOT EXISTS (
        SELECT 1 FROM artifacts
        WHERE artifacts.id = NEW.transcript_artifact_id
          AND artifacts.project_id = NEW.project_id
    )
)
BEGIN
    SELECT RAISE(ABORT, 'context version project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS context_injection_ledger_project_lineage_insert
BEFORE INSERT ON context_injection_ledger
WHEN NOT EXISTS (
    SELECT 1 FROM executions
    WHERE executions.id = NEW.execution_id
      AND executions.project_id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'context injection project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS context_injection_ledger_project_lineage_update
BEFORE UPDATE OF project_id, execution_id ON context_injection_ledger
WHEN NOT EXISTS (
    SELECT 1 FROM executions
    WHERE executions.id = NEW.execution_id
      AND executions.project_id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'context injection project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS compaction_records_project_lineage_insert
BEFORE INSERT ON compaction_records
WHEN NOT EXISTS (
    SELECT 1 FROM executions
    WHERE executions.id = NEW.execution_id
      AND executions.project_id = NEW.project_id
)
OR (
    NEW.memory_delta_artifact_id IS NOT NULL
    AND NOT EXISTS (
        SELECT 1 FROM artifacts
        WHERE artifacts.id = NEW.memory_delta_artifact_id
          AND artifacts.project_id = NEW.project_id
    )
)
OR (
    NEW.transcript_artifact_id IS NOT NULL
    AND NOT EXISTS (
        SELECT 1 FROM artifacts
        WHERE artifacts.id = NEW.transcript_artifact_id
          AND artifacts.project_id = NEW.project_id
    )
)
BEGIN
    SELECT RAISE(ABORT, 'compaction record project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS compaction_records_project_lineage_update
BEFORE UPDATE OF project_id, execution_id, memory_delta_artifact_id, transcript_artifact_id ON compaction_records
WHEN NOT EXISTS (
    SELECT 1 FROM executions
    WHERE executions.id = NEW.execution_id
      AND executions.project_id = NEW.project_id
)
OR (
    NEW.memory_delta_artifact_id IS NOT NULL
    AND NOT EXISTS (
        SELECT 1 FROM artifacts
        WHERE artifacts.id = NEW.memory_delta_artifact_id
          AND artifacts.project_id = NEW.project_id
    )
)
OR (
    NEW.transcript_artifact_id IS NOT NULL
    AND NOT EXISTS (
        SELECT 1 FROM artifacts
        WHERE artifacts.id = NEW.transcript_artifact_id
          AND artifacts.project_id = NEW.project_id
    )
)
BEGIN
    SELECT RAISE(ABORT, 'compaction record project lineage mismatch');
END;

-- Agent/worktree evidence graph ------------------------------------------
CREATE TRIGGER IF NOT EXISTS command_runs_project_lineage_insert
BEFORE INSERT ON command_runs
WHEN NOT EXISTS (
    SELECT 1
    FROM worktrees
    JOIN tasks ON tasks.id = NEW.task_id
    WHERE worktrees.id = NEW.worktree_id
      AND worktrees.project_id = NEW.project_id
      AND worktrees.task_id = NEW.task_id
      AND tasks.project_id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'command run project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS tasks_agent_type_project_lineage_insert
BEFORE INSERT ON tasks
WHEN NEW.agent_type_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1 FROM agent_types
    WHERE agent_types.id = NEW.agent_type_id
      AND agent_types.project_id = NEW.project_id
 )
BEGIN
    SELECT RAISE(ABORT, 'task agent type project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS tasks_agent_type_project_lineage_update
BEFORE UPDATE OF project_id, agent_type_id ON tasks
WHEN NEW.agent_type_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1 FROM agent_types
    WHERE agent_types.id = NEW.agent_type_id
      AND agent_types.project_id = NEW.project_id
 )
BEGIN
    SELECT RAISE(ABORT, 'task agent type project lineage mismatch');
END;

-- Interface bindings and event identity ----------------------------------
CREATE TRIGGER IF NOT EXISTS interface_event_claims_binding_lineage_insert
BEFORE INSERT ON interface_event_claims
WHEN NEW.binding_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1 FROM interface_bindings
    WHERE interface_bindings.id = NEW.binding_id
      AND interface_bindings.platform = NEW.platform
      AND NEW.binding_scope = interface_bindings.id
 )
BEGIN
    SELECT RAISE(ABORT, 'interface event claim binding lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS interface_event_claims_binding_lineage_update
BEFORE UPDATE OF platform, binding_scope, binding_id ON interface_event_claims
WHEN NEW.binding_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1 FROM interface_bindings
    WHERE interface_bindings.id = NEW.binding_id
      AND interface_bindings.platform = NEW.platform
      AND NEW.binding_scope = interface_bindings.id
 )
BEGIN
    SELECT RAISE(ABORT, 'interface event claim binding lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS interface_event_log_project_lineage_insert
BEFORE INSERT ON interface_event_log
WHEN NEW.binding_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1 FROM interface_bindings
    WHERE interface_bindings.id = NEW.binding_id
      AND interface_bindings.project_id = NEW.project_id
      AND interface_bindings.platform = NEW.platform
      AND NEW.binding_scope = interface_bindings.id
 )
BEGIN
    SELECT RAISE(ABORT, 'interface event project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS interface_event_log_project_lineage_update
BEFORE UPDATE OF project_id, platform, binding_scope, binding_id ON interface_event_log
WHEN NEW.binding_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1 FROM interface_bindings
    WHERE interface_bindings.id = NEW.binding_id
      AND interface_bindings.project_id = NEW.project_id
      AND interface_bindings.platform = NEW.platform
      AND NEW.binding_scope = interface_bindings.id
 )
BEGIN
    SELECT RAISE(ABORT, 'interface event project lineage mismatch');
END;

-- Integration review/evidence graph --------------------------------------
CREATE TRIGGER IF NOT EXISTS integration_reviews_worktree_lineage_insert
BEFORE INSERT ON integration_reviews
WHEN NEW.integration_worktree_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1 FROM integration_worktrees
    WHERE integration_worktrees.id = NEW.integration_worktree_id
      AND integration_worktrees.project_id = NEW.project_id
      AND integration_worktrees.execution_id = NEW.execution_id
 )
BEGIN
    SELECT RAISE(ABORT, 'integration review worktree lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS integration_reviews_worktree_lineage_update
BEFORE UPDATE OF project_id, execution_id, integration_worktree_id ON integration_reviews
WHEN NEW.integration_worktree_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1 FROM integration_worktrees
    WHERE integration_worktrees.id = NEW.integration_worktree_id
      AND integration_worktrees.project_id = NEW.project_id
      AND integration_worktrees.execution_id = NEW.execution_id
 )
BEGIN
    SELECT RAISE(ABORT, 'integration review worktree lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS integration_review_evidence_project_lineage_insert
BEFORE INSERT ON integration_review_evidence
WHEN NOT EXISTS (
    SELECT 1
    FROM integration_reviews review
    JOIN executions execution ON execution.id = NEW.execution_id
    JOIN integration_worktrees worktree ON worktree.id = NEW.integration_worktree_id
    WHERE review.id = NEW.review_id
      AND review.project_id = NEW.project_id
      AND review.execution_id = NEW.execution_id
      AND execution.project_id = NEW.project_id
      AND worktree.project_id = NEW.project_id
      AND worktree.execution_id = NEW.execution_id
)
BEGIN
    SELECT RAISE(ABORT, 'integration review evidence project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS integration_review_evidence_project_lineage_update
BEFORE UPDATE OF project_id, review_id, execution_id, integration_worktree_id ON integration_review_evidence
WHEN NOT EXISTS (
    SELECT 1
    FROM integration_reviews review
    JOIN executions execution ON execution.id = NEW.execution_id
    JOIN integration_worktrees worktree ON worktree.id = NEW.integration_worktree_id
    WHERE review.id = NEW.review_id
      AND review.project_id = NEW.project_id
      AND review.execution_id = NEW.execution_id
      AND execution.project_id = NEW.project_id
      AND worktree.project_id = NEW.project_id
      AND worktree.execution_id = NEW.execution_id
)
BEGIN
    SELECT RAISE(ABORT, 'integration review evidence project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS merge_proposals_worktree_lineage_insert
BEFORE INSERT ON merge_proposals
WHEN NEW.integration_worktree_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1 FROM integration_worktrees
    WHERE integration_worktrees.id = NEW.integration_worktree_id
      AND integration_worktrees.project_id = NEW.project_id
      AND integration_worktrees.execution_id = NEW.execution_id
 )
BEGIN
    SELECT RAISE(ABORT, 'merge proposal worktree lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS merge_proposals_worktree_lineage_update
BEFORE UPDATE OF project_id, execution_id, integration_worktree_id ON merge_proposals
WHEN NEW.integration_worktree_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1 FROM integration_worktrees
    WHERE integration_worktrees.id = NEW.integration_worktree_id
      AND integration_worktrees.project_id = NEW.project_id
      AND integration_worktrees.execution_id = NEW.execution_id
 )
BEGIN
    SELECT RAISE(ABORT, 'merge proposal worktree lineage mismatch');
END;

-- Result delivery boundary -----------------------------------------------
CREATE TRIGGER IF NOT EXISTS result_deliveries_project_lineage_insert
BEFORE INSERT ON result_deliveries
WHEN NOT EXISTS (
    SELECT 1 FROM executions
    WHERE executions.id = NEW.execution_id
      AND executions.project_id = NEW.project_id
)
OR NOT EXISTS (
    SELECT 1 FROM interface_bindings
    WHERE interface_bindings.id = NEW.binding_id
      AND interface_bindings.project_id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'result delivery project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS result_deliveries_project_lineage_update
BEFORE UPDATE OF project_id, execution_id, binding_id ON result_deliveries
WHEN NOT EXISTS (
    SELECT 1 FROM executions
    WHERE executions.id = NEW.execution_id
      AND executions.project_id = NEW.project_id
)
OR NOT EXISTS (
    SELECT 1 FROM interface_bindings
    WHERE interface_bindings.id = NEW.binding_id
      AND interface_bindings.project_id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'result delivery project lineage mismatch');
END;

-- Project-scoped documents -----------------------------------------------
CREATE TRIGGER IF NOT EXISTS rag_documents_project_lineage_insert
BEFORE INSERT ON rag_documents
WHEN NEW.superseded_by IS NOT NULL
 AND NOT EXISTS (
    SELECT 1 FROM rag_documents successor
    WHERE successor.id = NEW.superseded_by
      AND successor.project_id = NEW.project_id
 )
BEGIN
    SELECT RAISE(ABORT, 'RAG document project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS rag_documents_project_lineage_update
BEFORE UPDATE OF project_id, superseded_by ON rag_documents
WHEN NEW.superseded_by IS NOT NULL
 AND NOT EXISTS (
    SELECT 1 FROM rag_documents successor
    WHERE successor.id = NEW.superseded_by
      AND successor.project_id = NEW.project_id
 )
BEGIN
    SELECT RAISE(ABORT, 'RAG document project lineage mismatch');
END;
