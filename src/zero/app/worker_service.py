"""Worker service — Main Worker / Orchestrator.

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

Per ``zero-planner-worker-contract`` §"Durable state is stronger than
agent memory": the task graph, approvals, workspaces, running
processes, test outcomes, and blockers live in canonical backend
state.

Per ``zero-recovery-consistency`` §"Idempotency makes retries
ordinary": one execution per approved plan revision; one task attempt
per scheduled attempt ID.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict, deque
from datetime import UTC, datetime
from threading import Event, Lock
from typing import Any, cast

from zero.app.clock import now_utc_iso
from zero.app.authorization_service import AuthorizationService
from zero.domain.artifacts import ArtifactId, ArtifactNotFoundError
from zero.domain.audit import AuditEvent, AuditEventId, AuditSource
from zero.domain.authorization import AuthorizationDecision, AuthorizationError, Permission
from zero.domain.execution import (
    BLOCKING_TASK_STATES,
    AttemptIdentityError,
    CycleError,
    Execution,
    ExecutionId,
    ExecutionSnapshot,
    ExecutionSnapshotId,
    InvalidExecutionTransitionError,
    InvalidTaskTransitionError,
    LeaseOwnershipError,
    MissingDependencyError,
    MissingEvidenceError,
    PlanNotApprovedError,
    Task,
    TaskAttempt,
    TaskAttemptId,
    TaskDependency,
    TaskId,
    TaskNotFoundError,
    is_terminal_task_state,
    is_valid_execution_transition,
)
from zero.domain.identity import ProjectId, UserId
from zero.domain.ids import (
    generate_audit_event_id,
    generate_execution_id,
    generate_execution_snapshot_id,
    generate_task_attempt_id,
    generate_task_id,
)
from zero.domain.plans import (
    PlanHandoffId,
)
from zero.persistence.repositories.artifact_repository import ArtifactRepository
from zero.persistence.repositories.audit_repository import AuditRepository
from zero.persistence.repositories.execution_repository import (
    ExecutionRepository,
)
from zero.persistence.repositories.plan_repository import PlanRepository

logger = logging.getLogger(__name__)




_EVIDENCE_KINDS: dict[str, frozenset[str]] = {
    "provider_response": frozenset({"transcript", "other"}),
    "transcript": frozenset({"transcript"}),
    "artifact": frozenset(
        {
            "stdout",
            "stderr",
            "diff",
            "test_report",
            "exit_status",
            "other",
            "source_snapshot",
            "transcript",
        }
    ),
    "diff": frozenset({"diff"}),
    "test_report": frozenset({"test_report"}),
    "exit_status": frozenset({"exit_status"}),
    "stdout": frozenset({"stdout"}),
    "stderr": frozenset({"stderr"}),
    "source_snapshot": frozenset({"source_snapshot"}),
}


# ------------------------------------------------------------------
# Task specification (input to the Worker)
# ----------------------------------------------------------------------


class TaskSpec:
    """A specification for a task to be created by the Worker.

    The Worker's decomposition produces a list of :class:`TaskSpec`
    objects plus a list of dependency edges. The
    :meth:`WorkerService.create_execution_from_handoff` method turns
    these into durable :class:`Task` and :class:`TaskDependency`
    records.
    """

    __slots__ = (
        "agent_type_id",
        "expected_evidence",
        "key",
        "objective",
        "permitted_scope",
    )

    def __init__(
        self,
        *,
        objective: str,
        permitted_scope: tuple[str, ...] = (),
        expected_evidence: tuple[str, ...] = (),
        key: str | None = None,
        agent_type_id: str | None = None,
    ) -> None:
        self.objective = objective
        self.permitted_scope = permitted_scope
        self.expected_evidence = expected_evidence
        # key is an optional caller-supplied identifier used to express
        # dependencies before the task has a stable ID. The Worker
        # resolves keys to TaskIds when creating the graph.
        self.key = key
        # Optional Sub Agent Type assigned to this task. When set, the
        # runtime enforces the type's permitted tools, model policy,
        # context budget, and concurrency limit for this task.
        self.agent_type_id = agent_type_id


class DependencySpec:
    """A specification for a dependency edge: ``depends_on`` must
    complete before ``task`` can become ready.

    Both ``task`` and ``depends_on`` are keys from :class:`TaskSpec`.
    """

    __slots__ = ("depends_on_key", "task_key")

    def __init__(self, *, task_key: str, depends_on_key: str) -> None:
        if task_key == depends_on_key:
            raise CycleError(f"Task {task_key!r} cannot depend on itself")
        self.task_key = task_key
        self.depends_on_key = depends_on_key


# ----------------------------------------------------------------------
# Worker service
# ----------------------------------------------------------------------


class WorkerService:
    """The Main Worker / Orchestrator.

    The Worker:

    1. Accepts an approved plan revision (via its handoff record).
    2. Decomposes the plan into tasks and dependencies (caller-supplied
       for now; a future Main Planner adapter will produce these).
    3. Creates the execution and task graph durably.
    4. Computes task readiness based on dependencies.
    5. Provides a scheduler that claims ready tasks idempotently.
    6. Records task attempts and terminal states.
    7. Snapshots the graph state for restart recovery.
    8. Cancels tasks and executions with explicit propagation rules.
    """

    def __init__(
        self,
        execution_repo: ExecutionRepository,
        plan_repo: PlanRepository,
        audit_repo: AuditRepository,
        artifact_repo: ArtifactRepository,
        authorization_service: AuthorizationService,
        *,
        metrics: Any | None = None,
        task_max_attempts: int = 0,
        agent_type_repo: Any | None = None,
    ) -> None:
        self._execution_repo = execution_repo
        self._plan_repo = plan_repo
        self._audit_repo = audit_repo
        self._artifact_repo = artifact_repo
        self._authz = authorization_service
        self._metrics = metrics
        # Optional: when wired (production composition does), restart
        # recovery also releases agent-type instance leases held by
        # interrupted tasks; without it, leaked "running" instances
        # permanently exhaust the type's concurrency budget.
        self._agent_type_repo = agent_type_repo
        # Total attempts allowed per task (first run + retries). When a
        # failed task still holds retry budget, the execution pauses
        # ("awaiting automatic retry") instead of terminating as failed,
        # so the scheduler can requeue and reclaim it. 0 disables.
        self._task_max_attempts = max(0, int(task_max_attempts))
        self._cancellation_events: dict[str, Event] = {}
        self._cancellation_events_lock = Lock()

    def get_cancellation_event(self, execution_id: ExecutionId) -> Event:
        """Return the process-local cancellation signal for an execution."""
        with self._cancellation_events_lock:
            return self._cancellation_events.setdefault(execution_id.value, Event())

    def _discard_cancellation_event(self, execution_id: ExecutionId) -> None:
        """Drop the process-local cancellation signal for a terminal execution.

        The map is bounded: every finished execution releases its event so a
        long-lived process cannot accumulate one ``Event`` per execution.
        Threads already holding the old object are unaffected; a terminal
        execution never runs tasks again, so a fresh (unset) event can never
        be observed by new work.
        """
        with self._cancellation_events_lock:
            self._cancellation_events.pop(execution_id.value, None)

    def _require_project_scope(
        self,
        *,
        project_id: ProjectId | None,
        actor_id: UserId | None,
        permission: str,
        source: AuditSource,
    ) -> ProjectId:
        """Authorize before any globally-addressed execution lookup.

        ``None`` is deliberately rejected rather than treated as an internal
        caller.  Recovery and scheduler code must carry an explicit actor
        and project scope too.
        """
        if project_id is None or actor_id is None:
            denied_project = project_id or ProjectId("p_missing")
            raise AuthorizationError(
                AuthorizationDecision.deny(
                    actor_id=actor_id,
                    project_id=denied_project,
                    permission=cast(Permission, permission),
                    role=None,
                    reason="no_actor",
                )
            )
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission=permission,  # type: ignore[arg-type]
            source=source,
        )
        return project_id

    # ------------------------------------------------------------------
    # Execution creation
    # ------------------------------------------------------------------

    def create_execution_from_handoff(
        self,
        *,
        handoff_id: PlanHandoffId,
        actor_id: UserId,
        project_id: ProjectId,
        task_specs: list[TaskSpec],
        dependency_specs: list[DependencySpec] = (),
        source: AuditSource = "system",
    ) -> Execution:
        """Create an execution from an approved plan handoff.

        Per PLAN.md M5 invariant: "Worker accepts only a valid approved
        plan revision." We verify the handoff exists and that the plan
        is in the ``approved`` state.

        Per ``zero-planner-worker-contract`` §"Idempotency is part of
        normal operation": "one execution per approved plan revision".
        The UNIQUE(plan_revision_id) constraint on executions makes
        duplicate creation idempotent.

        Per PLAN.md M5 validation: "Cycles and missing dependencies
        are rejected." We run a topological sort before creating the
        graph and raise :class:`CycleError` if a cycle is detected.
        """
        dependency_specs = list(dependency_specs)

        self._require_project_scope(
            project_id=project_id,
            actor_id=actor_id,
            permission="execution.start",
            source=source,
        )

        # Look up the handoff inside the caller's project boundary.
        handoff = self._plan_repo.get_handoff(handoff_id, project_id=project_id)
        # Verify the plan is approved.
        plan = self._plan_repo.get_plan(handoff.plan_id, project_id=project_id)
        if plan.current_state != "approved":
            raise PlanNotApprovedError(
                f"Plan {plan.id} is in state {plan.current_state!r}, not 'approved'"
            )

        # Authorize: the actor must have execution.start permission.
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=plan.project_id,
            permission="execution.start",
            source=source,
        )

        # Check for an existing execution (idempotency).
        existing = self._execution_repo.get_execution_for_revision(handoff.revision_id)
        if existing is not None:
            return existing

        # Validate the task specs and dependency specs.
        self._validate_specs(task_specs, dependency_specs)

        # Detect cycles before creating anything.
        self._detect_cycles(task_specs, dependency_specs)

        # Create the execution.
        execution = Execution(
            id=ExecutionId(generate_execution_id()),
            plan_id=plan.id,
            plan_revision_id=handoff.revision_id,
            plan_handoff_id=handoff.id,
            project_id=plan.project_id,
            state="pending",
            idempotency_key=handoff.id.value,
            created_at=now_utc_iso(),
            updated_at=now_utc_iso(),
        )
        with self._execution_repo._database.transaction():
            # The transaction starts with BEGIN IMMEDIATE, so this re-read
            # is the authoritative exactly-once handoff claim. A concurrent
            # caller returns the persisted graph rather than its uncommitted
            # locally-generated identity.
            persisted = self._execution_repo.get_execution_for_revision(handoff.revision_id)
            if persisted is not None:
                return persisted
            self._execution_repo.insert_execution(execution, commit=False)

            # Create tasks.
            key_to_task_id: dict[str, TaskId] = {}
            for spec in task_specs:
                key = spec.key or spec.objective
                task_id = TaskId(generate_task_id())
                key_to_task_id[key] = task_id
                task = Task(
                    id=task_id,
                    execution_id=execution.id,
                    project_id=plan.project_id,
                    objective=spec.objective,
                    permitted_scope=spec.permitted_scope,
                    expected_evidence=spec.expected_evidence,
                    state="pending",  # will compute readiness below
                    agent_type_id=spec.agent_type_id,
                    created_at=now_utc_iso(),
                    updated_at=now_utc_iso(),
                )
                self._execution_repo.insert_task(task, commit=False)

            # Create dependencies.
            for dep_spec in dependency_specs:
                if dep_spec.task_key not in key_to_task_id:
                    raise MissingDependencyError(
                        f"Dependency task_key {dep_spec.task_key!r} not found"
                    )
                if dep_spec.depends_on_key not in key_to_task_id:
                    raise MissingDependencyError(
                        f"Dependency depends_on_key {dep_spec.depends_on_key!r} not found"
                    )
                dependency = TaskDependency(
                    task_id=key_to_task_id[dep_spec.task_key],
                    depends_on_task_id=key_to_task_id[dep_spec.depends_on_key],
                )
                self._execution_repo.insert_dependency(dependency, commit=False)

            # Compute initial readiness: a task with no dependencies is
            # ready; a task with dependencies stays pending.
            self._recompute_readiness(execution.id, commit=False)

            # Link the handoff to the execution.
            self._plan_repo.set_handoff_execution_id(handoff.id, execution.id.value, commit=False)

            # Take a snapshot for restart safety.
            self._snapshot(execution.id, "before_fan_out", commit=False)

            # Audit.
            self._audit_repo.insert(
                AuditEvent(
                    id=AuditEventId(generate_audit_event_id()),
                    project_id=plan.project_id,
                    actor_id=actor_id,
                    source=source,
                    operation="execution.create",
                    target_type="execution",
                    target_id=execution.id.value,
                    result="success",
                    redacted_summary=(
                        f"Created execution {execution.id.value} from plan {plan.id.value}"
                    ),
                    correlation_id=execution.id.value,
                    created_at=now_utc_iso(),
                ),
                commit=False,
            )

        return execution

    def _validate_specs(
        self,
        task_specs: list[TaskSpec],
        dependency_specs: list[DependencySpec],
    ) -> None:
        if not task_specs:
            raise ValueError("task_specs must not be empty")
        if len(task_specs) > 256:
            raise ValueError("task_specs exceeds the maximum graph size")
        if len(dependency_specs) > 1024:
            raise ValueError("dependency_specs exceeds the maximum graph size")
        for spec in task_specs:
            if not isinstance(spec.objective, str) or not spec.objective.strip():
                raise ValueError("task objective must be a non-empty string")
            if len(spec.objective) > 8192:
                raise ValueError("task objective exceeds the maximum length")
            if spec.key is not None and len(spec.key) > 256:
                raise ValueError("task key exceeds the maximum length")
            if len(spec.permitted_scope) > 64 or any(
                not isinstance(path, str) or len(path) > 1024 for path in spec.permitted_scope
            ):
                raise ValueError("task permitted_scope exceeds the policy limit")
            if len(spec.expected_evidence) > 64 or any(
                not isinstance(item, str) or not item.strip() or len(item) > 1024
                for item in spec.expected_evidence
            ):
                raise ValueError("task expected_evidence exceeds the policy limit")
        # Check for duplicate keys.
        keys = [s.key or s.objective for s in task_specs]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate task keys in task_specs")
        for dep in dependency_specs:
            if dep.task_key == dep.depends_on_key:
                raise CycleError(f"Task {dep.task_key!r} cannot depend on itself")

    def _detect_cycles(
        self,
        task_specs: list[TaskSpec],
        dependency_specs: list[DependencySpec],
    ) -> None:
        """Topological sort to detect cycles.

        Per PLAN.md M5 validation: "Cycles and missing dependencies
        are rejected."
        """
        keys = [s.key or s.objective for s in task_specs]
        key_set = set(keys)
        # Build adjacency list: dep_on -> [tasks that depend on it]
        adj: dict[str, list[str]] = defaultdict(list)
        in_degree: dict[str, int] = {k: 0 for k in keys}
        for dep in dependency_specs:
            if dep.task_key not in key_set:
                raise MissingDependencyError(f"Dependency task_key {dep.task_key!r} not found")
            if dep.depends_on_key not in key_set:
                raise MissingDependencyError(
                    f"Dependency depends_on_key {dep.depends_on_key!r} not found"
                )
            adj[dep.depends_on_key].append(dep.task_key)
            in_degree[dep.task_key] += 1
        # Kahn's algorithm.
        queue = deque(k for k in keys if in_degree[k] == 0)
        visited = 0
        while queue:
            node = queue.popleft()
            visited += 1
            for neighbor in adj[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)
        if visited != len(keys):
            # Find the cycle for the error message.
            remaining = [k for k in keys if in_degree[k] > 0]
            raise CycleError(
                f"Cycle detected in task dependency graph; nodes in cycles: {remaining}",
                cycle=remaining,
            )

    def _recompute_readiness(
        self,
        execution_id: ExecutionId,
        *,
        commit: bool = True,
    ) -> None:
        """Recompute task readiness based on dependency states.

        A task is ready when:
        - its state is ``pending`` (not yet started);
        - all its dependencies are in terminal *successful* state
          (``completed``);
        - if any dependency is in a blocking state (failed, blocked,
          cancelled), the task stays pending (it will be blocked by
          the caller).

        Dependency-``blocked`` tasks are also revisited: when every
        dependency has recovered to ``completed`` (for example after a
        requeued retry succeeded), a dependency-blocked task returns to
        ``ready``. Human/provider-unknown blocks are left untouched.
        """
        tasks = self._execution_repo.list_tasks_for_execution(execution_id)
        for task in tasks:
            if task.state == "blocked":
                reason = task.blocker_reason or ""
                if not reason.startswith("dependency"):
                    # Human decisions and provider reconciliation are
                    # not auto-revivable here.
                    continue
                deps = self._execution_repo.list_dependencies_for_task(task.id)
                dep_states = [
                    self._execution_repo.get_task(dep.depends_on_task_id).state for dep in deps
                ]
                if dep_states and all(state == "completed" for state in dep_states):
                    self._execution_repo.update_task_state(
                        task.id,
                        "ready",
                        blocker_reason="",
                        commit=commit,
                    )
                elif dep_states and not any(state in BLOCKING_TASK_STATES for state in dep_states):
                    # Dependencies recovered but are not all complete;
                    # return to pending so the normal readiness pass
                    # governs.
                    self._execution_repo.update_task_state(
                        task.id,
                        "pending",
                        blocker_reason="",
                        commit=commit,
                    )
                continue
            if task.state != "pending":
                continue
            deps = self._execution_repo.list_dependencies_for_task(task.id)
            if not deps:
                # No dependencies: ready.
                self._execution_repo.update_task_state(task.id, "ready", commit=commit)
                continue
            all_completed = True
            any_blocking = False
            for dep in deps:
                dep_task = self._execution_repo.get_task(dep.depends_on_task_id)
                if dep_task.state != "completed":
                    all_completed = False
                if dep_task.state in BLOCKING_TASK_STATES:
                    any_blocking = True
            if all_completed:
                self._execution_repo.update_task_state(task.id, "ready", commit=commit)
            elif any_blocking:
                # A dependency failed/blocked/cancelled: this task
                # cannot proceed. Mark it as blocked.
                self._execution_repo.update_task_state(
                    task.id,
                    "blocked",
                    blocker_reason="dependency failed/blocked/cancelled",
                    commit=commit,
                )

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------

    def list_ready_tasks(
        self,
        execution_id: ExecutionId,
        *,
        project_id: ProjectId | None = None,
        actor_id: UserId | None = None,
        source: AuditSource = "system",
    ) -> list[Task]:
        """List ready tasks only after project authorization."""
        project_id = self._require_project_scope(
            project_id=project_id,
            actor_id=actor_id,
            permission="execution.view_diffs",
            source=source,
        )
        self._execution_repo.get_execution(execution_id, project_id=project_id)
        tasks = self._execution_repo.list_tasks_for_execution(
            execution_id,
            project_id=project_id,
        )
        return [t for t in tasks if t.state == "ready"]

    def _validate_evidence_artifacts(
        self,
        *,
        task: Task,
        attempt_id: TaskAttemptId,
        evidence_artifact_ids: tuple[ArtifactId, ...],
    ) -> tuple[str, ...]:
        unique_ids = tuple(dict.fromkeys(evidence_artifact_ids))
        if task.expected_evidence and len(unique_ids) < len(task.expected_evidence):
            raise MissingEvidenceError(
                f"task {task.id} requires {len(task.expected_evidence)} "
                "distinct durable evidence artifacts"
            )

        validated: list[str] = []
        satisfied: set[str] = set()
        freeform_patterns = {
            expected: re.compile(rf"\b{re.escape(expected)}\b", re.IGNORECASE)
            for expected in task.expected_evidence
            if expected not in _EVIDENCE_KINDS
        }
        for artifact_id in unique_ids:
            try:
                artifact = self._artifact_repo.get_artifact(task.project_id, artifact_id)
            except ArtifactNotFoundError as exc:
                raise MissingEvidenceError(
                    f"evidence artifact {artifact_id.value} is not in the task project"
                ) from exc

            def _parse_provenance(text: str | None):
                try:
                    loaded = json.loads(text or "")
                except (TypeError, json.JSONDecodeError):
                    return None
                return loaded if isinstance(loaded, dict) else None

            # Bug fix (real run, 2026-08-28): artifact content deduplication
            # may return an artifact row whose `provenance` COLUMN carries
            # the FIRST storer's identity (e.g. an earlier attempt that
            # produced a byte-identical diff). The provenance contract is
            # per-STORE, not per-content: every store_artifact call appends
            # an independent artifact_provenance row ("Deduplication does
            # not merge provenance"). The validator therefore must check
            # whether ANY provenance row matches this task/attempt, not
            # just the stale first-store column.
            expected_provenance = {
                "execution_id": task.execution_id.value,
                "task_id": task.id.value,
                "attempt_id": attempt_id.value,
            }
            matching_rows: list[dict] = []
            column_provenance = _parse_provenance(artifact.provenance)
            if column_provenance is not None and all(
                column_provenance.get(key) == value
                for key, value in expected_provenance.items()
            ):
                matching_rows.append(column_provenance)
            try:
                rows = self._artifact_repo.list_provenance(
                    task.project_id, artifact_id
                )
            except Exception as prov_exc:  # noqa: BLE001 - fail closed below
                rows = []
                logger.debug(
                    "provenance listing failed for artifact %s: %s",
                    artifact_id.value,
                    type(prov_exc).__name__,
                )
            for row in rows:
                row_provenance = _parse_provenance(getattr(row, "provenance", None))
                if row_provenance is None:
                    continue
                if all(
                    row_provenance.get(key) == value
                    for key, value in expected_provenance.items()
                ):
                    matching_rows.append(row_provenance)
            if not matching_rows:
                raise MissingEvidenceError(
                    f"evidence artifact {artifact_id.value} does not belong to "
                    f"task {task.id.value} and attempt {attempt_id.value}"
                )
            labels: list[str] = []
            for row_provenance in matching_rows:
                row_labels = row_provenance.get("evidence_labels", ())
                if isinstance(row_labels, (list, tuple, set)):
                    labels.extend(str(item) for item in row_labels)
            label_set = set(labels)
            # Model-produced transcript artifacts are excluded from legacy
            # freeform matching: a model could otherwise satisfy any
            # human-acceptance label by echoing its text into a response.
            freeform_eligible_kind = artifact.kind not in {"transcript"}
            for expected in task.expected_evidence:
                if expected in satisfied:
                    continue
                kind_matches = (
                    expected in _EVIDENCE_KINDS and artifact.kind in _EVIDENCE_KINDS[expected]
                )
                explicit_match = expected in label_set
                # Legacy human acceptance text remains supported only when
                # the artifact itself asserts that exact term as a word and
                # the artifact is not model-authored prose. It is still not
                # enough for a caller to pass an unrelated label.
                freeform_match = (
                    expected not in _EVIDENCE_KINDS
                    and freeform_eligible_kind
                    and bool(freeform_patterns[expected].search(artifact.content))
                )
                if kind_matches or explicit_match or freeform_match:
                    satisfied.add(expected)
                    break
            validated.append(artifact_id.value)
        missing = tuple(item for item in task.expected_evidence if item not in satisfied)
        if missing:
            raise MissingEvidenceError(
                f"task {task.id} evidence artifacts do not prove: {', '.join(missing)}"
            )
        return tuple(validated)

    def _validate_attempt_identity(
        self,
        *,
        task: Task,
        attempt_id: TaskAttemptId,
        lease_owner: str | None,
        require_live_lease: bool = True,
    ) -> TaskAttempt:
        """Fence task transitions to the exact attempt lease.

        ``require_live_lease=False`` still demands the recorded owner
        but tolerates an expired clock: recording a terminal FAILURE or
        CANCELLATION by the same owner is safer than leaving a zombie
        ``running`` attempt that only restart recovery could reconcile.
        Completion keeps the strict live-lease requirement.
        """
        attempt = self._execution_repo.get_attempt(attempt_id)
        if attempt.task_id != task.id or attempt.project_id != task.project_id:
            raise AttemptIdentityError(f"attempt {attempt_id} does not belong to task {task.id}")
        if attempt.state != "running":
            raise InvalidTaskTransitionError(f"attempt {attempt_id} is not running")
        if not lease_owner or attempt.lease_owner != lease_owner:
            raise LeaseOwnershipError(f"lease owner is not current for attempt {attempt_id}")
        if not require_live_lease:
            return attempt
        if not attempt.lease_expires_at:
            raise LeaseOwnershipError(f"attempt {attempt_id} has no active lease")
        try:
            expires = datetime.fromisoformat(attempt.lease_expires_at)
        except ValueError as exc:
            raise LeaseOwnershipError(f"attempt {attempt_id} has an invalid lease") from exc
        if expires <= datetime.now(UTC):
            raise LeaseOwnershipError(f"lease expired for attempt {attempt_id}")
        return attempt

    def claim_task(
        self,
        *,
        execution_id: ExecutionId,
        task_id: TaskId,
        project_id: ProjectId | None = None,
        actor_id: UserId | None = None,
        lease_owner: str,
        lease_duration_seconds: int = 300,
        source: AuditSource = "system",
    ) -> TaskAttempt:
        """Claim a ready task for execution.

        Per ``zero-recovery-consistency`` §"Leases distinguish
        ownership from history": a lease identifies which worker
        currently owns progress and when ownership may be reconsidered.

        Per PLAN.md M5: "Replayed scheduler events do not duplicate
        work." Each claim creates a new attempt with an incremented
        attempt_number.

        Returns the new :class:`TaskAttempt`. The caller (a future
        task runner) is responsible for transitioning the attempt to
        ``succeeded`` or ``failed`` and for transitioning the task to
        ``completed`` or ``failed``.
        """
        project_id = self._require_project_scope(
            project_id=project_id,
            actor_id=actor_id,
            permission="execution.start",
            source=source,
        )
        if not lease_owner or len(lease_owner) > 256:
            raise LeaseOwnershipError("lease_owner must be a bounded non-empty value")
        if lease_duration_seconds < 1 or lease_duration_seconds > 86_400:
            raise LeaseOwnershipError("lease duration is outside the allowed range")
        task = self._execution_repo.get_task(task_id, project_id=project_id)
        execution = self._execution_repo.get_execution(execution_id, project_id=project_id)
        if task.execution_id != execution_id or task.project_id != execution.project_id:
            raise TaskNotFoundError(f"Task {task_id} does not belong to execution {execution_id}")
        if task.state != "ready":
            raise InvalidTaskTransitionError(
                f"Task {task_id} is in state {task.state!r}, not 'ready'"
            )
        with self._execution_repo._database.transaction():
            # Conditional UPDATE is the claim.  Two workers racing here
            # cannot both create a running attempt for the same ready task.
            self._execution_repo.claim_task_atomically(
                execution_id=execution_id,
                task_id=task.id,
                project_id=task.project_id,
                commit=False,
            )
            existing_attempts = self._execution_repo.list_attempts_for_task(
                task.id,
                project_id=project_id,
            )
            attempt_number = (
                max(
                    (attempt.attempt_number for attempt in existing_attempts),
                    default=0,
                )
                + 1
            )
            from datetime import timedelta

            lease_expires = datetime.now(UTC) + timedelta(seconds=lease_duration_seconds)
            attempt = TaskAttempt(
                id=TaskAttemptId(generate_task_attempt_id()),
                task_id=task.id,
                project_id=task.project_id,
                attempt_number=attempt_number,
                state="running",
                lease_owner=lease_owner,
                lease_expires_at=lease_expires.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                started_at=now_utc_iso(),
            )
            self._execution_repo.insert_attempt(attempt, commit=False)
            # A recovered/paused execution with ready work resumes when a
            # scheduler successfully claims the work. Clear the stale
            # retry blocker so the state stays truthful (live cosmetic
            # bug 2026-08-31: executions showed "awaiting automatic task
            # retry" while running).
            if execution.state in {"pending", "paused"}:
                self._execution_repo.update_execution_state(
                    execution_id, "running", blocker_reason="", commit=False
                )
        return attempt

    def renew_task_lease(
        self,
        *,
        execution_id: ExecutionId,
        task_id: TaskId,
        attempt_id: TaskAttemptId,
        project_id: ProjectId | None = None,
        actor_id: UserId,
        lease_owner: str,
        lease_duration_seconds: int = 300,
        source: AuditSource = "system",
    ) -> TaskAttempt:
        """Renew a live task lease without changing its owner."""
        project_id = self._require_project_scope(
            project_id=project_id,
            actor_id=actor_id,
            permission="execution.start",
            source=source,
        )
        execution = self._execution_repo.get_execution(execution_id, project_id=project_id)
        task = self._execution_repo.get_task(task_id, project_id=project_id)
        if task.execution_id != execution.id:
            raise TaskNotFoundError(f"Task {task_id} does not belong to execution {execution_id}")
        attempt = self._execution_repo.get_attempt(attempt_id)
        if attempt.task_id != task.id or attempt.project_id != project_id:
            raise AttemptIdentityError(f"attempt {attempt_id} does not belong to task {task_id}")
        self._execution_repo.renew_attempt_lease(
            attempt_id,
            task_id=task.id,
            project_id=project_id,
            lease_owner=lease_owner,
            lease_duration_seconds=lease_duration_seconds,
        )
        return self._execution_repo.get_attempt(attempt_id)

    def complete_task(
        self,
        *,
        execution_id: ExecutionId,
        project_id: ProjectId | None = None,
        task_id: TaskId,
        attempt_id: TaskAttemptId,
        actor_id: UserId,
        lease_owner: str | None = None,
        evidence: tuple[str, ...] = (),
        evidence_artifact_ids: tuple[ArtifactId, ...] = (),
        source: AuditSource = "system",
    ) -> Task:
        """Mark a task complete only under its current attempt lease."""
        project_id = self._require_project_scope(
            project_id=project_id,
            actor_id=actor_id,
            permission="execution.start",
            source=source,
        )
        execution = self._execution_repo.get_execution(execution_id, project_id=project_id)
        task = self._execution_repo.get_task(task_id, project_id=project_id)
        if task.execution_id != execution_id or task.project_id != execution.project_id:
            raise TaskNotFoundError(f"Task {task_id} does not belong to execution {execution_id}")
        provided = tuple(dict.fromkeys(evidence))
        missing = tuple(item for item in task.expected_evidence if item not in provided)
        if is_terminal_task_state(task.state):
            completed_attempt = self._execution_repo.get_attempt(attempt_id)
            if (
                completed_attempt.task_id != task.id
                or completed_attempt.project_id != task.project_id
            ):
                raise AttemptIdentityError(
                    f"attempt {attempt_id} does not belong to task {task.id}"
                )
            return task
        with self._execution_repo._database.transaction():
            self._validate_attempt_identity(
                task=task,
                attempt_id=attempt_id,
                lease_owner=lease_owner,
            )
            durable_evidence = self._validate_evidence_artifacts(
                task=task,
                attempt_id=attempt_id,
                evidence_artifact_ids=evidence_artifact_ids,
            )
            if missing:
                raise MissingEvidenceError(
                    f"task {task_id} is missing expected evidence: {', '.join(missing)}"
                )
            if task.state != "running":
                raise InvalidTaskTransitionError(
                    f"Task {task_id} is in state {task.state!r}, cannot transition to 'completed'"
                )
            self._execution_repo.update_attempt_state(
                attempt_id,
                "succeeded",
                expected_task_id=task.id,
                expected_project_id=task.project_id,
                expected_lease_owner=lease_owner,
                require_active_lease=True,
                commit=False,
            )
            self._execution_repo.update_task_state(
                task.id,
                "completed",
                completion_evidence=durable_evidence or provided,
                commit=False,
            )
            self._recompute_readiness(execution_id, commit=False)
            self._maybe_complete_execution(execution_id, commit=False)
            self._snapshot(execution_id, "after_task_complete", commit=False)
            self._audit_repo.insert(
                AuditEvent(
                    id=AuditEventId(generate_audit_event_id()),
                    project_id=execution.project_id,
                    actor_id=actor_id,
                    source=source,
                    operation="task.complete",
                    target_type="task",
                    target_id=task.id.value,
                    result="success",
                    correlation_id=execution_id.value,
                    created_at=now_utc_iso(),
                ),
                commit=False,
            )
        if self._metrics is not None:
            self._metrics.increment("task_transitions_total", result="success", source=source)
        return self._execution_repo.get_task(task_id, project_id=project_id)

    def fail_task(
        self,
        *,
        execution_id: ExecutionId,
        project_id: ProjectId | None = None,
        task_id: TaskId,
        attempt_id: TaskAttemptId,
        error_message: str,
        actor_id: UserId,
        lease_owner: str | None = None,
        source: AuditSource = "system",
    ) -> Task:
        """Mark a task failed under its attempt lease.

        The lease owner must match, but an expired clock does not block
        failure recording: the alternative is a zombie ``running``
        attempt that only restart recovery could reconcile.
        """
        project_id = self._require_project_scope(
            project_id=project_id,
            actor_id=actor_id,
            permission="execution.start",
            source=source,
        )
        execution = self._execution_repo.get_execution(execution_id, project_id=project_id)
        task = self._execution_repo.get_task(task_id, project_id=project_id)
        if task.execution_id != execution_id or task.project_id != execution.project_id:
            raise TaskNotFoundError(f"Task {task_id} does not belong to execution {execution_id}")
        if len(error_message) > 4096:
            error_message = error_message[:4096] + " [truncated]"
        if is_terminal_task_state(task.state):
            return task
        if task.state != "running":
            raise InvalidTaskTransitionError(
                f"Task {task_id} is in state {task.state!r}, cannot transition to 'failed'"
            )
        with self._execution_repo._database.transaction():
            self._validate_attempt_identity(
                task=task,
                attempt_id=attempt_id,
                lease_owner=lease_owner,
                require_live_lease=False,
            )
            self._execution_repo.update_attempt_state(
                attempt_id,
                "failed",
                error_message=error_message,
                expected_task_id=task.id,
                expected_project_id=task.project_id,
                expected_lease_owner=lease_owner,
                require_active_lease=False,
                commit=False,
            )
            self._execution_repo.update_task_state(task.id, "failed", commit=False)
            self._recompute_readiness(execution_id, commit=False)
            self._maybe_complete_execution(execution_id, commit=False)
            self._snapshot(execution_id, "after_task_fail", commit=False)
            self._audit_repo.insert(
                AuditEvent(
                    id=AuditEventId(generate_audit_event_id()),
                    project_id=execution.project_id,
                    actor_id=actor_id,
                    source=source,
                    operation="task.fail",
                    target_type="task",
                    target_id=task.id.value,
                    result="failure",
                    redacted_summary=f"Task {task.id.value} failed",
                    correlation_id=execution_id.value,
                    created_at=now_utc_iso(),
                ),
                commit=False,
            )
        if self._metrics is not None:
            self._metrics.increment("task_transitions_total", result="failure", source=source)
        return self._execution_repo.get_task(task_id, project_id=project_id)

    def mark_provider_outcome_unknown(
        self,
        *,
        execution_id: ExecutionId,
        project_id: ProjectId | None = None,
        task_id: TaskId,
        attempt_id: TaskAttemptId,
        error_message: str,
        actor_id: UserId,
        lease_owner: str | None = None,
        source: AuditSource = "system",
    ) -> Task:
        """Block a task when a provider may have accepted the request."""
        project_id = self._require_project_scope(
            project_id=project_id,
            actor_id=actor_id,
            permission="execution.start",
            source=source,
        )
        execution = self._execution_repo.get_execution(execution_id, project_id=project_id)
        task = self._execution_repo.get_task(task_id, project_id=project_id)
        if task.execution_id != execution_id or task.project_id != execution.project_id:
            raise TaskNotFoundError(f"Task {task_id} does not belong to execution {execution_id}")
        if len(error_message) > 4096:
            error_message = error_message[:4096] + " [truncated]"
        if is_terminal_task_state(task.state):
            return task
        if task.state != "running":
            raise InvalidTaskTransitionError(
                f"Task {task_id} is in state {task.state!r}, cannot mark unknown"
            )
        blocker_reason = "provider outcome unknown; reconciliation required"
        with self._execution_repo._database.transaction():
            self._validate_attempt_identity(
                task=task,
                attempt_id=attempt_id,
                lease_owner=lease_owner,
            )
            self._execution_repo.update_attempt_state(
                attempt_id,
                "unknown",
                error_message=error_message,
                expected_task_id=task.id,
                expected_project_id=task.project_id,
                expected_lease_owner=lease_owner,
                require_active_lease=True,
                commit=False,
            )
            self._execution_repo.update_task_state(
                task.id,
                "blocked",
                blocker_reason=blocker_reason,
                commit=False,
            )
            self._recompute_readiness(execution_id, commit=False)
            self._maybe_complete_execution(execution_id, commit=False)
            self._snapshot(execution_id, "after_provider_unknown", commit=False)
            self._audit_repo.insert(
                AuditEvent(
                    id=AuditEventId(generate_audit_event_id()),
                    project_id=execution.project_id,
                    actor_id=actor_id,
                    source=source,
                    operation="task.provider_unknown",
                    target_type="task",
                    target_id=task.id.value,
                    result="error",
                    redacted_summary=f"Task {task.id.value} requires provider reconciliation",
                    correlation_id=execution_id.value,
                    created_at=now_utc_iso(),
                ),
                commit=False,
            )
        if self._metrics is not None:
            self._metrics.increment("task_transitions_total", result="error", source=source)
        return self._execution_repo.get_task(task_id, project_id=project_id)

    def _maybe_complete_execution(
        self,
        execution_id: ExecutionId,
        *,
        commit: bool = True,
    ) -> None:
        """Advance the execution to its terminal or resting state.

        - all tasks completed -> ``completed``;
        - nothing runnable remains, blocked/failed work exists, no
          retry budget remains, and no provider-unknown reconciliation
          is pending -> ``failed`` (a graph that can never proceed must
          not pause forever);
        - otherwise -> ``paused`` (provider-unknown reconciliation or
          automatic task retry still possible).
        """
        tasks = self._execution_repo.list_tasks_for_execution(execution_id)
        if not tasks:
            return
        all_completed = all(t.state == "completed" for t in tasks)
        if all_completed:
            execution = self._execution_repo.get_execution(execution_id)
            if is_valid_execution_transition(execution.state, "completed"):
                # Blocker hygiene (real-run fix): the r7 completion was
                # delivered as "finished with state: completed" while
                # still carrying the stale pause-time blocker "task
                # failed or blocked" — misleading to humans reading the
                # delivery. A completed execution has no open blocker.
                self._execution_repo.update_execution_state(
                    execution_id, "completed", blocker_reason="", commit=commit
                )
                self._discard_cancellation_event(execution_id)
            return
        any_running_or_ready = any(t.state in ("running", "ready") for t in tasks)
        any_pending = any(t.state == "pending" for t in tasks)
        blocking_states = ("failed", "blocked", "cancelled")
        any_blocked = any(t.state in blocking_states for t in tasks)
        if not (any_blocked and not any_running_or_ready):
            return
        execution = self._execution_repo.get_execution(execution_id)
        # Automatic retry budget: a failed task that has not consumed
        # its attempt allowance keeps the execution paused so the
        # scheduler can requeue it (a terminal ``failed`` execution can
        # never be claimed again).
        if self._auto_retry_pending(tasks):
            if is_valid_execution_transition(execution.state, "paused"):
                self._execution_repo.update_execution_state(
                    execution_id,
                    "paused",
                    blocker_reason="awaiting automatic task retry",
                    commit=commit,
                )
            return
        # Provider-unknown blocks await operator reconciliation: keep the
        # execution paused rather than declaring it permanently failed.
        awaiting_reconciliation = any(
            t.state == "blocked"
            and t.blocker_reason
            and "provider outcome unknown" in t.blocker_reason
            for t in tasks
        )
        if (
            not awaiting_reconciliation
            and not any_pending
            and is_valid_execution_transition(execution.state, "failed")
        ):
            self._execution_repo.update_execution_state(
                execution_id,
                "failed",
                blocker_reason="task failed; graph cannot proceed",
                commit=commit,
            )
            self._discard_cancellation_event(execution_id)
            return
        if is_valid_execution_transition(execution.state, "paused"):
            self._execution_repo.update_execution_state(
                execution_id,
                "paused",
                blocker_reason="task failed or blocked",
                commit=commit,
            )

    def _auto_retry_pending(self, tasks: list[Task]) -> bool:
        """Whether any failed task still holds unused attempt budget."""
        if self._task_max_attempts <= 0:
            return False
        for task in tasks:
            if task.state != "failed":
                continue
            attempts = self._execution_repo.list_attempts_for_task(task.id)
            if len(attempts) < self._task_max_attempts:
                return True
        return False

    # ------------------------------------------------------------------
    # Cancellation
    # ------------------------------------------------------------------

    def cancel_execution(
        self,
        *,
        execution_id: ExecutionId,
        project_id: ProjectId | None = None,
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> Execution:
        """Cancel an execution and propagate to all non-terminal tasks.

        Per ``zero-planner-worker-contract`` §"Cancellation is a state
        transition": cancellation affects running attempts, dependent
        tasks, tool calls, worktrees, artifacts, and final reporting.
        Completed immutable evidence remains history; cancelled work
        does not become accepted project knowledge.

        Per ``zero-recovery-consistency`` §"Cancellation preserves
        evidence": cancellation stops future progress under policy,
        attempts to stop active children/tools, and records what
        remains.

        Propagation rule (tested):
        - Tasks in terminal states (completed, cancelled) are not
          changed.
        - Tasks in non-terminal states (pending, ready, running,
          blocked) are transitioned to ``cancelled``.
        - Running attempts are transitioned to ``cancelled``.
        """
        project_id = self._require_project_scope(
            project_id=project_id,
            actor_id=actor_id,
            permission="execution.stop",
            source=source,
        )
        execution = self._execution_repo.get_execution(execution_id, project_id=project_id)
        if not is_valid_execution_transition(execution.state, "cancelled"):
            raise InvalidExecutionTransitionError(
                f"Cannot cancel execution in state {execution.state!r}"
            )
        with self._execution_repo._database.transaction():
            # Cancel all non-terminal tasks.
            tasks = self._execution_repo.list_tasks_for_execution(execution_id)
            for task in tasks:
                if not is_terminal_task_state(task.state):
                    self._execution_repo.update_task_state(task.id, "cancelled", commit=False)
                    # Cancel any running attempts.
                    attempts = self._execution_repo.list_attempts_for_task(task.id)
                    for attempt in attempts:
                        if attempt.state == "running":
                            self._execution_repo.update_attempt_state(
                                attempt.id, "cancelled", commit=False
                            )
            # Transition execution.
            self._execution_repo.update_execution_state(execution_id, "cancelled", commit=False)
            # Snapshot.
            self._snapshot(execution_id, "after_cancel", commit=False)
            # Audit.
            self._audit_repo.insert(
                AuditEvent(
                    id=AuditEventId(generate_audit_event_id()),
                    project_id=execution.project_id,
                    actor_id=actor_id,
                    source=source,
                    operation="execution.cancel",
                    target_type="execution",
                    target_id=execution.id.value,
                    result="success",
                    redacted_summary=f"Cancelled execution {execution.id.value}",
                    correlation_id=execution_id.value,
                    created_at=now_utc_iso(),
                ),
                commit=False,
            )
        self.get_cancellation_event(execution_id).set()
        self._discard_cancellation_event(execution_id)
        return self._execution_repo.get_execution(execution_id, project_id=project_id)

    def requeue_failed_task(
        self,
        *,
        execution_id: ExecutionId,
        project_id: ProjectId | None = None,
        task_id: TaskId,
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> Task:
        """Requeue a failed task for another attempt (``failed -> ready``).

        The state machine permits this transition explicitly for retry;
        the scheduler bounds how many attempts may be consumed by
        ``ZERO_TASK_MAX_ATTEMPTS`` (default 0 = auto-retry disabled).
        """
        project_id = self._require_project_scope(
            project_id=project_id,
            actor_id=actor_id,
            permission="execution.start",
            source=source,
        )
        execution = self._execution_repo.get_execution(execution_id, project_id=project_id)
        task = self._execution_repo.get_task(task_id, project_id=project_id)
        if task.execution_id != execution_id or task.project_id != execution.project_id:
            raise TaskNotFoundError(f"Task {task_id} does not belong to execution {execution_id}")
        if is_terminal_task_state(task.state):
            raise InvalidTaskTransitionError(
                f"Task {task_id} is terminal ({task.state!r}); cannot requeue"
            )
        if task.state != "failed":
            raise InvalidTaskTransitionError(
                f"Task {task_id} is in state {task.state!r}, cannot requeue"
            )
        with self._execution_repo._database.transaction():
            self._execution_repo.update_task_state(task.id, "ready", commit=False)
            self._snapshot(execution_id, "task_requeue", commit=False)
            self._audit_repo.insert(
                AuditEvent(
                    id=AuditEventId(generate_audit_event_id()),
                    project_id=execution.project_id,
                    actor_id=actor_id,
                    source=source,
                    operation="task.requeue",
                    target_type="task",
                    target_id=task.id.value,
                    result="success",
                    redacted_summary=f"Task {task.id.value} requeued for retry",
                    correlation_id=execution_id.value,
                    created_at=now_utc_iso(),
                ),
                commit=False,
            )
        return self._execution_repo.get_task(task_id, project_id=project_id)

    def reconcile_blocked_task(
        self,
        *,
        execution_id: ExecutionId,
        project_id: ProjectId | None = None,
        task_id: TaskId,
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> Task:
        """Operator reconciliation for a blocked task (``blocked -> ready``).

        Real-run fix: ``mark_provider_outcome_unknown`` blocks a task and
        pauses the execution demanding "reconciliation required", but no
        service method performed that reconciliation — the task state
        machine explicitly permits ``blocked -> ready`` ("ready: human
        unblocked") yet the dead-end had no operator path. Typical
        reconciliation: the operator inspected the provider request log,
        confirmed the ambiguous request produced no consumed result, and
        unblocks the task for a fresh attempt. Audited like every other
        transition; readiness is recomputed so downstream tasks follow
        the graph normally.
        """
        project_id = self._require_project_scope(
            project_id=project_id,
            actor_id=actor_id,
            permission="execution.start",
            source=source,
        )
        execution = self._execution_repo.get_execution(execution_id, project_id=project_id)
        task = self._execution_repo.get_task(task_id, project_id=project_id)
        if task.execution_id != execution_id or task.project_id != execution.project_id:
            raise TaskNotFoundError(f"Task {task_id} does not belong to execution {execution_id}")
        if is_terminal_task_state(task.state):
            raise InvalidTaskTransitionError(
                f"Task {task_id} is terminal ({task.state!r}); cannot reconcile"
            )
        if task.state != "blocked":
            raise InvalidTaskTransitionError(
                f"Task {task_id} is in state {task.state!r}, cannot reconcile"
            )
        with self._execution_repo._database.transaction():
            self._execution_repo.update_task_state(
                task.id,
                "ready",
                blocker_reason=None,
                commit=False,
            )
            self._recompute_readiness(execution_id, commit=False)
            self._snapshot(execution_id, "task_reconciled", commit=False)
            self._audit_repo.insert(
                AuditEvent(
                    id=AuditEventId(generate_audit_event_id()),
                    project_id=execution.project_id,
                    actor_id=actor_id,
                    source=source,
                    operation="task.reconcile",
                    target_type="task",
                    target_id=task.id.value,
                    result="success",
                    redacted_summary=f"Task {task.id.value} reconciled by operator; requeued",
                    correlation_id=execution_id.value,
                    created_at=now_utc_iso(),
                ),
                commit=False,
            )
        if self._metrics is not None:
            self._metrics.increment("task_transitions_total", result="success", source=source)
        return self._execution_repo.get_task(task_id, project_id=project_id)

    def schedule_task_retry(
        self,
        *,
        task_id: TaskId,
        next_retry_at: str,
        project_id: ProjectId | None = None,
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> Task:
        """Stamp the earliest requeue instant on a failed task (GAP 12).

        The scheduler calls this after requeueing so subsequent ticks
        skip the task until backoff (or a provider Retry-After) has
        elapsed. The summary is redacted and carries no error content.
        """
        project_id = self._require_project_scope(
            project_id=project_id,
            actor_id=actor_id,
            permission="execution.start",
            source=source,
        )
        task = self._execution_repo.get_task(task_id, project_id=project_id)
        self._execution_repo.set_task_next_retry_at(task.id, next_retry_at)
        self._audit_repo.insert(
            AuditEvent(
                id=AuditEventId(generate_audit_event_id()),
                project_id=task.project_id,
                actor_id=actor_id,
                source=source,
                operation="task.retry_scheduled",
                target_type="task",
                target_id=task.id.value,
                result="success",
                redacted_summary=(f"Task {task.id.value} retry scheduled after {next_retry_at}"),
                correlation_id=task.execution_id.value,
                created_at=now_utc_iso(),
            )
        )
        return self._execution_repo.get_task(task_id, project_id=project_id)

    def cancel_task(
        self,
        *,
        execution_id: ExecutionId,
        project_id: ProjectId | None = None,
        task_id: TaskId,
        attempt_id: TaskAttemptId,
        actor_id: UserId,
        lease_owner: str | None = None,
        reason: str = "task cancelled",
        source: AuditSource = "system",
    ) -> Task:
        """Cancel one running task under its current attempt lease.

        Cancellation is a first-class state transition (not a failure):
        the attempt and the task both end ``cancelled``, dependents are
        re-evaluated, and the graph snapshot records ``after_cancel``.
        """
        project_id = self._require_project_scope(
            project_id=project_id,
            actor_id=actor_id,
            permission="execution.stop",
            source=source,
        )
        execution = self._execution_repo.get_execution(execution_id, project_id=project_id)
        task = self._execution_repo.get_task(task_id, project_id=project_id)
        if task.execution_id != execution_id or task.project_id != execution.project_id:
            raise TaskNotFoundError(f"Task {task_id} does not belong to execution {execution_id}")
        if len(reason) > 4096:
            reason = reason[:4096] + " [truncated]"
        if is_terminal_task_state(task.state):
            return task
        with self._execution_repo._database.transaction():
            self._validate_attempt_identity(
                task=task,
                attempt_id=attempt_id,
                lease_owner=lease_owner,
                require_live_lease=False,
            )
            self._execution_repo.update_attempt_state(
                attempt_id,
                "cancelled",
                error_message=reason,
                expected_task_id=task.id,
                expected_project_id=task.project_id,
                expected_lease_owner=lease_owner,
                require_active_lease=False,
                commit=False,
            )
            self._execution_repo.update_task_state(task.id, "cancelled", commit=False)
            self._recompute_readiness(execution_id, commit=False)
            self._maybe_complete_execution(execution_id, commit=False)
            self._snapshot(execution_id, "after_cancel", commit=False)
            self._audit_repo.insert(
                AuditEvent(
                    id=AuditEventId(generate_audit_event_id()),
                    project_id=execution.project_id,
                    actor_id=actor_id,
                    source=source,
                    operation="task.cancel",
                    target_type="task",
                    target_id=task.id.value,
                    result="success",
                    redacted_summary=f"Task {task.id.value} cancelled",
                    correlation_id=execution_id.value,
                    created_at=now_utc_iso(),
                ),
                commit=False,
            )
        if self._metrics is not None:
            self._metrics.increment("task_transitions_total", result="cancelled", source=source)
        return self._execution_repo.get_task(task_id, project_id=project_id)

    # ------------------------------------------------------------------
    # Restart recovery
    # ------------------------------------------------------------------

    def reconcile_expired_leases(
        self,
        *,
        project_id: ProjectId,
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> int:
        """Reconcile running tasks whose attempt lease has expired.

        Live-run fix (2026-08-31): recovery ran ONLY at boot. A task
        whose owner died mid-run kept its lease alive long enough to be
        "authoritative" at boot, then the lease expired with nobody
        watching — the task sat in ``running`` forever, its agent-type
        instance slot stayed leased, and every sibling of the graph was
        deferred (capacity) or blocked (dependencies) indefinitely.

        Per ``zero-recovery-consistency``: an expired lease proves that
        current ownership is ABSENT. Each scheduler tick therefore
        reconciles executions that hold a running task with an expired
        (or missing) lease by applying the same recovery contract as the
        boot path: the attempt is marked ``unknown``, the task returns
        to ``ready``, stale instance leases are released, and its
        worktree is abandoned at the next attempt. A live worker keeps
        its lease fresh via the heartbeat, so this can never steal
        genuinely running work.
        """
        from zero.domain.execution import EXECUTION_TRANSITIONS

        terminal_states = {
            state for state, nxt in EXECUTION_TRANSITIONS.items() if not nxt
        }
        reconciled = 0
        executions = self.list_project_executions(
            project_id=project_id,
            actor_id=actor_id,
            source=source,
        )
        for execution in executions:
            if execution.state in terminal_states:
                continue
            tasks = self._execution_repo.list_tasks_for_execution(
                execution.id, project_id=project_id
            )
            needs_recovery = False
            for task in tasks:
                if task.state != "running":
                    continue
                attempts = self._execution_repo.list_attempts_for_task(
                    task.id, project_id=project_id
                )
                running_attempts = [a for a in attempts if a.state == "running"]
                if not running_attempts:
                    # A running task with no running attempt cannot make
                    # progress; recover it.
                    needs_recovery = True
                    break
                latest = running_attempts[-1]
                if not latest.lease_expires_at:
                    needs_recovery = True
                    break
                try:
                    expires = datetime.fromisoformat(latest.lease_expires_at)
                except ValueError:
                    needs_recovery = True
                    break
                if expires <= datetime.now(UTC):
                    needs_recovery = True
                    break
            if needs_recovery:
                try:
                    self.recover_after_restart(
                        execution_id=execution.id,
                        project_id=project_id,
                        actor_id=actor_id,
                        source=source,
                    )
                    reconciled += 1
                except Exception:  # noqa: BLE001 - per-execution isolation
                    logger.debug(
                        "expired-lease reconciliation failed for execution %s",
                        execution.id.value,
                    )
        return reconciled

    def recover_after_restart(
        self,
        *,
        execution_id: ExecutionId,
        project_id: ProjectId | None = None,
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> Execution:
        """Reconstruct execution state after a process restart.

        Per ``zero-recovery-consistency`` §"Leases distinguish
        ownership from history": an expired lease does not prove
        failure. It proves that current ownership is absent;
        reconciliation inspects process, artifact, and external
        evidence.

        Per ``zero-planner-worker-contract`` §"Durable state is
        stronger than agent memory": after restart, the system should
        derive which tasks are complete, which are ready, which were
        interrupted, which worktrees belong to them, and what
        evidence exists — without asking a model to remember what
        happened.

        Recovery actions:
        - Tasks in ``running`` state with no active lease (or an
          expired lease) are transitioned back to ``ready`` so they
          can be re-claimed. Their last attempt is marked ``unknown``
          (per ``zero-tool-capability-runtime``: ``unknown`` is safer
          than invented failure or success).
        - Tasks in ``pending`` state have their readiness recomputed.
        - The execution state is left as-is unless it was ``running``
          and no tasks are running, in which case it transitions to
          ``paused``.
        """
        project_id = self._require_project_scope(
            project_id=project_id,
            actor_id=actor_id,
            permission="execution.start",
            source=source,
        )
        with self._execution_repo._database.transaction():
            execution = self._execution_repo.get_execution(
                execution_id,
                project_id=project_id,
            )
            tasks = self._execution_repo.list_tasks_for_execution(
                execution_id,
                project_id=project_id,
            )
            now = now_utc_iso()
            for task in tasks:
                if task.state != "running":
                    continue
                attempts = self._execution_repo.list_attempts_for_task(
                    task.id,
                    project_id=project_id,
                )
                running_attempts = [attempt for attempt in attempts if attempt.state == "running"]
                live_attempts = []
                for attempt in running_attempts:
                    if attempt.lease_expires_at:
                        try:
                            expires = datetime.fromisoformat(attempt.lease_expires_at)
                            if expires > datetime.now(UTC):
                                live_attempts.append(attempt)
                        except ValueError:
                            # A malformed lease is not evidence of live
                            # ownership; recover conservatively as unknown.
                            pass
                if live_attempts:
                    # A live owner is still authoritative.  Never reclaim
                    # or pause its task merely because this process restarted.
                    continue
                if running_attempts:
                    latest = running_attempts[-1]
                    self._execution_repo.update_attempt_state(latest.id, "unknown", commit=False)
                self._execution_repo.update_task_state(task.id, "ready", commit=False)
                # Release agent-type instance leases the dead worker held
                # (real-run fix): otherwise the leaked "running" instances
                # keep consuming the type's max_concurrent_instances
                # budget and every later claim fails with
                # ConcurrencyLimitExceededError. Best-effort: recovery of
                # the task graph must not depend on the optional repo.
                if self._agent_type_repo is not None:
                    try:
                        self._agent_type_repo.finish_running_instances_for_task(
                            task.id, "cancelled", commit=False
                        )
                    except Exception:  # noqa: BLE001 - bookkeeping is advisory
                        logger.debug(
                            "instance lease release failed for task %s", task.id.value
                        )
            # Recompute readiness for pending tasks and reconcile the
            # execution state from the durable task graph. Ready work is
            # schedulable and therefore resumes a paused execution; only a
            # graph with neither running nor ready work is paused.
            self._recompute_readiness(execution_id, commit=False)
            self._maybe_complete_execution(execution_id, commit=False)
            execution = self._execution_repo.get_execution(
                execution_id,
                project_id=project_id,
            )
            if execution.state not in {"completed", "failed", "cancelled"}:
                tasks = self._execution_repo.list_tasks_for_execution(
                    execution_id,
                    project_id=project_id,
                )
                any_running = any(t.state == "running" for t in tasks)
                any_ready = any(t.state == "ready" for t in tasks)
                if any_ready and execution.state in {"paused", "pending"}:
                    self._execution_repo.update_execution_state(
                        execution_id,
                        "running",
                        blocker_reason="",
                        commit=False,
                    )
                elif not any_running and not any_ready and execution.state == "running":
                    self._execution_repo.update_execution_state(
                        execution_id,
                        "paused",
                        blocker_reason="recovered after restart; no running or ready tasks",
                        commit=False,
                    )

            # Snapshot.
            self._snapshot(execution_id, "restart_recovery", commit=False)
            # Audit.
            self._audit_repo.insert(
                AuditEvent(
                    id=AuditEventId(generate_audit_event_id()),
                    project_id=execution.project_id,
                    actor_id=actor_id,
                    source=source,
                    operation="execution.recover",
                    target_type="execution",
                    target_id=execution.id.value,
                    result="success",
                    redacted_summary=(f"Recovered execution {execution.id.value} after restart"),
                    correlation_id=execution_id.value,
                    created_at=now,
                ),
                commit=False,
            )
        return self._execution_repo.get_execution(execution_id, project_id=project_id)

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    def _snapshot(
        self,
        execution_id: ExecutionId,
        reason: str,
        *,
        commit: bool = True,
    ) -> ExecutionSnapshot:
        """Capture a durable snapshot of the execution graph state.

        Per ``zero-recovery-consistency`` §"A checkpoint records
        facts, not confidence": the snapshot records task IDs, states,
        dependencies, attempt identities, leases, and unresolved
        blockers — not narrative summaries.
        """
        execution = self._execution_repo.get_execution(execution_id)
        tasks = self._execution_repo.list_tasks_for_execution(execution_id)
        dependencies = self._execution_repo.list_all_dependencies_for_execution(execution_id)
        # Build the graph state as JSON.
        graph = {
            "execution_id": execution_id.value,
            "execution_state": execution.state,
            "blocker_reason": execution.blocker_reason,
            "tasks": [
                {
                    "id": t.id.value,
                    "state": t.state,
                    "objective": t.objective,
                    "blocker_reason": t.blocker_reason,
                    "agent_type_id": t.agent_type_id,
                    "terminal_state_set_at": t.terminal_state_set_at,
                }
                for t in tasks
            ],
            "dependencies": [
                {
                    "task_id": d.task_id.value,
                    "depends_on_task_id": d.depends_on_task_id.value,
                }
                for d in dependencies
            ],
        }

        # Compute the next snapshot version inside the same write
        # transaction as the insert so concurrent transitions cannot
        # collide on the version number.
        def _capture() -> ExecutionSnapshot:
            existing = self._execution_repo.get_latest_snapshot(execution_id)
            version = (existing.snapshot_version + 1) if existing else 1
            snapshot = ExecutionSnapshot(
                id=ExecutionSnapshotId(generate_execution_snapshot_id()),
                execution_id=execution_id,
                project_id=execution.project_id,
                snapshot_version=version,
                graph_state=json.dumps(graph, sort_keys=True),
                snapshot_reason=reason,
                created_at=now_utc_iso(),
            )
            self._execution_repo.insert_snapshot(snapshot, commit=commit)
            return snapshot

        if commit:
            with self._execution_repo._database.transaction():
                return _capture()
        return _capture()

    def get_latest_snapshot(
        self,
        execution_id: ExecutionId,
        *,
        project_id: ProjectId | None = None,
        actor_id: UserId | None = None,
        source: AuditSource = "system",
    ) -> ExecutionSnapshot | None:
        project_id = self._require_project_scope(
            project_id=project_id,
            actor_id=actor_id,
            permission="execution.view_diffs",
            source=source,
        )
        self._execution_repo.get_execution(execution_id, project_id=project_id)
        return self._execution_repo.get_latest_snapshot(
            execution_id,
            project_id=project_id,
        )

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def get_execution(
        self,
        execution_id: ExecutionId,
        *,
        project_id: ProjectId | None = None,
        actor_id: UserId | None = None,
        source: AuditSource = "system",
    ) -> Execution:
        project_id = self._require_project_scope(
            project_id=project_id,
            actor_id=actor_id,
            permission="execution.view_diffs",
            source=source,
        )
        return self._execution_repo.get_execution(execution_id, project_id=project_id)

    def list_tasks(
        self,
        execution_id: ExecutionId,
        *,
        project_id: ProjectId | None = None,
        actor_id: UserId | None = None,
        source: AuditSource = "system",
    ) -> list[Task]:
        project_id = self._require_project_scope(
            project_id=project_id,
            actor_id=actor_id,
            permission="execution.view_diffs",
            source=source,
        )
        self._execution_repo.get_execution(execution_id, project_id=project_id)
        return self._execution_repo.list_tasks_for_execution(
            execution_id,
            project_id=project_id,
        )

    def list_dependencies(
        self,
        execution_id: ExecutionId,
        *,
        project_id: ProjectId | None = None,
        actor_id: UserId | None = None,
        source: AuditSource = "system",
    ) -> list[TaskDependency]:
        project_id = self._require_project_scope(
            project_id=project_id,
            actor_id=actor_id,
            permission="execution.view_diffs",
            source=source,
        )
        self._execution_repo.get_execution(execution_id, project_id=project_id)
        return self._execution_repo.list_all_dependencies_for_execution(
            execution_id,
            project_id=project_id,
        )

    def list_attempts(
        self,
        task_id: TaskId,
        *,
        project_id: ProjectId | None = None,
        actor_id: UserId | None = None,
        source: AuditSource = "system",
    ) -> list[TaskAttempt]:
        project_id = self._require_project_scope(
            project_id=project_id,
            actor_id=actor_id,
            permission="execution.view_diffs",
            source=source,
        )
        self._execution_repo.get_task(task_id, project_id=project_id)
        return self._execution_repo.list_attempts_for_task(
            task_id,
            project_id=project_id,
        )

    def list_project_executions(
        self,
        *,
        project_id: ProjectId | None = None,
        actor_id: UserId | None = None,
        source: AuditSource = "system",
    ) -> list[Execution]:
        """Authorized listing of a project's executions (scheduler entry)."""
        project_id = self._require_project_scope(
            project_id=project_id,
            actor_id=actor_id,
            permission="execution.view_diffs",
            source=source,
        )
        return self._execution_repo.list_executions_for_project(project_id)
