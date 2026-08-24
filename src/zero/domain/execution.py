"""Execution graph domain types.

Per ``zero-planner-worker-contract`` SKILL.md §"Worker decomposition
preserves intent":

- The Worker turns an approved outcome into tasks. It may choose
  technical ordering, split independent work, or request specialist
  agents. It may not silently drop acceptance criteria or reinterpret
  exclusions.
- Each task is easier to reason about when it has: stable identity,
  objective tied to plan acceptance, dependency IDs, input and
  permitted scope, expected evidence, current status, retry/cancellation
  semantics.
- A graph is useful because readiness becomes a state question rather
  than model intuition.

Per ``zero-recovery-consistency`` SKILL.md §"A checkpoint records
facts, not confidence": a useful checkpoint identifies accepted plan
revision, execution graph and task states, attempt identities and
leases, repository/worktree/base revisions, running tool/provider
requests, durable artifacts and hashes, accepted memory deltas,
unresolved blockers and unknown outcomes.

Per PLAN.md M5 invariants:
- Worker accepts only a valid approved plan revision.
- Task state is typed and durable, not held only in model context.
- Dependencies determine readiness and concurrency.
- Retries and duplicate events are idempotent.
- Human-decision conflicts pause rather than being guessed away.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from zero.domain.identity import ProjectId
from zero.domain.plans import PlanHandoffId, PlanId, PlanRevisionId

#: Prefixes for stable server-issued IDs.
EXECUTION_ID_PREFIX = "exec_"
TASK_ID_PREFIX = "task_"
TASK_ATTEMPT_ID_PREFIX = "att_"
EXECUTION_SNAPSHOT_ID_PREFIX = "snap_"

# ----------------------------------------------------------------------
# Execution state machine
# ----------------------------------------------------------------------

ExecutionState = Literal[
    "pending",  # created, not started
    "running",  # at least one task is running
    "paused",  # waiting for a human decision
    "completed",  # all tasks completed successfully
    "failed",  # at least one task failed and cannot proceed
    "cancelled",  # cancelled by an authorized user
]

#: Allowed execution-level state transitions.
EXECUTION_TRANSITIONS: dict[ExecutionState, frozenset[ExecutionState]] = {
    "pending": frozenset({"running", "cancelled"}),
    "running": frozenset({"paused", "completed", "failed", "cancelled"}),
    "paused": frozenset({"running", "cancelled"}),
    "completed": frozenset(),  # terminal
    "failed": frozenset(),  # terminal
    "cancelled": frozenset(),  # terminal
}


def is_valid_execution_transition(from_state: ExecutionState, to_state: ExecutionState) -> bool:
    return to_state in EXECUTION_TRANSITIONS.get(from_state, frozenset())


# ----------------------------------------------------------------------
# Task state machine
# ----------------------------------------------------------------------

TaskState = Literal[
    "pending",  # waiting for dependencies
    "ready",  # dependencies met, not started
    "running",  # an attempt is in progress
    "completed",
    "failed",
    "blocked",  # waiting for a human decision
    "cancelled",
]

#: Allowed task-level state transitions.
TASK_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    "pending": frozenset({"ready", "cancelled"}),
    "ready": frozenset({"running", "cancelled", "blocked"}),
    "running": frozenset({"completed", "failed", "blocked", "cancelled"}),
    "completed": frozenset(),  # terminal
    "failed": frozenset({"ready", "cancelled"}),  # ready: retry allowed
    "blocked": frozenset({"ready", "cancelled"}),  # ready: human unblocked
    "cancelled": frozenset(),  # terminal
}

#: Terminal task states (no further transitions possible).
TERMINAL_TASK_STATES: frozenset[TaskState] = frozenset({"completed", "cancelled"})

#: States that block dependents (per PLAN.md M5: "Failed prerequisites
#: block dependents safely").
BLOCKING_TASK_STATES: frozenset[TaskState] = frozenset({"failed", "blocked", "cancelled"})


def is_valid_task_transition(from_state: TaskState, to_state: TaskState) -> bool:
    return to_state in TASK_TRANSITIONS.get(from_state, frozenset())


def is_terminal_task_state(state: TaskState) -> bool:
    return state in TERMINAL_TASK_STATES


# ----------------------------------------------------------------------
# Task attempt state
# ----------------------------------------------------------------------

AttemptState = Literal[
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "unknown",  # per zero-recovery-consistency: unknown is safer than invented
]


# ----------------------------------------------------------------------
# Stable IDs
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionId:
    value: str

    def __post_init__(self) -> None:
        if not self.value or not isinstance(self.value, str):
            raise ValueError("ExecutionId must be a non-empty string")
        if not self.value.startswith(EXECUTION_ID_PREFIX):
            raise ValueError(
                f"ExecutionId must start with {EXECUTION_ID_PREFIX!r}; got {self.value!r}"
            )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class TaskId:
    value: str

    def __post_init__(self) -> None:
        if not self.value or not isinstance(self.value, str):
            raise ValueError("TaskId must be a non-empty string")
        if not self.value.startswith(TASK_ID_PREFIX):
            raise ValueError(f"TaskId must start with {TASK_ID_PREFIX!r}; got {self.value!r}")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class TaskAttemptId:
    value: str

    def __post_init__(self) -> None:
        if not self.value or not isinstance(self.value, str):
            raise ValueError("TaskAttemptId must be a non-empty string")
        if not self.value.startswith(TASK_ATTEMPT_ID_PREFIX):
            raise ValueError(
                f"TaskAttemptId must start with {TASK_ATTEMPT_ID_PREFIX!r}; got {self.value!r}"
            )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class ExecutionSnapshotId:
    value: str

    def __post_init__(self) -> None:
        if not self.value or not isinstance(self.value, str):
            raise ValueError("ExecutionSnapshotId must be a non-empty string")
        if not self.value.startswith(EXECUTION_SNAPSHOT_ID_PREFIX):
            raise ValueError(
                f"ExecutionSnapshotId must start with "
                f"{EXECUTION_SNAPSHOT_ID_PREFIX!r}; got {self.value!r}"
            )

    def __str__(self) -> str:
        return self.value


# ----------------------------------------------------------------------
# Execution
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class Execution:
    """An execution: the durable representation of an approved plan
    being turned into work.

    Per ``zero-planner-worker-contract`` §"Durable state is stronger
    than agent memory": the task graph, approvals, workspaces, running
    processes, test outcomes, and blockers live in canonical backend
    state.

    Attributes:
        id: stable server-issued ID.
        plan_id: the plan this execution fulfills.
        plan_revision_id: the approved revision being executed.
        plan_handoff_id: the handoff record that produced this execution.
        project_id: the project (denormalized for fast scoping).
        state: the execution's state.
        blocker_reason: optional reason when paused for a human decision.
        idempotency_key: makes duplicate execution creation idempotent.
        created_at: ISO-8601 timestamp.
        updated_at: ISO-8601 timestamp of the last state change.
    """

    id: ExecutionId
    plan_id: PlanId
    plan_revision_id: PlanRevisionId
    plan_handoff_id: PlanHandoffId
    project_id: ProjectId
    state: ExecutionState
    blocker_reason: str | None = None
    idempotency_key: str = ""
    created_at: str = ""
    updated_at: str = ""


# ----------------------------------------------------------------------
# Task
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class Task:
    """A task: a node in the execution graph.

    Attributes:
        id: stable server-issued ID.
        execution_id: the execution this task belongs to.
        project_id: the project (denormalized).
        objective: what this task accomplishes (tied to plan acceptance).
        permitted_scope: what the task is allowed to touch.
        expected_evidence: what the task must produce.
        state: the task's state.
        blocker_reason: optional reason when blocked for a human decision.
        agent_type_id: optional Sub Agent Type assigned to this task (M7).
        terminal_state_set_at: timestamp when the task reached a
            terminal state, or None.
        created_at: ISO-8601 timestamp.
        updated_at: ISO-8601 timestamp.
    """

    id: TaskId
    execution_id: ExecutionId
    project_id: ProjectId
    objective: str
    permitted_scope: tuple[str, ...]
    expected_evidence: tuple[str, ...]
    state: TaskState
    completion_evidence: tuple[str, ...] = ()
    blocker_reason: str | None = None
    agent_type_id: str | None = None
    terminal_state_set_at: str | None = None
    #: GAP 12: earliest instant the scheduler may requeue this failed
    #: task (backoff/Retry-After). NULL means immediately eligible.
    next_retry_at: str | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class TaskDependency:
    """An edge in the execution graph: ``task_id`` depends on
    ``depends_on_task_id``.

    ``depends_on_task_id`` must complete before ``task_id`` can become
    ready.
    """

    task_id: TaskId
    depends_on_task_id: TaskId


# ----------------------------------------------------------------------
# Task attempt
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class TaskAttempt:
    """An individual attempt to run a task (for retries).

    Per ``zero-recovery-consistency`` §"Leases distinguish ownership
    from history": an expired lease does not prove failure. It proves
    that current ownership is absent; reconciliation inspects process,
    artifact, and external evidence.

    Attributes:
        id: stable server-issued ID.
        task_id: the task this attempt belongs to.
        project_id: the project (denormalized).
        attempt_number: 1-based attempt number within the task.
        state: the attempt's state.
        error_message: optional error message on failure (no secrets).
        lease_owner: the worker that currently owns this attempt.
        lease_expires_at: when the lease expires.
        started_at: ISO-8601 timestamp.
        completed_at: ISO-8601 timestamp when the attempt reached a
            terminal state, or None.
    """

    id: TaskAttemptId
    task_id: TaskId
    project_id: ProjectId
    attempt_number: int
    state: AttemptState
    error_message: str | None = None
    lease_owner: str | None = None
    lease_expires_at: str | None = None
    started_at: str = ""
    completed_at: str | None = None


# ----------------------------------------------------------------------
# Execution snapshot (durable restart-safe state)
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionSnapshot:
    """A durable snapshot of the execution graph state.

    Per ``zero-recovery-consistency`` §"A checkpoint records facts,
    not confidence": the snapshot records task IDs, states,
    dependencies, attempt identities, leases, and unresolved blockers
    — not narrative summaries.

    Per ``zero-planner-worker-contract`` §"Durable state is stronger
    than agent memory": after restart, the system should derive which
    tasks are complete, which are ready, which were interrupted, which
    worktrees belong to them, and what evidence exists — without
    asking a model to remember what happened.

    Attributes:
        id: stable server-issued ID.
        execution_id: the execution this snapshot belongs to.
        project_id: the project (denormalized).
        snapshot_version: incremented for each new snapshot.
        graph_state: a JSON document capturing the full task graph state.
        snapshot_reason: why the snapshot was taken.
        created_at: ISO-8601 timestamp.
    """

    id: ExecutionSnapshotId
    execution_id: ExecutionId
    project_id: ProjectId
    snapshot_version: int
    graph_state: str  # JSON
    snapshot_reason: str
    created_at: str = ""


# ----------------------------------------------------------------------
# Typed failures
# ----------------------------------------------------------------------


class ExecutionError(RuntimeError):
    """Base class for execution-domain typed failures."""


class ExecutionNotFoundError(ExecutionError):
    pass


class TaskNotFoundError(ExecutionError):
    pass


class AttemptIdentityError(ExecutionError):
    """An attempt does not belong to the task being mutated."""


class LeaseOwnershipError(ExecutionError):
    """The caller does not hold the current attempt lease."""


class MissingEvidenceError(ExecutionError):
    """A task cannot complete without its expected evidence."""


class InvalidExecutionTransitionError(ExecutionError):
    """A state transition was attempted that is not allowed."""


class InvalidTaskTransitionError(ExecutionError):
    """A state transition was attempted that is not allowed."""


class CycleError(ExecutionError):
    """A cycle was detected in the task dependency graph.

    Per PLAN.md M5 validation: "Cycles and missing dependencies are
    rejected."
    """

    def __init__(self, message: str, *, cycle: list[str] | None = None) -> None:
        super().__init__(message)
        self.cycle = cycle or []


class MissingDependencyError(ExecutionError):
    """A task depends on a task that does not exist."""


class PlanNotApprovedError(ExecutionError):
    """The Worker was asked to create an execution from a plan revision
    that has not been approved."""


class DuplicateAttemptError(ExecutionError):
    """An attempt with the same number already exists for this task."""
