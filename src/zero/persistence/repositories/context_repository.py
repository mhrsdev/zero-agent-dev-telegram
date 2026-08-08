"""Context and compaction repository — context versions, injection ledger,
compaction records.

Per ``zero-context-memory`` §"Replace context only after durable
commit": the safe commit order is:
1. canonical memory transaction committed;
2. transcript/segment artifact stored and verified;
3. typed execution snapshot stored;
4. compact summary validated;
5. provider-ready history validated;
6. active context pointer advanced atomically;
7. audit event recorded.
"""

from __future__ import annotations

import json
import sqlite3

from zero.domain.artifacts import ArtifactId
from zero.domain.context import (
    CompactionRecord,
    CompactionRecordId,
    CompactionState,
    ContextVersion,
    ContextVersionId,
    ContextVersionNotFoundError,
    InjectionLedger,
    InjectionLedgerId,
)
from zero.domain.execution import ExecutionId
from zero.domain.identity import ProjectId
from zero.persistence.connection import Database


def _row_to_context_version(row: sqlite3.Row) -> ContextVersion:
    return ContextVersion(
        id=ContextVersionId(row["id"]),
        project_id=ProjectId(row["project_id"]),
        execution_id=ExecutionId(row["execution_id"]),
        version=row["version"],
        active=bool(row["active"]),
        system_message=row["system_message"],
        user_prefix=row["user_prefix"],
        plan_contract=row["plan_contract"],
        execution_snapshot=row["execution_snapshot"],
        retrieved_context=row["retrieved_context"],
        conversation_tail=row["conversation_tail"],
        compaction_summary=row["compaction_summary"],
        transcript_artifact_id=ArtifactId(row["transcript_artifact_id"])
        if row["transcript_artifact_id"]
        else None,
        token_count=row["token_count"],
        created_at=row["created_at"],
    )


def _row_to_injection_ledger(row: sqlite3.Row) -> InjectionLedger:
    selected = json.loads(row["selected"]) if row["selected"] else []
    omitted = json.loads(row["omitted"]) if row["omitted"] else []
    return InjectionLedger(
        id=InjectionLedgerId(row["id"]),
        project_id=ProjectId(row["project_id"]),
        execution_id=ExecutionId(row["execution_id"]),
        context_version=row["context_version"],
        selected=tuple(
            (s["source"], s["record_id"], s["token_count"]) for s in selected
        ),
        omitted=tuple(
            (o["source"], o["record_id"], o["reason"]) for o in omitted
        ),
        total_candidates=row["total_candidates"],
        total_tokens=row["total_tokens"],
        budget_tokens=row["budget_tokens"],
        created_at=row["created_at"],
    )


def _row_to_compaction_record(row: sqlite3.Row) -> CompactionRecord:
    return CompactionRecord(
        id=CompactionRecordId(row["id"]),
        project_id=ProjectId(row["project_id"]),
        execution_id=ExecutionId(row["execution_id"]),
        source_context_version=row["source_context_version"],
        target_context_version=row["target_context_version"],
        source_event_range=row["source_event_range"],
        memory_delta_artifact_id=ArtifactId(
            row["memory_delta_artifact_id"]
        )
        if row["memory_delta_artifact_id"]
        else None,
        transcript_artifact_id=ArtifactId(
            row["transcript_artifact_id"]
        )
        if row["transcript_artifact_id"]
        else None,
        summary=row["summary"],
        fit_rung=row["fit_rung"],
        state=row["state"],  # type: ignore[arg-type]
        no_thrash_count=row["no_thrash_count"],
        created_at=row["created_at"],
    )


