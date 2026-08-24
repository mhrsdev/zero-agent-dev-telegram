-- Preserve migration checksums while hardening project ownership and
-- normalizing active provider rows created before lease fencing.
-- Project ownership is a root invariant: denormalized project_id values are
-- immutable rather than movable across projects through direct SQL.

CREATE TRIGGER IF NOT EXISTS plans_project_ownership_immutable
BEFORE UPDATE OF project_id ON plans
WHEN NEW.project_id IS NOT OLD.project_id
BEGIN
    SELECT RAISE(ABORT, 'project ownership is immutable');
END;

CREATE TRIGGER IF NOT EXISTS agent_types_project_ownership_immutable
BEFORE UPDATE OF project_id ON agent_types
WHEN NEW.project_id IS NOT OLD.project_id
BEGIN
    SELECT RAISE(ABORT, 'project ownership is immutable');
END;

CREATE TRIGGER IF NOT EXISTS knowledge_records_project_ownership_immutable
BEFORE UPDATE OF project_id ON knowledge_records
WHEN NEW.project_id IS NOT OLD.project_id
BEGIN
    SELECT RAISE(ABORT, 'project ownership is immutable');
END;

CREATE TRIGGER IF NOT EXISTS rag_documents_project_ownership_immutable
BEFORE UPDATE OF project_id ON rag_documents
WHEN NEW.project_id IS NOT OLD.project_id
BEGIN
    SELECT RAISE(ABORT, 'project ownership is immutable');
END;

-- A pre-fencing streaming row has no durable owner or token that can fence
-- the old worker. Treating it as unknown is safer than replaying it as if the
-- provider call definitely did not happen. Pending rows were never dispatched
-- and may be claimed normally after their legacy lease fields are cleared.
UPDATE provider_requests
SET state = 'unknown',
    error_class = 'unknown_outcome',
    error_message = 'provider request was active before lease fencing',
    completed_at = COALESCE(completed_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    claim_owner = NULL,
    claim_token = NULL,
    lease_expires_at = NULL,
    heartbeat_at = NULL
WHERE state = 'streaming' AND claim_token IS NULL;

UPDATE provider_requests
SET claim_owner = NULL,
    claim_token = NULL,
    lease_expires_at = NULL,
    heartbeat_at = NULL
WHERE state = 'pending';
