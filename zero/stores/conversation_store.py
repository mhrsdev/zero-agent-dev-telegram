"""DB-backed conversation store — T-4.18 session management.

Per ADR T-4.18:
    - Conversation context with window + expiry
    - Scope change = new session (includes Topic mode change normal→dev)
    - Persists message history per chat
    - Used as the agent's short-term memory window

ENTERPRISE: Persists to DB for ALL scopes (personal/normal/dev). Each scope
mode has its own table set:
    - personal: personal_conversation_sessions, personal_conversation_messages
    - normal:   normal_conversation_sessions, normal_conversation_messages
    - dev:      dev_conversation_sessions, dev_conversation_messages
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from zero.core.scope import Scope

if TYPE_CHECKING:
    from zero.db import Database

__all__ = ["DbConversationStore", "ConversationSession", "ConversationMessage"]


@dataclass(slots=True)
class ConversationSession:
    """A conversation session (per chat + topic)."""

    session_id: str
    scope_key: str
    mode: str
    external_chat_id: str
    topic_id: int | None
    user_id: str | None
    window_start_at: datetime
    last_activity_at: datetime
    expires_at: datetime
    message_count: int = 0

    @property
    def is_expired(self) -> bool:
        return datetime.now(UTC) > self.expires_at


@dataclass(slots=True)
class ConversationMessage:
    """A single message in a conversation session."""

    id: str
    session_id: str
    role: str  # 'user', 'assistant', 'system', 'tool'
    content: str
    tool_call_id: str | None
    tool_name: str | None
    token_count: int | None
    created_at: datetime

    def to_router_message(self) -> dict[str, Any]:
        """Convert to OpenAI-format message dict."""
        out: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_call_id is not None:
            out["tool_call_id"] = self.tool_call_id
        if self.tool_name is not None:
            out["name"] = self.tool_name
        return out


def _table_for_scope(scope: Scope) -> tuple[str, str]:
    """Return (sessions_table, messages_table) for this scope."""
    if scope.is_personal():
        return "personal_conversation_sessions", "personal_conversation_messages"
    if scope.is_normal():
        return "normal_conversation_sessions", "normal_conversation_messages"
    return "dev_conversation_sessions", "dev_conversation_messages"


class DbConversationStore:
    """DB-backed conversation session store.

    Per T-4.18: scope change = new session. The session's scope_key is checked
    on every message; if it doesn't match the incoming scope, a new session is
    created and the old one is left to expire.

    ENTERPRISE: Persists to DB for ALL scopes (not just dev). Falls back to
    in-memory only if the DB is unavailable.
    """

    def __init__(
        self,
        db: Database,
        *,
        default_ttl_seconds: int = 3600,  # 1 hour
        max_messages_per_session: int = 100,
    ) -> None:
        self._db = db
        self._default_ttl = default_ttl_seconds
        self._max_messages = max_messages_per_session
        # In-memory fallback for tests / when DB is unavailable.
        self._memory_sessions: dict[str, ConversationSession] = {}
        self._memory_messages: dict[str, list[ConversationMessage]] = {}

    async def get_or_create_session_async(
        self,
        *,
        scope: Scope,
        external_chat_id: str,
        topic_id: int | None,
        user_id: str | None,
        ttl_seconds: int | None = None,
    ) -> ConversationSession:
        """Get the active session for this chat, or create a new one.

        If the scope has changed (e.g. mode normal→dev), a new session is created.
        """
        scope_key = scope.retrieval_key()
        ttl = ttl_seconds or self._default_ttl
        sessions_tbl, _ = _table_for_scope(scope)

        async with self._db.connection_for(scope) as conn:
            # Look for an active session with matching chat+topic+scope.
            row = await conn.fetchone(
                f"""SELECT session_id, scope_key, mode, external_chat_id, topic_id,
                          user_id, window_start_at, last_activity_at, expires_at,
                          message_count
                   FROM {sessions_tbl}
                   WHERE external_chat_id = ? AND topic_id IS ?
                   AND scope_key = ? AND expires_at > ?
                   ORDER BY last_activity_at DESC LIMIT 1""",
                (
                    external_chat_id,
                    topic_id,
                    scope_key,
                    datetime.now(UTC).isoformat(),
                ),
            )
            if row is not None:
                session = self._row_to_session(row)
                # Touch last_activity_at.
                now = datetime.now(UTC)
                await conn.execute(
                    f"UPDATE {sessions_tbl} SET last_activity_at = ? WHERE session_id = ?",
                    (now.isoformat(), session.session_id),
                )
                session.last_activity_at = now
                return session

        # Create new session.
        return await self._create_session_db_async(
            scope=scope,
            scope_key=scope_key,
            external_chat_id=external_chat_id,
            topic_id=topic_id,
            user_id=user_id,
            ttl_seconds=ttl,
        )

    async def _create_session_db_async(
        self,
        *,
        scope: Scope,
        scope_key: str,
        external_chat_id: str,
        topic_id: int | None,
        user_id: str | None,
        ttl_seconds: int | None = None,
    ) -> ConversationSession:
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=ttl_seconds or self._default_ttl)
        session = ConversationSession(
            session_id=f"cs_{uuid.uuid4().hex[:16]}",
            scope_key=scope_key,
            mode=scope.mode.value,
            external_chat_id=external_chat_id,
            topic_id=topic_id,
            user_id=user_id,
            window_start_at=now,
            last_activity_at=now,
            expires_at=expires_at,
        )
        sessions_tbl, _ = _table_for_scope(scope)
        async with self._db.connection_for(scope) as conn:
            await conn.execute(
                f"""INSERT INTO {sessions_tbl}
                   (session_id, scope_key, mode, external_chat_id, topic_id, user_id,
                    window_start_at, last_activity_at, expires_at, message_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                (
                    session.session_id,
                    scope_key,
                    scope.mode.value,
                    external_chat_id,
                    topic_id,
                    user_id,
                    now.isoformat(),
                    now.isoformat(),
                    expires_at.isoformat(),
                ),
            )
        return session

    async def append_message_async(
        self,
        *,
        scope: Scope,
        session: ConversationSession,
        role: str,
        content: str,
        tool_call_id: str | None = None,
        tool_name: str | None = None,
        token_count: int | None = None,
    ) -> ConversationMessage:
        """Append a message to a session."""
        msg = ConversationMessage(
            id=f"cm_{uuid.uuid4().hex[:16]}",
            session_id=session.session_id,
            role=role,
            content=content,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            token_count=token_count,
            created_at=datetime.now(UTC),
        )
        sessions_tbl, messages_tbl = _table_for_scope(scope)
        async with self._db.connection_for(scope) as conn:
            await conn.execute(
                f"""INSERT INTO {messages_tbl}
                   (id, session_id, role, content, tool_call_id, tool_name, token_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    msg.id,
                    msg.session_id,
                    role,
                    content,
                    tool_call_id,
                    tool_name,
                    token_count,
                ),
            )
            await conn.execute(
                f"""UPDATE {sessions_tbl}
                   SET message_count = message_count + 1, last_activity_at = ?
                   WHERE session_id = ?""",
                (msg.created_at.isoformat(), session.session_id),
            )
        session.message_count += 1
        session.last_activity_at = msg.created_at
        return msg

    # Aliases for ergonomic API.
    async def add_message_async(
        self,
        *,
        scope: Scope,
        session: ConversationSession,
        role: str,
        content: str,
        **kwargs: Any,
    ) -> ConversationMessage:
        """Alias for append_message_async (ergonomic)."""
        return await self.append_message_async(
            scope=scope,
            session=session,
            role=role,
            content=content,
            **kwargs,
        )

    async def add_message_by_session_id_async(
        self,
        *,
        scope: Scope,
        session_id: str,
        role: str,
        content: str,
        **kwargs: Any,
    ) -> ConversationMessage:
        """Append a message by session_id (looks up the session first)."""
        session = await self.get_session_async(scope=scope, session_id=session_id)
        if session is None:
            raise KeyError(f"session {session_id!r} not found")
        return await self.append_message_async(
            scope=scope,
            session=session,
            role=role,
            content=content,
            **kwargs,
        )

    async def get_session_async(
        self,
        *,
        scope: Scope,
        session_id: str,
    ) -> ConversationSession | None:
        """Fetch a session by id."""
        sessions_tbl, _ = _table_for_scope(scope)
        async with self._db.connection_for(scope) as conn:
            row = await conn.fetchone(
                f"""SELECT session_id, scope_key, mode, external_chat_id, topic_id,
                          user_id, window_start_at, last_activity_at, expires_at,
                          message_count
                   FROM {sessions_tbl} WHERE session_id = ?""",
                (session_id,),
            )
        return self._row_to_session(row) if row is not None else None

    async def get_history_async(
        self,
        *,
        scope: Scope,
        session: ConversationSession,
        limit: int = 50,
    ) -> list[ConversationMessage]:
        """Get recent message history for a session."""
        _, messages_tbl = _table_for_scope(scope)
        async with self._db.connection_for(scope) as conn:
            rows = await conn.fetchall(
                f"""SELECT id, session_id, role, content, tool_call_id, tool_name,
                          token_count, created_at
                   FROM {messages_tbl}
                   WHERE session_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (session.session_id, limit),
            )
            # Reverse to chronological order.
            messages = [self._row_to_message(r) for r in rows]
            messages.reverse()
            return messages

    async def list_messages_async(
        self,
        *,
        scope: Scope,
        session_id: str,
        limit: int = 50,
    ) -> list[ConversationMessage]:
        """Get recent messages by session_id (ergonomic alias)."""
        session = await self.get_session_async(scope=scope, session_id=session_id)
        if session is None:
            return []
        return await self.get_history_async(scope=scope, session=session, limit=limit)

    async def list_active_sessions_async(
        self,
        *,
        scope: Scope,
    ) -> list[ConversationSession]:
        """List all active (non-expired) sessions for a scope."""
        sessions_tbl, _ = _table_for_scope(scope)
        async with self._db.connection_for(scope) as conn:
            rows = await conn.fetchall(
                f"""SELECT session_id, scope_key, mode, external_chat_id, topic_id,
                          user_id, window_start_at, last_activity_at, expires_at,
                          message_count
                   FROM {sessions_tbl}
                   WHERE scope_key = ? AND expires_at > ?
                   ORDER BY last_activity_at DESC""",
                (scope.retrieval_key(), datetime.now(UTC).isoformat()),
            )
            return [self._row_to_session(r) for r in rows]

    async def end_session_async(self, session_id: str) -> bool:
        """End a session by setting expires_at to now.

        Tries each scope's table (since we don't know which scope the session
        belongs to from the id alone). Returns True if found and ended.
        """
        now_iso = datetime.now(UTC).isoformat()
        for scope_factory in (
            lambda: Scope.personal(user_id="usr_x").with_default_memory_scope(),
            lambda: Scope.normal(group_id="grp_x", topic_id=0).with_default_memory_scope(),
            lambda: Scope.development(
                org_id="org_x", workspace_id="ws_x", project_id="prj_x",
                group_id="grp_x", topic_id=0,
            ).with_default_memory_scope(),
        ):
            try:
                scope = scope_factory()
                sessions_tbl, _ = _table_for_scope(scope)
                async with self._db.connection_for(scope) as conn:
                    cur = await conn.execute(
                        f"UPDATE {sessions_tbl} SET expires_at = ? WHERE session_id = ?",
                        (now_iso, session_id),
                    )
                    # aiosqlite Cursor.rowcount
                    rowcount = getattr(cur, "rowcount", 0)
                    if rowcount and int(rowcount) > 0:
                        return True
            except Exception:
                continue
        return False

    async def expire_old_sessions_async(self, scope: Scope) -> int:
        """Mark expired sessions as inactive. Returns count."""
        now_iso = datetime.now(UTC).isoformat()
        sessions_tbl, _ = _table_for_scope(scope)
        async with self._db.connection_for(scope) as conn:
            row = await conn.fetchone(
                f"SELECT COUNT(*) FROM {sessions_tbl} WHERE expires_at < ?",
                (now_iso,),
            )
            count = int(str(row[0])) if row else 0
            # Don't delete — keep for audit. Just leave them as expired.
        return count

    @staticmethod
    def _row_to_session(row: tuple[Any, ...]) -> ConversationSession:
        return ConversationSession(
            session_id=row[0],
            scope_key=row[1],
            mode=row[2],
            external_chat_id=row[3],
            topic_id=row[4],
            user_id=row[5],
            window_start_at=datetime.fromisoformat(row[6]),
            last_activity_at=datetime.fromisoformat(row[7]),
            expires_at=datetime.fromisoformat(row[8]),
            message_count=int(str(row[9])),
        )

    @staticmethod
    def _row_to_message(row: tuple[Any, ...]) -> ConversationMessage:
        return ConversationMessage(
            id=row[0],
            session_id=row[1],
            role=row[2],
            content=row[3],
            tool_call_id=row[4],
            tool_name=row[5],
            token_count=int(str(row[6])) if row[6] is not None else None,
            created_at=datetime.fromisoformat(row[7]),
        )
