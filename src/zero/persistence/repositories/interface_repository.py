"""Interface adapter repository — bindings, event log, callback tokens.

Per ``zero-interface-adapter-model`` §"Idempotency belongs at both
transport and domain boundaries": transport event IDs suppress duplicate
ingestion, while domain idempotency keys suppress duplicate transitions.

Per ``zero-project-isolation-evidence``: all queries filter by
``project_id`` before any row is loaded.
"""

from __future__ import annotations

import secrets
import sqlite3

from zero.domain.identity import ProjectId, UserId
from zero.domain.interfaces import (
    CallbackToken,
    CallbackTokenId,
    CallbackTokenNotFoundError,
    InterfaceBinding,
    InterfaceBindingId,
    InterfaceBindingNotFoundError,
    InterfaceDeliveryId,
    InterfaceEventId,
    InterfaceEventLogEntry,
    Platform,
    ResultDelivery,
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


def _row_to_delivery(row: sqlite3.Row) -> ResultDelivery:
    return ResultDelivery(
        id=InterfaceDeliveryId(row["id"]),
        project_id=ProjectId(row["project_id"]),
        execution_id=row["execution_id"],
        binding_id=InterfaceBindingId(row["binding_id"]),
        created_by=UserId(row["created_by"]),
        delivery_key=row["delivery_key"],
        content=row["content"],
        state=row["state"],  # type: ignore[arg-type]
        attempt_count=int(row["attempt_count"]),
        claim_token=row["claim_token"],
        lease_expires_at=row["lease_expires_at"],
        next_attempt_at=row["next_attempt_at"],
        external_message_id=row["external_message_id"],
        last_error=row["last_error"],
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
        resolved_user_id=UserId(row["resolved_user_id"]) if row["resolved_user_id"] else None,
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

    def insert_binding(self, binding: InterfaceBinding, *, commit: bool = True) -> None:
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

    def list_bindings_for_project(self, project_id: ProjectId) -> list[InterfaceBinding]:
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
        project_id: ProjectId | None = None,
        commit: bool = True,
    ) -> None:
        conn = self._database.connect()
        sql = (
            "UPDATE interface_bindings SET is_enabled = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE id = ?"
        )
        params: tuple[object, ...] = (1 if is_enabled else 0, binding_id.value)
        if project_id is not None:
            sql += " AND project_id = ?"
            params += (project_id.value,)
        cursor = conn.execute(sql, params)
        if cursor.rowcount == 0:
            raise InterfaceBindingNotFoundError(
                f"Binding {binding_id} not found"
                + (f" in project {project_id}" if project_id else "")
            )
        if commit:
            conn.commit()

    # ------------------------------------------------------------------
    # Durable result delivery queue
    # ------------------------------------------------------------------

    _DELIVERY_COLUMNS = (
        "id, project_id, execution_id, binding_id, created_by, delivery_key, "
        "content, state, attempt_count, claim_token, lease_expires_at, "
        "next_attempt_at, external_message_id, last_error, created_at, updated_at"
    )

    def insert_result_delivery(
        self, delivery: ResultDelivery, *, commit: bool = True
    ) -> ResultDelivery:
        conn = self._database.connect()
        conn.execute(
            "INSERT INTO result_deliveries "
            "(id, project_id, execution_id, binding_id, created_by, delivery_key, content) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(project_id, delivery_key) DO NOTHING",
            (
                delivery.id.value,
                delivery.project_id.value,
                delivery.execution_id,
                delivery.binding_id.value,
                delivery.created_by.value,
                delivery.delivery_key,
                delivery.content,
            ),
        )
        if commit:
            conn.commit()
        row = conn.execute(
            f"SELECT {self._DELIVERY_COLUMNS} FROM result_deliveries "
            "WHERE project_id = ? AND delivery_key = ?",
            (delivery.project_id.value, delivery.delivery_key),
        ).fetchone()
        if row is None:  # pragma: no cover - defensive database failure
            raise RuntimeError("result delivery was not persisted")
        return _row_to_delivery(row)

    def get_result_delivery(
        self, project_id: ProjectId, delivery_id: InterfaceDeliveryId
    ) -> ResultDelivery:
        conn = self._database.connect()
        row = conn.execute(
            f"SELECT {self._DELIVERY_COLUMNS} FROM result_deliveries "
            "WHERE project_id = ? AND id = ?",
            (project_id.value, delivery_id.value),
        ).fetchone()
        if row is None:
            raise KeyError(f"result delivery {delivery_id.value} not found")
        return _row_to_delivery(row)

    def list_result_deliveries(
        self, project_id: ProjectId, *, state: str | None = None
    ) -> list[ResultDelivery]:
        conn = self._database.connect()
        sql = f"SELECT {self._DELIVERY_COLUMNS} FROM result_deliveries WHERE project_id = ?"
        params: list[object] = [project_id.value]
        if state is not None:
            sql += " AND state = ?"
            params.append(state)
        sql += " ORDER BY created_at ASC, id ASC"
        return [_row_to_delivery(row) for row in conn.execute(sql, params).fetchall()]

    def claim_result_delivery(
        self,
        project_id: ProjectId,
        *,
        lease_seconds: int = 300,
        max_attempts: int = 5,
    ) -> ResultDelivery | None:
        if lease_seconds < 1 or max_attempts < 1:
            raise ValueError("delivery lease and attempt bounds must be positive")
        lease_modifier = f"+{lease_seconds} seconds"
        with self._database.transaction() as conn:
            conn.execute(
                "UPDATE result_deliveries SET state = 'unknown', claim_token = NULL, "
                "lease_expires_at = NULL, last_error = COALESCE(last_error, 'delivery lease expired'), "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE project_id = ? AND state = 'processing' AND "
                "lease_expires_at <= strftime('%Y-%m-%dT%H:%M:%fZ','now')",
                (project_id.value,),
            )
            row = conn.execute(
                f"SELECT {self._DELIVERY_COLUMNS} FROM result_deliveries "
                "WHERE project_id = ? AND attempt_count < ? AND "
                "state IN ('pending','failed') AND "
                "next_attempt_at <= strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "ORDER BY created_at ASC, id ASC LIMIT 1",
                (project_id.value, max_attempts),
            ).fetchone()
            if row is None:
                return None
            token = secrets.token_urlsafe(24)
            cursor = conn.execute(
                "UPDATE result_deliveries SET state = 'processing', "
                "attempt_count = attempt_count + 1, claim_token = ?, "
                "lease_expires_at = strftime('%Y-%m-%dT%H:%M:%fZ','now', ?), "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE project_id = ? AND id = ? AND attempt_count < ? AND "
                "state IN ('pending','failed') AND "
                "next_attempt_at <= strftime('%Y-%m-%dT%H:%M:%fZ','now')",
                (token, lease_modifier, project_id.value, row["id"], max_attempts),
            )
            if cursor.rowcount != 1:
                return None
            claimed = conn.execute(
                f"SELECT {self._DELIVERY_COLUMNS} FROM result_deliveries "
                "WHERE project_id = ? AND id = ?",
                (project_id.value, row["id"]),
            ).fetchone()
            return _row_to_delivery(claimed) if claimed is not None else None

    def complete_result_delivery(
        self,
        project_id: ProjectId,
        delivery_id: InterfaceDeliveryId,
        *,
        claim_token: str,
        external_message_id: str | None,
    ) -> bool:
        conn = self._database.connect()
        cursor = conn.execute(
            "UPDATE result_deliveries SET state = 'sent', claim_token = NULL, "
            "lease_expires_at = NULL, external_message_id = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE project_id = ? AND id = ? AND state = 'processing' AND claim_token = ?",
            (external_message_id, project_id.value, delivery_id.value, claim_token),
        )
        conn.commit()
        return cursor.rowcount == 1

    def fail_result_delivery(
        self,
        project_id: ProjectId,
        delivery_id: InterfaceDeliveryId,
        *,
        claim_token: str,
        error: str,
        retry_after_seconds: int = 30,
    ) -> bool:
        if retry_after_seconds < 0 or retry_after_seconds > 86_400:
            raise ValueError("retry_after_seconds must be between 0 and 86400")
        conn = self._database.connect()
        cursor = conn.execute(
            "UPDATE result_deliveries SET state = 'failed', claim_token = NULL, "
            "lease_expires_at = NULL, last_error = ?, "
            "next_attempt_at = strftime('%Y-%m-%dT%H:%M:%fZ','now', ?), "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE project_id = ? AND id = ? AND state = 'processing' AND claim_token = ?",
            (
                error[:2000],
                f"+{retry_after_seconds} seconds",
                project_id.value,
                delivery_id.value,
                claim_token,
            ),
        )
        conn.commit()
        return cursor.rowcount == 1

    def mark_result_delivery_unknown(
        self,
        project_id: ProjectId,
        delivery_id: InterfaceDeliveryId,
        *,
        claim_token: str,
        error: str,
    ) -> bool:
        conn = self._database.connect()
        cursor = conn.execute(
            "UPDATE result_deliveries SET state = 'unknown', claim_token = NULL, "
            "lease_expires_at = NULL, last_error = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE project_id = ? AND id = ? AND state = 'processing' AND claim_token = ?",
            (error[:2000], project_id.value, delivery_id.value, claim_token),
        )
        conn.commit()
        return cursor.rowcount == 1

    def recover_result_deliveries(self, project_id: ProjectId | None = None) -> int:
        conn = self._database.connect()
        sql = (
            "UPDATE result_deliveries SET state = 'unknown', claim_token = NULL, "
            "lease_expires_at = NULL, last_error = COALESCE(last_error, 'delivery lease expired'), "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE state = 'processing' AND lease_expires_at <= strftime('%Y-%m-%dT%H:%M:%fZ','now')"
        )
        params: tuple[object, ...] = ()
        if project_id is not None:
            sql += " AND project_id = ?"
            params = (project_id.value,)
        cursor = conn.execute(sql, params)
        conn.commit()
        return cursor.rowcount

    # ------------------------------------------------------------------
    # Durable polling cursors and event claims
    # ------------------------------------------------------------------

    def get_cursor(self, platform: Platform, scope_key: str) -> str | None:
        conn = self._database.connect()
        row = conn.execute(
            "SELECT cursor FROM interface_cursors WHERE platform = ? AND scope_key = ?",
            (platform, str(scope_key)),
        ).fetchone()
        return str(row[0]) if row else None

    def set_cursor(
        self, platform: Platform, scope_key: str, cursor: str, *, commit: bool = True
    ) -> None:
        conn = self._database.connect()
        conn.execute(
            "INSERT INTO interface_cursors (platform, scope_key, cursor) VALUES (?, ?, ?) "
            "ON CONFLICT(platform, scope_key) DO UPDATE SET cursor = excluded.cursor, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')",
            (platform, str(scope_key), str(cursor)),
        )
        if commit:
            conn.commit()

    def advance_cursor(
        self, platform: Platform, scope_key: str, cursor: str, *, commit: bool = True
    ) -> None:
        """Advance a numeric provider cursor without allowing regression."""
        current = self.get_cursor(platform, scope_key)
        if current is not None:
            try:
                if int(cursor) <= int(current):
                    return
            except ValueError:
                if cursor <= current:
                    return
        self.set_cursor(platform, scope_key, str(cursor), commit=commit)

    def claim_event(
        self,
        platform: Platform,
        external_event_id: str,
        *,
        binding_scope: str = "",
        binding_id: str | None = None,
        lease_seconds: int = 300,
        commit: bool = True,
    ) -> bool:
        """Atomically claim an event, returning a compatibility boolean."""
        return (
            self.claim_event_with_token(
                platform,
                external_event_id,
                binding_scope=binding_scope,
                binding_id=binding_id,
                lease_seconds=lease_seconds,
                commit=commit,
            )
            is not None
        )

    def claim_event_with_token(
        self,
        platform: Platform,
        external_event_id: str,
        *,
        binding_scope: str = "",
        binding_id: str | None = None,
        lease_seconds: int = 300,
        commit: bool = True,
    ) -> str | None:
        """Claim an event and return the lease-generation token."""
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        conn = self._database.connect()
        lease_modifier = f"+{lease_seconds} seconds"
        claim_token = secrets.token_urlsafe(24)
        cursor = conn.execute(
            "INSERT INTO interface_event_claims "
            "(platform, binding_scope, binding_id, external_event_id, state, "
            "attempt_count, lease_expires_at, claim_token) VALUES (?, ?, ?, ?, 'processing', 1, "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now', ?), ?) "
            "ON CONFLICT(platform, binding_scope, external_event_id) DO NOTHING",
            (
                platform,
                binding_scope,
                binding_id,
                external_event_id,
                lease_modifier,
                claim_token,
            ),
        )
        claimed = cursor.rowcount > 0
        if not claimed:
            cursor = conn.execute(
                "UPDATE interface_event_claims SET state = 'processing', "
                "attempt_count = attempt_count + 1, "
                "claimed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
                "lease_expires_at = strftime('%Y-%m-%dT%H:%M:%fZ','now', ?), "
                "completed_at = NULL, binding_id = COALESCE(binding_id, ?), "
                "claim_token = ? "
                "WHERE platform = ? AND binding_scope = ? AND external_event_id = ? "
                "AND (state = 'failed' OR "
                "(state = 'processing' AND lease_expires_at <= strftime('%Y-%m-%dT%H:%M:%fZ','now')))",
                (
                    lease_modifier,
                    binding_id,
                    claim_token,
                    platform,
                    binding_scope,
                    external_event_id,
                ),
            )
            claimed = cursor.rowcount > 0
        if commit:
            conn.commit()
        return claim_token if claimed else None

    def complete_event_claim(
        self,
        platform: Platform,
        external_event_id: str,
        *,
        binding_scope: str = "",
        claim_token: str | None = None,
        commit: bool = True,
    ) -> bool:
        conn = self._database.connect()
        cursor = conn.execute(
            "UPDATE interface_event_claims SET state = 'succeeded', "
            "lease_expires_at = claimed_at, "
            "completed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE platform = ? AND binding_scope = ? AND external_event_id = ? "
            "AND state = 'processing' AND (? IS NULL OR claim_token = ?)",
            (
                platform,
                binding_scope,
                external_event_id,
                claim_token,
                claim_token,
            ),
        )
        if commit:
            conn.commit()
        return cursor.rowcount > 0

    def fail_event_claim(
        self,
        platform: Platform,
        external_event_id: str,
        *,
        binding_scope: str = "",
        claim_token: str | None = None,
        commit: bool = True,
    ) -> bool:
        conn = self._database.connect()
        cursor = conn.execute(
            "UPDATE interface_event_claims SET state = 'failed', "
            "lease_expires_at = claimed_at, "
            "completed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE platform = ? AND binding_scope = ? AND external_event_id = ? "
            "AND state = 'processing' AND (? IS NULL OR claim_token = ?)",
            (
                platform,
                binding_scope,
                external_event_id,
                claim_token,
                claim_token,
            ),
        )
        if commit:
            conn.commit()
        return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Event log (idempotent processing)
    # ------------------------------------------------------------------

    def insert_event_log(
        self,
        entry: InterfaceEventLogEntry,
        *,
        binding_scope: str = "",
        binding_id: str | None = None,
        commit: bool = True,
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
                "(id, project_id, platform, binding_scope, binding_id, external_event_id, "
                "external_actor_id, resolved_user_id, chat_id, topic_id, "
                "event_kind, event_content, processing_result, "
                "processing_detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.id.value,
                    entry.project_id.value if entry.project_id else None,
                    entry.platform,
                    binding_scope,
                    binding_id,
                    entry.external_event_id,
                    entry.external_actor_id,
                    entry.resolved_user_id.value if entry.resolved_user_id else None,
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
        self,
        platform: Platform,
        external_event_id: str,
        *,
        binding_scope: str = "",
    ) -> bool:
        """Check if an event was processed in the specified interface scope."""
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT 1 FROM interface_event_log "
            "WHERE platform = ? AND binding_scope = ? AND external_event_id = ?",
            (platform, binding_scope, external_event_id),
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

    def insert_callback_token(self, token: CallbackToken, *, commit: bool = True) -> None:
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

    def get_callback_token(self, token_id: CallbackTokenId) -> CallbackToken:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, plan_id, revision_number, action, "
            "expires_at, used_at, created_by, created_at "
            "FROM callback_tokens WHERE id = ?",
            (token_id.value,),
        )
        row = cursor.fetchone()
        if row is None:
            raise CallbackTokenNotFoundError(f"Callback token {token_id} not found")
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
            "UPDATE callback_tokens SET used_at = ? WHERE id = ? AND used_at IS NULL",
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
