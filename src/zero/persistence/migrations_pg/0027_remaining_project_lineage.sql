-- GENERATED from 0027_remaining_project_lineage.sql by scripts/gen_pg_migrations.py.
-- PostgreSQL dialect translation of the canonical SQLite schema.
-- Do not edit directly; re-run the generator instead.

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














CREATE OR REPLACE FUNCTION zero_conversation_events_project_lineage_insert_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'conversation event project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS conversation_events_project_lineage_insert ON conversation_events;
CREATE TRIGGER conversation_events_project_lineage_insert
    BEFORE INSERT ON conversation_events
    FOR EACH ROW EXECUTE FUNCTION zero_conversation_events_project_lineage_insert_fn();

CREATE OR REPLACE FUNCTION zero_conversation_events_project_lineage_update_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'conversation event project_id is immutable';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS conversation_events_project_lineage_update ON conversation_events;
CREATE TRIGGER conversation_events_project_lineage_update
    BEFORE UPDATE OF project_id ON conversation_events
    FOR EACH ROW EXECUTE FUNCTION zero_conversation_events_project_lineage_update_fn();

CREATE OR REPLACE FUNCTION zero_interface_bindings_project_lineage_insert_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'interface binding project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS interface_bindings_project_lineage_insert ON interface_bindings;
CREATE TRIGGER interface_bindings_project_lineage_insert
    BEFORE INSERT ON interface_bindings
    FOR EACH ROW EXECUTE FUNCTION zero_interface_bindings_project_lineage_insert_fn();

CREATE OR REPLACE FUNCTION zero_interface_bindings_project_lineage_update_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'interface binding project_id is immutable';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS interface_bindings_project_lineage_update ON interface_bindings;
CREATE TRIGGER interface_bindings_project_lineage_update
    BEFORE UPDATE OF project_id ON interface_bindings
    FOR EACH ROW EXECUTE FUNCTION zero_interface_bindings_project_lineage_update_fn();

CREATE OR REPLACE FUNCTION zero_repositories_project_lineage_insert_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'repository project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS repositories_project_lineage_insert ON repositories;
CREATE TRIGGER repositories_project_lineage_insert
    BEFORE INSERT ON repositories
    FOR EACH ROW EXECUTE FUNCTION zero_repositories_project_lineage_insert_fn();

CREATE OR REPLACE FUNCTION zero_repositories_project_lineage_update_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'repository project_id is immutable';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS repositories_project_lineage_update ON repositories;
CREATE TRIGGER repositories_project_lineage_update
    BEFORE UPDATE OF project_id ON repositories
    FOR EACH ROW EXECUTE FUNCTION zero_repositories_project_lineage_update_fn();

CREATE OR REPLACE FUNCTION zero_secret_references_project_lineage_insert_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'secret reference project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS secret_references_project_lineage_insert ON secret_references;
CREATE TRIGGER secret_references_project_lineage_insert
    BEFORE INSERT ON secret_references
    FOR EACH ROW EXECUTE FUNCTION zero_secret_references_project_lineage_insert_fn();

CREATE OR REPLACE FUNCTION zero_secret_references_project_lineage_update_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'secret reference project_id is immutable';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS secret_references_project_lineage_update ON secret_references;
CREATE TRIGGER secret_references_project_lineage_update
    BEFORE UPDATE OF project_id ON secret_references
    FOR EACH ROW EXECUTE FUNCTION zero_secret_references_project_lineage_update_fn();

CREATE OR REPLACE FUNCTION zero_tool_grants_project_lineage_insert_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'tool grant project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS tool_grants_project_lineage_insert ON tool_grants;
CREATE TRIGGER tool_grants_project_lineage_insert
    BEFORE INSERT ON tool_grants
    FOR EACH ROW EXECUTE FUNCTION zero_tool_grants_project_lineage_insert_fn();

CREATE OR REPLACE FUNCTION zero_tool_grants_project_lineage_update_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'tool grant project_id is immutable';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS tool_grants_project_lineage_update ON tool_grants;
CREATE TRIGGER tool_grants_project_lineage_update
    BEFORE UPDATE OF project_id ON tool_grants
    FOR EACH ROW EXECUTE FUNCTION zero_tool_grants_project_lineage_update_fn();

CREATE OR REPLACE FUNCTION zero_topology_snapshots_project_lineage_insert_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'topology snapshot project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS topology_snapshots_project_lineage_insert ON topology_snapshots;
CREATE TRIGGER topology_snapshots_project_lineage_insert
    BEFORE INSERT ON topology_snapshots
    FOR EACH ROW EXECUTE FUNCTION zero_topology_snapshots_project_lineage_insert_fn();

CREATE OR REPLACE FUNCTION zero_topology_snapshots_project_lineage_update_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'topology snapshot project_id is immutable';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS topology_snapshots_project_lineage_update ON topology_snapshots;
CREATE TRIGGER topology_snapshots_project_lineage_update
    BEFORE UPDATE OF project_id ON topology_snapshots
    FOR EACH ROW EXECUTE FUNCTION zero_topology_snapshots_project_lineage_update_fn();

CREATE OR REPLACE FUNCTION zero_project_memberships_project_lineage_insert_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'project membership project lineage mismatch';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS project_memberships_project_lineage_insert ON project_memberships;
CREATE TRIGGER project_memberships_project_lineage_insert
    BEFORE INSERT ON project_memberships
    FOR EACH ROW EXECUTE FUNCTION zero_project_memberships_project_lineage_insert_fn();

CREATE OR REPLACE FUNCTION zero_project_memberships_project_lineage_update_fn() RETURNS trigger AS $zero$
BEGIN
    RAISE EXCEPTION 'project membership project_id is immutable';
END;
$zero$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS project_memberships_project_lineage_update ON project_memberships;
CREATE TRIGGER project_memberships_project_lineage_update
    BEFORE UPDATE OF project_id ON project_memberships
    FOR EACH ROW EXECUTE FUNCTION zero_project_memberships_project_lineage_update_fn();
