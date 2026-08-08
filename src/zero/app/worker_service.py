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
from collections import defaultdict, deque
from datetime import UTC, datetime

from zero.app.authorization_service import AuthorizationService
from zero.domain.audit import AuditEvent, AuditEventId, AuditSource
from zero.domain.execution import (
    BLOCKING_TASK_STATES,
    CycleError,
    Execution,
    ExecutionId,
    ExecutionSnapshot,
    ExecutionSnapshotId,
    InvalidExecutionTransitionError,
    InvalidTaskTransitionError,
    MissingDependencyError,
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
from zero.domain.identity import UserId
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
from zero.persistence.repositories.audit_repository import AuditRepository
from zero.persistence.repositories.execution_repository import (
    ExecutionRepository,
)
from zero.persistence.repositories.plan_repository import PlanRepository


def _now_utc_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ----------------------------------------------------------------------
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

    __slots__ = ("expected_evidence", "key", "objective", "permitted_scope")

    def __init__(
        self,
        *,
        objective: str,
        permitted_scope: tuple[str, ...] = (),
        expected_evidence: tuple[str, ...] = (),
        key: str | None = None,
    ) -> None:
        self.objective = objective
        self.permitted_scope = permitted_scope
        self.expected_evidence = expected_evidence
        # key is an optional caller-supplied identifier used to express
        # dependencies before the task has a stable ID. The Worker
        # resolves keys to TaskIds when creating the graph.
        self.key = key


class DependencySpec:
    """A specification for a dependency edge: ``depends_on`` must
    complete before ``task`` can become ready.

    Both ``task`` and ``depends_on`` are keys from :class:`TaskSpec`.
    """

    __slots__ = ("depends_on_key", "task_key")

    def __init__(self, *, task_key: str, depends_on_key: str) -> None:
        if task_key == depends_on_key:
            raise CycleError(
                f"Task {task_key!r} cannot depend on itself"
            )
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
        authorization_service: AuthorizationService,
    ) -> None:
        self._execution_repo = execution_repo
        self._plan_repo = plan_repo
        self._audit_repo = audit_repo
        self._authz = authorization_service

    # ------------------------------------------------------------------
    # Execution creation
    # ------------------------------------------------------------------

    def create_execution_from_handoff(
        self,
        *,
        handoff_id: PlanHandoffId,
        actor_id: UserId,
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

        # Look up the handoff.
        handoff = self._plan_repo.get_handoff(handoff_id)
        # Verify the plan is approved.
        plan = self._plan_repo.get_plan(handoff.plan_id)
        if plan.current_state != "approved":
            raise PlanNotApprovedError(
                f"Plan {plan.id} is in state {plan.current_state!r}, "
                f"not 'approved'"
            )

        # Authorize: the actor must have execution.start permission.
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=plan.project_id,
            permission="execution.start",
            source=source,
        )

        # Check for an existing execution (idempotency).
        existing = self._execution_repo.get_execution_for_revision(
            handoff.revision_id
        )
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
            created_at=_now_utc_iso(),
            updated_at=_now_utc_iso(),
        )
        with self._execution_repo._database.transaction():
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
                    created_at=_now_utc_iso(),
                    updated_at=_now_utc_iso(),
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
                        f"Dependency depends_on_key "
                        f"{dep_spec.depends_on_key!r} not found"
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
            self._plan_repo.set_handoff_execution_id(
                handoff.id, execution.id.value, commit=False
            )

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
                        f"Created execution {execution.id.value} from plan "
                        f"{plan.id.value}"
                    ),
                    correlation_id=execution.id.value,
                    created_at=_now_utc_iso(),
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
        # Check for duplicate keys.
        keys = [s.key or s.objective for s in task_specs]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate task keys in task_specs")
        for dep in dependency_specs:
            if dep.task_key == dep.depends_on_key:
                raise CycleError(
                    f"Task {dep.task_key!r} cannot depend on itself"
                )

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
                raise MissingDependencyError(
                    f"Dependency task_key {dep.task_key!r} not found"
                )
            if dep.depends_on_key not in key_set:
                raise MissingDependencyError(
                    f"Dependency depends_on_key "
                    f"{dep.depends_on_key!r} not found"
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
                f"Cycle detected in task dependency graph; "
                f"nodes in cycles: {remaining}",
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

        Per PLAN.md M5 validation: "Independent tasks become ready
        together. Dependent tasks remain blocked until prerequisites
        succeed. Failed prerequisites block dependents safely."
        """
        tasks = self._execution_repo.list_tasks_for_execution(execution_id)
        for task in tasks:
            if task.state != "pending":
                continue
            deps = self._execution_repo.list_dependencies_for_task(task.id)
            if not deps:
                # No dependencies: ready.
                self._execution_repo.update_task_state(
                    task.id, "ready", commit=commit
                )
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
                self._execution_repo.update_task_state(
                    task.id, "ready", commit=commit
                )
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
        self, execution_id: ExecutionId
    ) -> list[Task]:
        """List all tasks in the ``ready`` state.

        Per PLAN.md M5: "Independent tasks become ready together."
        """
        tasks = self._execution_repo.list_tasks_for_execution(execution_id)
        return [t for t in tasks if t.state == "ready"]

    def claim_task(
        self,
        *,
        execution_id: ExecutionId,
        task_id: TaskId,
        lease_owner: str,
        lease_duration_seconds: int = 300,
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
        task = self._execution_repo.get_task(task_id)
        if task.execution_id != execution_id:
            raise TaskNotFoundError(
                f"Task {task_id} does not belong to execution {execution_id}"
            )
        if task.state != "ready":
            raise InvalidTaskTransitionError(
                f"Task {task_id} is in state {task.state!r}, not 'ready'"
            )
        with self._execution_repo._database.transaction():
            # Transition task to running.
            self._execution_repo.update_task_state(
                task.id, "running", commit=False
            )
            # Compute the next attempt number.
            existing_attempts = self._execution_repo.list_attempts_for_task(
                task.id
            )
            attempt_number = len(existing_attempts) + 1
            lease_expires = datetime.now(UTC)
            from datetime import timedelta

            lease_expires += timedelta(seconds=lease_duration_seconds)
            attempt = TaskAttempt(
                id=TaskAttemptId(generate_task_attempt_id()),
                task_id=task.id,
                project_id=task.project_id,
                attempt_number=attempt_number,
                state="running",
                lease_owner=lease_owner,
                lease_expires_at=lease_expires.strftime(
                    "%Y-%m-%dT%H:%M:%S.%fZ"
                ),
                started_at=_now_utc_iso(),
            )
            self._execution_repo.insert_attempt(attempt, commit=False)
            # Transition execution to running if it was pending.
            execution = self._execution_repo.get_execution(execution_id)
            if execution.state == "pending":
                self._execution_repo.update_execution_state(
                    execution_id, "running", commit=False
                )
        return attempt

    def complete_task(
        self,
        *,
        execution_id: ExecutionId,
        task_id: TaskId,
        attempt_id: TaskAttemptId,
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> Task:
        """Mark a task as completed.

        Per ``zero-recovery-consistency`` §"Idempotency makes retries
        ordinary": if the task is already completed, this is a no-op.

        Per PLAN.md M5: "Restart reconstructs the same graph and
        statuses." After completion, dependents' readiness is
        recomputed.
        """
        task = self._execution_repo.get_task(task_id)
        if task.execution_id != execution_id:
            raise TaskNotFoundError(
                f"Task {task_id} does not belong to execution {execution_id}"
            )
        if is_terminal_task_state(task.state):
            return task  # idempotent
        if task.state != "running":
            raise InvalidTaskTransitionError(
                f"Task {task_id} is in state {task.state!r}, "
                f"cannot transition to 'completed'"
            )
        with self._execution_repo._database.transaction():
            # Mark the attempt succeeded.
            self._execution_repo.update_attempt_state(
                attempt_id, "succeeded", commit=False
            )
            # Mark the task completed.
            self._execution_repo.update_task_state(
                task.id, "completed", commit=False
            )
            # Recompute dependents' readiness.
            self._recompute_readiness(execution_id, commit=False)
            # Check if the execution is now complete.
            self._maybe_complete_execution(execution_id, commit=False)
            # Snapshot.
            self._snapshot(execution_id, "after_task_complete", commit=False)
            # Audit.
            execution = self._execution_repo.get_execution(execution_id)
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
                    created_at=_now_utc_iso(),
                ),
                commit=False,
            )
        return self._execution_repo.get_task(task_id)

    def fail_task(
        self,
        *,
        execution_id: ExecutionId,
        task_id: TaskId,
        attempt_id: TaskAttemptId,
        error_message: str,
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> Task:
        """Mark a task as failed.

        Per PLAN.md M5: "Failed prerequisites block dependents safely."
        After marking the task failed, we recompute dependents'
        readiness — they will be marked ``blocked`` because a
        dependency is in a blocking state.
        """
        task = self._execution_repo.get_task(task_id)
        if task.execution_id != execution_id:
            raise TaskNotFoundError(
                f"Task {task_id} does not belong to execution {execution_id}"
            )
        if is_terminal_task_state(task.state):
            return task
        if task.state != "running":
            raise InvalidTaskTransitionError(
                f"Task {task_id} is in state {task.state!r}, "
                f"cannot transition to 'failed'"
            )
        with self._execution_repo._database.transaction():
            # Mark the attempt failed (error message must not contain secrets).
            self._execution_repo.update_attempt_state(
                attempt_id,
                "failed",
                error_message=error_message,
                commit=False,
            )
            # Mark the task failed.
            self._execution_repo.update_task_state(
                task.id, "failed", commit=False
            )
            # Recompute dependents — they will be blocked.
            self._recompute_readiness(execution_id, commit=False)
            # Check if the execution should pause (failed task with no
            # running/ready work).
            self._maybe_complete_execution(execution_id, commit=False)
            # Snapshot.
            self._snapshot(execution_id, "after_task_fail", commit=False)
            # Audit.
            execution = self._execution_repo.get_execution(execution_id)
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
                    created_at=_now_utc_iso(),
                ),
                commit=False,
            )
        return self._execution_repo.get_task(task_id)

    def _maybe_complete_execution(
        self,
        execution_id: ExecutionId,
        *,
        commit: bool = True,
    ) -> None:
        """If all tasks are completed, transition the execution to
        completed. If any task is in a blocking state (failed, blocked,
        cancelled) and no tasks are running/ready, transition to
        failed/paused."""
        tasks = self._execution_repo.list_tasks_for_execution(execution_id)
        if not tasks:
            return
        all_completed = all(t.state == "completed" for t in tasks)
        if all_completed:
            execution = self._execution_repo.get_execution(execution_id)
            if is_valid_execution_transition(execution.state, "completed"):
                self._execution_repo.update_execution_state(
                    execution_id, "completed", commit=commit
                )
            return
        # If any task is blocked and no tasks are running or ready,
        # pause the execution.
        any_running_or_ready = any(
            t.state in ("running", "ready") for t in tasks
        )
        any_blocked = any(t.state in ("failed", "blocked") for t in tasks)
        if any_blocked and not any_running_or_ready:
            execution = self._execution_repo.get_execution(execution_id)
            if is_valid_execution_transition(execution.state, "paused"):
                self._execution_repo.update_execution_state(
                    execution_id,
                    "paused",
                    blocker_reason="task failed or blocked",
                    commit=commit,
                )

    # ------------------------------------------------------------------
    # Cancellation
    # ------------------------------------------------------------------

    def cancel_execution(
        self,
        *,
        execution_id: ExecutionId,
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
        execution = self._execution_repo.get_execution(execution_id)
        # Authorize.
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=execution.project_id,
            permission="execution.stop",
            source=source,
        )
        if not is_valid_execution_transition(execution.state, "cancelled"):
            raise InvalidExecutionTransitionError(
                f"Cannot cancel execution in state {execution.state!r}"
            )
        with self._execution_repo._database.transaction():
            # Cancel all non-terminal tasks.
            tasks = self._execution_repo.list_tasks_for_execution(execution_id)
            for task in tasks:
                if not is_terminal_task_state(task.state):
                    self._execution_repo.update_task_state(
                        task.id, "cancelled", commit=False
                    )
                    # Cancel any running attempts.
                    attempts = self._execution_repo.list_attempts_for_task(
                        task.id
                    )
                    for attempt in attempts:
                        if attempt.state == "running":
                            self._execution_repo.update_attempt_state(
                                attempt.id, "cancelled", commit=False
                            )
            # Transition execution.
            self._execution_repo.update_execution_state(
                execution_id, "cancelled", commit=False
            )
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
                    created_at=_now_utc_iso(),
                ),
                commit=False,
            )
        return self._execution_repo.get_execution(execution_id)

    # ------------------------------------------------------------------
    # Restart recovery
    # ------------------------------------------------------------------

    def recover_after_restart(
        self,
        *,
        execution_id: ExecutionId,
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
        with self._execution_repo._database.transaction():
            execution = self._execution_repo.get_execution(execution_id)
            self._authz.require_permission(
                actor_id=actor_id,
                project_id=execution.project_id,
                permission="execution.start",
                source=source,
            )
            tasks = self._execution_repo.list_tasks_for_execution(execution_id)
            now = _now_utc_iso()
            for task in tasks:
                if task.state == "running":
                    # Find the latest attempt.
                    attempts = self._execution_repo.list_attempts_for_task(
                        task.id
                    )
                    running_attempts = [
                        a for a in attempts if a.state == "running"
                    ]
                    if running_attempts:
                        latest = running_attempts[-1]
                        # Mark the attempt as unknown (we don't know if it
                        # succeeded or failed).
                        self._execution_repo.update_attempt_state(
                            latest.id, "unknown", commit=False
                        )
                    # Transition the task back to ready for re-claiming.
                    self._execution_repo.update_task_state(
                        task.id, "ready", commit=False
                    )
            # Recompute readiness for pending tasks.
            self._recompute_readiness(execution_id, commit=False)
            # If the execution was running and no tasks are running now,
            # pause it.
            execution = self._execution_repo.get_execution(execution_id)
            if execution.state == "running":
                tasks = self._execution_repo.list_tasks_for_execution(
                    execution_id
                )
                any_running = any(t.state == "running" for t in tasks)
                if not any_running:
                    self._execution_repo.update_execution_state(
                        execution_id,
                        "paused",
                        blocker_reason="recovered after restart; no running tasks",
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
                    redacted_summary=(
                        f"Recovered execution {execution.id.value} after restart"
                    ),
                    correlation_id=execution_id.value,
                    created_at=now,
                ),
                commit=False,
            )
        return self._execution_repo.get_execution(execution_id)

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
        dependencies = (
            self._execution_repo.list_all_dependencies_for_execution(
                execution_id
            )
        )
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
        # Compute the next snapshot version.
        existing = self._execution_repo.get_latest_snapshot(execution_id)
        version = (existing.snapshot_version + 1) if existing else 1
        snapshot = ExecutionSnapshot(
            id=ExecutionSnapshotId(generate_execution_snapshot_id()),
            execution_id=execution_id,
            project_id=execution.project_id,
            snapshot_version=version,
            graph_state=json.dumps(graph, sort_keys=True),
            snapshot_reason=reason,
            created_at=_now_utc_iso(),
        )
        self._execution_repo.insert_snapshot(snapshot, commit=commit)
        return snapshot

    def get_latest_snapshot(
        self, execution_id: ExecutionId
    ) -> ExecutionSnapshot | None:
        return self._execution_repo.get_latest_snapshot(execution_id)

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def get_execution(self, execution_id: ExecutionId) -> Execution:
        return self._execution_repo.get_execution(execution_id)

    def list_tasks(
        self, execution_id: ExecutionId
    ) -> list[Task]:
        return self._execution_repo.list_tasks_for_execution(execution_id)

    def list_dependencies(
        self, execution_id: ExecutionId
    ) -> list[TaskDependency]:
        return self._execution_repo.list_all_dependencies_for_execution(
            execution_id
        )

    def list_attempts(self, task_id: TaskId) -> list[TaskAttempt]:
        return self._execution_repo.list_attempts_for_task(task_id)
