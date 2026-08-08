"""Dynamic Sub Agent Type domain types.

Per ``zero-agent-execution-lifecycle`` SKILL.md:

- A Sub Agent Type describes a durable project-specific responsibility
  and knowledge scope. An instance is one bounded runtime actor of
  that type. A task is a unit of approved work.
- Dynamic does not mean arbitrary: Sub Agent Types emerge from the
  real project shape. A type is justified by a current boundary such
  as distinct domain ownership, specialist knowledge that should not
  fill every context, a different tool or permission boundary, a
  different model/cost policy, or independent concurrency with a
  clear integration contract.
- Instances share accepted knowledge, not mutable scratch state.
- Topology evolution is a data migration: snapshot, lineage, mandatory
  record reconciliation, activation, archive, and rollback belong to
  the transition.

Per ``zero-context-memory`` SKILL.md §"Non-negotiable invariants":
- Removing, splitting, or merging a sub-agent type never deletes its
  knowledge.
- Split, merge, retirement, and role changes are lossless and
  reversible.

Per PLAN.md M7 invariants:
- Type responsibility, memory scope, tool rights, model policy,
  context budget, and concurrency limit are explicit.
- Instances share accepted type knowledge but not task-local scratch
  context.
- Split, merge, retirement, and role changes are lossless and
  reversible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from zero.domain.execution import TaskId
from zero.domain.identity import ProjectId

#: Prefixes for stable server-issued IDs.
AGENT_TYPE_ID_PREFIX = "at_"
AGENT_INSTANCE_ID_PREFIX = "ai_"
KNOWLEDGE_RECORD_ID_PREFIX = "kr_"
TOPOLOGY_SNAPSHOT_ID_PREFIX = "topo_"

# ----------------------------------------------------------------------
# Type state machine
# ----------------------------------------------------------------------

AgentTypeState = Literal["active", "archived", "retired"]

#: Allowed type state transitions.
AGENT_TYPE_TRANSITIONS: dict[AgentTypeState, frozenset[AgentTypeState]] = {
    "active": frozenset({"archived", "retired"}),
    "archived": frozenset({"active", "retired"}),  # active = rollback
    "retired": frozenset(),  # terminal
}


def is_valid_agent_type_transition(
    from_state: AgentTypeState, to_state: AgentTypeState
) -> bool:
    return to_state in AGENT_TYPE_TRANSITIONS.get(from_state, frozenset())


# ----------------------------------------------------------------------
# Instance state machine
# ----------------------------------------------------------------------

AgentInstanceState = Literal[
    "idle", "running", "completed", "failed", "cancelled"
]

INSTANCE_TRANSITIONS: dict[AgentInstanceState, frozenset[AgentInstanceState]] = {
    "idle": frozenset({"running", "cancelled"}),
    "running": frozenset({"completed", "failed", "cancelled"}),
    "completed": frozenset(),  # terminal
    "failed": frozenset({"idle"}),  # can be reused
    "cancelled": frozenset({"idle"}),  # can be reused
}


# ----------------------------------------------------------------------
# Knowledge record state
# ----------------------------------------------------------------------

KnowledgeKind = Literal[
    "decision", "fact", "constraint", "contract", "failure", "other"
]

KnowledgeState = Literal["candidate", "approved", "superseded", "archived"]

KNOWLEDGE_TRANSITIONS: dict[KnowledgeState, frozenset[KnowledgeState]] = {
    "candidate": frozenset({"approved", "archived"}),
    "approved": frozenset({"superseded", "archived"}),
    "superseded": frozenset({"archived"}),
    "archived": frozenset(),  # terminal
}


# ----------------------------------------------------------------------
# Stable IDs
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class AgentTypeId:
    value: str

    def __post_init__(self) -> None:
        if not self.value or not isinstance(self.value, str):
            raise ValueError("AgentTypeId must be a non-empty string")
        if not self.value.startswith(AGENT_TYPE_ID_PREFIX):
            raise ValueError(
                f"AgentTypeId must start with "
                f"{AGENT_TYPE_ID_PREFIX!r}; got {self.value!r}"
            )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class AgentInstanceId:
    value: str

    def __post_init__(self) -> None:
        if not self.value or not isinstance(self.value, str):
            raise ValueError("AgentInstanceId must be a non-empty string")
        if not self.value.startswith(AGENT_INSTANCE_ID_PREFIX):
            raise ValueError(
                f"AgentInstanceId must start with "
                f"{AGENT_INSTANCE_ID_PREFIX!r}; got {self.value!r}"
            )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class KnowledgeRecordId:
    value: str

    def __post_init__(self) -> None:
        if not self.value or not isinstance(self.value, str):
            raise ValueError("KnowledgeRecordId must be a non-empty string")
        if not self.value.startswith(KNOWLEDGE_RECORD_ID_PREFIX):
            raise ValueError(
                f"KnowledgeRecordId must start with "
                f"{KNOWLEDGE_RECORD_ID_PREFIX!r}; got {self.value!r}"
            )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class TopologySnapshotId:
    value: str

    def __post_init__(self) -> None:
        if not self.value or not isinstance(self.value, str):
            raise ValueError("TopologySnapshotId must be a non-empty string")
        if not self.value.startswith(TOPOLOGY_SNAPSHOT_ID_PREFIX):
            raise ValueError(
                f"TopologySnapshotId must start with "
                f"{TOPOLOGY_SNAPSHOT_ID_PREFIX!r}; got {self.value!r}"
            )

    def __str__(self) -> str:
        return self.value


# ----------------------------------------------------------------------
# Agent type
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class AgentType:
    """A project-specific Sub Agent Type.

    Per ``zero-agent-execution-lifecycle``: a type describes a durable
    project-specific responsibility and knowledge scope.

    Attributes:
        id: stable server-issued ID.
        project_id: the project this type belongs to.
        name: human-readable name.
        responsibility: what this type owns.
        memory_scope: what knowledge this type manages.
        permitted_tools: tuple of tool IDs this type may invoke.
        model_policy: which model/provider to use (dict; empty means
            "use project default").
        context_budget_tokens: max context tokens for instances.
        max_concurrent_instances: how many instances may run at once.
        state: active, archived, or retired.
        version: incremented on each modification; used for optimistic
            concurrency and topology versioning.
        superseded_by: the ID of the successor type if this type was
            split/merged into another.
        created_at: ISO-8601 timestamp.
        updated_at: ISO-8601 timestamp.
    """

    id: AgentTypeId
    project_id: ProjectId
    name: str
    responsibility: str
    memory_scope: str
    permitted_tools: tuple[str, ...] = ()
    model_policy: dict[str, str] = field(default_factory=dict)
    context_budget_tokens: int = 100000
    max_concurrent_instances: int = 1
    state: AgentTypeState = "active"
    version: int = 1
    superseded_by: AgentTypeId | None = None
    created_at: str = ""
    updated_at: str = ""


# ----------------------------------------------------------------------
# Agent instance
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class AgentInstance:
    """A runtime instance of a Sub Agent Type.

    Per ``zero-agent-execution-lifecycle``: an instance is one bounded
    runtime actor of a type. Multiple instances of one type may use
    the same durable approved knowledge. Their current prompts,
    temporary files, command output, and unaccepted conclusions
    remain task-local.

    Attributes:
        id: stable server-issued ID.
        project_id: the project (denormalized).
        agent_type_id: the type this instance is an instance of.
        task_id: the task this instance is assigned to, or None.
        state: idle, running, completed, failed, cancelled.
        created_at: ISO-8601 timestamp.
        updated_at: ISO-8601 timestamp.
    """

    id: AgentInstanceId
    project_id: ProjectId
    agent_type_id: AgentTypeId
    task_id: TaskId | None = None
    state: AgentInstanceState = "idle"
    created_at: str = ""
    updated_at: str = ""


# ----------------------------------------------------------------------
# Knowledge record
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class KnowledgeRecord:
    """A unit of agent-type-scoped memory.

    Per ``zero-context-memory`` §"Non-negotiable invariants": removing,
    splitting, or merging a sub-agent type never deletes its knowledge.

    Attributes:
        id: stable server-issued ID.
        project_id: the project (denormalized).
        agent_type_id: the type that owns this record, or None for
            project-wide (Project RAG, M8).
        kind: the knowledge kind (decision, fact, constraint, etc.).
        content: the knowledge text.
        content_hash: SHA-256 of content for integrity.
        provenance: where this record came from.
        state: candidate, approved, superseded, archived.
        superseded_by: the ID of the record that supersedes this one.
        migrated_from: the original record ID if this record was
            migrated from another type (split/merge provenance).
        created_at: ISO-8601 timestamp.
        updated_at: ISO-8601 timestamp.
    """

    id: KnowledgeRecordId
    project_id: ProjectId
    agent_type_id: AgentTypeId | None
    kind: KnowledgeKind
    content: str
    content_hash: str
    provenance: str | None = None
    state: KnowledgeState = "approved"
    superseded_by: KnowledgeRecordId | None = None
    migrated_from: KnowledgeRecordId | None = None
    created_at: str = ""
    updated_at: str = ""


# ----------------------------------------------------------------------
# Topology snapshot
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class TopologySnapshot:
    """A frozen topology state for rollback.

    Per ``zero-agent-execution-lifecycle`` §"Topology evolution is a
    data migration": freeze/version the source topology before
    migration. Never hard-delete source topology or memory as part
    of evolution.

    Attributes:
        id: stable server-issued ID.
        project_id: the project.
        snapshot_version: incremented for each new snapshot.
        reason: why the snapshot was taken.
        topology_state: JSON document capturing all agent types, their
            versions, states, and knowledge record counts.
        created_at: ISO-8601 timestamp.
    """

    id: TopologySnapshotId
    project_id: ProjectId
    snapshot_version: int
    reason: str
    topology_state: str  # JSON
    created_at: str = ""


# ----------------------------------------------------------------------
# Typed failures
# ----------------------------------------------------------------------


class AgentTypeError(RuntimeError):
    """Base class for agent-type-domain typed failures."""


class AgentTypeNotFoundError(AgentTypeError):
    pass


class AgentInstanceNotFoundError(AgentTypeError):
    pass


class KnowledgeRecordNotFoundError(AgentTypeError):
    pass


class InvalidAgentTypeTransitionError(AgentTypeError):
    """A state transition was attempted that is not allowed."""


class AgentTypeAlreadyExistsError(AgentTypeError):
    """A type with the same name already exists in this project."""


class ConcurrencyLimitExceededError(AgentTypeError):
    """The type's max_concurrent_instances limit has been reached."""


class KnowledgeReconciliationError(AgentTypeError):
    """A split/merge/retire could not reconcile all mandatory knowledge
    records.

    Per PLAN.md M7: "Split routes all mandatory knowledge to
    destinations or archive" and "Retirement is blocked until
    reconciliation passes."
    """

    def __init__(
        self, message: str, *, unaccounted_records: list[str] | None = None
    ) -> None:
        super().__init__(message)
        self.unaccounted_records = unaccounted_records or []


class TopologyRollbackError(AgentTypeError):
    """A topology rollback could not be performed."""


class CrossProjectTypeError(AgentTypeError):
    """An operation attempted to access a type or knowledge record from
    another project."""
