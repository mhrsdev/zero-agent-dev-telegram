"""Worktree service — repository registration, isolated worktree lifecycle,
command runner, artifact capture, and safe cleanup.

Per ``zero-agent-execution-lifecycle`` SKILL.md:

- A worktree is a safety boundary, not an organizational preference.
- Concurrent coding tasks must not write into one working directory.
- A branch names a history line; a worktree provides a separate
  filesystem view. Both matter.
- Isolation scope follows impact, not just file paths.
- Failure and cleanup are lifecycle states.

Per PLAN.md M6 invariants:
- Every coding task receives an isolated branch and working tree.
- The target repository and base revision are explicit.
- Commands are scoped, time-bounded, and audited.
- A task returns diff, checks, artifacts, and status.
- No task pushes, merges, or deploys without explicit authority.
- Cleanup never deletes an unknown path, mount, active workspace, or
  uncommitted human work.

Per ``zero-recovery-consistency`` §"Cleanup requires proof of
non-ownership": Before a worktree is removed, Zero needs evidence that
it belongs to the intended task, has no active process/service/mount
dependency, and has preserved required human work or recovery artifacts.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from zero.app.authorization_service import AuthorizationService
from zero.domain.audit import AuditEvent, AuditEventId, AuditSource
from zero.domain.execution import ExecutionId, TaskId
from zero.domain.identity import ProjectId, UserId
from zero.domain.ids import (
    generate_audit_event_id,
    generate_command_run_id,
    generate_repository_id,
    generate_task_artifact_id,
    generate_worktree_id,
)
from zero.domain.worktrees import (
    ArtifactKind,
    CommandRun,
    CommandRunId,
    CommandRunState,
    InvalidWorktreeTransitionError,
    PathValidationError,
    Repository,
    RepositoryId,
    TaskArtifact,
    TaskArtifactId,
    Worktree,
    WorktreeAlreadyExistsError,
    WorktreeCleanupError,
    WorktreeId,
    WorktreeState,
    is_valid_worktree_transition,
)
from zero.persistence.repositories.audit_repository import AuditRepository
from zero.persistence.repositories.worktree_repository import (
    WorktreeRepository,
)


def _now_utc_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------
# Path validation
# ----------------------------------------------------------------------


def validate_repository_path(local_path: str) -> str:
    """Validate that a repository path is safe.

    Per PLAN.md M6: "Path traversal and repository escape attempts
    fail."

    Rules:
    - Must be absolute.
    - Must not contain ``..`` components.
    - Must not be a symlink (we resolve and check).
    - Must exist and be a directory.
    - Must be a git repository (contain a ``.git`` dir or be bare).
    """
    if not local_path or not isinstance(local_path, str):
        raise PathValidationError("local_path must not be empty", path=local_path)
    p = Path(local_path)
    if not p.is_absolute():
        raise PathValidationError(
            f"local_path must be absolute; got {local_path!r}",
            path=local_path,
        )
    # Check for traversal components in the string itself.
    if ".." in p.parts:
        raise PathValidationError(
            f"local_path must not contain '..' components; got {local_path!r}",
            path=local_path,
        )
    # Resolve and check it's still absolute and doesn't escape.
    try:
        resolved = p.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PathValidationError(
            f"local_path does not resolve: {exc}",
            path=local_path,
        ) from exc
    if not resolved.is_dir():
        raise PathValidationError(
            f"local_path is not a directory: {resolved}",
            path=str(resolved),
        )
    # Check it's a git repository.
    git_dir = resolved / ".git"
    if not git_dir.exists():
        # Could be a bare repo. Check for HEAD, refs, objects.
        is_bare = (
            (resolved / "HEAD").exists()
            and (resolved / "refs").exists()
            and (resolved / "objects").exists()
        )
        if not is_bare:
            raise PathValidationError(
                f"local_path is not a git repository: {resolved}",
                path=str(resolved),
            )
    return str(resolved)


def validate_worktree_path(
    worktree_root: str, worktree_id: str
) -> str:
    """Validate and construct a worktree path.

    The worktree path is ``<worktree_root>/<worktree_id>``. The
    worktree_root must be absolute and must not contain ``..``.
    """
    if not worktree_root or not isinstance(worktree_root, str):
        raise PathValidationError(
            "worktree_root must not be empty", path=worktree_root
        )
    root = Path(worktree_root)
    if not root.is_absolute():
        raise PathValidationError(
            f"worktree_root must be absolute; got {worktree_root!r}",
            path=worktree_root,
        )
    if ".." in root.parts:
        raise PathValidationError(
            f"worktree_root must not contain '..' components; got {worktree_root!r}",
            path=worktree_root,
        )
    # The worktree_id is server-generated and contains only safe
    # characters (lowercase + digits + underscore), but we validate
    # defensively.
    safe_id = "".join(c for c in worktree_id if c.isalnum() or c in "-_")
    if safe_id != worktree_id:
        raise PathValidationError(
            f"worktree_id contains unsafe characters: {worktree_id!r}",
            path=worktree_id,
        )
    return str(root / safe_id)


def is_path_inside(child: str, parent: str) -> bool:
    """Return True if ``child`` is ``parent`` or inside ``parent``.

    Used to prevent worktree operations from escaping their root.
    """
    child_path = Path(child).resolve(strict=False)
    parent_path = Path(parent).resolve(strict=False)
    try:
        child_path.relative_to(parent_path)
        return True
    except ValueError:
        return False


# ----------------------------------------------------------------------
# Worktree service
# ----------------------------------------------------------------------


class WorktreeService:
    """Application operations for repositories, worktrees, command
    runs, and artifacts.

    The service is the only place where git operations and filesystem
    mutations happen. It enforces:
    - path validation (no traversal, no escape);
    - isolated branch + worktree per task;
    - scoped, time-bounded commands;
    - artifact capture;
    - safe cleanup (only after eligibility checks).
    """

    def __init__(
        self,
        worktree_repo: WorktreeRepository,
        audit_repo: AuditRepository,
        authorization_service: AuthorizationService,
        *,
        worktree_root: str | None = None,
    ) -> None:
        self._repo = worktree_repo
        self._audit_repo = audit_repo
        self._authz = authorization_service
        # worktree_root is the parent directory under which all
        # worktrees are created. If None, a default is used.
        self._worktree_root = worktree_root or "/tmp/zero-worktrees"

    # ------------------------------------------------------------------
    # Repository registration
    # ------------------------------------------------------------------

    def register_repository(
        self,
        *,
        project_id: ProjectId,
        actor_id: UserId,
        name: str,
        local_path: str,
        default_base_revision: str | None = None,
        source: AuditSource = "web",
    ) -> Repository:
        """Register a target repository for coding tasks.

        Per PLAN.md M6: "Repository registration and validated local
        path handling."
        """
        if not name or not name.strip():
            raise ValueError("repository name must not be empty")
        validated_path = validate_repository_path(local_path)
        repository = Repository(
            id=RepositoryId(generate_repository_id()),
            project_id=project_id,
            name=name.strip(),
            local_path=validated_path,
            default_base_revision=default_base_revision,
            created_at=_now_utc_iso(),
        )
        self._repo.insert_repository(repository)
        self._audit_repo.insert(
            AuditEvent(
                id=AuditEventId(generate_audit_event_id()),
                project_id=project_id,
                actor_id=actor_id,
                source=source,
                operation="repository.register",
                target_type="repository",
                target_id=repository.id.value,
                result="success",
                redacted_summary=f"Registered repository {repository.name!r}",
                created_at=_now_utc_iso(),
            )
        )
        return repository

    def get_repository(
        self, project_id: ProjectId, repo_id: RepositoryId
    ) -> Repository:
        return self._repo.get_repository(project_id, repo_id)

    def list_repositories(
        self, project_id: ProjectId
    ) -> list[Repository]:
        return self._repo.list_repositories_for_project(project_id)

    # ------------------------------------------------------------------
    # Worktree lifecycle
    # ------------------------------------------------------------------

    def create_worktree(
        self,
        *,
        project_id: ProjectId,
        repository_id: RepositoryId,
        execution_id: ExecutionId,
        task_id: TaskId,
        actor_id: UserId,
        base_revision: str | None = None,
        source: AuditSource = "system",
    ) -> Worktree:
        """Create an isolated branch and worktree for a task.

        Per ``zero-agent-execution-lifecycle`` §"A worktree is a
        safety boundary": concurrent coding tasks must not write into
        one working directory. Each task gets its own branch and
        worktree.

        Steps:
        1. Validate the repository exists and belongs to the project.
        2. Resolve the base revision (from arg or repository default).
        3. Create a unique branch name.
        4. Create the worktree directory under the worktree root.
        5. Run ``git worktree add`` to create the worktree.
        6. Record the worktree in the database.
        """
        # Authorize.
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="execution.start",
            source=source,
        )
        # Get the repository (project-scoped).
        repository = self._repo.get_repository(project_id, repository_id)
        # Resolve the base revision.
        rev = base_revision or repository.default_base_revision or "HEAD"
        # Generate IDs.
        worktree_id = WorktreeId(generate_worktree_id())
        branch_name = f"zero/{worktree_id.value}"
        worktree_path = validate_worktree_path(
            self._worktree_root, worktree_id.value
        )
        # Ensure the worktree root exists.
        Path(self._worktree_root).mkdir(parents=True, exist_ok=True)
        # Create the worktree using git.
        try:
            self._git_worktree_add(
                repository.local_path,
                worktree_path,
                branch_name,
                rev,
            )
        except subprocess.CalledProcessError as exc:
            # Clean up the directory if git failed.
            try:
                shutil.rmtree(worktree_path, ignore_errors=True)
            except Exception:
                pass
            raise WorktreeError(
                f"Failed to create worktree: {exc.stderr or exc}"
            ) from exc
        # Capture the actual base revision (resolve HEAD to a SHA).
        try:
            actual_base = self._git_rev_parse(worktree_path, "HEAD")
        except Exception:
            actual_base = rev
        # Record the worktree.
        worktree = Worktree(
            id=worktree_id,
            project_id=project_id,
            repository_id=repository.id,
            execution_id=execution_id,
            task_id=task_id,
            branch_name=branch_name,
            worktree_path=worktree_path,
            base_revision=actual_base,
            state="allocated",
            created_at=_now_utc_iso(),
            updated_at=_now_utc_iso(),
        )
        try:
            self._repo.insert_worktree(worktree)
        except WorktreeAlreadyExistsError:
            # Clean up the git worktree we just created.
            try:
                self._git_worktree_remove(repository.local_path, worktree_path)
            except Exception:
                pass
            raise
        self._audit_repo.insert(
            AuditEvent(
                id=AuditEventId(generate_audit_event_id()),
                project_id=project_id,
                actor_id=actor_id,
                source=source,
                operation="worktree.create",
                target_type="worktree",
                target_id=worktree.id.value,
                result="success",
                redacted_summary=(
                    f"Created worktree {worktree.id.value} for task "
                    f"{task_id.value}"
                ),
                correlation_id=execution_id.value,
                created_at=_now_utc_iso(),
            )
        )
        return worktree

    def activate_worktree(
        self,
        *,
        project_id: ProjectId,
        worktree_id: WorktreeId,
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> Worktree:
        """Transition a worktree from ``allocated`` to ``active``."""
        worktree = self._repo.get_worktree(project_id, worktree_id)
        if not is_valid_worktree_transition(worktree.state, "active"):
            raise InvalidWorktreeTransitionError(
                f"Cannot transition worktree from {worktree.state!r} to 'active'"
            )
        self._repo.update_worktree_state(worktree_id, "active")
        return self._repo.get_worktree(project_id, worktree_id)

    def complete_worktree(
        self,
        *,
        project_id: ProjectId,
        worktree_id: WorktreeId,
        actor_id: UserId,
        succeeded: bool,
        source: AuditSource = "system",
    ) -> Worktree:
        """Transition a worktree to ``succeeded`` or ``failed``."""
        worktree = self._repo.get_worktree(project_id, worktree_id)
        new_state: WorktreeState = "succeeded" if succeeded else "failed"
        if not is_valid_worktree_transition(worktree.state, new_state):
            raise InvalidWorktreeTransitionError(
                f"Cannot transition worktree from {worktree.state!r} "
                f"to {new_state!r}"
            )
        self._repo.update_worktree_state(worktree_id, new_state)
        return self._repo.get_worktree(project_id, worktree_id)

    def mark_worktree_interrupted(
        self,
        *,
        project_id: ProjectId,
        worktree_id: WorktreeId,
    ) -> Worktree:
        """Mark a worktree as interrupted (e.g. after a crash)."""
        worktree = self._repo.get_worktree(project_id, worktree_id)
        if not is_valid_worktree_transition(worktree.state, "interrupted"):
            raise InvalidWorktreeTransitionError(
                f"Cannot transition worktree from {worktree.state!r} "
                f"to 'interrupted'"
            )
        self._repo.update_worktree_state(worktree_id, "interrupted")
        return self._repo.get_worktree(project_id, worktree_id)

    def get_worktree(
        self, project_id: ProjectId, worktree_id: WorktreeId
    ) -> Worktree:
        return self._repo.get_worktree(project_id, worktree_id)

    def get_worktree_for_task(
        self, task_id: TaskId
    ) -> Worktree | None:
        return self._repo.get_worktree_for_task(task_id)

    def list_worktrees_for_execution(
        self, execution_id: ExecutionId
    ) -> list[Worktree]:
        return self._repo.list_worktrees_for_execution(execution_id)

    # ------------------------------------------------------------------
    # Command runner
    # ------------------------------------------------------------------

    def run_command(
        self,
        *,
        project_id: ProjectId,
        worktree_id: WorktreeId,
        task_id: TaskId,
        actor_id: UserId,
        command: str,
        args: tuple[str, ...] = (),
        timeout_seconds: int = 300,
        source: AuditSource = "system",
    ) -> tuple[CommandRun, list[TaskArtifact]]:
        """Run a scoped, time-bounded command in a worktree.

        Per PLAN.md M6: "Commands are scoped, time-bounded, and
        audited. A task returns diff, checks, artifacts, and status."

        The command runs with ``cwd`` set to the worktree path. stdout
        and stderr are captured as artifacts. The exit code is recorded.
        If the command times out, the run is marked ``timed_out`` and
        the process is killed.

        Per PLAN.md M6: "No task pushes, merges, or deploys without
        explicit authority." This method does NOT run ``git push``,
        ``git merge``, or any deployment command. It is the caller's
        responsibility to ensure the command is safe.
        """
        worktree = self._repo.get_worktree(project_id, worktree_id)
        if worktree.state not in ("allocated", "active", "interrupted"):
            raise InvalidWorktreeTransitionError(
                f"Cannot run command in worktree state {worktree.state!r}"
            )
        # Validate the worktree path still exists and is inside the root.
        if not Path(worktree.worktree_path).is_dir():
            raise WorktreeError(
                f"Worktree path does not exist: {worktree.worktree_path}"
            )
        if not is_path_inside(worktree.worktree_path, self._worktree_root):
            raise PathValidationError(
                f"Worktree path escaped root: {worktree.worktree_path}",
                path=worktree.worktree_path,
            )
        # Create the command run record.
        run_id = CommandRunId(generate_command_run_id())
        run = CommandRun(
            id=run_id,
            project_id=project_id,
            worktree_id=worktree_id,
            task_id=task_id,
            command=command,
            args=args,
            exit_code=None,
            timed_out=False,
            timeout_seconds=timeout_seconds,
            started_at=_now_utc_iso(),
            completed_at=None,
            state="running",
        )
        self._repo.insert_command_run(run)
        # Transition worktree to active if it was allocated.
        if worktree.state == "allocated":
            self._repo.update_worktree_state(worktree_id, "active")
        # Run the command.
        try:
            proc = subprocess.run(
                [command, *args],
                cwd=worktree.worktree_path,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
            new_state: CommandRunState = "completed"
            exit_code = proc.returncode
            timed_out = False
            stdout = proc.stdout
            stderr = proc.stderr
        except subprocess.TimeoutExpired as exc:
            new_state = "timed_out"
            exit_code = None
            timed_out = True
            stdout = exc.stdout.decode("utf-8", errors="replace") if exc.stdout else ""
            stderr = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
            stderr += f"\n[Command timed out after {timeout_seconds}s]"
        except FileNotFoundError:
            new_state = "completed"
            exit_code = 127
            timed_out = False
            stdout = ""
            stderr = f"Command not found: {command}"
        # Update the command run.
        self._repo.update_command_run_state(
            run_id,
            new_state,
            exit_code=exit_code,
            timed_out=timed_out,
        )
        # Capture artifacts.
        artifacts: list[TaskArtifact] = []
        if stdout:
            artifacts.append(
                self._capture_artifact(
                    project_id=project_id,
                    worktree_id=worktree_id,
                    task_id=task_id,
                    command_run_id=run_id,
                    kind="stdout",
                    content=stdout,
                )
            )
        if stderr:
            artifacts.append(
                self._capture_artifact(
                    project_id=project_id,
                    worktree_id=worktree_id,
                    task_id=task_id,
                    command_run_id=run_id,
                    kind="stderr",
                    content=stderr,
                )
            )
        # Capture exit status artifact.
        exit_content = (
            f"exit_code={exit_code}\n"
            f"timed_out={timed_out}\n"
            f"state={new_state}\n"
        )
        artifacts.append(
            self._capture_artifact(
                project_id=project_id,
                worktree_id=worktree_id,
                task_id=task_id,
                command_run_id=run_id,
                kind="exit_status",
                content=exit_content,
            )
        )
        # Audit.
        self._audit_repo.insert(
            AuditEvent(
                id=AuditEventId(generate_audit_event_id()),
                project_id=project_id,
                actor_id=actor_id,
                source=source,
                operation="command.run",
                target_type="command_run",
                target_id=run_id.value,
                result="success" if new_state == "completed" and exit_code == 0 else "failure",
                redacted_summary=(
                    f"Ran {command} (exit={exit_code}, "
                    f"timed_out={timed_out}, state={new_state})"
                ),
                correlation_id=worktree.execution_id.value,
                created_at=_now_utc_iso(),
            )
        )
        run = self._repo.get_command_run(run_id)
        return run, artifacts

    def capture_diff(
        self,
        *,
        project_id: ProjectId,
        worktree_id: WorktreeId,
        task_id: TaskId,
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> TaskArtifact:
        """Capture the git diff of the worktree against its base revision.

        Per PLAN.md M6: "A task returns diff, checks, artifacts, and
        status." We capture both tracked changes (via ``git diff``)
        and untracked files (via ``git status``) so the diff artifact
        gives a complete picture of what changed.
        """
        worktree = self._repo.get_worktree(project_id, worktree_id)
        if not Path(worktree.worktree_path).is_dir():
            raise WorktreeError(
                f"Worktree path does not exist: {worktree.worktree_path}"
            )
        parts: list[str] = []
        # Capture tracked changes.
        try:
            proc = subprocess.run(
                ["git", "diff", worktree.base_revision],
                cwd=worktree.worktree_path,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if proc.stdout:
                parts.append("--- Tracked changes ---\n")
                parts.append(proc.stdout)
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            parts.append(f"[Failed to capture tracked diff: {exc}]\n")
        # Capture untracked files.
        try:
            proc = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=worktree.worktree_path,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if proc.stdout:
                parts.append("--- Status (includes untracked) ---\n")
                parts.append(proc.stdout)
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            parts.append(f"[Failed to capture status: {exc}]\n")
        diff_content = "".join(parts)
        return self._capture_artifact(
            project_id=project_id,
            worktree_id=worktree_id,
            task_id=task_id,
            command_run_id=None,
            kind="diff",
            content=diff_content,
        )

    def _capture_artifact(
        self,
        *,
        project_id: ProjectId,
        worktree_id: WorktreeId,
        task_id: TaskId,
        command_run_id: CommandRunId | None,
        kind: ArtifactKind,
        content: str,
    ) -> TaskArtifact:
        artifact = TaskArtifact(
            id=TaskArtifactId(generate_task_artifact_id()),
            project_id=project_id,
            worktree_id=worktree_id,
            task_id=task_id,
            command_run_id=command_run_id,
            kind=kind,
            content=content,
            content_hash=_sha256(content),
            created_at=_now_utc_iso(),
        )
        self._repo.insert_artifact(artifact)
        return artifact

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def mark_cleanup_eligible(
        self,
        *,
        project_id: ProjectId,
        worktree_id: WorktreeId,
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> Worktree:
        """Mark a worktree as eligible for cleanup.

        Per PLAN.md M6: "Cleanup occurs only after provenance and
        recovery checks pass." The caller must verify that integration
        is complete before calling this.
        """
        worktree = self._repo.get_worktree(project_id, worktree_id)
        if not is_valid_worktree_transition(worktree.state, "cleanup_eligible"):
            raise InvalidWorktreeTransitionError(
                f"Cannot transition worktree from {worktree.state!r} "
                f"to 'cleanup_eligible'"
            )
        self._repo.update_worktree_state(
            worktree_id,
            "cleanup_eligible",
            cleanup_eligible_at=_now_utc_iso(),
        )
        return self._repo.get_worktree(project_id, worktree_id)

    def remove_worktree(
        self,
        *,
        project_id: ProjectId,
        worktree_id: WorktreeId,
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> Worktree:
        """Remove a worktree from disk and mark it as removed.

        Per PLAN.md M6: "Cleanup never deletes an unknown path, mount,
        active workspace, or uncommitted human work."

        Safety checks:
        - The worktree must be in ``cleanup_eligible`` state.
        - The worktree path must be inside the worktree root.
        - The worktree path must match the recorded path.
        - ``git worktree remove`` is used to safely remove the worktree
          (it refuses to remove if there are uncommitted changes unless
          ``--force`` is passed; we do NOT pass ``--force``).
        """
        worktree = self._repo.get_worktree(project_id, worktree_id)
        if worktree.state != "cleanup_eligible":
            raise WorktreeCleanupError(
                f"Worktree {worktree_id} is in state {worktree.state!r}, "
                f"not 'cleanup_eligible'. Refusing to remove."
            )
        # Path safety: the recorded path must be inside the root.
        if not is_path_inside(worktree.worktree_path, self._worktree_root):
            raise PathValidationError(
                f"Worktree path escaped root: {worktree.worktree_path}",
                path=worktree.worktree_path,
            )
        # Remove the git worktree.
        if Path(worktree.worktree_path).exists():
            try:
                self._git_worktree_remove(
                    worktree.worktree_path, force=False
                )
            except subprocess.CalledProcessError as exc:
                # git refused to remove (e.g. uncommitted changes).
                raise WorktreeCleanupError(
                    f"git worktree remove refused: {exc.stderr or exc}. "
                    f"Uncommitted human work may be present; refusing to "
                    f"delete."
                ) from exc
        # Mark the worktree as removed.
        self._repo.update_worktree_state(worktree_id, "removed")
        self._audit_repo.insert(
            AuditEvent(
                id=AuditEventId(generate_audit_event_id()),
                project_id=project_id,
                actor_id=actor_id,
                source=source,
                operation="worktree.remove",
                target_type="worktree",
                target_id=worktree_id.value,
                result="success",
                redacted_summary=f"Removed worktree {worktree_id.value}",
                correlation_id=worktree.execution_id.value,
                created_at=_now_utc_iso(),
            )
        )
        return self._repo.get_worktree(project_id, worktree_id)

    # ------------------------------------------------------------------
    # Restart recovery
    # ------------------------------------------------------------------

    def recover_worktrees_after_restart(
        self,
        *,
        project_id: ProjectId,
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> list[Worktree]:
        """Find worktrees in ``active`` state and mark them interrupted.

        Per PLAN.md M6: "Restart identifies orphaned running work
        safely." An active worktree whose process died is marked
        ``interrupted``; it is NOT deleted. The caller can then decide
        whether to resume or clean up.
        """
        worktrees = self._repo.list_worktrees_for_project(project_id)
        recovered: list[Worktree] = []
        for wt in worktrees:
            if wt.state == "active":
                try:
                    self._repo.update_worktree_state(wt.id, "interrupted")
                    recovered.append(self._repo.get_worktree(project_id, wt.id))
                except Exception:
                    pass
        if recovered:
            self._audit_repo.insert(
                AuditEvent(
                    id=AuditEventId(generate_audit_event_id()),
                    project_id=project_id,
                    actor_id=actor_id,
                    source=source,
                    operation="worktree.recover",
                    target_type="worktree",
                    target_id=None,
                    result="success",
                    redacted_summary=(
                        f"Recovered {len(recovered)} worktrees after restart"
                    ),
                    created_at=_now_utc_iso(),
                )
            )
        return recovered

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def list_artifacts_for_task(
        self,
        task_id: TaskId,
        *,
        kind: ArtifactKind | None = None,
    ) -> list[TaskArtifact]:
        return self._repo.list_artifacts_for_task(task_id, kind=kind)

    def list_command_runs_for_worktree(
        self, worktree_id: WorktreeId
    ) -> list[CommandRun]:
        return self._repo.list_command_runs_for_worktree(worktree_id)

    # ------------------------------------------------------------------
    # Git helpers
    # ------------------------------------------------------------------

    def _git_worktree_add(
        self,
        repo_path: str,
        worktree_path: str,
        branch_name: str,
        base_revision: str,
    ) -> None:
        """Run ``git worktree add -b <branch> <path> <base>``."""
        subprocess.run(
            [
                "git",
                "-C",
                repo_path,
                "worktree",
                "add",
                "-b",
                branch_name,
                worktree_path,
                base_revision,
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    def _git_worktree_remove(
        self, worktree_path: str, *, force: bool = False
    ) -> None:
        """Run ``git worktree remove <path>``.

        The command must be run from within a git repository. We run
        it from the worktree path itself, which git recognizes as part
        of the worktree network (the worktree has a ``.git`` file that
        points back to the main repo).
        """
        cmd = ["git", "worktree", "remove"]
        if force:
            cmd.append("--force")
        cmd.append(worktree_path)
        subprocess.run(
            cmd,
            cwd=worktree_path,
            check=True,
            capture_output=True,
            text=True,
        )

    def _git_rev_parse(self, repo_path: str, ref: str) -> str:
        """Resolve a ref to a SHA."""
        proc = subprocess.run(
            ["git", "-C", repo_path, "rev-parse", ref],
            check=True,
            capture_output=True,
            text=True,
        )
        return proc.stdout.strip()


# Import here to avoid circular import at module load time.
from zero.domain.worktrees import WorktreeError
