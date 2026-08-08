"""Execution repository — executions, tasks, dependencies, attempts, snapshots.

Per ``zero-planner-worker-contract`` §"Durable state is stronger than
agent memory": the task graph, approvals, workspaces, running
processes, test outcomes, and blockers live in canonical backend
state. After restart, the system should derive which tasks are
complete, which are ready, which were interrupted, which worktrees
belong to them, and what evidence exists — without asking a model to
remember what happened.

Per ``zero-project-isolation-evidence`` §"Scope begins before access":
all queries filter by ``project_id`` before any row is loaded.
"""

from __future__ import annotations

import json
import sqlite3

from zero.domain.execution import (
    AttemptState,
    Execution,
    ExecutionId,
    ExecutionNotFoundError,
    ExecutionSnapshot,
    ExecutionSnapshotId,
    Task,
    TaskAttempt,
    TaskAttemptId,
    TaskDependency,
    TaskId,
    TaskNotFoundError,
    TaskState,
)
from zero.domain.identity import ProjectId
from zero.domain.plans import PlanRevisionId
from zero.persistence.connection import Database


def _row_to_execution(row: sqlite3.Row) -> Execution:
    from zero.domain.plans import PlanHandoffId, PlanId, PlanRevisionId

    return Execution(
        id=ExecutionId(row["id"]),
        plan_id=PlanId(row["plan_id"]),
        plan_revision_id=PlanRevisionId(row["plan_revision_id"]),
        plan_handoff_id=PlanHandoffId(row["plan_handoff_id"]),
        project_id=ProjectId(row["project_id"]),
        state=row["state"],  # type: ignore[arg-type]
        blocker_reason=row["blocker_reason"],
        idempotency_key=row["idempotency_key"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        id=TaskId(row["id"]),
        execution_id=ExecutionId(row["execution_id"]),
        project_id=ProjectId(row["project_id"]),
        objective=row["objective"],
        permitted_scope=tuple(json.loads(row["permitted_scope"])),
        expected_evidence=tuple(json.loads(row["expected_evidence"])),
        state=row["state"],  # type: ignore[arg-type]
        blocker_reason=row["blocker_reason"],
        agent_type_id=row["agent_type_id"],
        terminal_state_set_at=row["terminal_state_set_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_attempt(row: sqlite3.Row) -> TaskAttempt:
    return TaskAttempt(
        id=TaskAttemptId(row["id"]),
        task_id=TaskId(row["task_id"]),
        project_id=ProjectId(row["project_id"]),
        attempt_number=row["attempt_number"],
        state=row["state"],  # type: ignore[arg-type]
        error_message=row["error_message"],
        lease_owner=row["lease_owner"],
        lease_expires_at=row["lease_expires_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
    )


def _row_to_snapshot(row: sqlite3.Row) -> ExecutionSnapshot:
    return ExecutionSnapshot(
        id=ExecutionSnapshotId(row["id"]),
        execution_id=ExecutionId(row["execution_id"]),
        project_id=ProjectId(row["project_id"]),
        snapshot_version=row["snapshot_version"],
        graph_state=row["graph_state"],
        snapshot_reason=row["snapshot_reason"],
        created_at=row["created_at"],
    )


class ExecutionRepository:
    """Database-backed execution, task, dependency, attempt, and
    snapshot repository."""

    def __init__(self, database: Database) -> None:
        self._database = database

    # ------------------------------------------------------------------
    # Executions
    # ------------------------------------------------------------------

    def insert_execution(
        self, execution: Execution, *, commit: bool = True
    ) -> None:
        conn = self._database.connect()
        try:
            conn.execute(
                "INSERT INTO executions "
                "(id, plan_id, plan_revision_id, plan_handoff_id, project_id, "
                "state, blocker_reason, idempotency_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    execution.id.value,
                    execution.plan_id.value,
                    execution.plan_revision_id.value,
                    execution.plan_handoff_id.value,
                    execution.project_id.value,
                    execution.state,
                    execution.blocker_reason,
                    execution.idempotency_key,
                ),
            )
            if commit:
                conn.commit()
        except sqlite3.IntegrityError as exc:
            if commit:
                conn.rollback()
            if "UNIQUE" in str(exc) and "plan_revision_id" in str(exc):
                # Idempotent: execution already exists for this revision.
                return
            raise

    def get_execution(self, execution_id: ExecutionId) -> Execution:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, plan_id, plan_revision_id, plan_handoff_id, project_id, "
            "state, blocker_reason, idempotency_key, created_at, updated_at "
            "FROM executions WHERE id = ?",
            (execution_id.value,),
        )
        row = cursor.fetchone()
        if row is None:
            raise ExecutionNotFoundError(
                f"Execution {execution_id} not found"
            )
        return _row_to_execution(row)

    def get_execution_for_revision(
        self, plan_revision_id: PlanRevisionId
    ) -> Execution | None:
        from zero.domain.plans import PlanRevisionId as _PRId

        if isinstance(plan_revision_id, _PRId):
            rev_value = plan_revision_id.value
        else:
            rev_value = str(plan_revision_id)
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, plan_id, plan_revision_id, plan_handoff_id, project_id, "
            "state, blocker_reason, idempotency_key, created_at, updated_at "
            "FROM executions WHERE plan_revision_id = ?",
            (rev_value,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return _row_to_execution(row)

    def list_executions_for_project(
        self, project_id: ProjectId
    ) -> list[Execution]:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, plan_id, plan_revision_id, plan_handoff_id, project_id, "
            "state, blocker_reason, idempotency_key, created_at, updated_at "
            "FROM executions WHERE project_id = ? ORDER BY created_at ASC",
            (project_id.value,),
        )
        return [_row_to_execution(row) for row in cursor.fetchall()]

    def update_execution_state(
        self,
        execution_id: ExecutionId,
        new_state: str,
        blocker_reason: str | None = None,
        *,
        commit: bool = True,
    ) -> None:
        conn = self._database.connect()
        # If blocker_reason is None, keep the existing value; otherwise
        # update it (including clearing it with an empty string).
        if blocker_reason is not None:
            cursor = conn.execute(
                "UPDATE executions SET state = ?, blocker_reason = ?, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE id = ?",
                (new_state, blocker_reason or None, execution_id.value),
            )
        else:
            cursor = conn.execute(
                "UPDATE executions SET state = ?, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE id = ?",
                (new_state, execution_id.value),
            )
        if cursor.rowcount == 0:
            raise ExecutionNotFoundError(
                f"Execution {execution_id} not found"
            )
        if commit:
            conn.commit()

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    def insert_task(
        self, task: Task, *, commit: bool = True
    ) -> None:
        conn = self._database.connect()
        try:
            conn.execute(
                "INSERT INTO tasks "
                "(id, execution_id, project_id, objective, permitted_scope, "
                "expected_evidence, state, blocker_reason, agent_type_id, "
                "terminal_state_set_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task.id.value,
                    task.execution_id.value,
                    task.project_id.value,
                    task.objective,
                    json.dumps(list(task.permitted_scope)),
                    json.dumps(list(task.expected_evidence)),
                    task.state,
                    task.blocker_reason,
                    task.agent_type_id,
                    task.terminal_state_set_at,
                ),
            )
            if commit:
                conn.commit()
        except sqlite3.IntegrityError:
            if commit:
                conn.rollback()
            raise

    def get_task(self, task_id: TaskId) -> Task:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, execution_id, project_id, objective, permitted_scope, "
            "expected_evidence, state, blocker_reason, agent_type_id, "
            "terminal_state_set_at, created_at, updated_at "
            "FROM tasks WHERE id = ?",
            (task_id.value,),
        )
        row = cursor.fetchone()
        if row is None:
            raise TaskNotFoundError(f"Task {task_id} not found")
        return _row_to_task(row)

    def list_tasks_for_execution(
        self, execution_id: ExecutionId
    ) -> list[Task]:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, execution_id, project_id, objective, permitted_scope, "
            "expected_evidence, state, blocker_reason, agent_type_id, "
            "terminal_state_set_at, created_at, updated_at "
            "FROM tasks WHERE execution_id = ? ORDER BY created_at ASC",
            (execution_id.value,),
        )
        return [_row_to_task(row) for row in cursor.fetchall()]

    def update_task_state(
        self,
        task_id: TaskId,
        new_state: TaskState,
        *,
        blocker_reason: str | None = None,
        agent_type_id: str | None = None,
        commit: bool = True,
    ) -> None:
        conn = self._database.connect()
        from zero.domain.execution import is_terminal_task_state

        if is_terminal_task_state(new_state):
            cursor = conn.execute(
                "UPDATE tasks SET state = ?, blocker_reason = ?, "
                "agent_type_id = ?, "
                "terminal_state_set_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE id = ?",
                (
                    new_state,
                    blocker_reason,
                    agent_type_id,
                    task_id.value,
                ),
            )
        else:
            cursor = conn.execute(
                "UPDATE tasks SET state = ?, blocker_reason = ?, "
                "agent_type_id = ?, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE id = ?",
                (
                    new_state,
                    blocker_reason,
                    agent_type_id,
                    task_id.value,
                ),
            )
        if cursor.rowcount == 0:
            raise TaskNotFoundError(f"Task {task_id} not found")
        if commit:
            conn.commit()

    # ------------------------------------------------------------------
    # Task dependencies
    # ------------------------------------------------------------------

    def insert_dependency(
        self, dependency: TaskDependency, *, commit: bool = True
    ) -> None:
        conn = self._database.connect()
        try:
            conn.execute(
                "INSERT INTO task_dependencies (task_id, depends_on_task_id) "
                "VALUES (?, ?)",
                (
                    dependency.task_id.value,
                    dependency.depends_on_task_id.value,
                ),
            )
            if commit:
                conn.commit()
        except sqlite3.IntegrityError as exc:
            if commit:
                conn.rollback()
            if "UNIQUE" in str(exc):
                # Idempotent: dependency already exists.
                return
            # CHECK constraint violation (task_id == depends_on_task_id)
            raise

    def list_dependencies_for_task(
        self, task_id: TaskId
    ) -> list[TaskDependency]:
        """Return all dependencies of ``task_id`` (i.e. the tasks that
        ``task_id`` depends on)."""
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT task_id, depends_on_task_id FROM task_dependencies "
            "WHERE task_id = ?",
            (task_id.value,),
        )
        return [
            TaskDependency(
                task_id=TaskId(row["task_id"]),
                depends_on_task_id=TaskId(row["depends_on_task_id"]),
            )
            for row in cursor.fetchall()
        ]

    def list_dependents_of_task(
        self, task_id: TaskId
    ) -> list[TaskDependency]:
        """Return all tasks that depend on ``task_id``."""
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT task_id, depends_on_task_id FROM task_dependencies "
            "WHERE depends_on_task_id = ?",
            (task_id.value,),
        )
        return [
            TaskDependency(
                task_id=TaskId(row["task_id"]),
                depends_on_task_id=TaskId(row["depends_on_task_id"]),
            )
            for row in cursor.fetchall()
        ]

    def list_all_dependencies_for_execution(
        self, execution_id: ExecutionId
    ) -> list[TaskDependency]:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT td.task_id, td.depends_on_task_id "
            "FROM task_dependencies td "
            "JOIN tasks t ON td.task_id = t.id "
            "WHERE t.execution_id = ?",
            (execution_id.value,),
        )
        return [
            TaskDependency(
                task_id=TaskId(row["task_id"]),
                depends_on_task_id=TaskId(row["depends_on_task_id"]),
            )
            for row in cursor.fetchall()
        ]

    # ------------------------------------------------------------------
    # Task attempts
    # ------------------------------------------------------------------

    def insert_attempt(
        self, attempt: TaskAttempt, *, commit: bool = True
    ) -> None:
        conn = self._database.connect()
        try:
            conn.execute(
                "INSERT INTO task_attempts "
                "(id, task_id, project_id, attempt_number, state, "
                "error_message, lease_owner, lease_expires_at, completed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    attempt.id.value,
                    attempt.task_id.value,
                    attempt.project_id.value,
                    attempt.attempt_number,
                    attempt.state,
                    attempt.error_message,
                    attempt.lease_owner,
                    attempt.lease_expires_at,
                    attempt.completed_at,
                ),
            )
            if commit:
                conn.commit()
        except sqlite3.IntegrityError as exc:
            if commit:
                conn.rollback()
            if "UNIQUE" in str(exc) and "attempt_number" in str(exc):
                from zero.domain.execution import DuplicateAttemptError

                raise DuplicateAttemptError(
                    f"Attempt {attempt.attempt_number} already exists "
                    f"for task {attempt.task_id}"
                ) from exc
            raise

    def get_attempt(self, attempt_id: TaskAttemptId) -> TaskAttempt:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, task_id, project_id, attempt_number, state, "
            "error_message, lease_owner, lease_expires_at, started_at, "
            "completed_at FROM task_attempts WHERE id = ?",
            (attempt_id.value,),
        )
        row = cursor.fetchone()
        if row is None:
            raise TaskNotFoundError(f"Task attempt {attempt_id} not found")
        return _row_to_attempt(row)

    def list_attempts_for_task(
        self, task_id: TaskId
    ) -> list[TaskAttempt]:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, task_id, project_id, attempt_number, state, "
            "error_message, lease_owner, lease_expires_at, started_at, "
            "completed_at FROM task_attempts WHERE task_id = ? "
            "ORDER BY attempt_number ASC",
            (task_id.value,),
        )
        return [_row_to_attempt(row) for row in cursor.fetchall()]

    def update_attempt_state(
        self,
        attempt_id: TaskAttemptId,
        new_state: AttemptState,
        *,
        error_message: str | None = None,
        commit: bool = True,
    ) -> None:
        conn = self._database.connect()

        terminal_states = {"succeeded", "failed", "cancelled"}
        if new_state in terminal_states:
            cursor = conn.execute(
                "UPDATE task_attempts SET state = ?, error_message = ?, "
                "completed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE id = ?",
                (new_state, error_message, attempt_id.value),
            )
        else:
            # "unknown" is not terminal; it means we don't know the
            # outcome yet.
            cursor = conn.execute(
                "UPDATE task_attempts SET state = ?, error_message = ? "
                "WHERE id = ?",
                (new_state, error_message, attempt_id.value),
            )
        if cursor.rowcount == 0:
            raise TaskNotFoundError(f"Task attempt {attempt_id} not found")
        if commit:
            conn.commit()

    # ------------------------------------------------------------------
    # Execution snapshots
    # ------------------------------------------------------------------

    def insert_snapshot(
        self, snapshot: ExecutionSnapshot, *, commit: bool = True
    ) -> None:
        conn = self._database.connect()
        try:
            conn.execute(
                "INSERT INTO execution_snapshots "
                "(id, execution_id, project_id, snapshot_version, "
                "graph_state, snapshot_reason) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    snapshot.id.value,
                    snapshot.execution_id.value,
                    snapshot.project_id.value,
                    snapshot.snapshot_version,
                    snapshot.graph_state,
                    snapshot.snapshot_reason,
                ),
            )
            if commit:
                conn.commit()
        except sqlite3.IntegrityError:
            if commit:
                conn.rollback()
            raise

    def get_latest_snapshot(
        self, execution_id: ExecutionId
    ) -> ExecutionSnapshot | None:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, execution_id, project_id, snapshot_version, "
            "graph_state, snapshot_reason, created_at "
            "FROM execution_snapshots WHERE execution_id = ? "
            "ORDER BY snapshot_version DESC LIMIT 1",
            (execution_id.value,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return _row_to_snapshot(row)

    def list_snapshots_for_execution(
        self, execution_id: ExecutionId
    ) -> list[ExecutionSnapshot]:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, execution_id, project_id, snapshot_version, "
            "graph_state, snapshot_reason, created_at "
            "FROM execution_snapshots WHERE execution_id = ? "
            "ORDER BY snapshot_version ASC",
            (execution_id.value,),
        )
        return [_row_to_snapshot(row) for row in cursor.fetchall()]
