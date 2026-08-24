"""Durable database-backed scheduler for approved Zero work.

This first scheduler intentionally uses SQLite state as its queue. It is a
single-owner tick primitive that can be hosted by a supervised process; it
does not pretend that an HTTP request is an autonomous scheduler.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from threading import Event

from zero.app.agent_runtime import AgentRuntime, RuntimeTaskResult
from zero.app.authorization_service import AuthorizationService
from zero.app.integration_service import IntegrationService
from zero.app.plan_service import PlanService
from zero.app.result_delivery_service import ResultDeliveryService
from zero.app.worker_service import TaskSpec, WorkerService
from zero.domain.audit import AuditSource, redact_sensitive_text
from zero.domain.execution import TaskId
from zero.domain.identity import ProjectId, UserId
from zero.domain.worktrees import RepositoryId

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SchedulerTickResult:
    handoffs_claimed: int
    executions_seen: int
    tasks_run: int
    task_results: tuple[RuntimeTaskResult, ...]
    integration_review_ids: tuple[str, ...]
    merge_proposal_ids: tuple[str, ...]
    errors: tuple[str, ...]
    result_delivery_ids: tuple[str, ...] = ()


class SchedulerService:
    """Claim approved handoffs and drain ready tasks with durable fencing."""

    def __init__(
        self,
        *,
        plans: PlanService,
        worker: WorkerService,
        runtime: AgentRuntime,
        authorization: AuthorizationService,
        integration: IntegrationService | None = None,
        result_delivery: ResultDeliveryService | None = None,
        agent_type_repo=None,
        task_max_attempts: int = 0,
    ) -> None:
        self._plans = plans
        self._worker = worker
        self._runtime = runtime
        self._authorization = authorization
        self._integration = integration
        self._result_delivery = result_delivery
        self._agent_type_repo = agent_type_repo
        # Total attempts allowed per task (first run + retries).
        # 0 disables automatic requeueing of failed tasks.
        self._task_max_attempts = max(0, int(task_max_attempts))

    def _default_agent_type_id(self, project_id: ProjectId) -> str | None:
        """Select the project's first active agent type for new tasks.

        Per the release audit (Phase 1): "Assign every execution task an
        agent type." Scheduler-created tasks previously defaulted to
        ``None``; when a project defines active types, the oldest one is
        the deterministic default. Tasks remain assignable to other
        types by explicit TaskSpec.
        """
        if self._agent_type_repo is None:
            return None
        try:
            for agent_type in self._agent_type_repo.list_agent_types_for_project(project_id):
                if agent_type.state == "active":
                    return agent_type.id.value
        except Exception:
            logger.warning("could not resolve default agent type", exc_info=True)
        return None

    @staticmethod
    def _format_execution_result(execution, task_results: list[RuntimeTaskResult]) -> str:
        lines = [f"Execution {execution.id.value} finished with state: {execution.state}."]
        for result in task_results:
            if result.task.execution_id != execution.id:
                continue
            content = redact_sensitive_text(result.response.content or "")[:4000]
            lines.append(f"Task {result.task.id.value}: {content}")
        return "\n".join(lines)[:32_000]

    def run_once(
        self,
        *,
        project_id: ProjectId,
        actor_id: UserId,
        lease_owner: str,
        provider: str,
        model_name: str,
        repository_id: RepositoryId | None = None,
        combined_test_command: str | None = None,
        combined_test_args: tuple[str, ...] = (),
        combined_test_timeout_seconds: int = 300,
        max_handoffs: int = 8,
        max_tasks: int = 16,
        source: AuditSource = "system",
    ) -> SchedulerTickResult:
        """Perform one bounded, replay-safe scheduler tick."""
        if max_handoffs < 1 or max_handoffs > 64 or max_tasks < 1 or max_tasks > 128:
            raise ValueError("scheduler bounds are outside the allowed range")
        self._authorization.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="execution.start",
            source=source,
        )
        errors: list[str] = []
        results: list[RuntimeTaskResult] = []
        default_agent_type_id = self._default_agent_type_id(project_id)
        handoffs = self._plans.list_unclaimed_handoffs(
            project_id,
            limit=max_handoffs,
            actor_id=actor_id,
            source=source,
        )
        handoffs_claimed = 0
        for handoff in handoffs:
            try:
                revision = self._plans.get_revision(
                    handoff.revision_id,
                    project_id=project_id,
                    actor_id=actor_id,
                    source=source,
                )
                evidence = (
                    ("diff", "test_report", "exit_status")
                    if repository_id is not None
                    else ("provider_response",)
                )
                was_unclaimed = getattr(handoff, "execution_id", None) is None
                execution = self._worker.create_execution_from_handoff(
                    handoff_id=handoff.id,
                    actor_id=actor_id,
                    project_id=project_id,
                    task_specs=[
                        TaskSpec(
                            key="implementation",
                            objective=revision.content.objective,
                            permitted_scope=revision.content.scope,
                            expected_evidence=evidence,
                            agent_type_id=default_agent_type_id,
                        )
                    ],
                    source=source,
                )
                # Count a claim only when this tick actually created the
                # graph; the idempotent path returns an existing
                # execution for a handoff another worker already claimed.
                if was_unclaimed:
                    handoffs_claimed += 1
            except (OSError, RuntimeError, ValueError) as exc:
                errors.append(
                    f"handoff {handoff.id.value}: {type(exc).__name__}: "
                    f"{redact_sensitive_text(str(exc))[:200]}"
                )

        executions = self._worker.list_project_executions(
            project_id=project_id,
            actor_id=actor_id,
            source=source,
        )
        runnable = [
            execution
            for execution in executions
            if execution.state in {"pending", "running", "paused"}
        ]
        for execution in runnable:
            if len(results) >= max_tasks:
                break
            try:
                batch = self._runtime.run_ready_tasks(
                    execution_id=execution.id,
                    project_id=project_id,
                    actor_id=actor_id,
                    lease_owner=lease_owner,
                    provider=provider,
                    model_name=model_name,
                    repository_id=repository_id,
                    max_tasks=max_tasks - len(results),
                    source=source,
                )
                results.extend(batch)
            except (OSError, RuntimeError, ValueError) as exc:
                errors.append(f"execution {execution.id.value}: {type(exc).__name__}")
        # Bounded auto-retry: requeue failed tasks while the task has
        # not yet consumed its configured attempt budget. Disabled by
        # default (ZERO_TASK_MAX_ATTEMPTS=0).
        if self._task_max_attempts > 0:
            for execution in runnable:
                try:
                    tasks = self._worker.list_tasks(
                        execution.id,
                        project_id=project_id,
                        actor_id=actor_id,
                        source=source,
                    )
                    for task in tasks:
                        if task.state != "failed":
                            continue
                        attempts = self._worker.list_attempts(
                            task.id,
                            project_id=project_id,
                            actor_id=actor_id,
                            source=source,
                        )
                        if len(attempts) < self._task_max_attempts:
                            self._worker.requeue_failed_task(
                                execution_id=execution.id,
                                project_id=project_id,
                                task_id=task.id,
                                actor_id=actor_id,
                                source=source,
                            )
                except (OSError, RuntimeError, ValueError) as exc:
                    errors.append(f"retry {execution.id.value}: {type(exc).__name__}")
        integration_review_ids: list[str] = []
        merge_proposal_ids: list[str] = []
        result_delivery_ids: list[str] = []
        if self._integration is not None and repository_id is not None:
            for execution in runnable:
                try:
                    current = self._worker.get_execution(
                        execution.id,
                        project_id=project_id,
                        actor_id=actor_id,
                        source=source,
                    )
                    if current.state != "completed":
                        continue
                    tasks = self._worker.list_tasks(
                        current.id,
                        project_id=project_id,
                        actor_id=actor_id,
                        source=source,
                    )
                    source_tasks = tuple(
                        TaskId(task.id.value) for task in tasks if task.state == "completed"
                    )
                    if not source_tasks:
                        continue
                    reviews = self._integration.list_reviews(current.id, project_id=project_id)
                    if reviews:
                        review = reviews[-1]
                    else:
                        review = self._integration.create_review(
                            project_id=project_id,
                            execution_id=current.id,
                            source_task_ids=source_tasks,
                            actor_id=actor_id,
                            source=source,
                        )
                        if combined_test_command is not None:
                            review = self._integration.run_combined_tests(
                                project_id=project_id,
                                review_id=review.id,
                                command=combined_test_command,
                                args=combined_test_args,
                                timeout_seconds=combined_test_timeout_seconds,
                                actor_id=actor_id,
                                source=source,
                            )
                    integration_review_ids.append(review.id.value)
                    if review.state == "approved":
                        proposals = self._integration.list_proposals(
                            current.id, project_id=project_id
                        )
                        if not any(
                            proposal.integration_review_id == review.id for proposal in proposals
                        ):
                            proposal = self._integration.create_merge_proposal(
                                project_id=project_id,
                                review_id=review.id,
                                execution_id=current.id,
                                source_tasks=source_tasks,
                                actor_id=actor_id,
                                source=source,
                            )
                            merge_proposal_ids.append(proposal.id.value)
                except (OSError, RuntimeError, ValueError) as exc:
                    errors.append(f"integration {execution.id.value}: {type(exc).__name__}")
        if self._result_delivery is not None:
            for execution in executions:
                try:
                    current = self._worker.get_execution(
                        execution.id,
                        project_id=project_id,
                        actor_id=actor_id,
                        source=source,
                    )
                    if current.state not in {"completed", "failed", "cancelled"}:
                        continue
                    content = self._format_execution_result(current, results)
                    for binding in self._result_delivery.list_enabled_bindings(project_id):
                        delivery = self._result_delivery.enqueue_execution_result(
                            project_id=project_id,
                            execution_id=current.id,
                            binding_id=binding.id,
                            actor_id=actor_id,
                            content=content,
                            source=source,
                        )
                        result_delivery_ids.append(delivery.id.value)
                except (OSError, RuntimeError, ValueError) as exc:
                    errors.append(f"delivery {execution.id.value}: {type(exc).__name__}")
            if self._result_delivery.is_outbound_configured:
                try:
                    delivered = self._result_delivery.drain_once(project_id=project_id)
                    if delivered is not None:
                        result_delivery_ids.append(delivered.id.value)
                except (OSError, RuntimeError, ValueError) as exc:
                    errors.append(f"delivery drain: {type(exc).__name__}")
        return SchedulerTickResult(
            handoffs_claimed=handoffs_claimed,
            executions_seen=len(executions),
            tasks_run=len(results),
            task_results=tuple(results),
            integration_review_ids=tuple(integration_review_ids),
            merge_proposal_ids=tuple(merge_proposal_ids),
            errors=tuple(errors),
            result_delivery_ids=tuple(result_delivery_ids),
        )

    def run_forever(
        self,
        *,
        project_id: ProjectId,
        actor_id: UserId,
        lease_owner: str,
        provider: str,
        model_name: str,
        repository_id: RepositoryId | None = None,
        combined_test_command: str | None = None,
        combined_test_args: tuple[str, ...] = (),
        combined_test_timeout_seconds: int = 300,
        interval_seconds: float = 1.0,
        stop_event: Event | None = None,
        on_tick=None,
    ) -> None:
        """Host durable ticks in a supervised process until cancellation."""
        if interval_seconds < 0.1 or interval_seconds > 300:
            raise ValueError("scheduler interval must be between 0.1 and 300 seconds")
        stop_event = stop_event or Event()
        while not stop_event.is_set():
            try:
                result = self.run_once(
                    project_id=project_id,
                    actor_id=actor_id,
                    lease_owner=lease_owner,
                    provider=provider,
                    model_name=model_name,
                    repository_id=repository_id,
                    combined_test_command=combined_test_command,
                    combined_test_args=combined_test_args,
                    combined_test_timeout_seconds=combined_test_timeout_seconds,
                )
                if on_tick is not None:
                    on_tick(result)
            except (OSError, RuntimeError, ValueError) as exc:
                # The next tick retries from durable state; never mark work
                # successful merely because the loop survived an exception.
                logger.warning("scheduler tick failed: %s", type(exc).__name__)
            stop_event.wait(interval_seconds)


__all__ = ["SchedulerService", "SchedulerTickResult"]
