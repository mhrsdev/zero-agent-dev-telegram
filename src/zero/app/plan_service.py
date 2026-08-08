"""Plan service — conversation intake, plan proposal, approval, rejection.

Per ``zero-planner-worker-contract`` SKILL.md:

- The Main Planner converts authorized human discussion into a proposed
  decision. The Main Worker converts an approved decision into durable
  work. Neither role substitutes for the other.
- A persuasive plan is not approval, and a model's intention to act is
  not an execution record.
- Natural intent is not automatic execution: Planner usefulness
  depends on recognizing when discussion is becoming actionable
  without requiring a magic command. Safety depends on keeping
  recognition separate from authorization.

Per ``zero-context-memory`` SKILL.md §7: "Never treat ``role=user``
as proof of human intent. Planner approval can only originate from an
authenticated human event."

Per ``zero-control-plane-trust`` §"Atomicity follows the business
fact": approval + audit evidence + handoff record should become durable
together. The :meth:`approve_revision` method performs the approval,
the state transition, the handoff creation, and the audit event in one
logical operation.
"""

from __future__ import annotations

from datetime import UTC, datetime

from zero.app.authorization_service import AuthorizationService
from zero.domain.audit import AuditEvent, AuditEventId, AuditSource
from zero.domain.identity import ProjectId, UserId
from zero.domain.ids import (
    generate_audit_event_id,
    generate_conversation_event_id,
    generate_plan_approval_id,
    generate_plan_handoff_id,
    generate_plan_id,
    generate_plan_revision_id,
)
from zero.domain.plans import (
    ConversationEvent,
    ConversationEventId,
    ConversationEventNotFoundError,
    EventOriginKind,
    EventSource,
    InvalidPlanTransitionError,
    Plan,
    PlanApproval,
    PlanApprovalId,
    PlanContentValidationError,
    PlanHandoff,
    PlanHandoffId,
    PlanId,
    PlanRevision,
    PlanRevisionContent,
    PlanRevisionId,
    PlanState,
    StaleRevisionError,
    is_valid_transition,
)
from zero.persistence.repositories.audit_repository import AuditRepository
from zero.persistence.repositories.plan_repository import PlanRepository


