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
import logging
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Literal

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
    CommandPolicyError,
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
    WorktreeError,
    WorktreeId,
    WorktreeState,
    is_valid_worktree_transition,
)
from zero.persistence.repositories.audit_repository import AuditRepository
from zero.persistence.repositories.execution_repository import ExecutionRepository
from zero.persistence.repositories.worktree_repository import (
    WorktreeRepository,
)

logger = logging.getLogger(__name__)


def _now_utc_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _run_bounded_git_output(
    args: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    timeout: float,
    max_bytes: int,
) -> tuple[str, int, bool]:
    """Run a git read command without buffering unbounded child output.

    A dedicated reader thread drains the child's stdout so the parent
    never blocks on a full pipe. This works on both POSIX selectors and
    Windows, where ``select()`` cannot wait on pipes.
    """
    capture_limit = max_bytes + 1
    process = subprocess.Popen(
        args,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    def terminate_process_group() -> None:
        if hasattr(os, "killpg"):
            try:
                os.killpg(process.pid, signal.SIGKILL)
                return
            except ProcessLookupError:
                pass
        try:
            process.kill()
        except ProcessLookupError:
            pass

    chunks: list[bytes] = []
    total = 0
    truncated_early = False

    def drain() -> None:
        nonlocal total, truncated_early
        assert process.stdout is not None
        while True:
            try:
                data = process.stdout.read(8192)
            except (OSError, ValueError):
                return
            if not data:
                return
            room = capture_limit - total
            if room > 0:
                chunks.append(data[:room])
                total += min(len(data), room)
            else:
                truncated_early = True
            if total >= capture_limit:
                terminate_process_group()

    reader = threading.Thread(target=drain, name="zero-bounded-git-reader", daemon=True)
    reader.start()
    try:
        reader.join(timeout=timeout)
        if reader.is_alive():
            # The child kept producing output past the deadline.
            terminate_process_group()
            reader.join(timeout=5)
            raise subprocess.TimeoutExpired(args, timeout)
        if (truncated_early or total >= capture_limit) and process.poll() is None:
            # The budget was reached; stop the child before waiting.
            terminate_process_group()
        process.wait(timeout=5)
    except BaseException:
        terminate_process_group()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        raise
    finally:
        if process.stdout is not None:
            process.stdout.close()
    raw = b"".join(chunks)
    truncated = len(raw) > max_bytes
    return raw[:max_bytes].decode("utf-8", errors="replace"), process.returncode, truncated


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
    # Reject links before resolving them.  Resolving a symlink and then
    # accepting its target turns a path indirection into an escape hatch.
    if p.is_symlink():
        raise PathValidationError(
            f"local_path must not be a symlink: {local_path!r}",
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


def validate_worktree_path(worktree_root: str, worktree_id: str) -> str:
    """Validate and construct a worktree path.

    The worktree path is ``<worktree_root>/<worktree_id>``. The
    worktree_root must be absolute and must not contain ``..``.
    """
    if not worktree_root or not isinstance(worktree_root, str):
        raise PathValidationError("worktree_root must not be empty", path=worktree_root)
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
        execution_repo: ExecutionRepository | None = None,
        worktree_root: str | None = None,
        allowed_commands: frozenset[str] | set[str] | tuple[str, ...] = (),
        isolation_mode: Literal["disabled", "host_bounded"] = "host_bounded",
        max_timeout_seconds: int = 300,
        max_output_bytes: int = 64 * 1024,
        command_executor: Any = None,
    ) -> None:
        self._repo = worktree_repo
        self._audit_repo = audit_repo
        self._authz = authorization_service
        self._execution_repo = execution_repo or ExecutionRepository(worktree_repo._database)
        # worktree_root is the parent directory under which all
        # worktrees are created. If None, a default is used.
        from zero.config import DEFAULT_WORKTREE_ROOT

        self._worktree_root = worktree_root or DEFAULT_WORKTREE_ROOT
        self._allowed_commands = frozenset(allowed_commands)
        if isolation_mode not in {"disabled", "host_bounded"}:
            raise ValueError("unsupported worktree isolation mode")
        self._isolation_mode = isolation_mode
        self._max_timeout_seconds = max_timeout_seconds
        self._max_output_bytes = max_output_bytes
        # GAP 3: pluggable sandbox backend; None keeps the historical
        # host-bounded path.
        self._command_executor = command_executor

    @property
    def max_command_timeout_seconds(self) -> int:
        """The configured upper bound for worktree command timeouts."""
        return self._max_timeout_seconds

    @property
    def allowed_commands(self) -> tuple[str, ...]:
        """The exact binaries ``run_command`` will execute.

        Public read-only view so tool declarations can advertise the same
        policy the validator enforces (the model must be able to see the
        constraints it is expected to satisfy).
        """
        return tuple(sorted(self._allowed_commands))

    def _ensure_private_worktree_root(self) -> Path:
        root = Path(self._worktree_root)
        if root.exists() and root.is_symlink():
            raise PathValidationError("worktree root must not be a symlink", path=str(root))
        if root.exists() and not root.is_dir():
            raise PathValidationError("worktree root must be a directory", path=str(root))
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(root, 0o700)
            owner = root.stat().st_uid
        except OSError as exc:
            raise WorktreeError(f"cannot secure worktree root: {exc}") from exc
        geteuid = getattr(os, "geteuid", None)
        if geteuid is None:
            # POSIX ownership semantics are unavailable (e.g. Windows,
            # where st_uid is a placeholder). The 0700 permission set is
            # best-effort there; surface the limitation instead of
            # silently pretending the ownership check passed.
            logger.warning(
                "worktree root %s: POSIX uid ownership check unavailable on this "
                "platform; relying on directory permissions only",
                root,
            )
        elif owner != geteuid():
            raise WorktreeError("worktree root is not owned by the service user")
        return root

    def _require_execution_isolation(self) -> None:
        """Reject execution when no genuine isolation contract is configured."""
        if self._isolation_mode == "disabled":
            raise CommandPolicyError(
                "command execution is disabled: no genuine isolation backend is configured"
            )

    def _require_no_active_commands(
        self,
        *,
        project_id: ProjectId,
        worktree_id: WorktreeId,
    ) -> None:
        active = self._repo.list_command_runs_for_worktree(
            worktree_id,
            project_id=project_id,
        )
        terminal = {"completed", "timed_out", "cancelled", "unknown"}
        if any(run.state not in terminal for run in active):
            raise WorktreeCleanupError(
                f"worktree {worktree_id} has a non-terminal command run; refusing cleanup"
            )

    #: Hardline floor (Hermes ``approval.py`` parity): commands and
    #: argument shapes that are ALWAYS refused, even when the command
    #: name itself is allowlisted. Zero's runner passes argv directly
    #: (no shell), so shell-injection patterns are structurally
    #: impossible; this floor targets host-damaging operations.
    _HARDLINE_COMMANDS: ClassVar[frozenset[str]] = frozenset(
        {
            "mkfs",
            "mkfs.ext2",
            "mkfs.ext4",
            "mkfs.xfs",
            "shutdown",
            "reboot",
            "halt",
            "poweroff",
            "diskpart",
        }
    )
    _DEVICE_TARGET_RE: ClassVar[re.Pattern[str]] = re.compile(
        r"of=/dev/(?:sd|nvme|hd|mmcblk|vd|xvd)", re.IGNORECASE
    )
    _FORK_BOMB_RE: ClassVar[re.Pattern[str]] = re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;")

    def _validate_command(
        self,
        command: str,
        args: tuple[str, ...],
        timeout_seconds: int,
    ) -> None:
        """Apply the fail-closed command policy before touching a worktree."""
        lowered = command.lower() if isinstance(command, str) else ""
        # Hardline floor FIRST: unconditional refusals apply even when
        # an operator has (ill-advisedly) allowlisted the name.
        if lowered in self._HARDLINE_COMMANDS or lowered.startswith("mkfs"):
            raise CommandPolicyError(f"command {command!r} is unconditionally refused")
        for arg in args or ():
            if isinstance(arg, str) and (
                self._DEVICE_TARGET_RE.search(arg) or self._FORK_BOMB_RE.search(arg)
            ):
                raise CommandPolicyError(
                    "command arguments target host-destructive operations and are refused"
                )
            stripped = arg.strip() if isinstance(arg, str) else ""
            if lowered == "rm" and stripped in {"/", "/*"}:
                raise CommandPolicyError("rm of filesystem root is refused")
        if (
            not command
            or not isinstance(command, str)
            or Path(command).name != command
            or "/" in command
            or "\\" in command
            or command not in self._allowed_commands
        ):
            raise CommandPolicyError(
                f"command {command!r} is not permitted by the configured policy"
            )
        if not isinstance(args, tuple) or len(args) > 64:
            raise CommandPolicyError("command arguments exceed the policy limit")
        if any(not isinstance(arg, str) or "\x00" in arg or len(arg) > 8192 for arg in args):
            raise CommandPolicyError("command argument violates the policy")
        if timeout_seconds < 1 or timeout_seconds > self._max_timeout_seconds:
            raise CommandPolicyError(
                f"timeout must be between 1 and {self._max_timeout_seconds} seconds"
            )

    def _run_bounded_process(
        self,
        argv: list[str],
        *,
        cwd: str,
        timeout_seconds: int,
    ) -> tuple[int | None, bool, str, str]:
        """Run one allowlisted command with bounded output and group cleanup.

        GAP 3: when a sandbox executor is configured the command runs
        through it; callers never know the backend.
        """
        if self._command_executor is not None:
            result = self._command_executor.execute(
                argv,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                output_limit=self._max_output_bytes,
            )
            return result.exit_code, result.timed_out, result.stdout, result.stderr
        from zero.app.executors.sandbox import run_bounded_process, scrubbed_env

        exec_result = run_bounded_process(
            argv,
            cwd=cwd,
            env=scrubbed_env(cwd),
            timeout_seconds=timeout_seconds,
            output_limit=self._max_output_bytes,
        )
        return (
            exec_result.exit_code,
            exec_result.timed_out,
            exec_result.stdout,
            exec_result.stderr,
        )

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
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="execution.start",
            source=source,
        )
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
        self,
        project_id: ProjectId,
        repo_id: RepositoryId,
        *,
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> Repository:
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="execution.view_diffs",
            source=source,
        )
        return self._repo.get_repository(project_id, repo_id)

    def list_repositories(
        self,
        project_id: ProjectId,
        *,
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> list[Repository]:
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="execution.view_diffs",
            source=source,
        )
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
        self._require_execution_isolation()
        # Get the repository (project-scoped).
        repository = self._repo.get_repository(project_id, repository_id)
        # Validate the complete project -> execution -> task lineage before
        # creating directories or invoking Git.
        execution = self._execution_repo.get_execution(execution_id, project_id=project_id)
        task = self._execution_repo.get_task(task_id, project_id=project_id)
        if task.execution_id != execution.id:
            raise WorktreeError(f"Task {task_id} does not belong to execution {execution_id}")
        # Resolve the base revision.
        rev = base_revision or repository.default_base_revision or "HEAD"
        # Generate IDs.
        worktree_id = WorktreeId(generate_worktree_id())
        branch_name = f"zero/{worktree_id.value}"
        worktree_path = validate_worktree_path(self._worktree_root, worktree_id.value)
        # Ensure the worktree root exists, is private, and is owned by
        # the service user before allocating a child path.
        self._ensure_private_worktree_root()
        # Create the worktree using git.
        try:
            self._git_worktree_add(
                repository.local_path,
                worktree_path,
                branch_name,
                rev,
            )
            os.chmod(worktree_path, 0o700)
        except (subprocess.CalledProcessError, OSError) as exc:
            # Clean up the directory if Git or root hardening failed.
            shutil.rmtree(worktree_path, ignore_errors=True)
            detail = getattr(exc, "stderr", None) or exc
            raise WorktreeError(f"Failed to create worktree: {detail}") from exc
        # Capture the actual base revision (resolve HEAD to a SHA).
        try:
            actual_base = self._git_rev_parse(worktree_path, "HEAD")
        except (OSError, WorktreeError, subprocess.SubprocessError) as exc:
            try:
                self._git_worktree_remove(worktree_path, force=True)
            except (OSError, WorktreeError):
                shutil.rmtree(worktree_path, ignore_errors=True)
            raise WorktreeError("failed to resolve created worktree HEAD") from exc
        self._ensure_worktree_gitignore(Path(worktree_path))
        # Commit the hygiene baseline immediately: the worktree is clean
        # right after creation, so capture_diff can never be satisfied by
        # the untracked .gitignore itself, and the ignore rules ride
        # along the branch lineage for downstream tasks.
        self._git_commit_all(
            Path(worktree_path),
            "zero: worktree hygiene baseline",
            worktree_id=worktree_id.value,
        )
        # Hermes-audit fix (real bug #15, 2026-08-28): the diff BASE must
        # include the hygiene baseline. With the tracked-diff capture
        # actually working again (flag-order fix in capture_diff), the
        # auto-managed .gitignore otherwise surfaced as a tracked change
        # in EVERY task's diff artifact — two tasks then "conflicted" on
        # .gitignore and integration reviews demanded human decisions
        # for purely server-managed infrastructure. The base is
        # re-resolved after the baseline commit so diffs show only the
        # task's own work.
        try:
            actual_base = self._git_rev_parse(worktree_path, "HEAD")
        except (OSError, WorktreeError, subprocess.SubprocessError) as exc:
            try:
                self._git_worktree_remove(worktree_path, force=True)
            except (OSError, WorktreeError):
                shutil.rmtree(worktree_path, ignore_errors=True)
            raise WorktreeError("failed to resolve post-baseline worktree HEAD") from exc
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
                self._git_worktree_remove(worktree_path)
            except (OSError, WorktreeError):
                logger.debug("duplicate worktree cleanup failed")
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
                redacted_summary=(f"Created worktree {worktree.id.value} for task {task_id.value}"),
                correlation_id=execution_id.value,
                created_at=_now_utc_iso(),
            )
        )
        return worktree

    def _ensure_worktree_gitignore(self, worktree_path: str) -> None:
        """Keep interpreter/build noise out of diffs and evidence commits.

        Real-run fix (2026-08-28): ``git add -A`` in the evidence
        checkpoint committed ``__pycache__/*.pyc`` bytecode, and a later
        task's diff evidence was satisfied by bytecode churn while the
        actual required file was never produced. A worktree-local
        .gitignore (never overwriting one the repository already ships)
        keeps capture_diff and the commit clean; adding it here is
        server-owned hygiene, not model-visible work.
        """
        ignore = worktree_path / ".gitignore"
        if ignore.exists():
            return
        try:
            ignore.write_text(
                (
                    "# Zero worktree hygiene (auto-managed)\n"
                    "__pycache__/\n"
                    "*.py[cod]\n"
                    ".pytest_cache/\n"
                    ".mypy_cache/\n"
                    ".ruff_cache/\n"
                    ".coverage\n"
                ),
                encoding="utf-8",
            )
            os.chmod(ignore, 0o600)
        except OSError as exc:
            logger.debug("worktree .gitignore write failed: %s", exc)

    def activate_worktree(
        self,
        *,
        project_id: ProjectId,
        worktree_id: WorktreeId,
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> Worktree:
        """Transition a worktree from ``allocated`` to ``active``."""
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="execution.start",
            source=source,
        )
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
        """Transition a worktree to ``succeeded`` or ``failed``.

        Bug fix (real run, 2026-08-28): a succeeded worktree used to keep
        its results as uncommitted working-directory changes, so the task
        branch held nothing. Downstream tasks (which branch from their
        dependencies' worktree branches — see the runtime's base
        resolution) therefore started from the bare repository and could
        never see earlier tasks' files; a "run the tests" task failed
        because its worktree contained no test suite. On success the
        service now commits the full worktree state onto the task branch
        (internal, server-owned git — deliberately NOT the model-facing
        run_command policy) so branch refs carry the durable result.
        """
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="execution.start",
            source=source,
        )
        worktree = self._repo.get_worktree(project_id, worktree_id)
        new_state: WorktreeState = "succeeded" if succeeded else "failed"
        if not is_valid_worktree_transition(worktree.state, new_state):
            raise InvalidWorktreeTransitionError(
                f"Cannot transition worktree from {worktree.state!r} to {new_state!r}"
            )
        if succeeded:
            self._commit_worktree_state(worktree)
        self._repo.update_worktree_state(worktree_id, new_state)
        return self._repo.get_worktree(project_id, worktree_id)

    def _commit_worktree_state(self, worktree) -> None:
        """Commit the whole worktree state onto its task branch.

        Internal git invocation with an injected identity (repositories
        may not carry user.name/user.email). ``--allow-empty`` keeps
        file-less tasks (read-only analysis) chainable too. A failed
        commit is logged and does NOT fail the completion: the evidence
        artifacts already hold the durable result, and downstream base
        resolution falls back to the repository default when a branch
        has no usable commit.
        """

        worktree_path = Path(worktree.worktree_path)
        if not worktree_path.is_dir():
            logger.warning(
                "worktree %s cannot be committed: path missing", worktree.id.value
            )
            return
        message = f"zero(task {worktree.task_id.value}): evidence checkpoint"
        self._git_commit_all(worktree_path, message, worktree_id=worktree.id.value)

    def _git_commit_all(
        self,
        worktree_path: Path | str,
        message: str,
        *,
        worktree_id: str | None = None,
    ) -> None:
        """Commit the full worktree state (internal, server-owned git)."""
        import subprocess as _sp

        commands = (
            ["git", "add", "-A", "--"],
            [
                "git",
                "-c", "user.name=Zero Runtime",
                "-c", "user.email=zero@internal",
                "commit",
                "--allow-empty",
                "-q",
                "-m", message,
            ],
        )
        for argv in commands:
            try:
                proc = _sp.run(
                    argv,
                    cwd=str(worktree_path),
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
            except (OSError, _sp.TimeoutExpired) as exc:
                logger.warning(
                    "worktree %s commit error: %s", worktree_id or "?", exc
                )
                return
            if proc.returncode != 0:
                logger.warning(
                    "worktree %s commit failed (%s): %s",
                    worktree_id or "?",
                    argv[1] if len(argv) > 1 else argv,
                    (proc.stderr or proc.stdout or "").strip()[:300],
                )
                return

    def mark_worktree_interrupted(
        self,
        *,
        project_id: ProjectId,
        worktree_id: WorktreeId,
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> Worktree:
        """Mark a worktree as interrupted (e.g. after a crash)."""
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="execution.start",
            source=source,
        )
        worktree = self._repo.get_worktree(project_id, worktree_id)
        if not is_valid_worktree_transition(worktree.state, "interrupted"):
            raise InvalidWorktreeTransitionError(
                f"Cannot transition worktree from {worktree.state!r} to 'interrupted'"
            )
        self._repo.update_worktree_state(worktree_id, "interrupted")
        return self._repo.get_worktree(project_id, worktree_id)

    def get_worktree(
        self,
        project_id: ProjectId,
        worktree_id: WorktreeId,
        *,
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> Worktree:
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="execution.view_diffs",
            source=source,
        )
        return self._repo.get_worktree(project_id, worktree_id)

    def get_worktree_for_task(
        self,
        project_id: ProjectId,
        task_id: TaskId,
        *,
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> Worktree | None:
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="execution.view_diffs",
            source=source,
        )
        return self._repo.get_worktree_for_task(task_id, project_id=project_id)

    def list_worktrees_for_execution(
        self,
        project_id: ProjectId,
        execution_id: ExecutionId,
        *,
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> list[Worktree]:
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="execution.view_diffs",
            source=source,
        )
        return self._repo.list_worktrees_for_execution(
            execution_id,
            project_id=project_id,
        )

    def list_worktrees_for_project(
        self,
        project_id: ProjectId,
        *,
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> list[Worktree]:
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="execution.view_diffs",
            source=source,
        )
        return self._repo.list_worktrees_for_project(project_id)

    # ------------------------------------------------------------------
    # Scoped file operations
    # ------------------------------------------------------------------

    def _owned_worktree_for_file(
        self,
        *,
        project_id: ProjectId,
        worktree_id: WorktreeId,
        task_id: TaskId,
        actor_id: UserId,
        permission: Literal["execution.start", "execution.view_diffs"],
        source: AuditSource,
    ) -> Worktree:
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission=permission,
            source=source,
        )
        worktree = self._repo.get_worktree(project_id, worktree_id)
        if worktree.task_id != task_id:
            raise WorktreeError("task does not own the requested worktree")
        if worktree.state not in ("allocated", "active", "interrupted"):
            raise InvalidWorktreeTransitionError(
                f"Cannot access files in worktree state {worktree.state!r}"
            )
        worktree_path = Path(worktree.worktree_path)
        if worktree_path.is_symlink() or not worktree_path.is_dir():
            raise PathValidationError(
                "worktree path is not a resident directory", path=worktree.worktree_path
            )
        if not is_path_inside(str(worktree_path), self._worktree_root):
            raise PathValidationError(
                "worktree path escaped the private root", path=worktree.worktree_path
            )
        return worktree

    @staticmethod
    def _resolve_task_file(worktree: Worktree, relative_path: str) -> tuple[Path, str]:
        if not isinstance(relative_path, str) or not relative_path.strip():
            raise PathValidationError("relative_path must not be empty", path=str(relative_path))
        if len(relative_path) > 4096:
            raise PathValidationError("relative_path is too long", path=relative_path)
        candidate = Path(relative_path)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise PathValidationError(
                "relative_path must stay inside the worktree", path=relative_path
            )
        if any(part == ".git" or part.startswith(".git/") for part in candidate.parts):
            raise PathValidationError("Git metadata is not a task file", path=relative_path)
        root = Path(worktree.worktree_path).resolve(strict=True)
        target = root.joinpath(candidate)
        try:
            resolved = target.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise PathValidationError(
                "relative_path could not be resolved", path=relative_path
            ) from exc
        if not is_path_inside(str(resolved), str(root)):
            raise PathValidationError("relative_path escaped the worktree", path=relative_path)
        if target.exists() and target.is_symlink():
            raise PathValidationError("symlinked task files are not permitted", path=relative_path)
        return resolved, str(candidate)

    def read_file(
        self,
        *,
        project_id: ProjectId,
        worktree_id: WorktreeId,
        task_id: TaskId,
        actor_id: UserId,
        relative_path: str,
        max_bytes: int = 256 * 1024,
        source: AuditSource = "system",
    ) -> str:
        """Read a bounded UTF-8 file from the task-owned worktree."""
        if max_bytes < 1 or max_bytes > 1024 * 1024:
            raise ValueError("max_bytes must be between 1 and 1048576")
        worktree = self._owned_worktree_for_file(
            project_id=project_id,
            worktree_id=worktree_id,
            task_id=task_id,
            actor_id=actor_id,
            permission="execution.start",
            source=source,
        )
        path, display_path = self._resolve_task_file(worktree, relative_path)
        if not path.is_file():
            raise WorktreeError(f"task file does not exist: {display_path}")
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise WorktreeError(f"task file could not be read: {display_path}") from exc
        if len(raw) > max_bytes:
            raise WorktreeError(f"task file exceeds the {max_bytes}-byte read limit")
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorktreeError("binary task files are not exposed to the text model") from exc
        self._audit_repo.insert(
            AuditEvent(
                id=AuditEventId(generate_audit_event_id()),
                project_id=project_id,
                actor_id=actor_id,
                source=source,
                operation="file.read",
                target_type="worktree_file",
                target_id=f"{worktree.id.value}:{display_path}",
                result="success",
                redacted_summary=f"Read bounded task file {display_path!r}",
                correlation_id=worktree.execution_id.value,
                created_at=_now_utc_iso(),
            )
        )
        return content

    def write_file(
        self,
        *,
        project_id: ProjectId,
        worktree_id: WorktreeId,
        task_id: TaskId,
        actor_id: UserId,
        relative_path: str,
        content: str,
        max_bytes: int = 256 * 1024,
        source: AuditSource = "system",
    ) -> str:
        """Atomically write one bounded UTF-8 file inside a task worktree."""
        if not isinstance(content, str):
            raise TypeError("content must be text")
        encoded = content.encode("utf-8")
        if len(encoded) > max_bytes or max_bytes < 1 or max_bytes > 1024 * 1024:
            raise WorktreeError(f"task file exceeds the {max_bytes}-byte write limit")
        worktree = self._owned_worktree_for_file(
            project_id=project_id,
            worktree_id=worktree_id,
            task_id=task_id,
            actor_id=actor_id,
            permission="execution.start",
            source=source,
        )
        path, display_path = self._resolve_task_file(worktree, relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.is_symlink():
            raise PathValidationError("symlinked task files are not permitted", path=relative_path)
        temporary = path.with_name(f".{path.name}.zero-write-{os.getpid()}-{threading.get_ident()}")
        try:
            temporary.write_bytes(encoded)
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise WorktreeError(f"task file could not be written: {display_path}") from exc
        content_hash = _sha256(content)
        self._audit_repo.insert(
            AuditEvent(
                id=AuditEventId(generate_audit_event_id()),
                project_id=project_id,
                actor_id=actor_id,
                source=source,
                operation="file.write",
                target_type="worktree_file",
                target_id=f"{worktree.id.value}:{display_path}",
                result="success",
                redacted_summary=(
                    f"Wrote task file {display_path!r} "
                    f"({len(encoded)} bytes, sha256={content_hash})"
                ),
                correlation_id=worktree.execution_id.value,
                created_at=_now_utc_iso(),
            )
        )
        return content_hash

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
        # Check authority and the explicit policy before looking up the
        # worktree.  This both fails closed and avoids exposing resource
        # details to callers without execution authority.
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="execution.start",
            source=source,
        )
        self._require_execution_isolation()
        self._validate_command(command, args, timeout_seconds)
        worktree = self._repo.get_worktree(project_id, worktree_id)
        if worktree.task_id != task_id:
            raise WorktreeError("task does not own the requested worktree; refusing command")
        if worktree.state not in ("allocated", "active", "interrupted"):
            raise InvalidWorktreeTransitionError(
                f"Cannot run command in worktree state {worktree.state!r}"
            )
        # Validate the worktree path still exists and is inside the root.
        if not Path(worktree.worktree_path).is_dir():
            raise WorktreeError(f"Worktree path does not exist: {worktree.worktree_path}")
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
        # Run the command in a clean environment and a dedicated process
        # group.  The helper drains pipes while the process runs, so a noisy
        # child cannot deadlock the runner or grow stored output without a
        # bound.
        try:
            exit_code, timed_out, stdout, stderr = self._run_bounded_process(
                [command, *args],
                cwd=worktree.worktree_path,
                timeout_seconds=timeout_seconds,
            )
        except (OSError, WorktreeError, subprocess.SubprocessError) as exc:
            try:
                self._repo.update_command_run_state(
                    run_id,
                    "unknown",
                    exit_code=None,
                    timed_out=False,
                )
            except (OSError, RuntimeError, sqlite3.Error) as cleanup_exc:
                logger.debug(
                    "command-run failure state could not be persisted: %s",
                    type(cleanup_exc).__name__,
                )
            raise WorktreeError(f"command runner failed before completion: {exc}") from exc
        new_state: CommandRunState = "timed_out" if timed_out else "completed"
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
        exit_content = f"exit_code={exit_code}\ntimed_out={timed_out}\nstate={new_state}\n"
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
                    f"Ran {command} (exit={exit_code}, timed_out={timed_out}, state={new_state})"
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
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="execution.view_diffs",
            source=source,
        )
        self._require_execution_isolation()
        worktree = self._repo.get_worktree(project_id, worktree_id)
        if worktree.task_id != task_id:
            raise WorktreeError("task does not own the requested worktree; refusing diff")
        worktree_path = Path(worktree.worktree_path)
        if worktree_path.is_symlink() or not is_path_inside(
            str(worktree_path), self._worktree_root
        ):
            raise PathValidationError(
                "worktree path is outside the private worktree root",
                path=worktree.worktree_path,
            )
        if not worktree_path.is_dir():
            raise WorktreeError(f"Worktree path does not exist: {worktree.worktree_path}")
        parts: list[str] = []
        # Capture tracked changes.
        try:
            tracked_output, tracked_returncode, tracked_truncated = _run_bounded_git_output(
                [
                    "git",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "diff.external=",
                    "-C",
                    worktree.worktree_path,
                    # Hermes-audit fix (real bug #14, 2026-08-28):
                    # --no-ext-diff/--no-textconv are options of the
                    # ``diff`` subcommand, NOT global git options. Before
                    # the subcommand git exits 129 (stderr devnull-
                    # swallowed) and the tracked diff was silently EMPTY
                    # forever — diff evidence only ever showed the
                    # untracked status section.
                    "diff",
                    "--no-ext-diff",
                    "--no-textconv",
                    worktree.base_revision,
                ],
                cwd=worktree.worktree_path,
                timeout=60,
                env=self._git_environment(),
                max_bytes=self._max_output_bytes,
            )
            if tracked_output:
                parts.append("--- Tracked changes ---\n")
                parts.append(tracked_output)
            if tracked_truncated:
                parts.append("\n[tracked diff truncated by policy]\n")
            if not tracked_output and tracked_returncode != 0:
                # Hermes-audit hardening (real bug #14 class, 2026-08-28):
                # a failed git invocation must never masquerade as "no
                # changes". Surface the failure in the evidence artifact.
                parts.append(
                    f"[git diff failed with exit code {tracked_returncode}; "
                    "tracked changes could not be captured]\n"
                )
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            parts.append(f"[Failed to capture tracked diff: {exc}]\n")
        # Capture untracked files.
        try:
            status_output, _returncode, status_truncated = _run_bounded_git_output(
                [
                    "git",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-C",
                    worktree.worktree_path,
                    "status",
                    "--porcelain",
                ],
                cwd=worktree.worktree_path,
                timeout=60,
                env=self._git_environment(),
                max_bytes=self._max_output_bytes,
            )
            if status_output:
                parts.append("--- Status (includes untracked) ---\n")
                parts.append(status_output)
            if status_truncated:
                parts.append("\n[git status truncated by policy]\n")
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            parts.append(f"[Failed to capture status: {exc}]\n")
        diff_content = "".join(parts)
        # Hermes-parity audit fix (real run r10, 2026-08-28): a chained
        # aggregation task ("capture the final diff of everything")
        # branches from succeeded dependency branches whose evidence
        # checkpoints already contain all earlier work. Its INCREMENTAL
        # diff is therefore empty even though the execution's real
        # change set is large — the task failed with "required diff
        # evidence contains no file change". When this task changed
        # nothing on top of its base, fall back to the cumulative diff
        # against the repository's default base revision so aggregate
        # evidence stays provable and truthful (clearly labeled).
        if not diff_content.strip():
            cumulative = self._cumulative_execution_diff(worktree)
            if cumulative:
                diff_content = (
                    "--- Cumulative execution diff ---\n"
                    "(this task made no changes on top of its dependency "
                    "branches; the diff below is the whole execution's "
                    "change set against the repository base revision)\n\n"
                    + cumulative
                )
        encoded = diff_content.encode("utf-8")
        if len(encoded) > self._max_output_bytes:
            diff_content = (
                encoded[: self._max_output_bytes].decode("utf-8", errors="replace")
                + "\n[output truncated by policy]\n"
            )
        return self._capture_artifact(
            project_id=project_id,
            worktree_id=worktree_id,
            task_id=task_id,
            command_run_id=None,
            kind="diff",
            content=diff_content,
        )

    def _cumulative_execution_diff(self, worktree) -> str:
        """Diff the worktree HEAD against the repository's default base.

        Chained tasks branch from succeeded dependency branches whose
        evidence checkpoints already contain the earlier tasks' committed
        work, so an aggregation task's incremental diff is empty while
        the execution's real change set is large. Best-effort: any
        resolution/transport failure returns ``""`` (the historical
        empty-diff evidence verdict then applies unchanged).
        """
        try:
            repository = self._repo.get_repository(worktree.project_id, worktree.repository_id)
        except Exception:  # noqa: BLE001 - best-effort evidence fallback
            return ""
        base = (repository.default_base_revision or "").strip()
        if not base:
            try:
                head_output, _rc, _truncated = _run_bounded_git_output(
                    ["git", "-C", repository.local_path, "rev-parse", "HEAD"],
                    cwd=repository.local_path,
                    timeout=30,
                    env=self._git_environment(),
                    max_bytes=self._max_output_bytes,
                )
                base = head_output.strip()
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                return ""
        if not base or base == worktree.base_revision:
            # No dependency chain: the incremental diff IS the cumulative
            # one, and it was already empty — nothing more to prove.
            return ""
        try:
            output, _rc, truncated = _run_bounded_git_output(
                [
                    "git",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "diff.external=",
                    "-C",
                    worktree.worktree_path,
                    "diff",
                    "--no-ext-diff",
                    "--no-textconv",
                    f"{base}..HEAD",
                ],
                cwd=worktree.worktree_path,
                timeout=60,
                env=self._git_environment(),
                max_bytes=self._max_output_bytes,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return ""
        if truncated:
            output += "\n[cumulative diff truncated by policy]\n"
        return output

    def capture_source_snapshot(
        self,
        *,
        project_id: ProjectId,
        worktree_id: WorktreeId,
        task_id: TaskId,
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> TaskArtifact:
        """Capture a typed record of the worktree's source state.

        The snapshot records the exact base commit, the current HEAD,
        and the porcelain status so the source state a task started
        from (and ended in) is provable without shipping file contents.
        This makes the ``source_snapshot`` evidence label satisfiable by
        the runtime instead of permanently unprovable.
        """
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="execution.view_diffs",
            source=source,
        )
        self._require_execution_isolation()
        worktree = self._repo.get_worktree(project_id, worktree_id)
        if worktree.task_id != task_id:
            raise WorktreeError("task does not own the requested worktree; refusing snapshot")
        worktree_path = Path(worktree.worktree_path)
        if worktree_path.is_symlink() or not is_path_inside(
            str(worktree_path), self._worktree_root
        ):
            raise PathValidationError(
                "worktree path is outside the private worktree root",
                path=worktree.worktree_path,
            )
        if not worktree_path.is_dir():
            raise WorktreeError(f"Worktree path does not exist: {worktree.worktree_path}")
        parts: list[str] = [
            f"base_revision: {worktree.base_revision}\n",
        ]
        for label, args in (
            ("head", ["rev-parse", "HEAD"]),
            ("status", ["status", "--porcelain"]),
        ):
            try:
                output, _returncode, truncated = _run_bounded_git_output(
                    [
                        "git",
                        "-c",
                        "core.hooksPath=/dev/null",
                        "-C",
                        worktree.worktree_path,
                        *args,
                    ],
                    cwd=worktree.worktree_path,
                    timeout=60,
                    env=self._git_environment(),
                    max_bytes=self._max_output_bytes,
                )
                parts.append(f"--- {label} ---\n{output}")
                if truncated:
                    parts.append(f"\n[{label} truncated by policy]\n")
            except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
                parts.append(f"[Failed to capture {label}: {exc}]\n")
        # The worktree-local task_artifacts table does not carry this
        # kind (its CHECK predates the evidence label); the caller
        # persists the snapshot canonically at the project artifact
        # layer, which does accept ``source_snapshot``.
        content = "".join(parts)
        return TaskArtifact(
            id=TaskArtifactId(generate_task_artifact_id()),
            project_id=project_id,
            worktree_id=worktree_id,
            task_id=task_id,
            command_run_id=None,
            kind="source_snapshot",
            content=content,
            content_hash=_sha256(content),
            created_at=_now_utc_iso(),
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
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="execution.start",
            source=source,
        )
        worktree = self._repo.get_worktree(project_id, worktree_id)
        self._require_no_active_commands(
            project_id=project_id,
            worktree_id=worktree_id,
        )
        if not is_valid_worktree_transition(worktree.state, "cleanup_eligible"):
            raise InvalidWorktreeTransitionError(
                f"Cannot transition worktree from {worktree.state!r} to 'cleanup_eligible'"
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
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="execution.start",
            source=source,
        )
        worktree = self._repo.get_worktree(project_id, worktree_id)
        self._require_no_active_commands(
            project_id=project_id,
            worktree_id=worktree_id,
        )
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
                self._git_worktree_remove(worktree.worktree_path, force=False)
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
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="execution.start",
            source=source,
        )
        worktrees = self._repo.list_worktrees_for_project(project_id)
        recovered: list[Worktree] = []
        for wt in worktrees:
            if wt.state == "active":
                try:
                    self._repo.update_worktree_state(wt.id, "interrupted")
                    recovered.append(self._repo.get_worktree(project_id, wt.id))
                except (OSError, RuntimeError, WorktreeError, sqlite3.Error) as recovery_exc:
                    logger.debug(
                        "worktree recovery failed: %s",
                        type(recovery_exc).__name__,
                    )
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
                    redacted_summary=(f"Recovered {len(recovered)} worktrees after restart"),
                    created_at=_now_utc_iso(),
                )
            )
        return recovered

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def list_artifacts_for_task(
        self,
        project_id: ProjectId,
        task_id: TaskId,
        *,
        actor_id: UserId,
        kind: ArtifactKind | None = None,
        source: AuditSource = "system",
    ) -> list[TaskArtifact]:
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="execution.view_diffs",
            source=source,
        )
        return self._repo.list_artifacts_for_task(
            task_id,
            project_id=project_id,
            kind=kind,
        )

    def list_command_runs_for_worktree(
        self,
        project_id: ProjectId,
        worktree_id: WorktreeId,
        *,
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> list[CommandRun]:
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="execution.view_diffs",
            source=source,
        )
        return self._repo.list_command_runs_for_worktree(
            worktree_id,
            project_id=project_id,
        )

    # ------------------------------------------------------------------
    # Git helpers
    # ------------------------------------------------------------------

    def _git_environment(self) -> dict[str, str]:
        return {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": "/nonexistent",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "GIT_EDITOR": ":",
        }

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
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "diff.external=",
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
            timeout=60,
            env=self._git_environment(),
        )

    def _git_worktree_remove(self, worktree_path: str, *, force: bool = False) -> None:
        """Run ``git worktree remove <path>``.

        The command must run from within the repository network. We
        resolve the main repository from the worktree's ``.git`` file
        and run from there. In particular we do NOT use the worktree
        itself as the working directory: on Windows a process holds a
        delete lock on its current directory, which would make the
        removal fail with a spurious permission error.
        """
        cmd = ["git", "-c", "core.hooksPath=/dev/null", "worktree", "remove"]
        if force:
            cmd.append("--force")
        cmd.append(worktree_path)
        subprocess.run(
            cmd,
            cwd=self._worktree_parent_repo_path(worktree_path),
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
            env=self._git_environment(),
        )

    @staticmethod
    def _worktree_parent_repo_path(worktree_path: str) -> str:
        """Resolve the main repository path for a linked worktree."""
        git_file = Path(worktree_path) / ".git"
        if git_file.is_file():
            try:
                first_line = git_file.read_text(encoding="utf-8", errors="replace").splitlines()[0]
            except (OSError, IndexError):
                first_line = ""
            if first_line.startswith("gitdir:"):
                gitdir = Path(first_line[len("gitdir:") :].strip())
                # ``<repo>/.git/worktrees/<name>`` -> repo root is three
                # levels up when relative; absolute paths are kept.
                if not gitdir.is_absolute():
                    gitdir = Path(worktree_path) / gitdir
                candidate = gitdir.parent.parent.parent
                if (candidate / ".git").exists():
                    return str(candidate)
        return worktree_path

    def _git_rev_parse(self, repo_path: str, ref: str) -> str:
        """Resolve a ref to a SHA."""
        proc = subprocess.run(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "diff.external=",
                "-C",
                repo_path,
                "rev-parse",
                ref,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
            env=self._git_environment(),
        )
        return proc.stdout.strip()
