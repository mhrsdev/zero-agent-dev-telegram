-- Cross-table lineage defense for denormalized project IDs.
-- SQLite foreign keys validate existence, while these triggers validate
-- that every referenced graph node belongs to the same project/lineage.

CREATE TRIGGER IF NOT EXISTS executions_project_lineage_insert
BEFORE INSERT ON executions
WHEN NOT EXISTS (
    SELECT 1
    FROM plans p
    JOIN plan_revisions r ON r.id = NEW.plan_revision_id
    JOIN plan_handoffs h ON h.id = NEW.plan_handoff_id
    WHERE p.id = NEW.plan_id
      AND p.project_id = NEW.project_id
      AND r.plan_id = p.id
      AND r.project_id = NEW.project_id
      AND h.revision_id = r.id
      AND h.plan_id = p.id
      AND h.project_id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'execution project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS executions_project_lineage_update
BEFORE UPDATE OF plan_id, plan_revision_id, plan_handoff_id, project_id ON executions
WHEN NOT EXISTS (
    SELECT 1
    FROM plans p
    JOIN plan_revisions r ON r.id = NEW.plan_revision_id
    JOIN plan_handoffs h ON h.id = NEW.plan_handoff_id
    WHERE p.id = NEW.plan_id
      AND p.project_id = NEW.project_id
      AND r.plan_id = p.id
      AND r.project_id = NEW.project_id
      AND h.revision_id = r.id
      AND h.plan_id = p.id
      AND h.project_id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'execution project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS task_dependencies_project_lineage_insert
BEFORE INSERT ON task_dependencies
WHEN NOT EXISTS (
    SELECT 1 FROM tasks a JOIN tasks b
      ON b.id = NEW.depends_on_task_id
    WHERE a.id = NEW.task_id
      AND a.project_id = b.project_id
      AND a.execution_id = b.execution_id
)
BEGIN
    SELECT RAISE(ABORT, 'dependency project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS task_dependencies_project_lineage_update
BEFORE UPDATE OF task_id, depends_on_task_id ON task_dependencies
WHEN NOT EXISTS (
    SELECT 1 FROM tasks a JOIN tasks b
      ON b.id = NEW.depends_on_task_id
    WHERE a.id = NEW.task_id
      AND a.project_id = b.project_id
      AND a.execution_id = b.execution_id
)
BEGIN
    SELECT RAISE(ABORT, 'dependency project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS command_runs_project_lineage_update
BEFORE UPDATE OF worktree_id, task_id, project_id ON command_runs
WHEN NOT EXISTS (
    SELECT 1 FROM worktrees w JOIN tasks t ON t.id = NEW.task_id
    WHERE w.id = NEW.worktree_id
      AND w.project_id = NEW.project_id
      AND w.task_id = NEW.task_id
      AND t.project_id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'command run project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS task_artifacts_project_lineage_update
BEFORE UPDATE OF worktree_id, task_id, command_run_id, project_id ON task_artifacts
WHEN NOT EXISTS (
    SELECT 1 FROM worktrees w JOIN tasks t ON t.id = NEW.task_id
    WHERE w.id = NEW.worktree_id
      AND w.project_id = NEW.project_id
      AND w.task_id = NEW.task_id
      AND t.project_id = NEW.project_id
)
OR (
    NEW.command_run_id IS NOT NULL
    AND NOT EXISTS (
        SELECT 1 FROM command_runs c
        WHERE c.id = NEW.command_run_id
          AND c.project_id = NEW.project_id
          AND c.worktree_id = NEW.worktree_id
          AND c.task_id = NEW.task_id
    )
)
BEGIN
    SELECT RAISE(ABORT, 'task artifact project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS task_artifacts_command_lineage_insert
BEFORE INSERT ON task_artifacts
WHEN NEW.command_run_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1 FROM command_runs c
    WHERE c.id = NEW.command_run_id
      AND c.project_id = NEW.project_id
      AND c.worktree_id = NEW.worktree_id
      AND c.task_id = NEW.task_id
 )
BEGIN
    SELECT RAISE(ABORT, 'task artifact command lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS integration_reviews_project_lineage_insert
BEFORE INSERT ON integration_reviews
WHEN NOT EXISTS (
    SELECT 1 FROM executions e
    WHERE e.id = NEW.execution_id AND e.project_id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'integration review project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS integration_reviews_project_lineage_update
BEFORE UPDATE OF execution_id, project_id ON integration_reviews
WHEN NOT EXISTS (
    SELECT 1 FROM executions e
    WHERE e.id = NEW.execution_id AND e.project_id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'integration review project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS merge_proposals_project_lineage_insert
BEFORE INSERT ON merge_proposals
WHEN NOT EXISTS (
    SELECT 1
    FROM integration_reviews r JOIN executions e ON e.id = NEW.execution_id
    WHERE r.id = NEW.integration_review_id
      AND r.project_id = NEW.project_id
      AND r.execution_id = NEW.execution_id
      AND e.project_id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'merge proposal project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS merge_proposals_project_lineage_update
BEFORE UPDATE OF integration_review_id, execution_id, project_id ON merge_proposals
WHEN NOT EXISTS (
    SELECT 1
    FROM integration_reviews r JOIN executions e ON e.id = NEW.execution_id
    WHERE r.id = NEW.integration_review_id
      AND r.project_id = NEW.project_id
      AND r.execution_id = NEW.execution_id
      AND e.project_id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'merge proposal project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS integration_worktrees_project_lineage_insert
BEFORE INSERT ON integration_worktrees
WHEN NOT EXISTS (
    SELECT 1 FROM executions e JOIN repositories r ON r.id = NEW.repository_id
    WHERE e.id = NEW.execution_id
      AND e.project_id = NEW.project_id
      AND r.project_id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'integration worktree project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS integration_worktrees_project_lineage_update
BEFORE UPDATE OF execution_id, repository_id, project_id ON integration_worktrees
WHEN NOT EXISTS (
    SELECT 1 FROM executions e JOIN repositories r ON r.id = NEW.repository_id
    WHERE e.id = NEW.execution_id
      AND e.project_id = NEW.project_id
      AND r.project_id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'integration worktree project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS integration_evidence_project_lineage_insert
BEFORE INSERT ON integration_evidence
WHEN NOT EXISTS (
    SELECT 1
    FROM merge_proposals m JOIN executions e ON e.id = NEW.execution_id
    WHERE m.id = NEW.proposal_id
      AND m.project_id = NEW.project_id
      AND m.execution_id = NEW.execution_id
      AND e.project_id = NEW.project_id
)
OR (
    NEW.integration_worktree_id IS NOT NULL
    AND NOT EXISTS (
        SELECT 1 FROM integration_worktrees w
        WHERE w.id = NEW.integration_worktree_id
          AND w.project_id = NEW.project_id
          AND w.execution_id = NEW.execution_id
    )
)
BEGIN
    SELECT RAISE(ABORT, 'integration evidence project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS integration_evidence_project_lineage_update
BEFORE UPDATE OF proposal_id, execution_id, integration_worktree_id, project_id ON integration_evidence
WHEN NOT EXISTS (
    SELECT 1
    FROM merge_proposals m JOIN executions e ON e.id = NEW.execution_id
    WHERE m.id = NEW.proposal_id
      AND m.project_id = NEW.project_id
      AND m.execution_id = NEW.execution_id
      AND e.project_id = NEW.project_id
)
OR (
    NEW.integration_worktree_id IS NOT NULL
    AND NOT EXISTS (
        SELECT 1 FROM integration_worktrees w
        WHERE w.id = NEW.integration_worktree_id
          AND w.project_id = NEW.project_id
          AND w.execution_id = NEW.execution_id
    )
)
BEGIN
    SELECT RAISE(ABORT, 'integration evidence project lineage mismatch');
END;