def _now_utc_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class PlanService:
    """Application operations for the plan lifecycle.

    The service is the only place where plan state transitions happen.
    HTTP handlers, future Telegram adapters, and the Main Planner
    adapter call this service rather than touching the repository
    directly, so the trust boundary is in one place.
    """

    def __init__(
        self,
        plan_repo: PlanRepository,
        audit_repo: AuditRepository,
        authorization_service: AuthorizationService,
    ) -> None:
        self._plan_repo = plan_repo
        self._audit_repo = audit_repo
        self._authz = authorization_service

    # ------------------------------------------------------------------
    # Conversation intake
    # ------------------------------------------------------------------

    def ingest_conversation_event(
        self,
        *,
        project_id: ProjectId,
        actor_id: UserId,
        source: EventSource,
        origin_kind: EventOriginKind,
        content: str,
        external_event_id: str | None = None,
    ) -> ConversationEvent:
        """Ingest a conversation event.

        Per ``zero-planner-worker-contract`` §"Human events carry
        authenticated actor and origin metadata": every event carries
        the authenticated actor and the source interface.

        Per ``zero-context-memory`` §7: ``origin_kind`` classifies the
        event structurally. ``role=user`` alone never proves human
        intent; only ``authenticated_human`` does.

        Per PLAN.md M4: "Duplicate delivery is idempotent." The
        repository's UNIQUE(source, external_event_id) constraint makes
        duplicate delivery a no-op; we raise
        :class:`DuplicateConversationEventError` so the caller knows.
        """
        if not content or not content.strip():
            raise PlanContentValidationError("content must not be empty")
        event = ConversationEvent(
            id=ConversationEventId(generate_conversation_event_id()),
            project_id=project_id,
            actor_id=actor_id,
            source=source,
            external_event_id=external_event_id,
            origin_kind=origin_kind,
            content=content.strip(),
            created_at=_now_utc_iso(),
        )
        # The repository's UNIQUE(source, external_event_id) constraint
        # makes duplicate delivery idempotent. We let the
        # DuplicateConversationEventError propagate so the caller knows
        # the event was already processed; the caller can treat this
        # as a success by looking up the existing event.
        self._plan_repo.insert_conversation_event(event)
        return event

    def list_conversation_events(
        self,
        *,
        project_id: ProjectId,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ConversationEvent]:
        return self._plan_repo.list_conversation_events_for_project(
            project_id, limit=limit, offset=offset
        )

    # ------------------------------------------------------------------
    # Plan creation and proposal
    # ------------------------------------------------------------------

    def create_plan(
        self,
        *,
        project_id: ProjectId,
        actor_id: UserId,
        source: AuditSource = "web",
    ) -> Plan:
        """Create a new plan in ``draft`` state.

        Per ``zero-planner-worker-contract`` §"Plans are versioned
        proposals": a plan starts in draft and transitions to proposed
        when the Planner proposes the first revision.
        """
        # Authorize: only members can create plans.
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="plan.propose",
            source=source,
        )
        plan = Plan(
            id=PlanId(generate_plan_id()),
            project_id=project_id,
            current_state="draft",
            current_revision_number=0,
            created_at=_now_utc_iso(),
            updated_at=_now_utc_iso(),
        )
        with self._plan_repo._database.transaction():
            self._plan_repo.insert_plan(plan, commit=False)
            self._audit_repo.insert(
                AuditEvent(
                    id=AuditEventId(generate_audit_event_id()),
                    project_id=project_id,
                    actor_id=actor_id,
                    source=source,
                    operation="plan.create",
                    target_type="plan",
                    target_id=plan.id.value,
                    result="success",
                    redacted_summary=f"Created plan {plan.id.value}",
                    created_at=_now_utc_iso(),
                ),
                commit=False,
            )
        return plan

    def propose_revision(
        self,
        *,
        plan_id: PlanId,
        actor_id: UserId,
        content: PlanRevisionContent,
        source: AuditSource = "web",
    ) -> PlanRevision:
        """Propose a new revision of a plan.

        Per ``zero-planner-worker-contract`` §"Model output becomes
        data only after validation": Planner output is untrusted
        structured content. We validate the content before storing it.

        Per ``zero-planner-worker-contract`` §"Editing produces a new
        review target; it does not retroactively change what was
        approved": each proposal creates a new revision with an
        incremented revision_number.

        Per ``zero-context-memory`` §7: the source_event_ids must
        contain at least one ``authenticated_human`` event; otherwise
        the proposal has no human provenance and is rejected.
        """
        # Authorize.
        plan = self._plan_repo.get_plan(plan_id)
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=plan.project_id,
            permission="plan.propose",
            source=source,
        )

        # Validate content.
        self._validate_revision_content(content, plan.project_id)

        # Compute the new revision number.
        new_revision_number = plan.current_revision_number + 1

        # Check the state transition.
        if plan.current_state == "draft":
            new_plan_state: PlanState = "proposed"
        elif plan.current_state == "proposed":
            new_plan_state = "proposed"  # edit creates a new proposed revision
        else:
            raise InvalidPlanTransitionError(
                f"Cannot propose a revision on a plan in state "
                f"{plan.current_state!r}"
            )

        # Create the revision.
        revision = PlanRevision(
            id=PlanRevisionId(generate_plan_revision_id()),
            plan_id=plan.id,
            project_id=plan.project_id,
            revision_number=new_revision_number,
            content=content,
            proposed_by=actor_id,
            state="proposed",
            created_at=_now_utc_iso(),
        )
        # Insert revision and update plan state atomically.
        with self._plan_repo._database.transaction():
            self._plan_repo.insert_revision(revision, commit=False)
            self._plan_repo.update_plan_state(
                plan.id, new_plan_state, new_revision_number, commit=False
            )
            self._audit_repo.insert(
                AuditEvent(
                    id=AuditEventId(generate_audit_event_id()),
                    project_id=plan.project_id,
                    actor_id=actor_id,
                    source=source,
                    operation="plan.propose",
                    target_type="plan_revision",
                    target_id=revision.id.value,
                    result="success",
                    redacted_summary=(
                        f"Proposed revision {new_revision_number} for plan "
                        f"{plan.id.value}"
                    ),
                    correlation_id=plan.id.value,
                    created_at=_now_utc_iso(),
                ),
                commit=False,
            )
        return revision

    def _validate_revision_content(
        self,
        content: PlanRevisionContent,
        project_id: ProjectId,
    ) -> None:
        """Validate the structured content of a plan revision.

        Per ``zero-planner-worker-contract`` §"Model output becomes
        data only after validation": validate required fields, project
        scope, references, size, and current state before storing.
        """
        errors: list[str] = []
        if not content.objective or not content.objective.strip():
            errors.append("objective must not be empty")
        if not content.acceptance_criteria:
            errors.append("acceptance_criteria must not be empty")
        # Source event provenance: at least one authenticated_human event.
        if not content.source_event_ids:
            errors.append("source_event_ids must not be empty")
        else:
            for eid in content.source_event_ids:
                try:
                    event = self._plan_repo.get_conversation_event(eid)
                    if event.project_id != project_id:
                        errors.append(
                            f"source event {eid} belongs to a different project"
                        )
                    if not event.is_authenticated_human:
                        errors.append(
                            f"source event {eid} is not authenticated_human"
                        )
                except ConversationEventNotFoundError:
                    errors.append(f"source event {eid} not found")
        if errors:
            raise PlanContentValidationError(
                "Plan revision content validation failed", errors=errors
            )

    # ------------------------------------------------------------------
    # Approval and rejection
    # ------------------------------------------------------------------

    def approve_revision(
        self,
        *,
        plan_id: PlanId,
        actor_id: UserId,
        expected_revision_number: int,
        idempotency_key: str,
        source: AuditSource = "web",
        redacted_reason: str | None = None,
    ) -> tuple[PlanApproval, PlanHandoff]:
        """Approve the current revision of a plan.

        Per ``zero-planner-worker-contract`` §"Approval names a
        revision": approval names a specific revision. If the caller's
        ``expected_revision_number`` does not match the plan's current
        revision, we raise :class:`StaleRevisionError`.

        Per ``zero-control-plane-trust`` §"Atomicity follows the
        business fact": approval + state transition + handoff +
        audit are performed in one transaction.

        Per PLAN.md M4: "Duplicate approval events are idempotent."
        The UNIQUE(revision_id, result, idempotency_key) constraint on
        plan_approvals and the UNIQUE(revision_id) constraint on
        plan_handoffs ensure that a duplicate approval request returns
        the same records without creating new ones.

        Per PLAN.md M4: "A plan cannot approve itself." The actor must
        be a human (UserId), not a system identity. We additionally
        require the actor to have the ``plan.approve`` permission.
        """
        plan = self._plan_repo.get_plan(plan_id)
        # Authorize.
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=plan.project_id,
            permission="plan.approve",
            source=source,
        )

        # Stale revision check.
        if expected_revision_number != plan.current_revision_number:
            raise StaleRevisionError(
                f"Expected revision {expected_revision_number} but plan "
                f"{plan_id} is at revision {plan.current_revision_number}",
                expected_revision=expected_revision_number,
                actual_revision=plan.current_revision_number,
            )

        # Get the current revision.
        revision = self._plan_repo.get_current_revision(plan_id)

        # Check for an existing approval (idempotency).
        existing_approval = self._plan_repo.get_approval_for_revision(
            revision.id, "approved"
        )
        if existing_approval is not None:
            existing_handoff = self._plan_repo.get_handoff_for_revision(
                revision.id
            )
            assert existing_handoff is not None  # invariant
            return existing_approval, existing_handoff

        # Check the plan state allows approval.
        if not is_valid_transition(plan.current_state, "approved"):
            raise InvalidPlanTransitionError(
                f"Cannot approve a plan in state {plan.current_state!r}"
            )

        # Create approval, transition plan state, transition revision
        # state, create handoff, and audit — all in one transaction.
        approval = PlanApproval(
            id=PlanApprovalId(generate_plan_approval_id()),
            plan_id=plan.id,
            revision_id=revision.id,
            project_id=plan.project_id,
            approved_by=actor_id,
            source=source,
            result="approved",
            idempotency_key=idempotency_key,
            redacted_reason=redacted_reason,
            created_at=_now_utc_iso(),
        )
        handoff = PlanHandoff(
            id=PlanHandoffId(generate_plan_handoff_id()),
            plan_id=plan.id,
            revision_id=revision.id,
            project_id=plan.project_id,
            approved_by=actor_id,
            execution_id=None,
            created_at=_now_utc_iso(),
        )
        with self._plan_repo._database.transaction():
            self._plan_repo.insert_approval(approval, commit=False)
            self._plan_repo.update_revision_state(
                revision.id, "approved", commit=False
            )
            self._plan_repo.update_plan_state(
                plan.id, "approved", commit=False
            )
            self._plan_repo.insert_handoff(handoff, commit=False)
            self._audit_repo.insert(
                AuditEvent(
                    id=AuditEventId(generate_audit_event_id()),
                    project_id=plan.project_id,
                    actor_id=actor_id,
                    source=source,
                    operation="plan.approve",
                    target_type="plan_revision",
                    target_id=revision.id.value,
                    result="success",
                    redacted_summary=(
                        f"Approved revision {revision.revision_number} "
                        f"of plan {plan.id.value}"
                    ),
                    correlation_id=plan.id.value,
                    created_at=_now_utc_iso(),
                ),
                commit=False,
            )
        return approval, handoff

    def reject_revision(
        self,
        *,
        plan_id: PlanId,
        actor_id: UserId,
        expected_revision_number: int,
        idempotency_key: str,
        source: AuditSource = "web",
        redacted_reason: str | None = None,
    ) -> PlanApproval:
        """Reject the current revision of a plan.

        Per ``zero-planner-worker-contract`` §"Reject stops the flow":
        rejection produces no runnable handoff. The plan transitions
        to ``rejected`` and no handoff record is created.

        Per PLAN.md M4: "Rejection leaves no runnable execution
        request."
        """
        plan = self._plan_repo.get_plan(plan_id)
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=plan.project_id,
            permission="plan.reject",
            source=source,
        )

        if expected_revision_number != plan.current_revision_number:
            raise StaleRevisionError(
                f"Expected revision {expected_revision_number} but plan "
                f"{plan_id} is at revision {plan.current_revision_number}",
                expected_revision=expected_revision_number,
                actual_revision=plan.current_revision_number,
            )

        revision = self._plan_repo.get_current_revision(plan_id)

        existing_rejection = self._plan_repo.get_approval_for_revision(
            revision.id, "rejected"
        )
        if existing_rejection is not None:
            return existing_rejection

        if not is_valid_transition(plan.current_state, "rejected"):
            raise InvalidPlanTransitionError(
                f"Cannot reject a plan in state {plan.current_state!r}"
            )

        approval = PlanApproval(
            id=PlanApprovalId(generate_plan_approval_id()),
            plan_id=plan.id,
            revision_id=revision.id,
            project_id=plan.project_id,
            approved_by=actor_id,
            source=source,
            result="rejected",
            idempotency_key=idempotency_key,
            redacted_reason=redacted_reason,
            created_at=_now_utc_iso(),
        )
        with self._plan_repo._database.transaction():
            self._plan_repo.insert_approval(approval, commit=False)
            self._plan_repo.update_revision_state(
                revision.id, "rejected", commit=False
            )
            self._plan_repo.update_plan_state(
                plan.id, "rejected", commit=False
            )
            self._audit_repo.insert(
                AuditEvent(
                    id=AuditEventId(generate_audit_event_id()),
                    project_id=plan.project_id,
                    actor_id=actor_id,
                    source=source,
                    operation="plan.reject",
                    target_type="plan_revision",
                    target_id=revision.id.value,
                    result="success",
                    redacted_summary=(
                        f"Rejected revision {revision.revision_number} "
                        f"of plan {plan.id.value}"
                    ),
                    correlation_id=plan.id.value,
                    created_at=_now_utc_iso(),
                ),
                commit=False,
            )
        return approval

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def get_plan(self, plan_id: PlanId) -> Plan:
        return self._plan_repo.get_plan(plan_id)

    def get_current_revision(self, plan_id: PlanId) -> PlanRevision:
        return self._plan_repo.get_current_revision(plan_id)

    def list_revisions(self, plan_id: PlanId) -> list[PlanRevision]:
        return self._plan_repo.list_revisions_for_plan(plan_id)

    def list_plans_for_project(self, project_id: ProjectId) -> list[Plan]:
        return self._plan_repo.list_plans_for_project(project_id)

    def get_handoff(self, handoff_id: PlanHandoffId) -> PlanHandoff:
        return self._plan_repo.get_handoff(handoff_id)

    def get_handoff_for_revision(
        self, revision_id: PlanRevisionId
    ) -> PlanHandoff | None:
        return self._plan_repo.get_handoff_for_revision(revision_id)

    def list_handoffs_for_project(
        self, project_id: ProjectId
    ) -> list[PlanHandoff]:
        return self._plan_repo.list_handoffs_for_project(project_id)
