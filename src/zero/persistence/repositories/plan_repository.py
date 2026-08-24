"""Plan repository — plans, revisions, approvals, handoffs, conversation events.

Per ``zero-planner-worker-contract`` §"Plans are versioned proposals":
plans have identity, revision, state, provenance, and explicit
transitions. Editing produces a new revision; it does not retroactively
change what was approved.

Per ``zero-control-plane-trust`` §"Atomicity follows the business
fact": approval + audit evidence + handoff record should become durable
together. The repository exposes ``approve_revision`` which performs
the approval, the state transition, and the handoff creation in one
transaction.
"""

from __future__ import annotations

import json
import sqlite3

from zero.domain.identity import ProjectId, UserId
from zero.domain.plans import (
    ApprovalResult,
    ConversationEvent,
    ConversationEventId,
    ConversationEventNotFoundError,
    DuplicateConversationEventError,
    Plan,
    PlanApproval,
    PlanApprovalId,
    PlanHandoff,
    PlanHandoffId,
    PlanId,
    PlanNotFoundError,
    PlanRevision,
    PlanRevisionContent,
    PlanRevisionId,
    PlanRevisionNotFoundError,
    PlanState,
    RevisionState,
)
from zero.persistence.connection import Database


def _row_to_plan(row: sqlite3.Row) -> Plan:
    return Plan(
        id=PlanId(row["id"]),
        project_id=ProjectId(row["project_id"]),
        current_state=row["current_state"],  # type: ignore[arg-type]
        current_revision_number=row["current_revision_number"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_revision(row: sqlite3.Row) -> PlanRevision:
    content = PlanRevisionContent(
        objective=row["objective"],
        scope=tuple(json.loads(row["scope"])),
        constraints=tuple(json.loads(row["constraints"])),
        acceptance_criteria=tuple(json.loads(row["acceptance_criteria"])),
        risks=tuple(json.loads(row["risks"])),
        unresolved_questions=tuple(json.loads(row["unresolved_questions"])),
        source_event_ids=tuple(
            ConversationEventId(eid) for eid in json.loads(row["source_event_ids"])
        ),
    )
    return PlanRevision(
        id=PlanRevisionId(row["id"]),
        plan_id=PlanId(row["plan_id"]),
        project_id=ProjectId(row["project_id"]),
        revision_number=row["revision_number"],
        content=content,
        proposed_by=UserId(row["proposed_by"]),
        state=row["state"],  # type: ignore[arg-type]
        created_at=row["created_at"],
    )


def _row_to_approval(row: sqlite3.Row) -> PlanApproval:
    return PlanApproval(
        id=PlanApprovalId(row["id"]),
        plan_id=PlanId(row["plan_id"]),
        revision_id=PlanRevisionId(row["revision_id"]),
        project_id=ProjectId(row["project_id"]),
        approved_by=UserId(row["approved_by"]),
        source=row["source"],  # type: ignore[arg-type]
        result=row["result"],  # type: ignore[arg-type]
        idempotency_key=row["idempotency_key"],
        redacted_reason=row["redacted_reason"],
        created_at=row["created_at"],
    )


def _row_to_handoff(row: sqlite3.Row) -> PlanHandoff:
    return PlanHandoff(
        id=PlanHandoffId(row["id"]),
        plan_id=PlanId(row["plan_id"]),
        revision_id=PlanRevisionId(row["revision_id"]),
        project_id=ProjectId(row["project_id"]),
        approved_by=UserId(row["approved_by"]),
        execution_id=row["execution_id"],
        created_at=row["created_at"],
    )


def _row_to_conversation_event(row: sqlite3.Row) -> ConversationEvent:
    return ConversationEvent(
        id=ConversationEventId(row["id"]),
        project_id=ProjectId(row["project_id"]),
        actor_id=UserId(row["actor_id"]),
        source=row["source"],  # type: ignore[arg-type]
        external_event_id=row["external_event_id"],
        origin_kind=row["origin_kind"],  # type: ignore[arg-type]
        content=row["content"],
        created_at=row["created_at"],
    )


class PlanRepository:
    """Database-backed plan, revision, approval, handoff, and
    conversation-event repository."""

    def __init__(self, database: Database) -> None:
        self._database = database

    # ------------------------------------------------------------------
    # Conversation events
    # ------------------------------------------------------------------

    def insert_conversation_event(self, event: ConversationEvent, *, commit: bool = True) -> None:
        """Insert a conversation event.

        Per PLAN.md M4: "Duplicate delivery is idempotent." The
        UNIQUE(source, external_event_id) constraint makes duplicate
        delivery a no-op; we raise
        :class:`DuplicateConversationEventError` so the caller knows
        the event was already processed.
        """
        conn = self._database.connect()
        try:
            conn.execute(
                "INSERT INTO conversation_events "
                "(id, project_id, actor_id, source, external_event_id, "
                "origin_kind, content) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    event.id.value,
                    event.project_id.value,
                    event.actor_id.value,
                    event.source,
                    event.external_event_id,
                    event.origin_kind,
                    event.content,
                ),
            )
            if commit:
                conn.commit()
        except sqlite3.IntegrityError as exc:
            if commit:
                conn.rollback()
            if "UNIQUE" in str(exc) and "external_event_id" in str(exc):
                raise DuplicateConversationEventError(
                    f"Conversation event with source={event.source} "
                    f"external_event_id={event.external_event_id} already exists"
                ) from exc
            raise

    def get_conversation_event(self, event_id: ConversationEventId) -> ConversationEvent:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, actor_id, source, external_event_id, "
            "origin_kind, content, created_at FROM conversation_events "
            "WHERE id = ?",
            (event_id.value,),
        )
        row = cursor.fetchone()
        if row is None:
            raise ConversationEventNotFoundError(f"Conversation event {event_id} not found")
        return _row_to_conversation_event(row)

    def get_conversation_event_by_external(
        self,
        *,
        project_id: ProjectId,
        source: str,
        external_event_id: str,
    ) -> ConversationEvent | None:
        """Load an already-ingested transport event for retry recovery."""
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, actor_id, source, external_event_id, "
            "origin_kind, content, created_at FROM conversation_events "
            "WHERE project_id = ? AND source = ? AND external_event_id = ?",
            (project_id.value, source, external_event_id),
        )
        row = cursor.fetchone()
        return _row_to_conversation_event(row) if row is not None else None

    def list_conversation_events_for_project(
        self,
        project_id: ProjectId,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ConversationEvent]:
        """List conversation events for a project, oldest first.

        Per ``zero-project-isolation-evidence`` §"Scope begins before
        access": the query filters by project_id before any row is
        loaded.
        """
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, actor_id, source, external_event_id, "
            "origin_kind, content, created_at FROM conversation_events "
            "WHERE project_id = ? ORDER BY created_at ASC, id ASC "
            "LIMIT ? OFFSET ?",
            (project_id.value, limit, offset),
        )
        return [_row_to_conversation_event(row) for row in cursor.fetchall()]

    # ------------------------------------------------------------------
    # Plans
    # ------------------------------------------------------------------

    def insert_plan(self, plan: Plan, *, commit: bool = True) -> None:
        conn = self._database.connect()
        try:
            conn.execute(
                "INSERT INTO plans (id, project_id, current_state, "
                "current_revision_number) VALUES (?, ?, ?, ?)",
                (
                    plan.id.value,
                    plan.project_id.value,
                    plan.current_state,
                    plan.current_revision_number,
                ),
            )
            if commit:
                conn.commit()
        except sqlite3.IntegrityError:
            if commit:
                conn.rollback()
            raise

    def get_plan(self, plan_id: PlanId, *, project_id: ProjectId | None = None) -> Plan:
        conn = self._database.connect()
        sql = (
            "SELECT id, project_id, current_state, current_revision_number, "
            "created_at, updated_at FROM plans WHERE id = ?"
        )
        params: tuple[object, ...] = (plan_id.value,)
        if project_id is not None:
            sql += " AND project_id = ?"
            params += (project_id.value,)
        cursor = conn.execute(sql, params)
        row = cursor.fetchone()
        if row is None:
            raise PlanNotFoundError(f"Plan {plan_id} not found")
        return _row_to_plan(row)

    def list_plans_for_project(self, project_id: ProjectId) -> list[Plan]:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, current_state, current_revision_number, "
            "created_at, updated_at FROM plans WHERE project_id = ? "
            "ORDER BY created_at ASC",
            (project_id.value,),
        )
        return [_row_to_plan(row) for row in cursor.fetchall()]

    def update_plan_state(
        self,
        plan_id: PlanId,
        new_state: PlanState,
        new_revision_number: int | None = None,
        *,
        commit: bool = True,
    ) -> None:
        """Transition a plan's state.

        Per ``zero-planner-worker-contract`` §"transitions are
        explicit": the application layer checks
        :func:`is_valid_transition` before calling this method. The
        repository performs the update and bumps updated_at.
        """
        conn = self._database.connect()
        if new_revision_number is not None:
            cursor = conn.execute(
                "UPDATE plans SET current_state = ?, "
                "current_revision_number = ?, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE id = ?",
                (new_state, new_revision_number, plan_id.value),
            )
        else:
            cursor = conn.execute(
                "UPDATE plans SET current_state = ?, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE id = ?",
                (new_state, plan_id.value),
            )
        if cursor.rowcount == 0:
            raise PlanNotFoundError(f"Plan {plan_id} not found")
        if commit:
            conn.commit()

    # ------------------------------------------------------------------
    # Plan revisions
    # ------------------------------------------------------------------

    def insert_revision(self, revision: PlanRevision, *, commit: bool = True) -> None:
        conn = self._database.connect()
        try:
            conn.execute(
                "INSERT INTO plan_revisions "
                "(id, plan_id, project_id, revision_number, objective, "
                "scope, constraints, acceptance_criteria, risks, "
                "unresolved_questions, source_event_ids, proposed_by, state) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    revision.id.value,
                    revision.plan_id.value,
                    revision.project_id.value,
                    revision.revision_number,
                    revision.content.objective,
                    json.dumps(list(revision.content.scope)),
                    json.dumps(list(revision.content.constraints)),
                    json.dumps(list(revision.content.acceptance_criteria)),
                    json.dumps(list(revision.content.risks)),
                    json.dumps(list(revision.content.unresolved_questions)),
                    json.dumps([eid.value for eid in revision.content.source_event_ids]),
                    revision.proposed_by.value,
                    revision.state,
                ),
            )
            if commit:
                conn.commit()
        except sqlite3.IntegrityError:
            if commit:
                conn.rollback()
            raise

    def get_revision(
        self,
        revision_id: PlanRevisionId,
        *,
        project_id: ProjectId | None = None,
    ) -> PlanRevision:
        conn = self._database.connect()
        sql = (
            "SELECT id, plan_id, project_id, revision_number, objective, "
            "scope, constraints, acceptance_criteria, risks, "
            "unresolved_questions, source_event_ids, proposed_by, state, "
            "created_at FROM plan_revisions WHERE id = ?"
        )
        params: tuple[object, ...] = (revision_id.value,)
        if project_id is not None:
            sql += " AND project_id = ?"
            params += (project_id.value,)
        cursor = conn.execute(sql, params)
        row = cursor.fetchone()
        if row is None:
            raise PlanRevisionNotFoundError(f"Plan revision {revision_id} not found")
        return _row_to_revision(row)

    def get_current_revision(
        self,
        plan_id: PlanId,
        *,
        project_id: ProjectId | None = None,
    ) -> PlanRevision:
        """Return the current revision of a plan.

        Per ``zero-planner-worker-contract`` §"Approval names a
        revision": approval must name the current revision. This
        method is used by the plan service to check staleness.
        """
        plan = self.get_plan(plan_id, project_id=project_id)
        if plan.current_revision_number == 0:
            raise PlanRevisionNotFoundError(f"Plan {plan_id} has no revisions")
        conn = self._database.connect()
        sql = (
            "SELECT id, plan_id, project_id, revision_number, objective, "
            "scope, constraints, acceptance_criteria, risks, "
            "unresolved_questions, source_event_ids, proposed_by, state, "
            "created_at FROM plan_revisions "
            "WHERE plan_id = ? AND revision_number = ?"
        )
        params: tuple[object, ...] = (plan_id.value, plan.current_revision_number)
        if project_id is not None:
            sql += " AND project_id = ?"
            params += (project_id.value,)
        cursor = conn.execute(sql, params)
        row = cursor.fetchone()
        if row is None:
            raise PlanRevisionNotFoundError(
                f"Plan {plan_id} revision {plan.current_revision_number} not found"
            )
        return _row_to_revision(row)

    def list_revisions_for_plan(
        self,
        plan_id: PlanId,
        *,
        project_id: ProjectId | None = None,
    ) -> list[PlanRevision]:
        """List all revisions of a plan, oldest first."""
        conn = self._database.connect()
        sql = (
            "SELECT id, plan_id, project_id, revision_number, objective, "
            "scope, constraints, acceptance_criteria, risks, "
            "unresolved_questions, source_event_ids, proposed_by, state, "
            "created_at FROM plan_revisions WHERE plan_id = ?"
        )
        params: tuple[object, ...] = (plan_id.value,)
        if project_id is not None:
            sql += " AND project_id = ?"
            params += (project_id.value,)
        sql += " ORDER BY revision_number ASC"
        cursor = conn.execute(sql, params)
        return [_row_to_revision(row) for row in cursor.fetchall()]

    def find_revision_by_source_event(
        self,
        *,
        project_id: ProjectId,
        event_id: ConversationEventId,
    ) -> PlanRevision | None:
        """Return the first revision already derived from an event."""
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, plan_id, project_id, revision_number, objective, "
            "scope, constraints, acceptance_criteria, risks, "
            "unresolved_questions, source_event_ids, proposed_by, state, "
            "created_at FROM plan_revisions "
            "WHERE project_id = ? AND source_event_ids LIKE ? "
            "ORDER BY created_at ASC LIMIT 1",
            (project_id.value, f'%"{event_id.value}"%'),
        )
        row = cursor.fetchone()
        return _row_to_revision(row) if row is not None else None

    def update_revision_state(
        self,
        revision_id: PlanRevisionId,
        new_state: RevisionState,
        *,
        commit: bool = True,
    ) -> None:
        conn = self._database.connect()
        cursor = conn.execute(
            "UPDATE plan_revisions SET state = ? WHERE id = ?",
            (new_state, revision_id.value),
        )
        if cursor.rowcount == 0:
            raise PlanRevisionNotFoundError(f"Plan revision {revision_id} not found")
        if commit:
            conn.commit()

    # ------------------------------------------------------------------
    # Plan approvals (immutable)
    # ------------------------------------------------------------------

    def insert_approval(self, approval: PlanApproval, *, commit: bool = True) -> None:
        conn = self._database.connect()
        try:
            conn.execute(
                "INSERT INTO plan_approvals "
                "(id, plan_id, revision_id, project_id, approved_by, source, "
                "result, idempotency_key, redacted_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    approval.id.value,
                    approval.plan_id.value,
                    approval.revision_id.value,
                    approval.project_id.value,
                    approval.approved_by.value,
                    approval.source,
                    approval.result,
                    approval.idempotency_key,
                    approval.redacted_reason,
                ),
            )
            if commit:
                conn.commit()
        except sqlite3.IntegrityError as exc:
            if commit:
                conn.rollback()
            if "UNIQUE" in str(exc):
                # Duplicate (revision_id, result, idempotency_key) —
                # idempotent: the same approval was already recorded.
                # We return silently; the caller can look up the
                # existing approval.
                return
            raise

    def get_approval_for_revision(
        self,
        revision_id: PlanRevisionId,
        result: ApprovalResult,
    ) -> PlanApproval | None:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, plan_id, revision_id, project_id, approved_by, source, "
            "result, idempotency_key, redacted_reason, created_at "
            "FROM plan_approvals WHERE revision_id = ? AND result = ? "
            "ORDER BY created_at ASC LIMIT 1",
            (revision_id.value, result),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return _row_to_approval(row)

    def list_approvals_for_plan(self, plan_id: PlanId) -> list[PlanApproval]:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, plan_id, revision_id, project_id, approved_by, source, "
            "result, idempotency_key, redacted_reason, created_at "
            "FROM plan_approvals WHERE plan_id = ? "
            "ORDER BY created_at ASC",
            (plan_id.value,),
        )
        return [_row_to_approval(row) for row in cursor.fetchall()]

    # ------------------------------------------------------------------
    # Plan handoffs (one per approved revision)
    # ------------------------------------------------------------------

    def insert_handoff(self, handoff: PlanHandoff, *, commit: bool = True) -> None:
        """Insert a handoff record.

        Per ``zero-planner-worker-contract`` §"Worker creates execution
        from revision 3 exactly once": the UNIQUE(revision_id)
        constraint ensures that approving the same revision twice
        produces one handoff, not many.
        """
        conn = self._database.connect()
        try:
            conn.execute(
                "INSERT INTO plan_handoffs "
                "(id, plan_id, revision_id, project_id, approved_by, "
                "execution_id) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    handoff.id.value,
                    handoff.plan_id.value,
                    handoff.revision_id.value,
                    handoff.project_id.value,
                    handoff.approved_by.value,
                    handoff.execution_id,
                ),
            )
            if commit:
                conn.commit()
        except sqlite3.IntegrityError as exc:
            if commit:
                conn.rollback()
            if "UNIQUE" in str(exc) and "revision_id" in str(exc):
                # Idempotent: the handoff already exists. Return
                # silently; the caller can look up the existing
                # handoff.
                return
            raise

    def get_handoff_for_revision(
        self,
        revision_id: PlanRevisionId,
        *,
        project_id: ProjectId | None = None,
    ) -> PlanHandoff | None:
        conn = self._database.connect()
        sql = (
            "SELECT id, plan_id, revision_id, project_id, approved_by, "
            "execution_id, created_at FROM plan_handoffs "
            "WHERE revision_id = ?"
        )
        params: tuple[object, ...] = (revision_id.value,)
        if project_id is not None:
            sql += " AND project_id = ?"
            params += (project_id.value,)
        cursor = conn.execute(sql, params)
        row = cursor.fetchone()
        if row is None:
            return None
        return _row_to_handoff(row)

    def get_handoff(
        self,
        handoff_id: PlanHandoffId,
        *,
        project_id: ProjectId | None = None,
    ) -> PlanHandoff:
        conn = self._database.connect()
        sql = (
            "SELECT id, plan_id, revision_id, project_id, approved_by, "
            "execution_id, created_at FROM plan_handoffs WHERE id = ?"
        )
        params: tuple[object, ...] = (handoff_id.value,)
        if project_id is not None:
            sql += " AND project_id = ?"
            params += (project_id.value,)
        cursor = conn.execute(sql, params)
        row = cursor.fetchone()
        if row is None:
            raise PlanNotFoundError(f"Plan handoff {handoff_id} not found")
        return _row_to_handoff(row)

    def list_unclaimed_handoffs(
        self,
        project_id: ProjectId,
        *,
        limit: int = 32,
    ) -> list[PlanHandoff]:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, plan_id, revision_id, project_id, approved_by, "
            "execution_id, created_at FROM plan_handoffs "
            "WHERE project_id = ? AND execution_id IS NULL "
            "ORDER BY created_at ASC LIMIT ?",
            (project_id.value, limit),
        )
        return [_row_to_handoff(row) for row in cursor.fetchall()]

    def list_handoffs_for_project(self, project_id: ProjectId) -> list[PlanHandoff]:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, plan_id, revision_id, project_id, approved_by, "
            "execution_id, created_at FROM plan_handoffs "
            "WHERE project_id = ? ORDER BY created_at ASC",
            (project_id.value,),
        )
        return [_row_to_handoff(row) for row in cursor.fetchall()]

    def set_handoff_execution_id(
        self,
        handoff_id: PlanHandoffId,
        execution_id: str,
        *,
        commit: bool = True,
    ) -> None:
        """Record that the Worker has created an execution from this handoff."""
        conn = self._database.connect()
        cursor = conn.execute(
            "UPDATE plan_handoffs SET execution_id = ? WHERE id = ? AND execution_id IS NULL",
            (execution_id, handoff_id.value),
        )
        if cursor.rowcount == 0:
            # Either the handoff doesn't exist, or it already has an
            # execution_id. Both are handled by the caller.
            pass
        if commit:
            conn.commit()
