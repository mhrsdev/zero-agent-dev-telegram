"""Durable database-backed scheduler for approved Zero work.

This first scheduler intentionally uses SQLite state as its queue. It is a
single-owner tick primitive that can be hosted by a supervised process; it
does not pretend that an HTTP request is an autonomous scheduler.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Event

from zero.app.agent_runtime import AgentRuntime, RuntimeTaskResult
from zero.app.authorization_service import AuthorizationService
from zero.app.integration_service import IntegrationService
from zero.app.plan_service import PlanService
from zero.app.result_delivery_service import ResultDeliveryService
from zero.app.retry_backoff import compute_retry_delay
from zero.app.worker_service import DependencySpec, TaskSpec, WorkerService
from zero.domain.audit import AuditSource, redact_sensitive_text
from zero.domain.execution import Task, TaskId
from zero.domain.identity import ProjectId, UserId
from zero.domain.worktrees import RepositoryId

logger = logging.getLogger(__name__)


def _env_flag(name: str) -> bool:
    """Read a boolean env flag (GAP 10 opt-ins)."""
    import os

    return (os.environ.get(name, "").strip().lower()) in {"1", "true", "yes", "on"}


def _parse_iso_utc(value: str) -> datetime | None:
    """Parse an ISO-8601 UTC timestamp leniently; None when invalid."""
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


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
        decomposer=None,
        decomposition_enabled: bool | None = None,
        parallel_executions: int = 1,
        repository_resolver=None,
    ) -> None:
        self._plans = plans
        self._worker = worker
        self._runtime = runtime
        self._authorization = authorization
        self._integration = integration
        self._result_delivery = result_delivery
        self._agent_type_repo = agent_type_repo
        # Optional callable (project_id, actor_id) -> RepositoryId | None
        # (real-run fix): when the caller passes no repository_id, the
        # scheduler resolves the project's single registered repository
        # itself. Without this, managed deployments (the background
        # worker host never passes repository_id) ran every coding task
        # as delegate-only: no worktree, no file/shell tools, and
        # evidence degraded to bare "provider_response" — the agent
        # could talk but never touch files.
        self._repository_resolver = repository_resolver
        # Total attempts allowed per task (first run + retries).
        # 0 disables automatic requeueing of failed tasks.
        self._task_max_attempts = max(0, int(task_max_attempts))
        # GAP 10: optional LLM task decomposition. Disabled unless the
        # composition root passes a decomposer AND the flag is on.
        self._decomposer = decomposer
        if decomposition_enabled is None:
            decomposition_enabled = _env_flag("ZERO_DECOMPOSITION_ENABLED")
        self._decomposition_enabled = bool(decomposition_enabled)
        # Round-7 routing alignment seam: the tick's LLM routing (task
        # execution + decomposition) used to come from
        # ``settings.openai_model`` (gpt-4o-mini default) — the routing
        # table never reached the tasks. config_sync now overrides this
        # with ``routing.primary_model`` at boot (live evidence: the
        # operator's gateway stopped serving the gpt-4o-mini default
        # outright — every task/decomposition call edge-403'd — while
        # the aligned planner and chat bridge kept working).
        self._tick_provider: str | None = None
        self._tick_model: str | None = None
        # GAP 8b/G3 (Hermes segment-planning lite): bounded cross-execution
        # parallelism inside one tick. Independent executions (separate
        # plans) dispatch concurrently; within a single execution the
        # dependency chain stays strictly serial so lease fencing and
        # DAG order semantics are untouched. 1 preserves serial ticks.
        self._parallel_executions = max(1, min(8, int(parallel_executions)))

    def _task_specs_for_revision(
        self,
        *,
        project_id: ProjectId,
        actor_id: UserId,
        revision,
        provider: str,
        model_name: str,
        source: AuditSource,
        default_agent_type_id: str | None,
        expected_evidence: tuple[str, ...],
    ) -> tuple[list[TaskSpec], list[DependencySpec]]:
        """Build task specs for one approved revision (GAP 10 seam).

        Default: the historical single ``implementation`` task. When a
        decomposer is wired and enabled, an LLM-produced validated graph
        replaces it; any failure falls back to the single task.
        """
        single = [
            TaskSpec(
                key="implementation",
                objective=revision.content.objective,
                permitted_scope=revision.content.scope,
                expected_evidence=expected_evidence,
                agent_type_id=default_agent_type_id,
            )
        ]
        if not self._decomposition_enabled or self._decomposer is None:
            return single, []
        try:
            graph = self._decomposer.decompose(
                project_id=project_id,
                actor_id=actor_id,
                revision_id=revision.id.value,
                revision_content=revision.content,
                provider=provider,
                model_name=model_name,
                source=source,
            )
        except Exception as exc:
            logger.warning(
                "decomposer raised for revision %s: %s",
                revision.id.value,
                type(exc).__name__,
                exc_info=True,
            )
            return single, []
        if graph is None or not graph.specs:
            return single, []
        specs: list[TaskSpec] = []
        for spec in graph.specs:
            specs.append(
                TaskSpec(
                    key=spec.key,
                    objective=spec.objective,
                    permitted_scope=spec.permitted_scope or revision.content.scope,
                    # Per-task evidence from the decomposer (real-run
                    # fix): read/analysis tasks declare
                    # ["provider_response"], code-changing tasks declare
                    # ["diff","test_report","exit_status"]. Tasks without
                    # an explicit choice fall back to the scheduler
                    # default so behavior is unchanged for old graphs.
                    expected_evidence=spec.expected_evidence or expected_evidence,
                    agent_type_id=default_agent_type_id,
                )
            )
        dependencies = list(graph.dependencies)
        logger.info(
            "plan revision %s decomposed into %d tasks / %d edges",
            revision.id.value,
            len(specs),
            len(dependencies),
        )
        return specs, dependencies

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

    def set_tick_routing(self, *, provider: str, model_name: str) -> None:
        """config_sync alignment seam: pin the tick's LLM routing.

        Every scheduler-driven LLM call (decomposition, task execution)
        must call exactly the model the operator configured in
        ``routing.primary_model`` — one routing truth, Hermes parity.
        """
        self._tick_provider = provider
        self._tick_model = model_name

    def tick_routing_override(self) -> tuple[str | None, str | None]:
        """(provider, model) pinned by config_sync, or (None, None)."""
        return self._tick_provider, self._tick_model

    @staticmethod
    def _retry_delay_elapsed(task: Task, *, now: datetime | None = None) -> bool:
        """GAP 12: True when a failed task's backoff window has passed."""
        if task.next_retry_at is None:
            return True
        scheduled = _parse_iso_utc(task.next_retry_at)
        if scheduled is None:
            # An unparsable stamp must never wedge a task forever.
            return True
        current = now or datetime.now(UTC)
        return current >= scheduled

    @staticmethod
    def _latest_task_error(
        worker: WorkerService,
        task_id: TaskId,
        *,
        project_id: ProjectId,
        actor_id: UserId,
        source: AuditSource,
    ) -> str:
        """Latest failed attempt's error text for one task (advisory)."""
        try:
            attempts = worker.list_attempts(
                task_id, project_id=project_id, actor_id=actor_id, source=source
            )
        except Exception:  # noqa: BLE001 - enrichment is advisory
            return ""
        last = max(attempts, key=lambda a: a.attempt_number, default=None)
        if last is None or not last.error_message:
            return ""
        return redact_sensitive_text(last.error_message)

    def _format_execution_result(
        self,
        execution,
        task_results: list[RuntimeTaskResult],
        *,
        project_id: ProjectId,
        actor_id: UserId,
        source: AuditSource,
    ) -> str:
        """Human-readable execution summary built from durable state.

        Real-run fix: the previous formatter only knew about tasks that
        ran inside the current tick. When the failing task's exception
        propagated out of ``run_ready_tasks`` (the nothing-succeeded
        re-raise), the delivery collapsed to a bare "finished with
        state: failed." line — no goal, no task titles, no error — which
        is opaque to a human reading the chat. The formatter now reads
        the plan goal, every task's objective/state, and failed
        attempts' errors from durable state; tick-local results remain
        optional enrichment for completed-task response bodies. Every
        enrichment step is advisory: durable-state reads must never
        break the delivery, so any failure degrades to the plain line.
        """
        lines = [f"Execution {execution.id.value} finished with state: {execution.state}."]
        if execution.blocker_reason:
            lines.append(f"Blocker: {redact_sensitive_text(execution.blocker_reason)[:300]}")
        try:
            revision = self._plans.get_revision(
                execution.plan_revision_id,
                project_id=project_id,
                actor_id=actor_id,
                source=source,
            )
            goal = redact_sensitive_text(revision.content.objective or "").strip()
            if goal:
                lines.append(f"Goal: {goal[:1000]}")
        except Exception:  # noqa: BLE001 - enrichment is advisory
            logger.debug("execution result: goal enrichment failed", exc_info=True)
        try:
            tasks = self._worker.list_tasks(
                execution.id, project_id=project_id, actor_id=actor_id, source=source
            )
        except Exception:  # noqa: BLE001 - enrichment is advisory
            tasks = []
        if tasks:
            lines.append("")
            lines.append("Tasks:")
            for task in tasks:
                objective = redact_sensitive_text(task.objective or "").strip()
                lines.append(f"- [{task.state}] {objective[:500]}")
                if task.blocker_reason:
                    lines.append(
                        "  blocked: " + redact_sensitive_text(task.blocker_reason)[:300]
                    )
                if task.state == "failed":
                    error_text = self._latest_task_error(
                        self._worker,
                        task.id,
                        project_id=project_id,
                        actor_id=actor_id,
                        source=source,
                    )
                    if error_text:
                        lines.append(f"  error: {error_text[:500]}")
        # Tick-local response bodies stay as optional enrichment: they
        # only exist for tasks that happened to run inside this tick.
        for result in task_results:
            if result.task.execution_id != execution.id:
                continue
            content = redact_sensitive_text(result.response.content or "")[:4000]
            if content:
                lines.append(f"Result of task {result.task.id.value}: {content}")
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
        # Resolve the project's single repository when the caller did not
        # pin one (real-run fix — see __init__ comment): coding tasks then
        # get worktrees + the workspace toolset and real file evidence
        # (diff, test_report, exit_status) instead of delegate-only runs.
        effective_repository_id = repository_id
        if effective_repository_id is None and self._repository_resolver is not None:
            try:
                effective_repository_id = self._repository_resolver(project_id, actor_id)
            except Exception:  # noqa: BLE001 - resolution is advisory
                effective_repository_id = None
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
                    if effective_repository_id is not None
                    else ("provider_response",)
                )
                task_specs, dependency_specs = self._task_specs_for_revision(
                    project_id=project_id,
                    actor_id=actor_id,
                    revision=revision,
                    provider=provider,
                    model_name=model_name,
                    source=source,
                    default_agent_type_id=default_agent_type_id,
                    expected_evidence=evidence,
                )
                was_unclaimed = getattr(handoff, "execution_id", None) is None
                execution = self._worker.create_execution_from_handoff(
                    handoff_id=handoff.id,
                    actor_id=actor_id,
                    project_id=project_id,
                    task_specs=task_specs,
                    dependency_specs=dependency_specs,
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

        def _drain(execution, budget):
            return self._runtime.run_ready_tasks(
                execution_id=execution.id,
                project_id=project_id,
                actor_id=actor_id,
                lease_owner=lease_owner,
                provider=provider,
                model_name=model_name,
                repository_id=effective_repository_id,
                max_tasks=budget,
                source=source,
            )

        use_parallel = self._parallel_executions > 1 and len(runnable) > 1
        if not use_parallel:
            for execution in runnable:
                if len(results) >= max_tasks:
                    break
                try:
                    batch = _drain(execution, max_tasks - len(results))
                    results.extend(batch)
                except (OSError, RuntimeError, ValueError) as exc:
                    errors.append(f"execution {execution.id.value}: {type(exc).__name__}")
        else:
            # Each concurrent drain gets the full task budget; combined
            # results are trimmed to max_tasks preserving runnable order.
            import concurrent.futures

            workers = min(self._parallel_executions, len(runnable))
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(_drain, execution, max_tasks) for execution in runnable]
            collected: list[RuntimeTaskResult] = []
            for execution, future in zip(runnable, futures):
                try:
                    collected.extend(future.result())
                except (OSError, RuntimeError, ValueError) as exc:
                    errors.append(f"execution {execution.id.value}: {type(exc).__name__}")
            results.extend(collected[:max_tasks])
        # Bounded auto-retry: requeue failed tasks while the task has
        # not yet consumed its configured attempt budget. Disabled by
        # default (ZERO_TASK_MAX_ATTEMPTS=0). GAP 12: each requeue is
        # stamped with a computed next_retry_at (exponential backoff,
        # jitter, provider Retry-After honored) and later ticks skip
        # tasks whose delay has not elapsed.
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
                        if not self._retry_delay_elapsed(task):
                            continue
                        attempts = self._worker.list_attempts(
                            task.id,
                            project_id=project_id,
                            actor_id=actor_id,
                            source=source,
                        )
                        if len(attempts) < self._task_max_attempts:
                            error_text = ""
                            last_attempt = max(
                                attempts, key=lambda a: a.attempt_number, default=None
                            )
                            if last_attempt is not None and last_attempt.error_message:
                                error_text = redact_sensitive_text(last_attempt.error_message)
                            self._worker.requeue_failed_task(
                                execution_id=execution.id,
                                project_id=project_id,
                                task_id=task.id,
                                actor_id=actor_id,
                                source=source,
                            )
                            delay_seconds = compute_retry_delay(len(attempts) + 1, error_text)
                            next_retry_at = (
                                datetime.now(UTC).replace(microsecond=0)
                                + timedelta(seconds=delay_seconds)
                            ).strftime("%Y-%m-%dT%H:%M:%SZ")
                            self._worker.schedule_task_retry(
                                task_id=task.id,
                                next_retry_at=next_retry_at,
                                project_id=project_id,
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
                    content = self._format_execution_result(
                        current,
                        results,
                        project_id=project_id,
                        actor_id=actor_id,
                        source=source,
                    )
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
