"""Plan lifecycle domain types.

Per ``zero-planner-worker-contract`` SKILL.md:

- Zero separates understanding from execution. The Main Planner
  converts authorized human discussion into a proposed decision. The
  Main Worker converts an approved decision into durable work.
- Plans are versioned proposals. A plan is not one mutable text field.
  It has identity, revision, state, provenance, and explicit
  transitions. Editing produces a new review target; it does not
  retroactively change what was approved.
- Natural intent is not automatic execution: Planner usefulness
  depends on recognizing when discussion is becoming actionable
  without requiring a magic command. Safety depends on keeping
  recognition separate from authorization.
- Model output becomes data only after validation: Planner output is
  untrusted structured content. Approval remains a separate
  actor-authenticated transition.

Per ``zero-context-memory`` SKILL.md §7: "Never treat ``role=user``
as proof of human intent. Planner approval can only originate from an
authenticated human event."

Per ``zero-control-plane-trust`` §"Atomicity follows the business
fact": approval + its audit evidence + the handoff record should
either become durable together or remain unapplied.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from zero.domain.identity import ProjectId, UserId

#: Prefixes for stable server-issued IDs.
PLAN_ID_PREFIX = "plan_"
PLAN_REVISION_ID_PREFIX = "pr_"
PLAN_APPROVAL_ID_PREFIX = "pa_"
PLAN_HANDOFF_ID_PREFIX = "ph_"
CONVERSATION_EVENT_ID_PREFIX = "evt_"

# ----------------------------------------------------------------------
# Plan state machine
# ----------------------------------------------------------------------

PlanState = Literal["draft", "proposed", "approved", "rejected", "superseded", "archived"]
RevisionState = Literal["proposed", "approved", "rejected", "superseded"]

#: Allowed plan-level state transitions. Enforced by the plan service.
#:
#: - draft -> proposed: Planner proposes the first revision.
#: - proposed -> proposed: Planner proposes a new revision (edit).
#: - proposed -> approved: Authorized user approves the current revision.
#: - proposed -> rejected: Authorized user rejects the current revision.
#: - approved -> archived: Plan is no longer active (e.g. execution complete).
#: - rejected -> archived: Rejected plans can be archived.
#: - approved -> superseded: A newer revision supersedes this one
#:   (rare; normally editing a proposed plan creates a new proposed
#:   revision, not a superseded approved one).
PLAN_TRANSITIONS: dict[PlanState, frozenset[PlanState]] = {
    "draft": frozenset({"proposed"}),
    "proposed": frozenset({"proposed", "approved", "rejected"}),
    "approved": frozenset({"archived", "superseded"}),
    "rejected": frozenset({"archived"}),
    "superseded": frozenset({"archived"}),
    "archived": frozenset(),  # terminal
}


def is_valid_transition(from_state: PlanState, to_state: PlanState) -> bool:
    return to_state in PLAN_TRANSITIONS.get(from_state, frozenset())


# ----------------------------------------------------------------------
# Conversation events (interface-neutral intake)
# ----------------------------------------------------------------------

EventOriginKind = Literal[
    "authenticated_human",
    "planner_injection",
    "system_reminder",
    "compaction_carrier",
    "tool_result",
    "auto_continue",
]

EventSource = Literal["web", "telegram", "discord", "system", "internal"]


@dataclass(frozen=True)
class ConversationEventId:
    value: str

    def __post_init__(self) -> None:
        if not self.value or not isinstance(self.value, str):
            raise ValueError("ConversationEventId must be a non-empty string")
        if not self.value.startswith(CONVERSATION_EVENT_ID_PREFIX):
            raise ValueError(
                f"ConversationEventId must start with "
                f"{CONVERSATION_EVENT_ID_PREFIX!r}; got {self.value!r}"
            )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class ConversationEvent:
    """An interface-neutral human conversation event.

    Per ``zero-planner-worker-contract`` §"Human events carry
    authenticated actor and origin metadata": every conversation
    event carries the authenticated actor and the source interface.

    Per ``zero-context-memory`` §7: ``origin_kind`` classifies the
    event structurally. ``role=user`` alone never proves human
    intent; only ``authenticated_human`` does.

    Attributes:
        id: stable server-issued ID.
        project_id: the project this event belongs to.
        actor_id: the authenticated Zero User who caused the event.
        source: the interface that originated the event.
        external_event_id: transport idempotency key (e.g. Telegram
            update_id). Optional; NULL for system-generated events.
        origin_kind: structural classification of the event's origin.
        content: the event's text content.
        created_at: ISO-8601 timestamp.
    """

    id: ConversationEventId
    project_id: ProjectId
    actor_id: UserId
    source: EventSource
    external_event_id: str | None
    origin_kind: EventOriginKind
    content: str
    created_at: str = ""

    @property
    def is_authenticated_human(self) -> bool:
        """Per zero-context-memory §7: only authenticated_human counts
        as real human intent."""
        return self.origin_kind == "authenticated_human"


# ----------------------------------------------------------------------
# Plan and revision
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class PlanId:
    value: str

    def __post_init__(self) -> None:
        if not self.value or not isinstance(self.value, str):
            raise ValueError("PlanId must be a non-empty string")
        if not self.value.startswith(PLAN_ID_PREFIX):
            raise ValueError(f"PlanId must start with {PLAN_ID_PREFIX!r}; got {self.value!r}")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class PlanRevisionId:
    value: str

    def __post_init__(self) -> None:
        if not self.value or not isinstance(self.value, str):
            raise ValueError("PlanRevisionId must be a non-empty string")
        if not self.value.startswith(PLAN_REVISION_ID_PREFIX):
            raise ValueError(
                f"PlanRevisionId must start with {PLAN_REVISION_ID_PREFIX!r}; got {self.value!r}"
            )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class Plan:
    """A plan: one objective, project-scoped, with a current state.

    Attributes:
        id: stable server-issued ID.
        project_id: the project this plan belongs to.
        current_state: the live state of the plan.
        current_revision_number: the latest revision number for this
            plan. 0 means no revision has been proposed yet.
        created_at: ISO-8601 timestamp.
        updated_at: ISO-8601 timestamp of the last state change.
    """

    id: PlanId
    project_id: ProjectId
    current_state: PlanState
    current_revision_number: int
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class PlanRevisionContent:
    """The structured content of a plan revision.

    Per ``zero-planner-worker-contract`` §"Model output becomes data
    only after validation": Planner output is untrusted structured
    content. The control plane validates required fields, project
    scope, references, size, and current state before storing a
    proposal.

    Attributes:
        objective: the human objective, in one sentence.
        scope: explicit in-scope items (what the plan covers).
        constraints: non-negotiable constraints.
        acceptance_criteria: evidence required for the plan to be
            considered complete.
        risks: risks and irreversible decisions.
        unresolved_questions: open decisions that need human input.
        source_event_ids: the conversation events that led to this
            proposal. Provenance for audit.
    """

    objective: str
    scope: tuple[str, ...]
    constraints: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    risks: tuple[str, ...]
    unresolved_questions: tuple[str, ...]
    source_event_ids: tuple[ConversationEventId, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "objective": self.objective,
            "scope": list(self.scope),
            "constraints": list(self.constraints),
            "acceptance_criteria": list(self.acceptance_criteria),
            "risks": list(self.risks),
            "unresolved_questions": list(self.unresolved_questions),
            "source_event_ids": [eid.value for eid in self.source_event_ids],
        }


@dataclass(frozen=True)
class PlanRevision:
    """An immutable revision of a plan.

    Editing a plan creates a new revision; the old revision is not
    modified. The revision's state may transition (proposed ->
    approved/rejected/superseded), but its content is immutable.

    Attributes:
        id: stable server-issued ID.
        plan_id: the plan this revision belongs to.
        project_id: the project (denormalized for fast scoping).
        revision_number: 1-based revision number within the plan.
        content: the structured content.
        proposed_by: the user who triggered the proposal.
        state: the revision's state.
        created_at: ISO-8601 timestamp.
    """

    id: PlanRevisionId
    plan_id: PlanId
    project_id: ProjectId
    revision_number: int
    content: PlanRevisionContent
    proposed_by: UserId
    state: RevisionState
    created_at: str = ""


# ----------------------------------------------------------------------
# Plan approval (immutable evidence)
# ----------------------------------------------------------------------

ApprovalResult = Literal["approved", "rejected"]


@dataclass(frozen=True)
class PlanApprovalId:
    value: str

    def __post_init__(self) -> None:
        if not self.value or not isinstance(self.value, str):
            raise ValueError("PlanApprovalId must be a non-empty string")
        if not self.value.startswith(PLAN_APPROVAL_ID_PREFIX):
            raise ValueError(
                f"PlanApprovalId must start with {PLAN_APPROVAL_ID_PREFIX!r}; got {self.value!r}"
            )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class PlanApproval:
    """Immutable approval or rejection evidence.

    Per ``zero-planner-worker-contract`` §"Approval names a revision":
    approval is tied to a specific revision. A stale approval (for an
    older revision) fails safely.

    Attributes:
        id: stable server-issued ID.
        plan_id: the plan.
        revision_id: the revision being approved/rejected.
        project_id: the project (denormalized).
        approved_by: the authorized human who performed the action.
        source: the interface the action came from.
        result: 'approved' or 'rejected'.
        idempotency_key: makes duplicate delivery idempotent.
        redacted_reason: optional reason, redacted of sensitive content.
        created_at: ISO-8601 timestamp.
    """

    id: PlanApprovalId
    plan_id: PlanId
    revision_id: PlanRevisionId
    project_id: ProjectId
    approved_by: UserId
    source: EventSource
    result: ApprovalResult
    idempotency_key: str
    redacted_reason: str | None = None
    created_at: str = ""


# ----------------------------------------------------------------------
# Plan handoff (the single immutable handoff record per approved revision)
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class PlanHandoffId:
    value: str

    def __post_init__(self) -> None:
        if not self.value or not isinstance(self.value, str):
            raise ValueError("PlanHandoffId must be a non-empty string")
        if not self.value.startswith(PLAN_HANDOFF_ID_PREFIX):
            raise ValueError(
                f"PlanHandoffId must start with {PLAN_HANDOFF_ID_PREFIX!r}; got {self.value!r}"
            )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class PlanHandoff:
    """The single immutable handoff record produced when an authorized
    user approves a plan revision.

    Per ``zero-planner-worker-contract`` §"Correct handoff": "authorized
    human approves revision 3 -> immutable approval record -> Worker
    creates execution from revision 3 exactly once".

    Per PLAN.md M4 acceptance: "exactly one immutable handoff record".

    Attributes:
        id: stable server-issued ID.
        plan_id: the plan.
        revision_id: the approved revision.
        project_id: the project (denormalized).
        approved_by: the authorized human.
        execution_id: the execution created from this handoff, or None
            if the Worker has not yet picked it up.
        created_at: ISO-8601 timestamp.
    """

    id: PlanHandoffId
    plan_id: PlanId
    revision_id: PlanRevisionId
    project_id: ProjectId
    approved_by: UserId
    execution_id: str | None = None
    created_at: str = ""


# ----------------------------------------------------------------------
# Typed failures
# ----------------------------------------------------------------------


class PlanError(RuntimeError):
    """Base class for plan-domain typed failures."""


class PlanNotFoundError(PlanError):
    pass


class PlanRevisionNotFoundError(PlanError):
    pass


class InvalidPlanTransitionError(PlanError):
    """A state transition was attempted that is not allowed.

    Per ``zero-planner-worker-contract`` §"Plans are versioned
    proposals": transitions are explicit. This error is raised when a
    caller attempts a transition not in :data:`PLAN_TRANSITIONS`.
    """


class StaleRevisionError(PlanError):
    """An approval was attempted against a revision that is not the
    current revision of the plan.

    Per ``zero-planner-worker-contract`` §"Correct example": "User
    approves revision 2 after revision 3 was proposed. The backend
    returns ``stale_revision``; no execution appears."

    Per PLAN.md M4 validation: "Approval of an old revision fails
    after edit."
    """

    def __init__(
        self,
        message: str,
        *,
        expected_revision: int,
        actual_revision: int,
    ) -> None:
        super().__init__(message)
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision


class ConversationEventNotFoundError(PlanError):
    pass


class DuplicateConversationEventError(PlanError):
    """A conversation event with the same (source, external_event_id)
    has already been ingested. Per PLAN.md M4: "Duplicate delivery
    is idempotent" — this error is raised so the caller knows the
    event was already processed, but the operation is not a failure."""


class PlanContentValidationError(PlanError):
    """The plan revision content failed validation.

    Per ``zero-planner-worker-contract`` §"Model output becomes data
    only after validation": Planner output is untrusted structured
    content. The control plane validates required fields, project
    scope, references, size, and current state before storing a
    proposal.
    """

    def __init__(self, message: str, *, errors: list[str] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []
