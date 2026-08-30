"""Worktree repository — repositories, worktrees, command runs, artifacts.

Per ``zero-project-isolation-evidence`` §"Scope begins before access":
all queries filter by ``project_id`` before any row is loaded.
"""

from __future__ import annotations

import hashlib
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
    content = row["content"]
    content_hash = row["content_hash"]
    expected_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if content_hash != expected_hash:
        raise ValueError(f"task artifact {row['id']} has invalid content_hash")
    return TaskArtifact(
        id=TaskArtifactId(row["id"]),
        project_id=ProjectId(row["project_id"]),
        worktree_id=WorktreeId(row["worktree_id"]),
        task_id=TaskId(row["task_id"]),
        command_run_id=CommandRunId(row["command_run_id"]) if row["command_run_id"] else None,
        kind=row["kind"],  # type: ignore[arg-type]
        content=content,
        content_hash=content_hash,
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

    def insert_repository(self, repo: Repository, *, commit: bool = True) -> None:
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

    def get_repository(self, project_id: ProjectId, repo_id: RepositoryId) -> Repository:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, name, local_path, "
            "default_base_revision, created_at FROM repositories "
            "WHERE id = ? AND project_id = ?",
            (repo_id.value, project_id.value),
        )
        row = cursor.fetchone()
        if row is None:
            raise RepositoryNotFoundError(f"Repository {repo_id} not found in project {project_id}")
        return _row_to_repository(row)

    def list_repositories_for_project(self, project_id: ProjectId) -> list[Repository]:
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

    def insert_worktree(self, worktree: Worktree, *, commit: bool = True) -> None:
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
                    f"An active worktree already exists for task {worktree.task_id}"
                ) from exc
            raise

    def get_worktree(self, project_id: ProjectId, worktree_id: WorktreeId) -> Worktree:
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
            raise WorktreeNotFoundError(f"Worktree {worktree_id} not found in project {project_id}")
        return _row_to_worktree(row)

    def get_worktree_for_task(
        self,
        task_id: TaskId,
        *,
        project_id: ProjectId | None = None,
    ) -> Worktree | None:
        """Return the active worktree for a task, or None."""
        conn = self._database.connect()
        query = (
            "SELECT id, project_id, repository_id, execution_id, task_id, "
            "branch_name, worktree_path, base_revision, state, "
            "cleanup_eligible_at, created_at, updated_at FROM worktrees "
        )
        if project_id is None:
            cursor = conn.execute(
                f"{query}WHERE task_id = ? AND state IN "
                "('allocated','active','interrupted') "
                "ORDER BY created_at DESC LIMIT 1",
                (task_id.value,),
            )
        else:
            cursor = conn.execute(
                f"{query}WHERE task_id = ? AND project_id = ? AND state IN "
                "('allocated','active','interrupted') "
                "ORDER BY created_at DESC LIMIT 1",
                (task_id.value, project_id.value),
            )
        row = cursor.fetchone()
        if row is None:
            return None
        return _row_to_worktree(row)

    def get_worktree_for_task_in_execution(
        self,
        project_id: ProjectId,
        execution_id: ExecutionId,
        task_id: TaskId,
    ) -> Worktree | None:
        """Resolve a worktree only across the complete lineage tuple."""
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, repository_id, execution_id, task_id, "
            "branch_name, worktree_path, base_revision, state, "
            "cleanup_eligible_at, created_at, updated_at FROM worktrees "
            "WHERE project_id = ? AND execution_id = ? AND task_id = ? "
            "AND state IN ('allocated','active','interrupted','succeeded','failed') "
            "ORDER BY created_at DESC LIMIT 1",
            (project_id.value, execution_id.value, task_id.value),
        )
        row = cursor.fetchone()
        return _row_to_worktree(row) if row is not None else None

    def list_worktrees_for_execution(
        self,
        execution_id: ExecutionId,
        *,
        project_id: ProjectId | None = None,
    ) -> list[Worktree]:
        conn = self._database.connect()
        query = (
            "SELECT id, project_id, repository_id, execution_id, task_id, "
            "branch_name, worktree_path, base_revision, state, "
            "cleanup_eligible_at, created_at, updated_at FROM worktrees "
        )
        if project_id is None:
            cursor = conn.execute(
                f"{query}WHERE execution_id = ? ORDER BY created_at ASC",
                (execution_id.value,),
            )
        else:
            cursor = conn.execute(
                f"{query}WHERE execution_id = ? AND project_id = ? ORDER BY created_at ASC",
                (execution_id.value, project_id.value),
            )
        return [_row_to_worktree(row) for row in cursor.fetchall()]

    def list_worktrees_for_project(self, project_id: ProjectId) -> list[Worktree]:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, repository_id, execution_id, task_id, "
            "branch_name, worktree_path, base_revision, state, "
            "cleanup_eligible_at, created_at, updated_at FROM worktrees "
            "WHERE project_id = ? ORDER BY created_at ASC",
            (project_id.value,),
        )
        return [_row_to_worktree(row) for row in cursor.fetchall()]

    def list_worktrees_in_states(
        self, states: tuple[str, ...] = ("allocated", "active", "interrupted")
    ) -> list[Worktree]:
        """List worktrees currently in the given states (no project filter).

        Boot-recovery companion (live-run fix 2026-08-31): worktrees left
        in the partial-unique states by a killed process permanently
        occupy ``idx_worktrees_task_active``, so the task's next attempt
        died with ``UNIQUE constraint failed: worktrees.task_id``. The
        service uses this to find and abandon them.
        """
        if not states:
            return []
        placeholders = ", ".join("?" for _ in states)
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, repository_id, execution_id, task_id, "
            "branch_name, worktree_path, base_revision, state, "
            "cleanup_eligible_at, created_at, updated_at FROM worktrees "
            f"WHERE state IN ({placeholders}) ORDER BY created_at ASC",
            tuple(states),
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

    def insert_command_run(self, run: CommandRun, *, commit: bool = True) -> None:
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

    def get_command_run(self, run_id: CommandRunId) -> CommandRun:
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
        self,
        worktree_id: WorktreeId,
        *,
        project_id: ProjectId | None = None,
    ) -> list[CommandRun]:
        conn = self._database.connect()
        if project_id is None:
            cursor = conn.execute(
                "SELECT id, project_id, worktree_id, task_id, command, args, "
                "exit_code, timed_out, timeout_seconds, started_at, "
                "completed_at, state FROM command_runs "
                "WHERE worktree_id = ? ORDER BY started_at ASC",
                (worktree_id.value,),
            )
        else:
            cursor = conn.execute(
                "SELECT id, project_id, worktree_id, task_id, command, args, "
                "exit_code, timed_out, timeout_seconds, started_at, "
                "completed_at, state FROM command_runs "
                "WHERE worktree_id = ? AND project_id = ? ORDER BY started_at ASC",
                (worktree_id.value, project_id.value),
            )
        return [_row_to_command_run(row) for row in cursor.fetchall()]

    # ------------------------------------------------------------------
    # Task artifacts
    # ------------------------------------------------------------------

    def insert_artifact(self, artifact: TaskArtifact, *, commit: bool = True) -> None:
        expected_hash = hashlib.sha256(artifact.content.encode("utf-8")).hexdigest()
        if artifact.content_hash != expected_hash:
            raise ValueError("content_hash does not match artifact content")
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
                    artifact.command_run_id.value if artifact.command_run_id else None,
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
        project_id: ProjectId | None = None,
        kind: ArtifactKind | None = None,
    ) -> list[TaskArtifact]:
        """List artifacts for a task, optionally filtered by kind."""
        conn = self._database.connect()
        sql = (
            "SELECT id, project_id, worktree_id, task_id, "
            "command_run_id, kind, content, content_hash, created_at "
            "FROM task_artifacts WHERE task_id = ?"
        )
        params: list[str] = [task_id.value]
        if project_id is not None:
            sql += " AND project_id = ?"
            params.append(project_id.value)
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY created_at ASC"
        cursor = conn.execute(sql, params)
        return [_row_to_artifact(row) for row in cursor.fetchall()]

    def list_artifacts_for_task_in_lineage(
        self,
        project_id: ProjectId,
        execution_id: ExecutionId,
        task_id: TaskId,
        worktree_id: WorktreeId,
        *,
        kind: ArtifactKind | None = None,
    ) -> list[TaskArtifact]:
        """List artifacts only when project/execution/task/worktree agree."""
        conn = self._database.connect()
        sql = (
            "SELECT a.id, a.project_id, a.worktree_id, a.task_id, "
            "a.command_run_id, a.kind, a.content, a.content_hash, a.created_at "
            "FROM task_artifacts a JOIN worktrees w ON w.id = a.worktree_id "
            "WHERE a.project_id = ? AND w.execution_id = ? AND a.task_id = ? "
            "AND a.worktree_id = ?"
        )
        params: list[str] = [
            project_id.value,
            execution_id.value,
            task_id.value,
            worktree_id.value,
        ]
        if kind is not None:
            sql += " AND a.kind = ?"
            params.append(kind)
        sql += " ORDER BY a.created_at ASC"
        cursor = conn.execute(sql, params)
        return [_row_to_artifact(row) for row in cursor.fetchall()]

    def list_artifacts_for_worktree(self, worktree_id: WorktreeId) -> list[TaskArtifact]:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, worktree_id, task_id, "
            "command_run_id, kind, content, content_hash, created_at "
            "FROM task_artifacts WHERE worktree_id = ? "
            "ORDER BY created_at ASC",
            (worktree_id.value,),
        )
        return [_row_to_artifact(row) for row in cursor.fetchall()]
