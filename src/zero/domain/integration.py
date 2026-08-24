"""Integration review and merge gate domain types.

Per ``zero-agent-execution-lifecycle`` SKILL.md §"Integration is impact
review, not diff aesthetics":

- The Integration / Compatibility Sub Agent is another dynamic type
  with a special responsibility: determine whether independently
  correct changes remain correct together.
- Its useful input begins with: immutable base revisions, diffs and
  changed paths, touched contracts and dependencies, schema/type/API/
  config changes, test results and failure artifacts, and approved plan
  constraints.
- Broad repository reading becomes justified only when impact analysis
  points outward.

Per ``zero-planner-worker-contract`` §"Merge is a controlled product
transition": a clean Git merge proves textual compatibility. Zero
additionally needs combined tests, migration ordering, contract
compatibility, required human decisions, source task and approval
provenance, authority to merge, and recoverable target state.

Per PLAN.md M11 invariants:
- Integration review is a dynamic Sub Agent Type governed by the same
  permissions and budgets.
- Review begins from diffs, touched contracts, dependencies, schema/API/
  type/config changes, and test evidence.
- It does not reread the entire repository without evidence that broad
  inspection is needed.
- Low-risk deterministic conflicts may be resolved by policy; product
  decisions return to humans.
- Merge requires explicit authority and passing gates.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from zero.domain.execution import ExecutionId, TaskId
from zero.domain.identity import ProjectId, UserId

#: Prefixes for stable server-issued IDs.
INTEGRATION_REVIEW_ID_PREFIX = "irev_"
MERGE_PROPOSAL_ID_PREFIX = "mp_"

# ----------------------------------------------------------------------
# Integration review state
# ----------------------------------------------------------------------

IntegrationReviewState = Literal[
    "pending",
    "reviewing",
    "approved",
    "rejected",
    "human_decision_paused",
]

ConflictClassification = Literal[
    "none",  # no conflicts detected
    "low_risk",  # deterministic conflict resolvable by policy
    "human_decision_required",  # product decision needed
]

CombinedTestResult = Literal["pass", "fail", "not_run"]


# ----------------------------------------------------------------------
# Stable IDs
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class IntegrationReviewId:
    value: str

    def __post_init__(self) -> None:
        if not self.value or not isinstance(self.value, str):
            raise ValueError("IntegrationReviewId must be a non-empty string")
        if not self.value.startswith(INTEGRATION_REVIEW_ID_PREFIX):
            raise ValueError(
                f"IntegrationReviewId must start with "
                f"{INTEGRATION_REVIEW_ID_PREFIX!r}; got {self.value!r}"
            )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class MergeProposalId:
    value: str

    def __post_init__(self) -> None:
        if not self.value or not isinstance(self.value, str):
            raise ValueError("MergeProposalId must be a non-empty string")
        if not self.value.startswith(MERGE_PROPOSAL_ID_PREFIX):
            raise ValueError(
                f"MergeProposalId must start with {MERGE_PROPOSAL_ID_PREFIX!r}; got {self.value!r}"
            )

    def __str__(self) -> str:
        return self.value


# ----------------------------------------------------------------------
# Impact set
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class ImpactEntry:
    """A single file or contract impacted by a task's changes.

    Attributes:
        file_path: the path of the changed file.
        change_type: "added", "modified", or "deleted".
        is_contract: True if this file is a contract (schema, API,
            type definition, config) that other tasks may depend on.
    """

    file_path: str
    change_type: Literal["added", "modified", "deleted"]
    is_contract: bool = False
    # Complete source provenance is retained for every path occurrence.
    # These fields are optional for backwards-compatible construction of
    # historical records, but newly derived entries populate all of them.
    project_id: str | None = None
    execution_id: str | None = None
    task_id: str | None = None
    worktree_id: str | None = None
    artifact_id: str | None = None
    base_revision: str | None = None
    content_hash: str | None = None


@dataclass(frozen=True)
class ConflictDetail:
    """A detected conflict between two tasks' changes.

    Attributes:
        conflict_type: "schema", "api", "type", "config", "file_collision".
        description: human-readable description of the conflict.
        source_tasks: tuple of task IDs involved in the conflict.
    """

    conflict_type: Literal["schema", "api", "type", "config", "file_collision"]
    description: str
    source_tasks: tuple[str, ...] = ()


# ----------------------------------------------------------------------
# Integration review
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class IntegrationReview:
    """A compatibility review of multiple task outputs.

    Per ``zero-agent-execution-lifecycle`` §"Integration is impact
    review": determine whether independently correct changes remain
    correct together.

    Attributes:
        id: stable server-issued ID.
        project_id: the project (denormalized).
        execution_id: the execution.
        source_task_ids: tuple of task IDs being reviewed.
        impact_set: tuple of ImpactEntry objects.
        touched_contracts: tuple of contract paths that were changed.
        combined_test_result: result of combined tests.
        conflict_classification: none, low_risk, or human_decision_required.
        conflict_details: tuple of ConflictDetail objects.
        state: the review's state.
        integration_worktree_id: optional worktree used for combined tests.
        reviewed_by: the user who reviewed (or None if automated).
        redacted_summary: safe summary for audit.
        created_at: ISO-8601 timestamp.
        updated_at: ISO-8601 timestamp.
    """

    id: IntegrationReviewId
    project_id: ProjectId
    execution_id: ExecutionId
    source_task_ids: tuple[TaskId, ...]
    impact_set: tuple[ImpactEntry, ...] = ()
    touched_contracts: tuple[str, ...] = ()
    combined_test_result: CombinedTestResult = "not_run"
    conflict_classification: ConflictClassification = "none"
    conflict_details: tuple[ConflictDetail, ...] = ()
    state: IntegrationReviewState = "pending"
    integration_worktree_id: str | None = None
    reviewed_by: UserId | None = None
    redacted_summary: str | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class CombinedTestEvidence:
    """Durable output from a combined integration-worktree test."""

    id: str
    project_id: ProjectId
    review_id: IntegrationReviewId
    execution_id: ExecutionId
    integration_worktree_id: str
    worktree_path: str
    kind: Literal["test", "preparation", "failure"]
    command: str
    args: tuple[str, ...]
    exit_code: int | None
    timed_out: bool
    stdout: str
    stderr: str
    content_hash: str
    created_at: str = ""


# ----------------------------------------------------------------------
# Merge proposal
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class MergeProposal:
    """A controlled merge proposal.

    Per ``zero-planner-worker-contract`` §"Merge is a controlled
    product transition": a clean Git merge proves textual compatibility.
    Zero additionally needs combined tests, migration ordering, contract
    compatibility, required human decisions, source task and approval
    provenance, authority to merge, and recoverable target state.

    Attributes:
        id: stable server-issued ID.
        project_id: the project (denormalized).
        integration_review_id: the review this proposal is based on.
        execution_id: the execution.
        source_tasks: tuple of task IDs included in the merge.
        source_diffs: tuple of artifact IDs containing diffs.
        checks_passed: whether all combined tests passed.
        risks: tuple of risk descriptions.
        state: proposed, approved, rejected, merged, cancelled.
        approved_by: the user who approved the merge.
        merged_at: when the merge was executed.
        created_at: ISO-8601 timestamp.
        updated_at: ISO-8601 timestamp.
    """

    id: MergeProposalId
    project_id: ProjectId
    integration_review_id: IntegrationReviewId
    execution_id: ExecutionId
    source_tasks: tuple[TaskId, ...]
    source_diffs: tuple[str, ...] = ()
    checks_passed: bool = False
    risks: tuple[str, ...] = ()
    state: MergeProposalState = "proposed"
    approved_by: UserId | None = None
    merged_at: str | None = None
    # Durable evidence of the external Git transition.
    integration_worktree_id: str | None = None
    target_revision: str | None = None
    rollback_revision: str | None = None
    evidence_ids: tuple[str, ...] = ()
    created_at: str = ""
    updated_at: str = ""


MergeProposalState = Literal[
    "proposed",
    "approved",
    "rejected",
    "merged",
    "cancelled",
]


# ----------------------------------------------------------------------
# Typed failures
# ----------------------------------------------------------------------


class IntegrationError(RuntimeError):
    """Base class for integration-domain typed failures."""


class IntegrationReviewNotFoundError(IntegrationError):
    pass


class MergeProposalNotFoundError(IntegrationError):
    pass


class MergeGateError(IntegrationError):
    """A merge gate check failed.

    Per PLAN.md M11: "Merge requires explicit authority and passing
    gates."
    """


class HumanDecisionRequiredError(IntegrationError):
    """A conflict requires a human decision.

    Per PLAN.md M11: "product decisions return to humans" and
    "Human-decision conflict pauses merge."
    """

    def __init__(self, message: str, *, conflicts: list[ConflictDetail] | None = None) -> None:
        super().__init__(message)
        self.conflicts = conflicts or []