class ContextRepository:
    """Database-backed context version, injection ledger, and compaction
    record repository."""

    def __init__(self, database: Database) -> None:
        self._database = database

    # ------------------------------------------------------------------
    # Context versions
    # ------------------------------------------------------------------

    def insert_context_version(
        self,
        cv: ContextVersion,
        *,
        commit: bool = True,
    ) -> None:
        conn = self._database.connect()
        try:
            conn.execute(
                "INSERT INTO context_versions "
                "(id, project_id, execution_id, version, active, "
                "system_message, user_prefix, plan_contract, "
                "execution_snapshot, retrieved_context, conversation_tail, "
                "compaction_summary, transcript_artifact_id, token_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    cv.id.value,
                    cv.project_id.value,
                    cv.execution_id.value,
                    cv.version,
                    1 if cv.active else 0,
                    cv.system_message,
                    cv.user_prefix,
                    cv.plan_contract,
                    cv.execution_snapshot,
                    cv.retrieved_context,
                    cv.conversation_tail,
                    cv.compaction_summary,
                    cv.transcript_artifact_id.value
                    if cv.transcript_artifact_id
                    else None,
                    cv.token_count,
                ),
            )
            if commit:
                conn.commit()
        except sqlite3.IntegrityError:
            if commit:
                conn.rollback()
            raise

    def get_context_version(
        self,
        execution_id: ExecutionId,
        version: int,
    ) -> ContextVersion:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, execution_id, version, active, "
            "system_message, user_prefix, plan_contract, "
            "execution_snapshot, retrieved_context, conversation_tail, "
            "compaction_summary, transcript_artifact_id, token_count, "
            "created_at FROM context_versions "
            "WHERE execution_id = ? AND version = ?",
            (execution_id.value, version),
        )
        row = cursor.fetchone()
        if row is None:
            raise ContextVersionNotFoundError(
                f"Context version {version} not found for execution "
                f"{execution_id}"
            )
        return _row_to_context_version(row)

    def get_active_context_version(
        self, execution_id: ExecutionId
    ) -> ContextVersion | None:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, execution_id, version, active, "
            "system_message, user_prefix, plan_contract, "
            "execution_snapshot, retrieved_context, conversation_tail, "
            "compaction_summary, transcript_artifact_id, token_count, "
            "created_at FROM context_versions "
            "WHERE execution_id = ? AND active = 1 LIMIT 1",
            (execution_id.value,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return _row_to_context_version(row)

    def get_latest_context_version(
        self, execution_id: ExecutionId
    ) -> ContextVersion | None:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, execution_id, version, active, "
            "system_message, user_prefix, plan_contract, "
            "execution_snapshot, retrieved_context, conversation_tail, "
            "compaction_summary, transcript_artifact_id, token_count, "
            "created_at FROM context_versions "
            "WHERE execution_id = ? ORDER BY version DESC LIMIT 1",
            (execution_id.value,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return _row_to_context_version(row)

    def list_context_versions(
        self, execution_id: ExecutionId
    ) -> list[ContextVersion]:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, execution_id, version, active, "
            "system_message, user_prefix, plan_contract, "
            "execution_snapshot, retrieved_context, conversation_tail, "
            "compaction_summary, transcript_artifact_id, token_count, "
            "created_at FROM context_versions "
            "WHERE execution_id = ? ORDER BY version ASC",
            (execution_id.value,),
        )
        return [_row_to_context_version(row) for row in cursor.fetchall()]

    def deactivate_all_context_versions(
        self,
        execution_id: ExecutionId,
        *,
        commit: bool = True,
    ) -> None:
        """Set active=0 on all context versions for an execution."""
        conn = self._database.connect()
        conn.execute(
            "UPDATE context_versions SET active = 0 "
            "WHERE execution_id = ?",
            (execution_id.value,),
        )
        if commit:
            conn.commit()

    def activate_context_version(
        self,
        execution_id: ExecutionId,
        version: int,
        *,
        commit: bool = True,
    ) -> None:
        """Deactivate all versions, then activate the given version.

        Per ``zero-context-memory`` §"Replace context only after durable
        commit": the active context pointer is advanced atomically.
        """
        conn = self._database.connect()
        conn.execute(
            "UPDATE context_versions SET active = 0 "
            "WHERE execution_id = ?",
            (execution_id.value,),
        )
        cursor = conn.execute(
            "UPDATE context_versions SET active = 1 "
            "WHERE execution_id = ? AND version = ?",
            (execution_id.value, version),
        )
        if cursor.rowcount == 0:
            raise ContextVersionNotFoundError(
                f"Context version {version} not found for execution "
                f"{execution_id}"
            )
        if commit:
            conn.commit()

    def count_context_versions(
        self, execution_id: ExecutionId
    ) -> int:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT COUNT(*) FROM context_versions WHERE execution_id = ?",
            (execution_id.value,),
        )
        return int(cursor.fetchone()[0])

    # ------------------------------------------------------------------
    # Injection ledger
    # ------------------------------------------------------------------

    def insert_injection_ledger(
        self,
        ledger: InjectionLedger,
        *,
        commit: bool = True,
    ) -> None:
        conn = self._database.connect()
        selected_json = json.dumps(
            [
                {"source": s, "record_id": r, "token_count": t}
                for s, r, t in ledger.selected
            ]
        )
        omitted_json = json.dumps(
            [
                {"source": s, "record_id": r, "reason": rea}
                for s, r, rea in ledger.omitted
            ]
        )
        conn.execute(
            "INSERT INTO context_injection_ledger "
            "(id, project_id, execution_id, context_version, selected, "
            "omitted, total_candidates, total_tokens, budget_tokens) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ledger.id.value,
                ledger.project_id.value,
                ledger.execution_id.value,
                ledger.context_version,
                selected_json,
                omitted_json,
                ledger.total_candidates,
                ledger.total_tokens,
                ledger.budget_tokens,
            ),
        )
        if commit:
            conn.commit()

    def get_injection_ledger(
        self,
        execution_id: ExecutionId,
        context_version: int,
    ) -> InjectionLedger | None:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, execution_id, context_version, "
            "selected, omitted, total_candidates, total_tokens, "
            "budget_tokens, created_at FROM context_injection_ledger "
            "WHERE execution_id = ? AND context_version = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (execution_id.value, context_version),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return _row_to_injection_ledger(row)

    # ------------------------------------------------------------------
    # Compaction records
    # ------------------------------------------------------------------

    def insert_compaction_record(
        self,
        record: CompactionRecord,
        *,
        commit: bool = True,
    ) -> None:
        conn = self._database.connect()
        try:
            conn.execute(
                "INSERT INTO compaction_records "
                "(id, project_id, execution_id, source_context_version, "
                "target_context_version, source_event_range, "
                "memory_delta_artifact_id, transcript_artifact_id, "
                "summary, fit_rung, state, no_thrash_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.id.value,
                    record.project_id.value,
                    record.execution_id.value,
                    record.source_context_version,
                    record.target_context_version,
                    record.source_event_range,
                    record.memory_delta_artifact_id.value
                    if record.memory_delta_artifact_id
                    else None,
                    record.transcript_artifact_id.value
                    if record.transcript_artifact_id
                    else None,
                    record.summary,
                    record.fit_rung,
                    record.state,
                    record.no_thrash_count,
                ),
            )
            if commit:
                conn.commit()
        except sqlite3.IntegrityError:
            if commit:
                conn.rollback()
            raise

    def update_compaction_state(
        self,
        record_id: CompactionRecordId,
        new_state: CompactionState,
        *,
        commit: bool = True,
    ) -> None:
        conn = self._database.connect()
        cursor = conn.execute(
            "UPDATE compaction_records SET state = ? WHERE id = ?",
            (new_state, record_id.value),
        )
        if cursor.rowcount == 0:
            raise ContextVersionNotFoundError(
                f"Compaction record {record_id} not found"
            )
        if commit:
            conn.commit()

    def list_compaction_records(
        self, execution_id: ExecutionId
    ) -> list[CompactionRecord]:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, execution_id, source_context_version, "
            "target_context_version, source_event_range, "
            "memory_delta_artifact_id, transcript_artifact_id, "
            "summary, fit_rung, state, no_thrash_count, created_at "
            "FROM compaction_records WHERE execution_id = ? "
            "ORDER BY created_at ASC",
            (execution_id.value,),
        )
        return [_row_to_compaction_record(row) for row in cursor.fetchall()]

    def get_latest_compaction_record(
        self, execution_id: ExecutionId
    ) -> CompactionRecord | None:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, execution_id, source_context_version, "
            "target_context_version, source_event_range, "
            "memory_delta_artifact_id, transcript_artifact_id, "
            "summary, fit_rung, state, no_thrash_count, created_at "
            "FROM compaction_records WHERE execution_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (execution_id.value,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return _row_to_compaction_record(row)
