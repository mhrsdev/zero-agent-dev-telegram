-- Migration 0027: database-level project-lineage protection for the
-- remaining project-scoped tables.
--
-- The release audit (§5.1) demonstrated that a conversation event could
-- be moved across projects with plain SQL because several project-scoped
-- tables relied only on application-layer filtering. This migration
-- closes those gaps at the database boundary for:
--
--   conversation_events, interface_bindings, repositories,
--   secret_references, tool_grants, topology_snapshots,
--   project_memberships
--
-- Semantics (matching 0025_project_lineage_hardening.sql):
-- - INSERT is rejected when the row references a project that does not
--   exist.
-- - UPDATE of the project_id column is rejected outright: moving a row
--   across projects would silently merge two isolation boundaries.
--
-- Note on rag_index_entries: it is an FTS5 virtual table; SQLite does
-- not support BEFORE INSERT/UPDATE triggers raising ABORT on virtual
-- FTS tables, so its integrity remains enforced by application-layer
-- project scoping plus the rag_documents lineage triggers from 0025.

CREATE TRIGGER IF NOT EXISTS conversation_events_project_lineage_insert
BEFORE INSERT ON conversation_events
WHEN NOT EXISTS (
    SELECT 1 FROM projects WHERE projects.id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'conversation event project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS conversation_events_project_lineage_update
BEFORE UPDATE OF project_id ON conversation_events
WHEN NEW.project_id != OLD.project_id
BEGIN
    SELECT RAISE(ABORT, 'conversation event project_id is immutable');
END;

CREATE TRIGGER IF NOT EXISTS interface_bindings_project_lineage_insert
BEFORE INSERT ON interface_bindings
WHEN NOT EXISTS (
    SELECT 1 FROM projects WHERE projects.id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'interface binding project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS interface_bindings_project_lineage_update
BEFORE UPDATE OF project_id ON interface_bindings
WHEN NEW.project_id != OLD.project_id
BEGIN
    SELECT RAISE(ABORT, 'interface binding project_id is immutable');
END;

CREATE TRIGGER IF NOT EXISTS repositories_project_lineage_insert
BEFORE INSERT ON repositories
WHEN NOT EXISTS (
    SELECT 1 FROM projects WHERE projects.id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'repository project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS repositories_project_lineage_update
BEFORE UPDATE OF project_id ON repositories
WHEN NEW.project_id != OLD.project_id
BEGIN
    SELECT RAISE(ABORT, 'repository project_id is immutable');
END;

CREATE TRIGGER IF NOT EXISTS secret_references_project_lineage_insert
BEFORE INSERT ON secret_references
WHEN NOT EXISTS (
    SELECT 1 FROM projects WHERE projects.id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'secret reference project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS secret_references_project_lineage_update
BEFORE UPDATE OF project_id ON secret_references
WHEN NEW.project_id != OLD.project_id
BEGIN
    SELECT RAISE(ABORT, 'secret reference project_id is immutable');
END;

CREATE TRIGGER IF NOT EXISTS tool_grants_project_lineage_insert
BEFORE INSERT ON tool_grants
WHEN NOT EXISTS (
    SELECT 1 FROM projects WHERE projects.id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'tool grant project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS tool_grants_project_lineage_update
BEFORE UPDATE OF project_id ON tool_grants
WHEN NEW.project_id != OLD.project_id
BEGIN
    SELECT RAISE(ABORT, 'tool grant project_id is immutable');
END;

CREATE TRIGGER IF NOT EXISTS topology_snapshots_project_lineage_insert
BEFORE INSERT ON topology_snapshots
WHEN NOT EXISTS (
    SELECT 1 FROM projects WHERE projects.id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'topology snapshot project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS topology_snapshots_project_lineage_update
BEFORE UPDATE OF project_id ON topology_snapshots
WHEN NEW.project_id != OLD.project_id
BEGIN
    SELECT RAISE(ABORT, 'topology snapshot project_id is immutable');
END;

CREATE TRIGGER IF NOT EXISTS project_memberships_project_lineage_insert
BEFORE INSERT ON project_memberships
WHEN NOT EXISTS (
    SELECT 1 FROM projects WHERE projects.id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'project membership project lineage mismatch');
END;

CREATE TRIGGER IF NOT EXISTS project_memberships_project_lineage_update
BEFORE UPDATE OF project_id ON project_memberships
WHEN NEW.project_id != OLD.project_id
BEGIN
    SELECT RAISE(ABORT, 'project membership project_id is immutable');
END;
