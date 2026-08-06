"""SQLite backend — three-file isolation (ADR 0003 §6, T-1.4).

Each Mode gets its own SQLite DB file:
    - ``personal.db``
    - ``normal.db``
    - ``dev.db``

Each file has its own connection pool; cross-file queries are impossible
because there's no ATTACH (and a structural test forbids ATTACH).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import aiosqlite

from zero.db import DatabaseError, SchemaName

__all__ = ["SqliteBackend", "SqliteConnection"]


# ---------------------------------------------------------------------- connection wrapper

class SqliteConnection:
    """Async wrapper around ``aiosqlite.Connection``.

    Implements the :class:`zero.db.Connection` protocol.
    """

    __slots__ = ("_conn", "_in_tx")

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn
        self._in_tx = False

    async def execute(self, sql: str, params: tuple[object, ...] = ()) -> object:
        # Forbid ATTACH — would let a personal-scope query read a dev table.
        if _is_attach_or_detach(sql):
            raise DatabaseError(
                "ATTACH/DETACH are forbidden — they would bypass three-file isolation"
            )
        cur = await self._conn.execute(sql, params)
        await self._conn.commit()
        return cur

    async def executemany(self, sql: str, params: list[tuple[object, ...]]) -> None:
        if _is_attach_or_detach(sql):
            raise DatabaseError("ATTACH/DETACH are forbidden")
        await self._conn.executemany(sql, params)
        await self._conn.commit()

    async def fetchone(self, sql: str, params: tuple[object, ...] = ()) -> tuple[object, ...] | None:
        if _is_attach_or_detach(sql):
            raise DatabaseError("ATTACH/DETACH are forbidden")
        async with self._conn.execute(sql, params) as cur:
            row = await cur.fetchone()
            return tuple(row) if row is not None else None

    async def fetchall(self, sql: str, params: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
        if _is_attach_or_detach(sql):
            raise DatabaseError("ATTACH/DETACH are forbidden")
        async with self._conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
            return [tuple(r) for r in rows]

    async def commit(self) -> None:
        await self._conn.commit()
        self._in_tx = False

    async def rollback(self) -> None:
        await self._conn.rollback()
        self._in_tx = False

    async def close(self) -> None:
        # Don't actually close the underlying shared connection.
        # For pooled backends, "close" means "release back to pool" (no-op here).
        # The underlying connection is closed by backend.disconnect().
        pass


def _is_attach_or_detach(sql: str) -> bool:
    """Detect ATTACH / DETACH statements (case-insensitive, prefix match)."""
    stripped = sql.lstrip().upper()
    return stripped.startswith("ATTACH") or stripped.startswith("DETACH")


# ---------------------------------------------------------------------- schema bootstrap

# Per-schema DDL. Each schema gets its own table set:
#
#   personal: personal_users, personal_messages, personal_memory, personal_audit
#   normal:   normal_groups, normal_topic_bindings, normal_messages, normal_memory
#   dev:      dev_orgs, dev_workspaces, dev_projects, dev_tasks, dev_memory, dev_adr
#
# (Plus a `schema_meta` table in each, tracking schema_version.)

_SCHEMA_DDL: dict[SchemaName, list[str]] = {
    "personal": [
        """CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS personal_users (
            user_id TEXT PRIMARY KEY,             -- usr_<ulid>
            display_name TEXT NOT NULL,
            telegram_user_id BIGINT UNIQUE,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS personal_messages (
            id TEXT PRIMARY KEY,                  -- msg_<ulid>
            user_id TEXT NOT NULL REFERENCES personal_users(user_id),
            role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system', 'tool')),
            content TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS personal_memory (
            id TEXT PRIMARY KEY,                  -- mem_<ulid>
            user_id TEXT NOT NULL REFERENCES personal_users(user_id),
            scope_key TEXT NOT NULL,              -- 'personal:usr_<ulid>'
            mode TEXT NOT NULL DEFAULT 'personal' CHECK (mode = 'personal'),
            kind TEXT NOT NULL CHECK (kind IN ('semantic', 'episodic', 'fact', 'decision', 'preference')),
            content TEXT NOT NULL,
            source TEXT NOT NULL,                 -- always set; ADR 0005 §2
            approved_by TEXT,                     -- required for kind IN ('fact','decision')
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            invalidated_at TIMESTAMP,
            CHECK (
                (kind NOT IN ('fact', 'decision')) OR
                (kind IN ('fact', 'decision') AND approved_by IS NOT NULL)
            )
        )""",
        """CREATE INDEX IF NOT EXISTS idx_personal_memory_user_kind
            ON personal_memory(user_id, kind) WHERE invalidated_at IS NULL""",
        # ---- Enterprise tables for personal schema ----
        """CREATE TABLE IF NOT EXISTS personal_sessions (
            session_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            scope_key TEXT NOT NULL,
            mode TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            scope_limit TEXT,
            expires_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_used_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            revoked_at TIMESTAMP,
            revoked_reason TEXT,
            locked_until TIMESTAMP,
            failed_attempts INTEGER NOT NULL DEFAULT 0
        )""",
        """CREATE INDEX IF NOT EXISTS idx_personal_sessions_user
            ON personal_sessions(user_id) WHERE revoked_at IS NULL""",
        """CREATE TABLE IF NOT EXISTS personal_role_bindings (
            binding_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            role TEXT NOT NULL,
            scope_kind TEXT NOT NULL,
            scope_id TEXT NOT NULL,
            granted_by TEXT NOT NULL,
            granted_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            revoked_at TIMESTAMP,
            UNIQUE (user_id, scope_kind, scope_id)
        )""",
        """CREATE TABLE IF NOT EXISTS personal_todos (
            todo_id TEXT PRIMARY KEY,
            scope_key TEXT NOT NULL,
            item_text TEXT NOT NULL,
            completed BOOLEAN NOT NULL DEFAULT FALSE,
            created_by TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            position INTEGER NOT NULL DEFAULT 0
        )""",
        """CREATE INDEX IF NOT EXISTS idx_personal_todos_scope
            ON personal_todos(scope_key, position)""",
        """CREATE TABLE IF NOT EXISTS personal_rate_limits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bucket_key TEXT NOT NULL,
            window_start TIMESTAMP NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            UNIQUE (bucket_key, window_start)
        )""",
        """CREATE TABLE IF NOT EXISTS personal_conversation_sessions (
            session_id TEXT PRIMARY KEY,
            scope_key TEXT NOT NULL,
            mode TEXT NOT NULL,
            external_chat_id TEXT NOT NULL,
            topic_id INTEGER,
            user_id TEXT,
            window_start_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_activity_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL,
            message_count INTEGER NOT NULL DEFAULT 0
        )""",
        """CREATE TABLE IF NOT EXISTS personal_conversation_messages (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES personal_conversation_sessions(session_id),
            role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system', 'tool')),
            content TEXT NOT NULL,
            tool_call_id TEXT,
            tool_name TEXT,
            token_count INTEGER,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE INDEX IF NOT EXISTS idx_personal_conv_messages_session
            ON personal_conversation_messages(session_id, created_at)""",
        """CREATE TABLE IF NOT EXISTS personal_approvals (
            approval_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            requester_id TEXT NOT NULL,
            approver_id TEXT,
            action TEXT NOT NULL,
            params_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            resolution_note TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            resolved_at TIMESTAMP,
            expires_at TIMESTAMP NOT NULL
        )""",
    ],
    "normal": [
        """CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS normal_groups (
            group_id TEXT PRIMARY KEY,            -- grp_<ulid>
            telegram_chat_id BIGINT UNIQUE NOT NULL,
            is_forum BOOLEAN NOT NULL DEFAULT FALSE,
            default_unconfigured_topic_mode TEXT NOT NULL DEFAULT 'normal'
                CHECK (default_unconfigured_topic_mode IN ('normal', 'disabled')),
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS normal_topic_bindings (
            group_id TEXT NOT NULL REFERENCES normal_groups(group_id),
            topic_id INTEGER NOT NULL,            -- 0 for non-Forum groups
            mode TEXT NOT NULL CHECK (mode IN ('normal', 'dev', 'disabled')),
            memory_scope_id TEXT NOT NULL,
            project_id TEXT,                      -- required iff mode='dev'
            configured_by TEXT NOT NULL,
            configured_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
            PRIMARY KEY (group_id, topic_id),
            -- ADR 0003: mode='dev' requires project_id; mode in ('normal','disabled') forbids it
            CHECK (
                (mode = 'dev' AND project_id IS NOT NULL) OR
                (mode IN ('normal', 'disabled') AND project_id IS NULL)
            )
        )""",
        """CREATE TABLE IF NOT EXISTS normal_messages (
            id TEXT PRIMARY KEY,
            group_id TEXT NOT NULL REFERENCES normal_groups(group_id),
            topic_id INTEGER NOT NULL,
            user_id TEXT,                         -- nullable: messages can be from agent
            role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system', 'tool')),
            content TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS normal_memory (
            id TEXT PRIMARY KEY,
            group_id TEXT NOT NULL,
            topic_id INTEGER NOT NULL,
            scope_key TEXT NOT NULL,              -- 'normal:grp_<ulid>:<topic_id>'
            mode TEXT NOT NULL DEFAULT 'normal' CHECK (mode = 'normal'),
            kind TEXT NOT NULL CHECK (kind IN ('semantic', 'episodic', 'preference')),
            -- normal mode FORBIDS kind='fact' or 'decision' (ADR 0003 §3, T-4.11)
            content TEXT NOT NULL,
            source TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            invalidated_at TIMESTAMP
        )""",
        """CREATE INDEX IF NOT EXISTS idx_normal_memory_scope
            ON normal_memory(group_id, topic_id) WHERE invalidated_at IS NULL""",
        # ---- Enterprise tables for normal schema ----
        """CREATE TABLE IF NOT EXISTS normal_sessions (
            session_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            scope_key TEXT NOT NULL,
            mode TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            scope_limit TEXT,
            expires_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_used_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            revoked_at TIMESTAMP,
            revoked_reason TEXT,
            locked_until TIMESTAMP,
            failed_attempts INTEGER NOT NULL DEFAULT 0
        )""",
        """CREATE TABLE IF NOT EXISTS normal_role_bindings (
            binding_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            role TEXT NOT NULL,
            scope_kind TEXT NOT NULL,
            scope_id TEXT NOT NULL,
            granted_by TEXT NOT NULL,
            granted_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            revoked_at TIMESTAMP,
            UNIQUE (user_id, scope_kind, scope_id)
        )""",
        """CREATE TABLE IF NOT EXISTS normal_todos (
            todo_id TEXT PRIMARY KEY,
            scope_key TEXT NOT NULL,
            item_text TEXT NOT NULL,
            completed BOOLEAN NOT NULL DEFAULT FALSE,
            created_by TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            position INTEGER NOT NULL DEFAULT 0
        )""",
        """CREATE INDEX IF NOT EXISTS idx_normal_todos_scope
            ON normal_todos(scope_key, position)""",
        """CREATE TABLE IF NOT EXISTS normal_rate_limits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bucket_key TEXT NOT NULL,
            window_start TIMESTAMP NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            UNIQUE (bucket_key, window_start)
        )""",
        """CREATE TABLE IF NOT EXISTS normal_conversation_sessions (
            session_id TEXT PRIMARY KEY,
            scope_key TEXT NOT NULL,
            mode TEXT NOT NULL,
            external_chat_id TEXT NOT NULL,
            topic_id INTEGER,
            user_id TEXT,
            window_start_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_activity_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL,
            message_count INTEGER NOT NULL DEFAULT 0
        )""",
        """CREATE TABLE IF NOT EXISTS normal_conversation_messages (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES normal_conversation_sessions(session_id),
            role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system', 'tool')),
            content TEXT NOT NULL,
            tool_call_id TEXT,
            tool_name TEXT,
            token_count INTEGER,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE INDEX IF NOT EXISTS idx_normal_conv_messages_session
            ON normal_conversation_messages(session_id, created_at)""",
        """CREATE TABLE IF NOT EXISTS normal_approvals (
            approval_id TEXT PRIMARY KEY,
            group_id TEXT NOT NULL,
            requester_id TEXT NOT NULL,
            approver_id TEXT,
            action TEXT NOT NULL,
            params_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            resolution_note TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            resolved_at TIMESTAMP,
            expires_at TIMESTAMP NOT NULL
        )""",
    ],
    "dev": [
        """CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS dev_orgs (
            org_id TEXT PRIMARY KEY,              -- org_<ulid>
            display_name TEXT NOT NULL,
            owner_type TEXT NOT NULL CHECK (owner_type IN ('user', 'org')),
            owner_id TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS dev_workspaces (
            workspace_id TEXT PRIMARY KEY,        -- ws_<ulid>
            org_id TEXT NOT NULL REFERENCES dev_orgs(org_id),
            display_name TEXT NOT NULL,
            is_default BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS dev_projects (
            project_id TEXT PRIMARY KEY,          -- prj_<ulid>
            workspace_id TEXT NOT NULL REFERENCES dev_workspaces(workspace_id),
            display_name TEXT NOT NULL,
            github_repo TEXT,                     -- full_name like "owner/repo"
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            archived_at TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS dev_tasks (
            task_id TEXT PRIMARY KEY,             -- tsk_<ulid>
            project_id TEXT NOT NULL REFERENCES dev_projects(project_id),
            parent_task_id TEXT REFERENCES dev_tasks(task_id),
            -- Subtask CANNOT have children — enforced at app layer (no DB CHECK possible cleanly)
            title TEXT NOT NULL,
            body TEXT,
            status TEXT NOT NULL DEFAULT 'todo'
                CHECK (status IN ('todo', 'in_progress', 'blocked', 'done', 'archived')),
            blocked_reason TEXT,                  -- required iff status='blocked'
            assignee TEXT,                        -- usr_<ulid> or agt_<ulid>
            lease_holder TEXT,                    -- who currently holds lease
            lease_expires_at TIMESTAMP,
            created_by TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (
                (status != 'blocked') OR
                (status = 'blocked' AND blocked_reason IS NOT NULL)
            )
        )""",
        """CREATE INDEX IF NOT EXISTS idx_dev_tasks_project_status
            ON dev_tasks(project_id, status)""",
        """CREATE TABLE IF NOT EXISTS dev_memory (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES dev_projects(project_id),
            topic_id INTEGER,                     -- nullable: Project Facts are topic-independent
            scope_key TEXT NOT NULL,              -- 'dev:prj_<ulid>'
            mode TEXT NOT NULL DEFAULT 'development' CHECK (mode = 'development'),
            kind TEXT NOT NULL CHECK (kind IN ('semantic', 'episodic', 'fact', 'decision', 'scratch')),
            content TEXT NOT NULL,
            source TEXT NOT NULL,
            approved_by TEXT,                     -- required for kind IN ('fact','decision')
            created_by TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP,                 -- for scratch kind
            invalidated_at TIMESTAMP,
            CHECK (
                (kind NOT IN ('fact', 'decision')) OR
                (kind IN ('fact', 'decision') AND approved_by IS NOT NULL)
            )
        )""",
        """CREATE INDEX IF NOT EXISTS idx_dev_memory_project_kind
            ON dev_memory(project_id, kind) WHERE invalidated_at IS NULL""",
        """CREATE TABLE IF NOT EXISTS dev_adr (
            adr_id TEXT PRIMARY KEY,              -- adr_<ulid>
            project_id TEXT NOT NULL REFERENCES dev_projects(project_id),
            number INTEGER NOT NULL,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'proposed'
                CHECK (status IN ('proposed', 'accepted', 'deprecated', 'superseded')),
            superseded_by TEXT REFERENCES dev_adr(adr_id),
            context TEXT NOT NULL,
            decision TEXT NOT NULL,
            consequences TEXT NOT NULL,
            -- ADR 0006 §6: 6 mandatory fields (we use 5 + status)
            created_by TEXT NOT NULL,
            accepted_by TEXT,                     -- required iff status='accepted'
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            accepted_at TIMESTAMP,
            UNIQUE (project_id, number),
            CHECK (
                (status != 'accepted') OR
                (status = 'accepted' AND accepted_by IS NOT NULL)
            )
        )""",
        """CREATE TABLE IF NOT EXISTS dev_audit_log (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            actor_type TEXT NOT NULL CHECK (actor_type IN ('human', 'agent', 'system')),
            actor_id TEXT NOT NULL,
            action TEXT NOT NULL,
            target_type TEXT,
            target_id TEXT,
            scope_key TEXT NOT NULL,
            mode TEXT NOT NULL,
            before_value TEXT,                    -- JSON
            after_value TEXT,                     -- JSON
            result TEXT NOT NULL CHECK (result IN ('success', 'failure', 'denied')),
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE INDEX IF NOT EXISTS idx_dev_audit_project_time
            ON dev_audit_log(project_id, created_at)""",
        """CREATE TABLE IF NOT EXISTS dev_agent_runs (
            run_id TEXT PRIMARY KEY,              -- run_<ulid>
            project_id TEXT NOT NULL REFERENCES dev_projects(project_id),
            agent_def_id TEXT NOT NULL,           -- agt_<ulid>
            launched_by TEXT NOT NULL,            -- usr_<ulid>
            task_id TEXT REFERENCES dev_tasks(task_id),
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'running', 'completed', 'failed', 'cancelled')),
            input_prompt TEXT NOT NULL,
            output_text TEXT,
            error_message TEXT,
            cost_usd REAL NOT NULL DEFAULT 0.0,
            started_at TIMESTAMP,
            completed_at TIMESTAMP,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS dev_approvals (
            approval_id TEXT PRIMARY KEY,         -- apv_<ulid>
            project_id TEXT NOT NULL,
            requester_id TEXT NOT NULL,
            approver_id TEXT,                     -- set on resolution
            action TEXT NOT NULL,                 -- e.g. 'merge_pr', 'promote_fact', 'sandbox_exec'
            params_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'approved', 'rejected', 'edited', 'changes_requested', 'expired')),
            resolution_note TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            resolved_at TIMESTAMP,
            expires_at TIMESTAMP NOT NULL,
            CHECK (
                (status IN ('approved', 'rejected', 'edited', 'changes_requested', 'expired')) OR
                (status = 'pending' AND approver_id IS NULL)
            ),
            -- ADR 0008 §3: requester cannot self-approve
            CHECK (
                status != 'approved' OR approver_id != requester_id
            )
        )""",
        # ---- Enterprise tables (Phase 4) ----
        # Persistent sessions (T-8.10)
        """CREATE TABLE IF NOT EXISTS dev_sessions (
            session_id TEXT PRIMARY KEY,              -- ssn_<ulid>
            user_id TEXT NOT NULL,
            scope_key TEXT NOT NULL,
            mode TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,          -- sha256 hex
            scope_limit TEXT,
            expires_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_used_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            revoked_at TIMESTAMP,
            revoked_reason TEXT,
            locked_until TIMESTAMP,
            failed_attempts INTEGER NOT NULL DEFAULT 0
        )""",
        """CREATE INDEX IF NOT EXISTS idx_dev_sessions_user
            ON dev_sessions(user_id) WHERE revoked_at IS NULL""",
        """CREATE INDEX IF NOT EXISTS idx_dev_sessions_expires
            ON dev_sessions(expires_at) WHERE revoked_at IS NULL""",
        # Role bindings (T-2.2)
        """CREATE TABLE IF NOT EXISTS dev_role_bindings (
            binding_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN (
                'agent', 'reviewer', 'developer', 'maintainer', 'teamlead', 'owner'
            )),
            scope_kind TEXT NOT NULL CHECK (scope_kind IN ('org', 'workspace', 'project')),
            scope_id TEXT NOT NULL,
            granted_by TEXT NOT NULL,
            granted_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            revoked_at TIMESTAMP,
            UNIQUE (user_id, scope_kind, scope_id)
        )""",
        """CREATE INDEX IF NOT EXISTS idx_dev_role_bindings_lookup
            ON dev_role_bindings(user_id, scope_kind, scope_id) WHERE revoked_at IS NULL""",
        # Todo items (DB-backed TodoTool)
        """CREATE TABLE IF NOT EXISTS dev_todos (
            todo_id TEXT PRIMARY KEY,
            scope_key TEXT NOT NULL,
            item_text TEXT NOT NULL,
            completed BOOLEAN NOT NULL DEFAULT FALSE,
            created_by TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            position INTEGER NOT NULL DEFAULT 0
        )""",
        """CREATE INDEX IF NOT EXISTS idx_dev_todos_scope
            ON dev_todos(scope_key, position)""",
        # Rate limit counters (T-8.10 sliding window)
        """CREATE TABLE IF NOT EXISTS dev_rate_limits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bucket_key TEXT NOT NULL,                 -- e.g. 'login:user:usr_01H...'
            window_start TIMESTAMP NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            UNIQUE (bucket_key, window_start)
        )""",
        """CREATE INDEX IF NOT EXISTS idx_dev_rate_limits_bucket
            ON dev_rate_limits(bucket_key, window_start)""",
        # Conversation sessions (T-4.18 — message history per chat)
        """CREATE TABLE IF NOT EXISTS dev_conversation_sessions (
            session_id TEXT PRIMARY KEY,              -- cs_<ulid>
            scope_key TEXT NOT NULL,
            mode TEXT NOT NULL,
            external_chat_id TEXT NOT NULL,
            topic_id INTEGER,
            user_id TEXT,
            window_start_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_activity_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL,
            message_count INTEGER NOT NULL DEFAULT 0
        )""",
        """CREATE INDEX IF NOT EXISTS idx_dev_conversation_sessions_chat
            ON dev_conversation_sessions(external_chat_id, topic_id)""",
        # Conversation messages
        """CREATE TABLE IF NOT EXISTS dev_conversation_messages (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES dev_conversation_sessions(session_id),
            role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system', 'tool')),
            content TEXT NOT NULL,
            tool_call_id TEXT,
            tool_name TEXT,
            token_count INTEGER,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE INDEX IF NOT EXISTS idx_dev_conversation_messages_session
            ON dev_conversation_messages(session_id, created_at)""",
    ],
}

# Cross-schema tables (audit lives in each schema's own table)
_CROSS_SCHEMA_TABLES = ("schema_meta",)


# ---------------------------------------------------------------------- backend

@dataclass(slots=True)
class SqliteBackend:
    """SQLite backend with three-file isolation.

    Construction:
        >>> backend = SqliteBackend(sqlite_dir=Path("~/.zero/db"))
        >>> await backend.migrate("personal")
    """

    sqlite_dir: Path
    _connections: dict[SchemaName, aiosqlite.Connection] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        # Expand user (~) and create dir if missing.
        self.sqlite_dir = self.sqlite_dir.expanduser()
        self.sqlite_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ paths

    def _db_path(self, schema: SchemaName) -> Path:
        return self.sqlite_dir / f"{schema}.db"

    # ------------------------------------------------------------------ connection

    async def connect(self, schema: SchemaName) -> SqliteConnection:
        if schema in self._connections:
            # Reuse pooled connection — but wrap in a fresh SqliteConnection
            # so callers can close it without dropping the underlying aiosqlite.
            # NOTE: For tests, prefer opening a fresh connection per call.
            conn = self._connections[schema]
        else:
            path = self._db_path(schema)
            conn = await aiosqlite.connect(path)
            # Recommended pragmas for SQLite + async.
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA foreign_keys=ON")
            await conn.execute("PRAGMA busy_timeout=5000")
            self._connections[schema] = conn
        return SqliteConnection(conn)

    async def disconnect(self) -> None:
        for conn in list(self._connections.values()):
            await conn.close()
        self._connections.clear()

    async def ping(self, schema: SchemaName) -> bool:
        try:
            conn = await self.connect(schema)
            try:
                row = await conn.fetchone("SELECT 1")
                return row is not None and row[0] == 1
            finally:
                await conn.close()
        except Exception:
            return False

    async def schema_version(self, schema: SchemaName) -> int:
        conn = await self.connect(schema)
        try:
            row = await conn.fetchone(
                "SELECT value FROM schema_meta WHERE key = ?",
                ("schema_version",),
            )
            if row is None:
                return 0
            try:
                return int(str(row[0]))
            except (ValueError, TypeError):
                return 0
        finally:
            await conn.close()

    async def migrate(self, schema: SchemaName, target_version: int | None = None) -> int:
        """Apply DDL for ``schema``. Idempotent (CREATE IF NOT EXISTS)."""
        conn = await self.connect(schema)
        try:
            for ddl in _SCHEMA_DDL[schema]:
                await conn.execute(ddl)

            # Set schema_version if missing.
            current = await self.schema_version(schema)
            new_version = target_version if target_version is not None else 1
            if current < new_version:
                await conn.execute(
                    "INSERT INTO schema_meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    ("schema_version", str(new_version)),
                )
            return new_version
        finally:
            await conn.close()


# ---------------------------------------------------------------------- in-memory backend for tests

class InMemorySqliteBackend(SqliteBackend):
    """SQLite backend backed by :memory: databases.

    Each call to ``connect()`` returns a connection to the same in-memory DB
    per schema (shared via a single shared connection held in the pool).

    For tests only — never use in production.
    """

    def __init__(self) -> None:  # skip docstring on test helper
        # Pass a dummy dir; we override _db_path to return ":memory:".
        super().__init__(sqlite_dir=Path("/tmp/zero-test-noop"))  # noqa: S108

    async def connect(self, schema: SchemaName) -> SqliteConnection:
        if schema not in self._connections:
            # Use a single shared in-memory DB per schema. The URI form
            # "file::memory:?cache=shared&uri=true" lets multiple connections
            # share the same in-memory database within a process.
            conn = await aiosqlite.connect(":memory:")
            await conn.execute("PRAGMA foreign_keys=ON")
            self._connections[schema] = conn
        return SqliteConnection(self._connections[schema])

    async def migrate(self, schema: SchemaName, target_version: int | None = None) -> int:
        # Same as parent, but using our overridden connect().
        conn = await self.connect(schema)
        try:
            for ddl in _SCHEMA_DDL[schema]:
                await conn.execute(ddl)
            await conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("schema_version", str(target_version or 1)),
            )
            return target_version or 1
        finally:
            await conn.close()
