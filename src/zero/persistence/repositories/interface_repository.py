"""Interface adapter repository — bindings, event log, callback tokens.

Per ``zero-interface-adapter-model`` §"Idempotency belongs at both
transport and domain boundaries": transport event IDs suppress duplicate
ingestion, while domain idempotency keys suppress duplicate transitions.

Per ``zero-project-isolation-evidence``: all queries filter by
``project_id`` before any row is loaded.
"""

from __future__ import annotations

import sqlite3

from zero.domain.identity import ProjectId, UserId
from zero.domain.interfaces import (
    CallbackToken,
    CallbackTokenId,
    CallbackTokenNotFoundError,
    InterfaceBinding,
    InterfaceBindingId,
    InterfaceBindingNotFoundError,
    InterfaceEventId,
    InterfaceEventLogEntry,
    Platform,
)
from zero.domain.plans import PlanId
from zero.persistence.connection import Database


def _row_to_binding(row: sqlite3.Row) -> InterfaceBinding:
    return InterfaceBinding(
        id=InterfaceBindingId(row["id"]),
        project_id=ProjectId(row["project_id"]),
        platform=row["platform"],  # type: ignore[arg-type]
        bot_token_ref=row["bot_token_ref"],
        chat_id=row["chat_id"],
        topic_id=row["topic_id"],
        is_enabled=bool(row["is_enabled"]),
        created_by=UserId(row["created_by"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_event_log(row: sqlite3.Row) -> InterfaceEventLogEntry:
    return InterfaceEventLogEntry(
        id=InterfaceEventId(row["id"]),
        project_id=ProjectId(row["project_id"]) if row["project_id"] else None,
        platform=row["platform"],  # type: ignore[arg-type]
        external_event_id=row["external_event_id"],
        external_actor_id=row["external_actor_id"],
        resolved_user_id=UserId(row["resolved_user_id"])
        if row["resolved_user_id"]
        else None,
        chat_id=row["chat_id"],
        topic_id=row["topic_id"],
        event_kind=row["event_kind"],  # type: ignore[arg-type]
        event_content=row["event_content"],
        processing_result=row["processing_result"],  # type: ignore[arg-type]
        processing_detail=row["processing_detail"],
        created_at=row["created_at"],
    )


def _row_to_callback_token(row: sqlite3.Row) -> CallbackToken:
    return CallbackToken(
        id=CallbackTokenId(row["id"]),
        project_id=ProjectId(row["project_id"]),
        plan_id=PlanId(row["plan_id"]),
        revision_number=row["revision_number"],
        action=row["action"],  # type: ignore[arg-type]
        expires_at=row["expires_at"],
        used_at=row["used_at"],
        created_by=UserId(row["created_by"]) if row["created_by"] else None,
        created_at=row["created_at"],
    )


class InterfaceRepository:
    """Database-backed interface binding, event log, and callback token
    repository."""

    def __init__(self, database: Database) -> None:
        self._database = database

    # ------------------------------------------------------------------
    # Interface bindings
    # ------------------------------------------------------------------

    def insert_binding(
        self, binding: InterfaceBinding, *, commit: bool = True
    ) -> None:
        conn = self._database.connect()
        try:
            conn.execute(
                "INSERT INTO interface_bindings "
                "(id, project_id, platform, bot_token_ref, chat_id, "
                "topic_id, is_enabled, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    binding.id.value,
                    binding.project_id.value,
                    binding.platform,
                    binding.bot_token_ref,
                    binding.chat_id,
                    binding.topic_id,
                    1 if binding.is_enabled else 0,
                    binding.created_by.value,
                ),
            )
            if commit:
                conn.commit()
        except sqlite3.IntegrityError:
            if commit:
                conn.rollback()
            raise

    def get_binding(
        self,
        platform: Platform,
        chat_id: str,
        topic_id: str | None,
    ) -> InterfaceBinding | None:
        """Look up a binding by (platform, chat_id, topic_id).

        Returns None if no binding exists.
        """
        conn = self._database.connect()
        if topic_id is not None:
            cursor = conn.execute(
                "SELECT id, project_id, platform, bot_token_ref, chat_id, "
                "topic_id, is_enabled, created_by, created_at, updated_at "
                "FROM interface_bindings "
                "WHERE platform = ? AND chat_id = ? AND topic_id = ?",
                (platform, chat_id, topic_id),
            )
        else:
            cursor = conn.execute(
                "SELECT id, project_id, platform, bot_token_ref, chat_id, "
                "topic_id, is_enabled, created_by, created_at, updated_at "
                "FROM interface_bindings "
                "WHERE platform = ? AND chat_id = ? AND topic_id IS NULL",
                (platform, chat_id),
            )
        row = cursor.fetchone()
        if row is None:
            return None
        return _row_to_binding(row)

    def get_binding_by_id(
        self, project_id: ProjectId, binding_id: InterfaceBindingId
    ) -> InterfaceBinding:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, platform, bot_token_ref, chat_id, "
            "topic_id, is_enabled, created_by, created_at, updated_at "
            "FROM interface_bindings "
            "WHERE id = ? AND project_id = ?",
            (binding_id.value, project_id.value),
        )
        row = cursor.fetchone()
        if row is None:
            raise InterfaceBindingNotFoundError(
                f"Binding {binding_id} not found in project {project_id}"
            )
        return _row_to_binding(row)

    def list_bindings_for_project(
        self, project_id: ProjectId
    ) -> list[InterfaceBinding]:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, platform, bot_token_ref, chat_id, "
            "topic_id, is_enabled, created_by, created_at, updated_at "
            "FROM interface_bindings WHERE project_id = ? "
            "ORDER BY created_at ASC",
            (project_id.value,),
        )
        return [_row_to_binding(row) for row in cursor.fetchall()]

    def update_binding_enabled(
        self,
        binding_id: InterfaceBindingId,
        is_enabled: bool,
        *,
        commit: bool = True,
    ) -> None:
        conn = self._database.connect()
        cursor = conn.execute(
            "UPDATE interface_bindings SET is_enabled = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE id = ?",
            (1 if is_enabled else 0, binding_id.value),
        )
        if cursor.rowcount == 0:
            raise InterfaceBindingNotFoundError(
                f"Binding {binding_id} not found"
            )
        if commit:
            conn.commit()

    # ------------------------------------------------------------------
    # Event log (idempotent processing)
    # ------------------------------------------------------------------

    def insert_event_log(
        self, entry: InterfaceEventLogEntry, *, commit: bool = True
    ) -> bool:
        """Insert an event log entry. Returns True if inserted, False
        if duplicate (idempotent deduplication).

        Per ``zero-interface-adapter-model`` §"Idempotency belongs at
        both transport and domain boundaries": the UNIQUE(platform,
        external_event_id) constraint ensures duplicate delivery is
        a no-op.
        """
        conn = self._database.connect()
        try:
            conn.execute(
                "INSERT INTO interface_event_log "
                "(id, project_id, platform, external_event_id, "
                "external_actor_id, resolved_user_id, chat_id, topic_id, "
                "event_kind, event_content, processing_result, "
                "processing_detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.id.value,
                    entry.project_id.value if entry.project_id else None,
                    entry.platform,
                    entry.external_event_id,
                    entry.external_actor_id,
                    entry.resolved_user_id.value
                    if entry.resolved_user_id
                    else None,
                    entry.chat_id,
                    entry.topic_id,
                    entry.event_kind,
                    entry.event_content,
                    entry.processing_result,
                    entry.processing_detail,
                ),
            )
            if commit:
                conn.commit()
            return True
        except sqlite3.IntegrityError as exc:
            if commit:
                conn.rollback()
            if "UNIQUE" in str(exc):
                return False  # duplicate event
            raise

    def event_already_processed(
        self, platform: Platform, external_event_id: str
    ) -> bool:
        """Check if an event has already been processed."""
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT 1 FROM interface_event_log "
            "WHERE platform = ? AND external_event_id = ?",
            (platform, external_event_id),
        )
        return cursor.fetchone() is not None

    def list_event_log_for_project(
        self,
        project_id: ProjectId,
        *,
        limit: int = 100,
    ) -> list[InterfaceEventLogEntry]:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, platform, external_event_id, "
            "external_actor_id, resolved_user_id, chat_id, topic_id, "
            "event_kind, event_content, processing_result, "
            "processing_detail, created_at "
            "FROM interface_event_log WHERE project_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (project_id.value, limit),
        )
        return [_row_to_event_log(row) for row in cursor.fetchall()]

    # ------------------------------------------------------------------
    # Callback tokens
    # ------------------------------------------------------------------

    def insert_callback_token(
        self, token: CallbackToken, *, commit: bool = True
    ) -> None:
        conn = self._database.connect()
        try:
            conn.execute(
                "INSERT INTO callback_tokens "
                "(id, project_id, plan_id, revision_number, action, "
                "expires_at, used_at, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    token.id.value,
                    token.project_id.value,
                    token.plan_id.value,
                    token.revision_number,
                    token.action,
                    token.expires_at,
                    token.used_at,
                    token.created_by.value if token.created_by else None,
                ),
            )
            if commit:
                conn.commit()
        except sqlite3.IntegrityError:
            if commit:
                conn.rollback()
            raise

    def get_callback_token(
        self, token_id: CallbackTokenId
    ) -> CallbackToken:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, plan_id, revision_number, action, "
            "expires_at, used_at, created_by, created_at "
            "FROM callback_tokens WHERE id = ?",
            (token_id.value,),
        )
        row = cursor.fetchone()
        if row is None:
            raise CallbackTokenNotFoundError(
                f"Callback token {token_id} not found"
            )
        return _row_to_callback_token(row)

    def mark_callback_token_used(
        self,
        token_id: CallbackTokenId,
        used_at: str,
        *,
        commit: bool = True,
    ) -> bool:
        """Mark a callback token as used. Returns True if updated,
        False if already used (idempotent).

        Per ``zero-interface-adapter-model`` §"Fast acknowledgement and
        durable processing are different outcomes": acknowledging the
        callback does not mean the plan was approved.
        """
        conn = self._database.connect()
        cursor = conn.execute(
            "UPDATE callback_tokens SET used_at = ? "
            "WHERE id = ? AND used_at IS NULL",
            (used_at, token_id.value),
        )
        if commit:
            conn.commit()
        return cursor.rowcount > 0

    def list_callback_tokens_for_plan(
        self,
        project_id: ProjectId,
        plan_id: PlanId,
    ) -> list[CallbackToken]:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, plan_id, revision_number, action, "
            "expires_at, used_at, created_by, created_at "
            "FROM callback_tokens "
            "WHERE project_id = ? AND plan_id = ? "
            "ORDER BY created_at DESC",
            (project_id.value, plan_id.value),
        )
        return [_row_to_callback_token(row) for row in cursor.fetchall()]
