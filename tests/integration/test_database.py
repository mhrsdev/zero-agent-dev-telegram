"""Integration tests for the database layer — ADR 0003 §6 (three-schema isolation)."""
from __future__ import annotations

import pytest
from zero.core.scope import Scope
from zero.db import Database, scope_to_schema
from zero.db.sqlite_backend import InMemorySqliteBackend


@pytest.fixture
async def db() -> Database:
    backend = InMemorySqliteBackend()
    d = Database(backend=backend)
    await d.start()
    yield d
    await d.stop()


@pytest.fixture
def personal_scope() -> Scope:
    return Scope.personal(user_id="usr_01HALICE").with_default_memory_scope()


@pytest.fixture
def normal_scope() -> Scope:
    return Scope.normal(group_id="grp_01HABC", topic_id=100).with_default_memory_scope()


@pytest.fixture
def dev_scope() -> Scope:
    return Scope.development(
        org_id="org_01HABC", workspace_id="ws_01HABC",
        project_id="prj_01HABC", group_id="grp_01HABC", topic_id=200,
    ).with_default_memory_scope()


# ---------------------------------------------------------------------- schema mapping

class TestScopeToSchema:
    def test_personal_maps_to_personal_schema(self, personal_scope: Scope) -> None:
        assert scope_to_schema(personal_scope) == "personal"

    def test_normal_maps_to_normal_schema(self, normal_scope: Scope) -> None:
        assert scope_to_schema(normal_scope) == "normal"

    def test_dev_maps_to_dev_schema(self, dev_scope: Scope) -> None:
        assert scope_to_schema(dev_scope) == "dev"


# ---------------------------------------------------------------------- migrations

class TestMigrations:
    @pytest.mark.asyncio
    async def test_migrate_creates_tables(self, db: Database) -> None:
        # After start(), all schemas should have schema_meta with version 1.
        for schema in ("personal", "normal", "dev"):
            v = await db.schema_version(schema)
            assert v >= 1

    @pytest.mark.asyncio
    async def test_personal_schema_has_users_table(self, db: Database, personal_scope: Scope) -> None:
        async with db.connection_for(personal_scope) as conn:
            # Should be able to insert + select from personal_users.
            await conn.execute(
                "INSERT INTO personal_users (user_id, display_name, telegram_user_id) VALUES (?, ?, ?)",
                ("usr_01HALICE", "Alice", 12345),
            )
            row = await conn.fetchone("SELECT display_name FROM personal_users WHERE user_id = ?",
                                       ("usr_01HALICE",))
            assert row is not None
            assert row[0] == "Alice"

    @pytest.mark.asyncio
    async def test_normal_schema_has_topic_bindings(self, db: Database, normal_scope: Scope) -> None:
        async with db.connection_for(normal_scope) as conn:
            # First create a group.
            await conn.execute(
                "INSERT INTO normal_groups (group_id, telegram_chat_id, is_forum) VALUES (?, ?, ?)",
                ("grp_01HABC", -1001234567890, True),
            )
            await conn.execute(
                """INSERT INTO normal_topic_bindings
                   (group_id, topic_id, mode, memory_scope_id, configured_by)
                   VALUES (?, ?, ?, ?, ?)""",
                ("grp_01HABC", 100, "normal", "mem:grp:grp_01HABC:100", "usr_01HALICE"),
            )
            row = await conn.fetchone(
                "SELECT mode FROM normal_topic_bindings WHERE group_id = ? AND topic_id = ?",
                ("grp_01HABC", 100),
            )
            assert row is not None
            assert row[0] == "normal"

    @pytest.mark.asyncio
    async def test_dev_schema_has_projects(self, db: Database, dev_scope: Scope) -> None:
        async with db.connection_for(dev_scope) as conn:
            await conn.execute(
                "INSERT INTO dev_orgs (org_id, display_name, owner_type, owner_id) VALUES (?, ?, ?, ?)",
                ("org_01HABC", "Test Org", "user", "usr_01HALICE"),
            )
            await conn.execute(
                "INSERT INTO dev_workspaces (workspace_id, org_id, display_name, is_default) VALUES (?, ?, ?, ?)",
                ("ws_01HABC", "org_01HABC", "Default", True),
            )
            await conn.execute(
                "INSERT INTO dev_projects (project_id, workspace_id, display_name) VALUES (?, ?, ?)",
                ("prj_01HABC", "ws_01HABC", "Test Project"),
            )
            row = await conn.fetchone(
                "SELECT display_name FROM dev_projects WHERE project_id = ?",
                ("prj_01HABC",),
            )
            assert row is not None
            assert row[0] == "Test Project"


# ---------------------------------------------------------------------- constraint enforcement

