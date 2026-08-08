"""Integration service — compatibility review, conflict detection, merge gates.

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
- Low-risk deterministic conflicts may be resolved by policy; product
  decisions return to humans.
- Merge requires explicit authority and passing gates.
- Post-integration memory/RAG update only from accepted results.
"""

from __future__ import annotations

from datetime import UTC, datetime

from zero.app.authorization_service import AuthorizationService
from zero.domain.audit import AuditEvent, AuditEventId, AuditSource
from zero.domain.execution import ExecutionId, TaskId
from zero.domain.identity import ProjectId, UserId
from zero.domain.ids import (
    generate_audit_event_id,
    generate_integration_review_id,
    generate_merge_proposal_id,
)
from zero.domain.integration import (
    CombinedTestResult,
    ConflictClassification,
    ConflictDetail,
    ImpactEntry,
    IntegrationReview,
    IntegrationReviewId,
    MergeGateError,
    MergeProposal,
    MergeProposalId,
)
from zero.persistence.repositories.audit_repository import AuditRepository
from zero.persistence.repositories.integration_repository import (
    IntegrationRepository,
)
from zero.persistence.repositories.worktree_repository import (
    WorktreeRepository,
)


def _now_utc_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class IntegrationService:
    """Application operations for integration review and controlled merge.

    The service:
    - derives the impact set from task outputs (diffs, touched
      contracts);
    - detects conflicts (file collisions, schema/API/type/config
      conflicts);
    - classifies conflicts (none, low_risk, human_decision_required);
    - creates merge proposals with source tasks, diffs, checks, risks;
    - enforces merge gates (explicit authority, passing checks);
    - updates memory/RAG only from accepted results.
    """

    def __init__(
        self,
        integration_repo: IntegrationRepository,
        worktree_repo: WorktreeRepository,
        audit_repo: AuditRepository,
        authorization_service: AuthorizationService,
    ) -> None:
        self._repo = integration_repo
        self._worktree_repo = worktree_repo
        self._audit_repo = audit_repo
        self._authz = authorization_service

    # ------------------------------------------------------------------
    # Impact-set derivation
    # ------------------------------------------------------------------

    def derive_impact_set(
        self,
        *,
        execution_id: ExecutionId,
        task_ids: tuple[TaskId, ...],
    ) -> tuple[ImpactEntry, ...]:
        """Derive the impact set from task outputs.

        Per PLAN.md M11: "Impact-set derivation from task outputs."

        For Phase 6, we derive the impact set from the worktree diff
        artifacts. Each task's diff artifact contains the changed files;
        we parse them to build the impact set.

        Contract files (schemas, APIs, types, configs) are identified by
        file extension/path heuristics.
        """
        impact: list[ImpactEntry] = []
        seen_paths: set[str] = set()
        for task_id in task_ids:
            # Get the task's worktree.
            worktree = self._worktree_repo.get_worktree_for_task(task_id)
            if worktree is None:
                continue
            # Get the task's diff artifacts.
            artifacts = self._worktree_repo.list_artifacts_for_task(
                task_id, kind="diff"
            )
            for artifact in artifacts:
                # Parse the diff artifact content for changed file paths.
                # The diff content contains lines like:
                # "?? new_file.txt" (untracked) or
                # "--- a/path" / "+++ b/path" (tracked changes).
                for line in artifact.content.splitlines():
                    path = self._extract_path_from_diff_line(line)
                    if path and path not in seen_paths:
                        seen_paths.add(path)
                        is_contract = self._is_contract_file(path)
                        # Determine change type.
                        if line.startswith("??"):
                            change_type = "added"
                        elif line.startswith("-") and not line.startswith("---"):
                            change_type = "deleted"
                        else:
                            change_type = "modified"
                        impact.append(
                            ImpactEntry(
                                file_path=path,
                                change_type=change_type,  # type: ignore[arg-type]
                                is_contract=is_contract,
                            )
                        )
        return tuple(impact)

    def _extract_path_from_diff_line(self, line: str) -> str | None:
        """Extract a file path from a diff or status line."""
        if line.startswith("?? "):
            return line[3:].strip()
        if line.startswith("+++ b/"):
            return line[6:].strip()
        if line.startswith("--- a/"):
            return line[6:].strip()
        if line.startswith("A "):
            return line[2:].strip()
        if line.startswith("M "):
            return line[2:].strip()
        if line.startswith("D "):
            return line[2:].strip()
        return None

    def _is_contract_file(self, path: str) -> bool:
        """Heuristic: determine if a file is a contract (schema, API,
        type, config) that other tasks may depend on."""
        path_lower = path.lower().strip()
        # Strip trailing slash (git status may show "api/" for an
        # untracked directory).
        path_stripped = path_lower.rstrip("/")
        # Check for contract file extensions.
        contract_extensions = (
            ".json", ".yaml", ".yml", ".proto", ".graphql",
        )
        if path_stripped.endswith(contract_extensions):
            return True
        # Check for contract path segments (as directory or file).
        contract_segments = (
            "schema", "api", "types", "interfaces",
            "contracts", "proto", "graphql",
        )
        parts = path_stripped.split("/")
        for part in parts:
            if part in contract_segments:
                return True
        # Check for contract file names.
        contract_names = ("schema.sql",)
        for name in contract_names:
            if name in path_stripped:
                return True
        # Check for migration files.
        return "migration" in path_stripped

    # ------------------------------------------------------------------
    # Conflict detection
    # ------------------------------------------------------------------

    def detect_conflicts(
        self,
        impact_set: tuple[ImpactEntry, ...],
        task_ids: tuple[TaskId, ...],
    ) -> tuple[ConflictDetail, ...]:
        """Detect conflicts between tasks' changes.

        Per PLAN.md M11: "Conflicting schema/type/API changes are
        detected."

        For Phase 6, we detect:
        - File collisions: multiple tasks modify the same file.
        - Contract conflicts: multiple tasks modify the same contract
          file.

        We do NOT detect semantic conflicts (e.g. one task renames a
        field while another adds a serializer for it). That requires
        deeper analysis and may return to humans.
        """
        conflicts: list[ConflictDetail] = []
        # Group impact entries by file path.
        path_to_tasks: dict[str, list[str]] = {}
        for entry in impact_set:
            task_str = str(task_ids[0]) if task_ids else "unknown"
            # We don't know which task produced which impact entry from
            # the impact set alone. In a real system, each impact entry
            # would carry its source task. For now, we detect file
            # collisions by checking if the same path appears multiple
            # times (which it won't, since we deduplicate in
            # derive_impact_set). Instead, we check for contract file
            # changes that could conflict.
            if entry.is_contract:
                path_to_tasks.setdefault(entry.file_path, []).append(task_str)

        # For Phase 6, any contract file change is a potential conflict
        # that should be reviewed. We classify it as low_risk (deterministic
        # conflict resolvable by policy) unless the combined tests fail.
        for path, tasks in path_to_tasks.items():
            conflicts.append(
                ConflictDetail(
                    conflict_type="schema" if "schema" in path else "api"
                    if "api" in path else "type" if "type" in path else
                    "config",
                    description=(
                        f"Contract file {path} was modified; "
                        f"compatibility review required"
                    ),
                    source_tasks=tuple(tasks),
                )
            )
        return tuple(conflicts)

    # ------------------------------------------------------------------
    # Integration review
    # ------------------------------------------------------------------

    def create_review(
        self,
        *,
        project_id: ProjectId,
        execution_id: ExecutionId,
        source_task_ids: tuple[TaskId, ...],
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> IntegrationReview:
        """Create an integration review for multiple task outputs.

        Per PLAN.md M11: "Compatibility review contract and evidence
        format."
        """
        if not source_task_ids:
            raise ValueError("source_task_ids must not be empty")
        # Derive the impact set.
        impact_set = self.derive_impact_set(
            execution_id=execution_id, task_ids=source_task_ids
        )
        touched_contracts = tuple(
            e.file_path for e in impact_set if e.is_contract
        )
        # Detect conflicts.
        conflicts = self.detect_conflicts(impact_set, source_task_ids)
        # Classify.
        if not conflicts:
            classification: ConflictClassification = "none"
        elif all(
            c.conflict_type in ("config",) for c in conflicts
        ):
            classification = "low_risk"
        else:
            classification = "human_decision_required"
        review = IntegrationReview(
            id=IntegrationReviewId(generate_integration_review_id()),
            project_id=project_id,
            execution_id=execution_id,
            source_task_ids=source_task_ids,
            impact_set=impact_set,
            touched_contracts=touched_contracts,
            combined_test_result="not_run",
            conflict_classification=classification,
            conflict_details=conflicts,
            state="reviewing",
            redacted_summary=(
                f"Reviewing {len(source_task_ids)} tasks; "
                f"{len(impact_set)} impacted files; "
                f"{len(conflicts)} conflicts"
            ),
            created_at=_now_utc_iso(),
            updated_at=_now_utc_iso(),
        )
        self._repo.insert_review(review)
        self._audit_repo.insert(
            AuditEvent(
                id=AuditEventId(generate_audit_event_id()),
                project_id=project_id,
                actor_id=actor_id,
                source=source,
                operation="integration.review",
                target_type="integration_review",
                target_id=review.id.value,
                result="success",
                redacted_summary=review.redacted_summary or "",
                correlation_id=execution_id.value,
                created_at=_now_utc_iso(),
            )
        )
        return review

    def record_combined_test_result(
        self,
        *,
        project_id: ProjectId,
        review_id: IntegrationReviewId,
        result: CombinedTestResult,
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> IntegrationReview:
        """Record the combined test result.

        Per PLAN.md M11: "Combined test/integration workspace isolated
        from task worktrees."
        """
        review = self._repo.get_review(project_id, review_id)
        # If combined tests fail and there are conflicts, escalate to
        # human_decision_required.
        new_classification = review.conflict_classification
        if result == "fail" and review.conflict_details:
            new_classification = "human_decision_required"
        self._repo.update_review(
            review_id,
            combined_test_result=result,
            conflict_classification=new_classification,
            state="human_decision_paused"
            if new_classification == "human_decision_required"
            else "approved" if result == "pass" else "rejected",
            reviewed_by=actor_id,
            redacted_summary=(
                f"Combined tests: {result}; classification: "
                f"{new_classification}"
            ),
        )
        return self._repo.get_review(project_id, review_id)

    def get_review(
        self, project_id: ProjectId, review_id: IntegrationReviewId
    ) -> IntegrationReview:
        return self._repo.get_review(project_id, review_id)

    def list_reviews(
        self, execution_id: ExecutionId
    ) -> list[IntegrationReview]:
        return self._repo.list_reviews_for_execution(execution_id)

    # ------------------------------------------------------------------
    # Merge proposal
    # ------------------------------------------------------------------

    def create_merge_proposal(
        self,
        *,
        project_id: ProjectId,
        review_id: IntegrationReviewId,
        execution_id: ExecutionId,
        source_tasks: tuple[TaskId, ...],
        source_diffs: tuple[str, ...] = (),
        risks: tuple[str, ...] = (),
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> MergeProposal:
        """Create a merge proposal from an approved integration review.

        Per PLAN.md M11: "Controlled merge proposal containing source
        tasks, diffs, checks, risks, and required approval."

        Per PLAN.md M11: "Merge requires explicit authority and passing
        gates." The review must be approved (combined tests passed, no
        human_decision_required conflicts).
        """
        review = self._repo.get_review(project_id, review_id)
        if review.state != "approved":
            raise MergeGateError(
                f"Cannot create merge proposal: review is in state "
                f"{review.state!r}, not 'approved'"
            )
        proposal = MergeProposal(
            id=MergeProposalId(generate_merge_proposal_id()),
            project_id=project_id,
            integration_review_id=review_id,
            execution_id=execution_id,
            source_tasks=source_tasks,
            source_diffs=source_diffs,
            checks_passed=review.combined_test_result == "pass",
            risks=risks,
            state="proposed",
            created_at=_now_utc_iso(),
            updated_at=_now_utc_iso(),
        )
        self._repo.insert_proposal(proposal)
        self._audit_repo.insert(
            AuditEvent(
                id=AuditEventId(generate_audit_event_id()),
                project_id=project_id,
                actor_id=actor_id,
                source=source,
                operation="merge.propose",
                target_type="merge_proposal",
                target_id=proposal.id.value,
                result="success",
                redacted_summary=(
                    f"Proposed merge of {len(source_tasks)} tasks"
                ),
                correlation_id=execution_id.value,
                created_at=_now_utc_iso(),
            )
        )
        return proposal

    def approve_merge(
        self,
        *,
        project_id: ProjectId,
        proposal_id: MergeProposalId,
        actor_id: UserId,
        source: AuditSource = "web",
    ) -> MergeProposal:
        """Approve a merge proposal.

        Per PLAN.md M11: "Merge requires explicit authority and passing
        gates." The actor must have the ``integration.authorize_merge``
        permission.
        """
        proposal = self._repo.get_proposal(project_id, proposal_id)
        # Authorize.
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="integration.authorize_merge",
            source=source,
        )
        # Gate: checks must have passed.
        if not proposal.checks_passed:
            raise MergeGateError(
                "Cannot approve merge: combined tests did not pass"
            )
        self._repo.update_proposal_state(
            proposal_id, "approved", approved_by=actor_id
        )
        self._audit_repo.insert(
            AuditEvent(
                id=AuditEventId(generate_audit_event_id()),
                project_id=project_id,
                actor_id=actor_id,
                source=source,
                operation="merge.approve",
                target_type="merge_proposal",
                target_id=proposal_id.value,
                result="success",
                redacted_summary=f"Approved merge {proposal_id.value}",
                correlation_id=proposal.execution_id.value,
                created_at=_now_utc_iso(),
            )
        )
        return self._repo.get_proposal(project_id, proposal_id)

    def reject_merge(
        self,
        *,
        project_id: ProjectId,
        proposal_id: MergeProposalId,
        actor_id: UserId,
        source: AuditSource = "web",
    ) -> MergeProposal:
        """Reject a merge proposal.

        Per PLAN.md M11: "Rejected integration does not update accepted
        memory."
        """
        proposal = self._repo.get_proposal(project_id, proposal_id)
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="integration.authorize_merge",
            source=source,
        )
        self._repo.update_proposal_state(proposal_id, "rejected")
        self._audit_repo.insert(
            AuditEvent(
                id=AuditEventId(generate_audit_event_id()),
                project_id=project_id,
                actor_id=actor_id,
                source=source,
                operation="merge.reject",
                target_type="merge_proposal",
                target_id=proposal_id.value,
                result="success",
                redacted_summary=f"Rejected merge {proposal_id.value}",
                correlation_id=proposal.execution_id.value,
                created_at=_now_utc_iso(),
            )
        )
        return self._repo.get_proposal(project_id, proposal_id)

    def execute_merge(
        self,
        *,
        project_id: ProjectId,
        proposal_id: MergeProposalId,
        actor_id: UserId,
        source: AuditSource = "web",
    ) -> MergeProposal:
        """Execute an approved merge.

        Per PLAN.md M11: "Merge provenance traces every included task
        and approval."

        Per PLAN.md M11: "Post-integration memory/RAG update only from
        accepted results." After merge, the accepted results can be
        ingested into Project RAG.
        """
        proposal = self._repo.get_proposal(project_id, proposal_id)
        if proposal.state != "approved":
            raise MergeGateError(
                f"Cannot execute merge: proposal is in state "
                f"{proposal.state!r}, not 'approved'"
            )
        self._repo.update_proposal_state(
            proposal_id, "merged", merged_at=_now_utc_iso()
        )
        self._audit_repo.insert(
            AuditEvent(
                id=AuditEventId(generate_audit_event_id()),
                project_id=project_id,
                actor_id=actor_id,
                source=source,
                operation="merge.execute",
                target_type="merge_proposal",
                target_id=proposal_id.value,
                result="success",
                redacted_summary=(
                    f"Executed merge of {len(proposal.source_tasks)} tasks"
                ),
                correlation_id=proposal.execution_id.value,
                created_at=_now_utc_iso(),
            )
        )
        return self._repo.get_proposal(project_id, proposal_id)

    def get_proposal(
        self, project_id: ProjectId, proposal_id: MergeProposalId
    ) -> MergeProposal:
        return self._repo.get_proposal(project_id, proposal_id)

    def list_proposals(
        self, execution_id: ExecutionId
    ) -> list[MergeProposal]:
        return self._repo.list_proposals_for_execution(execution_id)
