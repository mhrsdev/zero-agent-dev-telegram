-- Provider request/usage project-lineage enforcement.
-- Single-column foreign keys prove existence, not ownership. These triggers
-- reject confused-deputy rows even when a caller has valid IDs from another
-- project.

CREATE TRIGGER IF NOT EXISTS trg_provider_request_execution_project_lineage
BEFORE INSERT ON provider_requests
WHEN NEW.execution_id IS NOT NULL
 AND NOT EXISTS (
     SELECT 1 FROM executions
     WHERE executions.id = NEW.execution_id
       AND executions.project_id = NEW.project_id
 )
BEGIN
    SELECT RAISE(ABORT, 'provider request execution project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS trg_provider_request_execution_project_lineage_update
BEFORE UPDATE OF project_id, execution_id ON provider_requests
WHEN NEW.execution_id IS NOT NULL
 AND NOT EXISTS (
     SELECT 1 FROM executions
     WHERE executions.id = NEW.execution_id
       AND executions.project_id = NEW.project_id
 )
BEGIN
    SELECT RAISE(ABORT, 'provider request execution project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS trg_provider_request_artifact_project_lineage
BEFORE INSERT ON provider_requests
WHEN NEW.response_artifact_id IS NOT NULL
 AND NOT EXISTS (
     SELECT 1 FROM artifacts
     WHERE artifacts.id = NEW.response_artifact_id
       AND artifacts.project_id = NEW.project_id
 )
BEGIN
    SELECT RAISE(ABORT, 'provider request artifact project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS trg_provider_request_artifact_project_lineage_update
BEFORE UPDATE OF project_id, response_artifact_id ON provider_requests
WHEN NEW.response_artifact_id IS NOT NULL
 AND NOT EXISTS (
     SELECT 1 FROM artifacts
     WHERE artifacts.id = NEW.response_artifact_id
       AND artifacts.project_id = NEW.project_id
 )
BEGIN
    SELECT RAISE(ABORT, 'provider request artifact project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS trg_usage_provider_request_project_lineage
BEFORE INSERT ON usage_records
WHEN NOT EXISTS (
    SELECT 1 FROM provider_requests
    WHERE provider_requests.id = NEW.provider_request_id
      AND provider_requests.project_id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'usage provider request project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS trg_usage_provider_request_project_lineage_update
BEFORE UPDATE OF project_id, provider_request_id ON usage_records
WHEN NOT EXISTS (
    SELECT 1 FROM provider_requests
    WHERE provider_requests.id = NEW.provider_request_id
      AND provider_requests.project_id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'usage provider request project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS trg_usage_execution_project_lineage
BEFORE INSERT ON usage_records
WHEN NEW.execution_id IS NOT NULL
 AND NOT EXISTS (
     SELECT 1 FROM executions
     WHERE executions.id = NEW.execution_id
       AND executions.project_id = NEW.project_id
 )
BEGIN
    SELECT RAISE(ABORT, 'usage execution project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS trg_usage_execution_project_lineage_update
BEFORE UPDATE OF project_id, execution_id ON usage_records
WHEN NEW.execution_id IS NOT NULL
 AND NOT EXISTS (
     SELECT 1 FROM executions
     WHERE executions.id = NEW.execution_id
       AND executions.project_id = NEW.project_id
 )
BEGIN
    SELECT RAISE(ABORT, 'usage execution project lineage mismatch');
END;