class TestDBConstraints:
    @pytest.mark.asyncio
    async def test_topic_binding_dev_requires_project_id(self, db: Database, normal_scope: Scope) -> None:
        """DB CHECK rejects mode='dev' without project_id."""
        async with db.connection_for(normal_scope) as conn:
            await conn.execute(
                "INSERT INTO normal_groups (group_id, telegram_chat_id, is_forum) VALUES (?, ?, ?)",
                ("grp_01HABC", -1001234567890, True),
            )
            with pytest.raises(Exception):  # sqlite3.IntegrityError
                await conn.execute(
                    """INSERT INTO normal_topic_bindings
                       (group_id, topic_id, mode, memory_scope_id, configured_by)
                       VALUES (?, ?, ?, ?, ?)""",
                    ("grp_01HABC", 100, "dev", "mem:foo", "usr_01HALICE"),
                )

    @pytest.mark.asyncio
    async def test_topic_binding_normal_forbids_project_id(self, db: Database, normal_scope: Scope) -> None:
        """DB CHECK rejects mode='normal' with project_id."""
        async with db.connection_for(normal_scope) as conn:
            await conn.execute(
                "INSERT INTO normal_groups (group_id, telegram_chat_id, is_forum) VALUES (?, ?, ?)",
                ("grp_01HABC", -1001234567890, True),
            )
            with pytest.raises(Exception):
                await conn.execute(
                    """INSERT INTO normal_topic_bindings
                       (group_id, topic_id, mode, memory_scope_id, configured_by, project_id)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    ("grp_01HABC", 100, "normal", "mem:foo", "usr_01HALICE", "prj_01HABC"),
                )

    @pytest.mark.asyncio
    async def test_dev_memory_fact_requires_approved_by(self, db: Database, dev_scope: Scope) -> None:
        """DB CHECK rejects kind='fact' without approved_by (T-6.4)."""
        async with db.connection_for(dev_scope) as conn:
            # Need a project first.
            await conn.execute(
                "INSERT INTO dev_orgs (org_id, display_name, owner_type, owner_id) VALUES (?, ?, ?, ?)",
                ("org_01HABC", "Test", "user", "usr_01HALICE"),
            )
            await conn.execute(
                "INSERT INTO dev_workspaces (workspace_id, org_id, display_name, is_default) VALUES (?, ?, ?, ?)",
                ("ws_01HABC", "org_01HABC", "Default", True),
            )
            await conn.execute(
                "INSERT INTO dev_projects (project_id, workspace_id, display_name) VALUES (?, ?, ?)",
                ("prj_01HABC", "ws_01HABC", "Test Project"),
            )
            with pytest.raises(Exception):
                await conn.execute(
                    """INSERT INTO dev_memory
                       (id, project_id, scope_key, mode, kind, content, source, created_by)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    ("mem_test_1", "prj_01HABC", "dev:prj_01HABC", "development",
                     "fact", "the sky is blue", "test", "usr_01HALICE"),
                )

    @pytest.mark.asyncio
    async def test_dev_memory_fact_with_approved_by_ok(self, db: Database, dev_scope: Scope) -> None:
        async with db.connection_for(dev_scope) as conn:
            await conn.execute(
                "INSERT INTO dev_orgs (org_id, display_name, owner_type, owner_id) VALUES (?, ?, ?, ?)",
                ("org_01HABC", "Test", "user", "usr_01HALICE"),
            )
            await conn.execute(
                "INSERT INTO dev_workspaces (workspace_id, org_id, display_name, is_default) VALUES (?, ?, ?, ?)",
                ("ws_01HABC", "org_01HABC", "Default", True),
            )
            await conn.execute(
                "INSERT INTO dev_projects (project_id, workspace_id, display_name) VALUES (?, ?, ?)",
                ("prj_01HABC", "ws_01HABC", "Test Project"),
            )
            await conn.execute(
                """INSERT INTO dev_memory
                   (id, project_id, scope_key, mode, kind, content, source, created_by, approved_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("mem_test_2", "prj_01HABC", "dev:prj_01HABC", "development",
                 "fact", "the sky is blue", "test", "usr_01HALICE", "usr_01HBOB"),
            )

    @pytest.mark.asyncio
    async def test_normal_memory_forbids_fact(self, db: Database, normal_scope: Scope) -> None:
        """NORMAL schema forbids kind='fact' (T-4.11)."""
        async with db.connection_for(normal_scope) as conn:
            await conn.execute(
                "INSERT INTO normal_groups (group_id, telegram_chat_id, is_forum) VALUES (?, ?, ?)",
                ("grp_01HABC", -1001234567890, True),
            )
            with pytest.raises(Exception):
                await conn.execute(
                    """INSERT INTO normal_memory
                       (id, group_id, topic_id, scope_key, mode, kind, content, source)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    ("mem_test_3", "grp_01HABC", 100, "normal:grp_01HABC:100",
                     "normal", "fact", "test", "test"),
                )

    @pytest.mark.asyncio
    async def test_attach_forbidden(self, db: Database, personal_scope: Scope) -> None:
        """ATTACH is forbidden — would bypass three-file isolation."""
        async with db.connection_for(personal_scope) as conn:
            with pytest.raises(Exception, match="ATTACH"):
                await conn.execute("ATTACH 'dev.db' AS dev_schema")

    @pytest.mark.asyncio
    async def test_dev_approval_self_approve_blocked(self, db: Database, dev_scope: Scope) -> None:
        """DB CHECK: requester cannot self-approve."""
        async with db.connection_for(dev_scope) as conn:
            await conn.execute(
                "INSERT INTO dev_orgs (org_id, display_name, owner_type, owner_id) VALUES (?, ?, ?, ?)",
                ("org_01HABC", "Test", "user", "usr_01HALICE"),
            )
            await conn.execute(
                "INSERT INTO dev_workspaces (workspace_id, org_id, display_name, is_default) VALUES (?, ?, ?, ?)",
                ("ws_01HABC", "org_01HABC", "Default", True),
            )
            await conn.execute(
                "INSERT INTO dev_projects (project_id, workspace_id, display_name) VALUES (?, ?, ?)",
                ("prj_01HABC", "ws_01HABC", "Test Project"),
            )
            with pytest.raises(Exception):
                await conn.execute(
                    """INSERT INTO dev_approvals
                       (approval_id, project_id, requester_id, approver_id, action,
                        params_json, status, expires_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    ("apv_test_1", "prj_01HABC", "usr_01HALICE", "usr_01HALICE",
                     "merge_pr", "{}", "approved", "2026-12-31T23:59:59Z"),
                )

    @pytest.mark.asyncio
    async def test_dev_task_blocked_requires_reason(self, db: Database, dev_scope: Scope) -> None:
        """DB CHECK: status='blocked' requires blocked_reason."""
        async with db.connection_for(dev_scope) as conn:
            await conn.execute(
                "INSERT INTO dev_orgs (org_id, display_name, owner_type, owner_id) VALUES (?, ?, ?, ?)",
                ("org_01HABC", "Test", "user", "usr_01HALICE"),
            )
            await conn.execute(
                "INSERT INTO dev_workspaces (workspace_id, org_id, display_name, is_default) VALUES (?, ?, ?, ?)",
                ("ws_01HABC", "org_01HABC", "Default", True),
            )
            await conn.execute(
                "INSERT INTO dev_projects (project_id, workspace_id, display_name) VALUES (?, ?, ?)",
                ("prj_01HABC", "ws_01HABC", "Test Project"),
            )
            with pytest.raises(Exception):
                await conn.execute(
                    """INSERT INTO dev_tasks
                       (task_id, project_id, title, status, created_by)
                       VALUES (?, ?, ?, ?, ?)""",
                    ("tsk_test_1", "prj_01HABC", "Test", "blocked", "usr_01HALICE"),
                )


# ---------------------------------------------------------------------- cross-schema access prevention

class TestCrossSchemaIsolation:
    @pytest.mark.asyncio
    async def test_personal_conn_cannot_access_dev_tables(self, db: Database, personal_scope: Scope) -> None:
        """A personal-scope connection must NOT be able to query dev tables."""
        async with db.connection_for(personal_scope) as conn:
            # Try to query dev_projects — should fail because the personal.db
            # doesn't have that table.
            with pytest.raises(Exception):
                await conn.fetchone("SELECT * FROM dev_projects")

    @pytest.mark.asyncio
    async def test_normal_conn_cannot_access_personal_tables(self, db: Database, normal_scope: Scope) -> None:
        async with db.connection_for(normal_scope) as conn:
            with pytest.raises(Exception):
                await conn.fetchone("SELECT * FROM personal_users")

    @pytest.mark.asyncio
    async def test_dev_conn_cannot_access_normal_tables(self, db: Database, dev_scope: Scope) -> None:
        async with db.connection_for(dev_scope) as conn:
            with pytest.raises(Exception):
                await conn.fetchone("SELECT * FROM normal_groups")


# ---------------------------------------------------------------------- ATTACH structural test

class TestAttachStructural:
    @pytest.mark.asyncio
    async def test_attach_sql_rejected(self, db: Database, personal_scope: Scope) -> None:
        """Even raw ATTACH SQL is rejected by the connection wrapper."""
        async with db.connection_for(personal_scope) as conn:
            with pytest.raises(Exception, match="ATTACH"):
                await conn.execute("ATTACH DATABASE '/tmp/x.db' AS x")

            with pytest.raises(Exception, match="ATTACH"):
                await conn.execute("DETACH DATABASE x")
