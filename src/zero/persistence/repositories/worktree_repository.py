"""Worktree repository — repositories, worktrees, command runs, artifacts.

Per ``zero-project-isolation-evidence`` §"Scope begins before access":
all queries filter by ``project_id`` before any row is loaded.
"""

from __future__ import annotations

import json
import sqlite3

from zero.domain.execution import ExecutionId, TaskId
from zero.domain.identity import ProjectId
from zero.domain.worktrees import (
    ArtifactKind,
    CommandRun,
    CommandRunId,
    CommandRunState,
    Repository,
    RepositoryId,
    RepositoryNotFoundError,
    TaskArtifact,
    TaskArtifactId,
    Worktree,
    WorktreeId,
    WorktreeNotFoundError,
    WorktreeState,
)
from zero.persistence.connection import Database


def _row_to_repository(row: sqlite3.Row) -> Repository:
    return Repository(
        id=RepositoryId(row["id"]),
        project_id=ProjectId(row["project_id"]),
        name=row["name"],
        local_path=row["local_path"],
        default_base_revision=row["default_base_revision"],
        created_at=row["created_at"],
    )


def _row_to_worktree(row: sqlite3.Row) -> Worktree:
    return Worktree(
        id=WorktreeId(row["id"]),
        project_id=ProjectId(row["project_id"]),
        repository_id=RepositoryId(row["repository_id"]),
        execution_id=ExecutionId(row["execution_id"]),
        task_id=TaskId(row["task_id"]),
        branch_name=row["branch_name"],
        worktree_path=row["worktree_path"],
        base_revision=row["base_revision"],
        state=row["state"],  # type: ignore[arg-type]
        cleanup_eligible_at=row["cleanup_eligible_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_command_run(row: sqlite3.Row) -> CommandRun:
    return CommandRun(
        id=CommandRunId(row["id"]),
        project_id=ProjectId(row["project_id"]),
        worktree_id=WorktreeId(row["worktree_id"]),
        task_id=TaskId(row["task_id"]),
        command=row["command"],
        args=tuple(json.loads(row["args"])),
        exit_code=row["exit_code"],
        timed_out=bool(row["timed_out"]),
        timeout_seconds=row["timeout_seconds"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        state=row["state"],  # type: ignore[arg-type]
    )


def _row_to_artifact(row: sqlite3.Row) -> TaskArtifact:
    return TaskArtifact(
        id=TaskArtifactId(row["id"]),
        project_id=ProjectId(row["project_id"]),
        worktree_id=WorktreeId(row["worktree_id"]),
        task_id=TaskId(row["task_id"]),
        command_run_id=CommandRunId(row["command_run_id"])
        if row["command_run_id"]
        else None,
        kind=row["kind"],  # type: ignore[arg-type]
        content=row["content"],
        content_hash=row["content_hash"],
        created_at=row["created_at"],
    )


class WorktreeRepository:
    """Database-backed repository for repositories, worktrees, command
    runs, and artifacts."""

    def __init__(self, database: Database) -> None:
        self._database = database

    # ------------------------------------------------------------------
    # Repositories
    # ------------------------------------------------------------------

    def insert_repository(
        self, repo: Repository, *, commit: bool = True
    ) -> None:
        conn = self._database.connect()
        try:
            conn.execute(
                "INSERT INTO repositories "
                "(id, project_id, name, local_path, default_base_revision) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    repo.id.value,
                    repo.project_id.value,
                    repo.name,
                    repo.local_path,
                    repo.default_base_revision,
                ),
            )
            if commit:
                conn.commit()
        except sqlite3.IntegrityError:
            if commit:
                conn.rollback()
            raise

    def get_repository(
        self, project_id: ProjectId, repo_id: RepositoryId
    ) -> Repository:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, name, local_path, "
            "default_base_revision, created_at FROM repositories "
            "WHERE id = ? AND project_id = ?",
            (repo_id.value, project_id.value),
        )
        row = cursor.fetchone()
        if row is None:
            raise RepositoryNotFoundError(
                f"Repository {repo_id} not found in project {project_id}"
            )
        return _row_to_repository(row)

    def list_repositories_for_project(
        self, project_id: ProjectId
    ) -> list[Repository]:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, name, local_path, "
            "default_base_revision, created_at FROM repositories "
            "WHERE project_id = ? ORDER BY created_at ASC",
            (project_id.value,),
        )
        return [_row_to_repository(row) for row in cursor.fetchall()]

    # ------------------------------------------------------------------
    # Worktrees
    # ------------------------------------------------------------------

    def insert_worktree(
        self, worktree: Worktree, *, commit: bool = True
    ) -> None:
        conn = self._database.connect()
        try:
            conn.execute(
                "INSERT INTO worktrees "
                "(id, project_id, repository_id, execution_id, task_id, "
                "branch_name, worktree_path, base_revision, state) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    worktree.id.value,
                    worktree.project_id.value,
                    worktree.repository_id.value,
                    worktree.execution_id.value,
                    worktree.task_id.value,
                    worktree.branch_name,
                    worktree.worktree_path,
                    worktree.base_revision,
                    worktree.state,
                ),
            )
            if commit:
                conn.commit()
        except sqlite3.IntegrityError as exc:
            if commit:
                conn.rollback()
            if "idx_worktrees_task_active" in str(exc):
                from zero.domain.worktrees import WorktreeAlreadyExistsError

                raise WorktreeAlreadyExistsError(
                    f"An active worktree already exists for task "
                    f"{worktree.task_id}"
                ) from exc
            raise

    def get_worktree(
        self, project_id: ProjectId, worktree_id: WorktreeId
    ) -> Worktree:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, repository_id, execution_id, task_id, "
            "branch_name, worktree_path, base_revision, state, "
            "cleanup_eligible_at, created_at, updated_at FROM worktrees "
            "WHERE id = ? AND project_id = ?",
            (worktree_id.value, project_id.value),
        )
        row = cursor.fetchone()
        if row is None:
            raise WorktreeNotFoundError(
                f"Worktree {worktree_id} not found in project {project_id}"
            )
        return _row_to_worktree(row)

    def get_worktree_for_task(
        self, task_id: TaskId
    ) -> Worktree | None:
        """Return the active worktree for a task, or None."""
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, repository_id, execution_id, task_id, "
            "branch_name, worktree_path, base_revision, state, "
            "cleanup_eligible_at, created_at, updated_at FROM worktrees "
            "WHERE task_id = ? AND state IN "
            "('allocated','active','interrupted') "
            "ORDER BY created_at DESC LIMIT 1",
            (task_id.value,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return _row_to_worktree(row)

    def list_worktrees_for_execution(
        self, execution_id: ExecutionId
    ) -> list[Worktree]:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, repository_id, execution_id, task_id, "
            "branch_name, worktree_path, base_revision, state, "
            "cleanup_eligible_at, created_at, updated_at FROM worktrees "
            "WHERE execution_id = ? ORDER BY created_at ASC",
            (execution_id.value,),
        )
        return [_row_to_worktree(row) for row in cursor.fetchall()]

    def list_worktrees_for_project(
        self, project_id: ProjectId
    ) -> list[Worktree]:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, repository_id, execution_id, task_id, "
            "branch_name, worktree_path, base_revision, state, "
            "cleanup_eligible_at, created_at, updated_at FROM worktrees "
            "WHERE project_id = ? ORDER BY created_at ASC",
            (project_id.value,),
        )
        return [_row_to_worktree(row) for row in cursor.fetchall()]

    def update_worktree_state(
        self,
        worktree_id: WorktreeId,
        new_state: WorktreeState,
        *,
        cleanup_eligible_at: str | None = None,
        commit: bool = True,
    ) -> None:
        conn = self._database.connect()
        if cleanup_eligible_at is not None:
            cursor = conn.execute(
                "UPDATE worktrees SET state = ?, "
                "cleanup_eligible_at = ?, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE id = ?",
                (new_state, cleanup_eligible_at, worktree_id.value),
            )
        else:
            cursor = conn.execute(
                "UPDATE worktrees SET state = ?, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE id = ?",
                (new_state, worktree_id.value),
            )
        if cursor.rowcount == 0:
            raise WorktreeNotFoundError(f"Worktree {worktree_id} not found")
        if commit:
            conn.commit()

    # ------------------------------------------------------------------
    # Command runs
    # ------------------------------------------------------------------

    def insert_command_run(
        self, run: CommandRun, *, commit: bool = True
    ) -> None:
        conn = self._database.connect()
        try:
            conn.execute(
                "INSERT INTO command_runs "
                "(id, project_id, worktree_id, task_id, command, args, "
                "exit_code, timed_out, timeout_seconds, started_at, "
                "completed_at, state) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run.id.value,
                    run.project_id.value,
                    run.worktree_id.value,
                    run.task_id.value,
                    run.command,
                    json.dumps(list(run.args)),
                    run.exit_code,
                    1 if run.timed_out else 0,
                    run.timeout_seconds,
                    run.started_at,
                    run.completed_at,
                    run.state,
                ),
            )
            if commit:
                conn.commit()
        except sqlite3.IntegrityError:
            if commit:
                conn.rollback()
            raise

    def get_command_run(
        self, run_id: CommandRunId
    ) -> CommandRun:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, worktree_id, task_id, command, args, "
            "exit_code, timed_out, timeout_seconds, started_at, "
            "completed_at, state FROM command_runs WHERE id = ?",
            (run_id.value,),
        )
        row = cursor.fetchone()
        if row is None:
            from zero.domain.worktrees import CommandRunNotFoundError

            raise CommandRunNotFoundError(f"Command run {run_id} not found")
        return _row_to_command_run(row)

    def update_command_run_state(
        self,
        run_id: CommandRunId,
        new_state: CommandRunState,
        *,
        exit_code: int | None = None,
        timed_out: bool | None = None,
        commit: bool = True,
    ) -> None:
        conn = self._database.connect()
        timed_out_val = 1 if timed_out else 0 if timed_out is not None else None
        cursor = conn.execute(
            "UPDATE command_runs SET state = ?, "
            "exit_code = COALESCE(?, exit_code), "
            "timed_out = COALESCE(?, timed_out), "
            "completed_at = CASE WHEN ? IN ('completed','timed_out','cancelled','unknown') "
            "THEN strftime('%Y-%m-%dT%H:%M:%fZ','now') ELSE completed_at END "
            "WHERE id = ?",
            (
                new_state,
                exit_code,
                timed_out_val,
                new_state,
                run_id.value,
            ),
        )
        if cursor.rowcount == 0:
            from zero.domain.worktrees import CommandRunNotFoundError

            raise CommandRunNotFoundError(f"Command run {run_id} not found")
        if commit:
            conn.commit()

    def list_command_runs_for_worktree(
        self, worktree_id: WorktreeId
    ) -> list[CommandRun]:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, worktree_id, task_id, command, args, "
            "exit_code, timed_out, timeout_seconds, started_at, "
            "completed_at, state FROM command_runs "
            "WHERE worktree_id = ? ORDER BY started_at ASC",
            (worktree_id.value,),
        )
        return [_row_to_command_run(row) for row in cursor.fetchall()]

    # ------------------------------------------------------------------
    # Task artifacts
    # ------------------------------------------------------------------

    def insert_artifact(
        self, artifact: TaskArtifact, *, commit: bool = True
    ) -> None:
        conn = self._database.connect()
        try:
            conn.execute(
                "INSERT INTO task_artifacts "
                "(id, project_id, worktree_id, task_id, command_run_id, "
                "kind, content, content_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    artifact.id.value,
                    artifact.project_id.value,
                    artifact.worktree_id.value,
                    artifact.task_id.value,
                    artifact.command_run_id.value
                    if artifact.command_run_id
                    else None,
                    artifact.kind,
                    artifact.content,
                    artifact.content_hash,
                ),
            )
            if commit:
                conn.commit()
        except sqlite3.IntegrityError:
            if commit:
                conn.rollback()
            raise

    def list_artifacts_for_task(
        self,
        task_id: TaskId,
        *,
        kind: ArtifactKind | None = None,
    ) -> list[TaskArtifact]:
        """List artifacts for a task, optionally filtered by kind."""
        conn = self._database.connect()
        if kind is not None:
            cursor = conn.execute(
                "SELECT id, project_id, worktree_id, task_id, "
                "command_run_id, kind, content, content_hash, created_at "
                "FROM task_artifacts WHERE task_id = ? AND kind = ? "
                "ORDER BY created_at ASC",
                (task_id.value, kind),
            )
        else:
            cursor = conn.execute(
                "SELECT id, project_id, worktree_id, task_id, "
                "command_run_id, kind, content, content_hash, created_at "
                "FROM task_artifacts WHERE task_id = ? "
                "ORDER BY created_at ASC",
                (task_id.value,),
            )
        return [_row_to_artifact(row) for row in cursor.fetchall()]

    def list_artifacts_for_worktree(
        self, worktree_id: WorktreeId
    ) -> list[TaskArtifact]:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, worktree_id, task_id, "
            "command_run_id, kind, content, content_hash, created_at "
            "FROM task_artifacts WHERE worktree_id = ? "
            "ORDER BY created_at ASC",
            (worktree_id.value,),
        )
        return [_row_to_artifact(row) for row in cursor.fetchall()]
