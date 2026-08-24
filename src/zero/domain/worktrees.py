"""Repository and worktree domain types.

Per ``zero-agent-execution-lifecycle`` SKILL.md §"A worktree is a
safety boundary, not an organizational preference":

- Concurrent coding tasks must not write into one working directory.
  A branch names a history line; a worktree provides a separate
  filesystem view. Both matter.
- A task's execution identity should be able to resolve: repository
  and immutable base revision; branch and worktree identity; allowed
  path or domain scope when applicable; assigned agent instance;
  running process/tool leases; diff and artifact references; cleanup
  state.

Per ``zero-recovery-consistency`` §"Cleanup requires proof of
non-ownership": Before a worktree, artifact, cache, or temporary path
is removed, Zero needs evidence that it belongs to the intended task,
has no active process/service/mount dependency, and has preserved
required human work or recovery artifacts. Validated exact paths and
lineage are safer than broad age-based directory deletion.

Per PLAN.md M6 invariants:
- Every coding task receives an isolated branch and working tree.
- The target repository and base revision are explicit.
- Commands are scoped, time-bounded, and audited.
- A task returns diff, checks, artifacts, and status.
- No task pushes, merges, or deploys without explicit authority.
- Cleanup never deletes an unknown path, mount, active workspace, or
  uncommitted human work.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from zero.domain.execution import ExecutionId, TaskId
from zero.domain.identity import ProjectId

#: Prefixes for stable server-issued IDs.
REPOSITORY_ID_PREFIX = "repo_"
WORKTREE_ID_PREFIX = "wt_"
COMMAND_RUN_ID_PREFIX = "cr_"
TASK_ARTIFACT_ID_PREFIX = "art_"

# ----------------------------------------------------------------------
# Worktree state machine
# ----------------------------------------------------------------------

WorktreeState = Literal[
    "allocated",  # worktree created, not yet active
    "active",  # command(s) running
    "interrupted",  # process died or was interrupted; needs recovery
    "succeeded",  # task completed successfully; worktree preserved until integration
    "failed",  # task failed; worktree preserved for evidence
    "cancelled",  # task cancelled; worktree preserved for evidence
    "cleanup_eligible",  # integration resolved; safe to clean up
    "removed",  # worktree directory deleted; record retained for audit
]

#: Allowed worktree state transitions.
WORKTREE_TRANSITIONS: dict[WorktreeState, frozenset[WorktreeState]] = {
    "allocated": frozenset({"active", "cancelled", "interrupted"}),
    "active": frozenset({"succeeded", "failed", "interrupted", "cancelled"}),
    "interrupted": frozenset({"active", "cancelled", "cleanup_eligible"}),
    "succeeded": frozenset({"cleanup_eligible", "removed"}),
    "failed": frozenset({"cleanup_eligible", "removed"}),
    "cancelled": frozenset({"cleanup_eligible", "removed"}),
    "cleanup_eligible": frozenset({"removed"}),
    "removed": frozenset(),  # terminal
}

#: States where the worktree directory still exists on disk and must
#: not be deleted.
WORKTREE_DISK_RESIDENT_STATES: frozenset[WorktreeState] = frozenset(
    {"allocated", "active", "interrupted", "succeeded", "failed", "cancelled"}
)


def is_valid_worktree_transition(from_state: WorktreeState, to_state: WorktreeState) -> bool:
    return to_state in WORKTREE_TRANSITIONS.get(from_state, frozenset())


# ----------------------------------------------------------------------
# Command run state
# ----------------------------------------------------------------------

CommandRunState = Literal[
    "running",
    "completed",
    "timed_out",
    "cancelled",
    "unknown",
]


# ----------------------------------------------------------------------
# Stable IDs
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class RepositoryId:
    value: str

    def __post_init__(self) -> None:
        if not self.value or not isinstance(self.value, str):
            raise ValueError("RepositoryId must be a non-empty string")
        if not self.value.startswith(REPOSITORY_ID_PREFIX):
            raise ValueError(
                f"RepositoryId must start with {REPOSITORY_ID_PREFIX!r}; got {self.value!r}"
            )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class WorktreeId:
    value: str

    def __post_init__(self) -> None:
        if not self.value or not isinstance(self.value, str):
            raise ValueError("WorktreeId must be a non-empty string")
        if not self.value.startswith(WORKTREE_ID_PREFIX):
            raise ValueError(
                f"WorktreeId must start with {WORKTREE_ID_PREFIX!r}; got {self.value!r}"
            )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class CommandRunId:
    value: str

    def __post_init__(self) -> None:
        if not self.value or not isinstance(self.value, str):
            raise ValueError("CommandRunId must be a non-empty string")
        if not self.value.startswith(COMMAND_RUN_ID_PREFIX):
            raise ValueError(
                f"CommandRunId must start with {COMMAND_RUN_ID_PREFIX!r}; got {self.value!r}"
            )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class TaskArtifactId:
    value: str

    def __post_init__(self) -> None:
        if not self.value or not isinstance(self.value, str):
            raise ValueError("TaskArtifactId must be a non-empty string")
        if not self.value.startswith(TASK_ARTIFACT_ID_PREFIX):
            raise ValueError(
                f"TaskArtifactId must start with {TASK_ARTIFACT_ID_PREFIX!r}; got {self.value!r}"
            )

    def __str__(self) -> str:
        return self.value


# ----------------------------------------------------------------------
# Repository
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class Repository:
    """A registered target repository for coding tasks.

    Attributes:
        id: stable server-issued ID.
        project_id: the project this repository belongs to.
        name: human-readable name.
        local_path: absolute filesystem path to the bare or working
            clone that worktrees will be created from.
        default_base_revision: the revision to branch from when no
            explicit base is provided.
        created_at: ISO-8601 timestamp.
    """

    id: RepositoryId
    project_id: ProjectId
    name: str
    local_path: str
    default_base_revision: str | None = None
    created_at: str = ""


# ----------------------------------------------------------------------
# Worktree
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class Worktree:
    """An isolated working tree for a coding task.

    Per ``zero-agent-execution-lifecycle`` §"A worktree is a safety
    boundary": concurrent coding tasks must not write into one working
    directory. Each task gets its own branch and worktree.

    Attributes:
        id: stable server-issued ID.
        project_id: the project (denormalized for fast scoping).
        repository_id: the repository this worktree belongs to.
        execution_id: the execution this worktree serves.
        task_id: the task this worktree serves.
        branch_name: the git branch created for this worktree.
        worktree_path: absolute filesystem path to the worktree.
        base_revision: immutable revision the branch was created from.
        state: the worktree's state.
        cleanup_eligible_at: when the worktree became eligible for
            cleanup. None until cleanup is safe.
        created_at: ISO-8601 timestamp.
        updated_at: ISO-8601 timestamp of the last state change.
    """

    id: WorktreeId
    project_id: ProjectId
    repository_id: RepositoryId
    execution_id: ExecutionId
    task_id: TaskId
    branch_name: str
    worktree_path: str
    base_revision: str
    state: WorktreeState
    cleanup_eligible_at: str | None = None
    created_at: str = ""
    updated_at: str = ""

    @property
    def is_disk_resident(self) -> bool:
        """True if the worktree directory still exists on disk."""
        return self.state in WORKTREE_DISK_RESIDENT_STATES


# ----------------------------------------------------------------------
# Command run
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class CommandRun:
    """A scoped, time-bounded command invocation in a worktree.

    Per PLAN.md M6: "Commands are scoped, time-bounded, and audited."

    Attributes:
        id: stable server-issued ID.
        project_id: the project (denormalized).
        worktree_id: the worktree the command ran in.
        task_id: the task the command served.
        command: the executable name.
        args: tuple of string arguments.
        exit_code: the exit code, or None if still running/unknown.
        timed_out: True if the command exceeded its timeout.
        timeout_seconds: the per-command timeout.
        started_at: ISO-8601 timestamp.
        completed_at: ISO-8601 timestamp when the command reached a
            terminal state, or None.
        state: the command run's state.
    """

    id: CommandRunId
    project_id: ProjectId
    worktree_id: WorktreeId
    task_id: TaskId
    command: str
    args: tuple[str, ...]
    exit_code: int | None
    timed_out: bool
    timeout_seconds: int
    started_at: str
    completed_at: str | None
    state: CommandRunState


# ----------------------------------------------------------------------
# Task artifact
# ----------------------------------------------------------------------

ArtifactKind = Literal["stdout", "stderr", "diff", "test_report", "exit_status", "other"]


@dataclass(frozen=True)
class TaskArtifact:
    """A captured artifact from a task execution.

    Per PLAN.md M6: "A task returns diff, checks, artifacts, and
    status." Artifacts preserve full evidence outside model context.

    Per ``zero-artifact-provenance-model``: artifacts preserve full
    evidence; the model sees only bounded, redacted renderings.

    Attributes:
        id: stable server-issued ID.
        project_id: the project (denormalized).
        worktree_id: the worktree the artifact came from.
        task_id: the task the artifact belongs to.
        command_run_id: the command run that produced this artifact,
            or None for artifacts not tied to a specific command.
        kind: the artifact kind (stdout, stderr, diff, etc.).
        content: the captured text.
        content_hash: SHA-256 of the content for integrity.
        created_at: ISO-8601 timestamp.
    """

    id: TaskArtifactId
    project_id: ProjectId
    worktree_id: WorktreeId
    task_id: TaskId
    command_run_id: CommandRunId | None
    kind: ArtifactKind
    content: str
    content_hash: str
    created_at: str = ""


# ----------------------------------------------------------------------
# Typed failures
# ----------------------------------------------------------------------


class WorktreeError(RuntimeError):
    """Base class for worktree-domain typed failures."""


class RepositoryNotFoundError(WorktreeError):
    pass


class WorktreeNotFoundError(WorktreeError):
    pass


class CommandRunNotFoundError(WorktreeError):
    pass


class InvalidWorktreeTransitionError(WorktreeError):
    """A state transition was attempted that is not allowed."""


class PathValidationError(WorktreeError):
    """A filesystem path failed validation.

    Per PLAN.md M6: "Path traversal and repository escape attempts
    fail." This error is raised when a path contains traversal
    sequences, is outside the allowed root, or is otherwise unsafe.
    """

    def __init__(self, message: str, *, path: str = "") -> None:
        super().__init__(message)
        self.path = path


class WorktreeAlreadyExistsError(WorktreeError):
    """A worktree already exists for this task in an active state."""


class WorktreeCleanupError(WorktreeError):
    """Cleanup cannot proceed because the worktree is not eligible,
    has active processes, or has uncommitted human work."""


class CommandPolicyError(WorktreeError):
    """A command is not permitted by the configured execution policy."""


class CommandTimeoutError(WorktreeError):
    """A command exceeded its timeout.

    Per ``zero-tool-capability-runtime`` §"Retries depend on
    side-effect semantics": a timeout does not reveal whether a remote
    side effect occurred.
    """
