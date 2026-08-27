"""Concrete approved-task agent runtime.

This module is intentionally small but real: it owns the model/tool loop at
one task boundary and delegates all durable state transitions to the existing
worker, provider, tool, and artifact services. It does not treat a database
row as proof that work happened.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from threading import Event
from typing import TYPE_CHECKING, Any, cast

from zero.app.artifact_service import ArtifactService
from zero.app.authorization_service import AuthorizationService
from zero.app.delegation import DELEGATE_TOOL_NAME
from zero.app.provider_service import ProviderService
from zero.app.tool_service import ToolService
from zero.app.worker_service import WorkerService
from zero.domain.agent_types import AgentType, AgentTypeId
from zero.domain.artifacts import ArtifactId
from zero.domain.audit import AuditSource
from zero.domain.execution import (
    ExecutionId,
    LeaseOwnershipError,
    Task,
    TaskAttempt,
    TaskId,
    TaskNotFoundError,
)
from zero.domain.identity import ProjectId, UserId
from zero.domain.providers import (
    CanonicalMessage,
    CanonicalRequest,
    CanonicalResponse,
    ProviderCancelledError,
    ProviderRequestId,
    ProviderUnknownOutcomeError,
    ToolDeclaration,
)
from zero.domain.tools import AgentScope
from zero.domain.worktrees import (
    RepositoryId,
    TaskArtifact,
    WorktreeError,
    WorktreeId,
)

if TYPE_CHECKING:
    from zero.app.compaction_service import CompactionService
    from zero.app.retrieval_service import ContextBuilder
    from zero.app.worktree_service import WorktreeService
    from zero.persistence.repositories.agent_type_repository import (
        AgentTypeRepository,
    )

_MAX_EVIDENCE_BYTES = 64 * 1024
_DEFAULT_MAX_TOOL_ROUNDS = 8
_LOGGER = logging.getLogger(__name__)
#: Hermes-parity resilience knobs for the execution tool loop.
#:
#: A single malformed tool-call argument or one repeated tool failure
#: must never kill a whole attempt: structured error payloads go back to
#: the model so it can self-correct, truncated calls get a bounded
#: max_tokens boost instead of being executed half-parsed, and identical
#: failure loops trip a breaker that falls through to the summary nudge.
_MAX_TRUNCATION_BOOSTS_PER_LOOP = 2
_MAX_BOOSTED_TOKENS = 32768
_FAILURE_WARN_THRESHOLD = 3
_FAILURE_ABORT_THRESHOLD = 5
#: Final toolless request when the round budget is exhausted (Hermes
#: parity: request a summary instead of hard-failing the turn).
MAX_TOOL_ROUNDS_NUDGE_REQUEST = (
    "You've reached the maximum number of tool-calling rounds allowed. "
    "Please provide a final response summarizing what you've found and "
    "accomplished so far, without calling any more tools."
)
_SUPPORTED_EVIDENCE = {
    "provider_response",
    "transcript",
    "artifact",
    "diff",
    "test_report",
    "exit_status",
    "stdout",
    "stderr",
    "source_snapshot",
}


class RuntimeErrorBase(RuntimeError):
    """Base class for typed runtime failures."""


class RuntimeEvidenceError(RuntimeErrorBase):
    """The task's evidence contract cannot be proven by this runtime."""


class RuntimeToolError(RuntimeErrorBase):
    """A model tool call could not be safely executed."""


def _message_to_record(message: CanonicalMessage) -> dict[str, object]:
    """Render one canonical message for compaction without losing linkage.

    Tool-call structure (assistant ``tool_calls`` and ``tool_call_id`` on
    tool results) is preserved so stored transcripts and summaries keep
    the call/result pairing instead of collapsing to bare text.
    """
    record: dict[str, object] = {"role": message.role, "content": message.content}
    if message.tool_calls:
        record["tool_calls"] = [
            [name, call_id, arguments] for name, call_id, arguments in message.tool_calls
        ]
    if message.tool_call_id:
        record["tool_call_id"] = message.tool_call_id
    return record


@dataclass(frozen=True)
class RuntimeTaskResult:
    """Durable result of one task attempt."""

    task: Task
    attempt: TaskAttempt
    provider_request_id: ProviderRequestId
    evidence_artifact_id: ArtifactId
    response: CanonicalResponse
    evidence_artifact_ids: tuple[ArtifactId, ...] = ()
    worktree_id: WorktreeId | None = None
    context_ledger_id: str | None = None
    agent_instance_id: str | None = None
    agent_type_id: str | None = None


@dataclass(frozen=True)
class ResolvedAgentPolicy:
    """Server-side execution policy resolved from the task's agent type.

    Per the release audit (Phase 1): the data model contains policy
    fields; this resolution step is what makes them binding for the
    running agent rather than decorative CRUD data.
    """

    agent_type: AgentType | None = None

    @property
    def type_id(self) -> AgentTypeId | None:
        return self.agent_type.id if self.agent_type is not None else None

    @property
    def permitted_tools(self) -> frozenset[str] | None:
        """``None`` means unconstrained; otherwise only these tools run."""
        if self.agent_type is None or not self.agent_type.permitted_tools:
            return None
        return frozenset(self.agent_type.permitted_tools)

    @property
    def provider_override(self) -> str | None:
        if self.agent_type is None:
            return None
        value = self.agent_type.model_policy.get("provider", "").strip()
        return value or None

    @property
    def model_override(self) -> str | None:
        if self.agent_type is None:
            return None
        value = self.agent_type.model_policy.get("model", "").strip()
        return value or None


class AgentRuntime:
    """Run approved tasks through provider, tool, and evidence boundaries."""

    def __init__(
        self,
        *,
        worker: WorkerService,
        providers: ProviderService,
        artifacts: ArtifactService,
        authorization: AuthorizationService,
        tools: ToolService | None = None,
        worktrees: WorktreeService | None = None,
        context_builder: ContextBuilder | None = None,
        agent_type_repo: AgentTypeRepository | None = None,
        compaction: CompactionService | None = None,
        test_command: tuple[str, ...] | None = ("pytest", "-q"),
        enable_delegation: bool = False,
        approval_gate: Any | None = None,
        metrics: Any | None = None,
    ) -> None:
        self._worker = worker
        self._providers = providers
        self._artifacts = artifacts
        self._authorization = authorization
        self._tools = tools
        self._worktrees = worktrees
        self._context_builder = context_builder
        # GAP 8b/G2 Hermes parity: optional per-call tool approval gate
        # (ToolApprovalGate). ``None`` keeps historical plan-only posture.
        self._approval_gate = approval_gate
        # Optional MetricsService for execution-loop defect counters
        # (closed label vocabulary; per-model detail lives in the S7
        # JSONL ledger instead).
        self._metrics = metrics
        self._agent_type_repo = agent_type_repo
        self._compaction = compaction
        self._test_command = test_command
        # GAP 8: when enabled, the model may call the `delegate` tool to
        # run bounded subtasks in isolated child contexts.
        self._enable_delegation = enable_delegation

    def resolve_agent_policy(
        self,
        *,
        project_id: ProjectId,
        task: Task,
        explicit_agent_type_id: AgentTypeId | None = None,
    ) -> ResolvedAgentPolicy:
        """Resolve the authoritative execution policy for one task.

        The task's own ``agent_type_id`` wins over any caller-supplied
        value. Unknown, cross-project, or non-active types fail closed.
        """
        type_id_value = task.agent_type_id or (
            explicit_agent_type_id.value if explicit_agent_type_id is not None else None
        )
        if not type_id_value:
            return ResolvedAgentPolicy()
        if self._agent_type_repo is None:
            raise RuntimeEvidenceError(
                f"task {task.id.value} requires agent type {type_id_value} "
                "but no agent-type repository is wired"
            )
        try:
            type_id = AgentTypeId(type_id_value)
        except ValueError as exc:
            raise RuntimeEvidenceError(f"invalid agent type id {type_id_value!r}") from exc
        try:
            agent_type = self._agent_type_repo.get_agent_type(project_id, type_id)
        except Exception as exc:
            from zero.domain.agent_types import AgentTypeNotFoundError

            if isinstance(exc, AgentTypeNotFoundError):
                raise RuntimeEvidenceError(
                    f"agent type {type_id.value} does not exist in project {project_id.value}"
                ) from exc
            raise
        if agent_type.state != "active":
            raise RuntimeEvidenceError(
                f"agent type {type_id.value} is {agent_type.state!r}; "
                "tasks can only run on active agent types"
            )
        return ResolvedAgentPolicy(agent_type=agent_type)

    def run_ready_tasks(
        self,
        *,
        execution_id: ExecutionId,
        project_id: ProjectId,
        actor_id: UserId,
        lease_owner: str,
        provider: str,
        model_name: str,
        agent_scope: AgentScope = "main_worker",
        tool_names: tuple[str, ...] = (),
        repository_id: RepositoryId | None = None,
        max_tasks: int | None = None,
        source: AuditSource = "system",
        agent_type_id: AgentTypeId | None = None,
        stream_callback: Any = None,
    ) -> list[RuntimeTaskResult]:
        """Claim and run a bounded snapshot of currently ready tasks."""
        ready = self._worker.list_ready_tasks(
            execution_id,
            project_id=project_id,
            actor_id=actor_id,
            source=source,
        )
        if max_tasks is not None:
            if max_tasks < 1:
                raise ValueError("max_tasks must be positive")
            ready = ready[:max_tasks]
        # Per-task fault isolation: one poisoned task must not starve its
        # siblings in the same batch. Failures are logged with bounded
        # context; if nothing succeeded the first error propagates so
        # callers keep their existing single-task failure semantics.
        results: list[RuntimeTaskResult] = []
        errors: list[BaseException] = []
        for task in ready:
            try:
                results.append(
                    self.run_task(
                        execution_id=execution_id,
                        project_id=project_id,
                        task_id=task.id,
                        actor_id=actor_id,
                        lease_owner=lease_owner,
                        provider=provider,
                        model_name=model_name,
                        agent_scope=agent_scope,
                        tool_names=tool_names,
                        repository_id=repository_id,
                        source=source,
                        agent_type_id=agent_type_id,
                        stream_callback=stream_callback,
                    )
                )
            except BaseException as exc:
                if isinstance(exc, KeyboardInterrupt):
                    raise
                errors.append(exc)
                # Include the message, not just the class name: a bare
                # "RuntimeEvidenceError" hid WHY evidence was refused
                # (the exact silent-skip trap installation audit R6
                # documents).
                _LOGGER.warning(
                    "ready task %s in execution %s failed without blocking siblings: %s",
                    task.id.value,
                    execution_id.value,
                    exc,
                )
        if not results and errors:
            raise errors[0]
        return results

    def run_task(
        self,
        *,
        execution_id: ExecutionId,
        project_id: ProjectId,
        task_id: TaskId,
        actor_id: UserId,
        lease_owner: str,
        provider: str,
        model_name: str,
        lease_duration_seconds: int = 300,
        agent_scope: AgentScope = "main_worker",
        tool_names: tuple[str, ...] = (),
        repository_id: RepositoryId | None = None,
        max_tool_rounds: int = _DEFAULT_MAX_TOOL_ROUNDS,
        source: AuditSource = "system",
        agent_type_id: AgentTypeId | None = None,
        stream_callback: Any = None,
    ) -> RuntimeTaskResult:
        """Run one ready task and complete it only after evidence is stored.

        ``stream_callback`` (GAP 5) receives client-safe event dicts
        (``text_delta`` / ``tool_call`` / ``done``) as provider events
        arrive; evidence and usage paths are unchanged.
        """
        task = self._find_task(
            execution_id,
            task_id,
            project_id=project_id,
            actor_id=actor_id,
            source=source,
        )
        self._authorization.require_permission(
            actor_id=actor_id,
            project_id=task.project_id,
            permission="execution.start",
            source=source,
        )
        # The assigned agent type is authoritative: it may override the
        # requested provider/model and constrains tools and context.
        policy = self.resolve_agent_policy(
            project_id=task.project_id,
            task=task,
            explicit_agent_type_id=agent_type_id,
        )
        if policy.provider_override:
            provider = policy.provider_override
        if policy.model_override:
            model_name = policy.model_override
        if max_tool_rounds < 1 or max_tool_rounds > 32:
            raise ValueError("max_tool_rounds must be between 1 and 32")
        attempt = self._worker.claim_task(
            execution_id=execution_id,
            task_id=task.id,
            project_id=project_id,
            actor_id=actor_id,
            lease_owner=lease_owner,
            lease_duration_seconds=lease_duration_seconds,
            source=source,
        )

        def fail_and_raise(message: str, exc_class: type[RuntimeErrorBase]) -> None:
            self._worker.fail_task(
                execution_id=execution_id,
                project_id=project_id,
                task_id=task.id,
                attempt_id=attempt.id,
                error_message=message[:500],
                actor_id=actor_id,
                lease_owner=lease_owner,
                source=source,
            )
            raise exc_class(f"Task {task.id.value} was not completed: {message}")

        # Lease one bounded runtime instance of the assigned type. The
        # type's max_concurrent_instances limit is enforced atomically
        # by the repository (BEGIN IMMEDIATE count+insert).
        agent_instance_id: str | None = None

        def finish_instance(state: str) -> None:
            nonlocal agent_instance_id
            instance_value = agent_instance_id
            agent_instance_id = None
            if instance_value is None or self._agent_type_repo is None:
                return
            try:
                from zero.domain.agent_types import AgentInstanceId, AgentInstanceState

                self._agent_type_repo.finish_instance(
                    AgentInstanceId(instance_value),
                    cast("AgentInstanceState", state),
                )
            except Exception:  # noqa: BLE001 - bookkeeping must not mask outcomes
                _LOGGER.debug("agent instance %s could not be finished", instance_value)

        if policy.type_id is not None:
            from zero.domain.agent_types import ConcurrencyLimitExceededError

            assert self._agent_type_repo is not None  # ensured by resolve_agent_policy
            try:
                instance = self._agent_type_repo.lease_instance_for_task(
                    project_id=task.project_id,
                    type_id=policy.type_id,
                    task_id=task.id,
                )
                agent_instance_id = instance.id.value
            except ConcurrencyLimitExceededError as exc:
                # No instance was leased (agent_instance_id is None), so
                # there is nothing to finish; fail the task directly.
                fail_and_raise(f"agent type concurrency limit reached: {exc}", RuntimeEvidenceError)

        unsupported = set(task.expected_evidence) - _SUPPORTED_EVIDENCE
        if unsupported:
            finish_instance("failed")
            message = "runtime cannot prove required evidence: " + ", ".join(sorted(unsupported))
            fail_and_raise(message, RuntimeEvidenceError)

        workspace_labels = {
            "diff",
            "test_report",
            "exit_status",
            "stdout",
            "stderr",
            "source_snapshot",
        }
        requires_workspace = self._worktrees is not None and (
            repository_id is not None
            or bool(set(task.expected_evidence) & workspace_labels)
            or bool(tool_names)
        )
        worktree = None
        context_ledger_id: str | None = None
        effective_tool_names = tool_names
        system_message = ""
        try:
            if requires_workspace:
                assert self._worktrees is not None  # narrowed by the condition above
                if repository_id is None:
                    repositories = self._worktrees.list_repositories(
                        project_id,
                        actor_id=actor_id,
                        source=source,
                    )
                    if len(repositories) != 1:
                        raise RuntimeEvidenceError(
                            "a coding task requires repository_id when the project does not have exactly one repository"
                        )
                    repository_id = repositories[0].id
                worktree = self._worktrees.create_worktree(
                    project_id=project_id,
                    repository_id=repository_id,
                    execution_id=execution_id,
                    task_id=task.id,
                    actor_id=actor_id,
                    source=source,
                )
                worktree = self._worktrees.activate_worktree(
                    project_id=project_id,
                    worktree_id=worktree.id,
                    actor_id=actor_id,
                    source=source,
                )
                effective_tool_names = tool_names or (
                    "read_file",
                    "write_file",
                    "run_command",
                    "capture_diff",
                )
                # The agent type's permitted_tools are a server-side
                # authorization boundary: requested/default tools are
                # narrowed to the type's allow-list, never widened.
                permitted = policy.permitted_tools
                if permitted is not None:
                    effective_tool_names = tuple(
                        name for name in effective_tool_names if name in permitted
                    )

            if self._context_builder is not None:
                model = self._providers.get_model(provider, model_name)
                snapshot = self._worker.get_latest_snapshot(
                    execution_id,
                    project_id=project_id,
                    actor_id=actor_id,
                    source=source,
                )
                # The type's context budget caps the model window so an
                # instance can never fill more context than its policy
                # allows.
                context_window = model.context_window
                if policy.agent_type is not None:
                    context_window = min(context_window, policy.agent_type.context_budget_tokens)
                base_system_message = (
                    "You are an execution-scoped software development worker. "
                    "Only report actions supported by durable evidence."
                )
                if policy.agent_type is not None:
                    base_system_message += (
                        f"\nAgent role: {policy.agent_type.name} — "
                        f"{policy.agent_type.responsibility}"
                    )
                    if policy.agent_type.memory_scope:
                        base_system_message += f"\nMemory scope: {policy.agent_type.memory_scope}"
                context_text, ledger = self._context_builder.build_context(
                    project_id=project_id,
                    execution_id=execution_id,
                    actor_id=actor_id,
                    agent_type_id=policy.type_id,
                    system_message=base_system_message,
                    user_prefix=f"Project {project_id.value}",
                    plan_contract=self._task_prompt(task),
                    execution_snapshot=snapshot.graph_state if snapshot else "{}",
                    conversation_tail=[],
                    query=task.objective,
                    context_window=context_window,
                    model_name=model_name,
                )
                system_message = context_text
                context_ledger_id = ledger.id.value

            workspace_prompt = ""
            if worktree is not None:
                workspace_prompt = (
                    "A private task worktree is active. Use only the server-owned workspace tools; "
                    "do not claim a file change or test result until the tool produced evidence.\n"
                )
            messages = (
                CanonicalMessage(
                    role="user",
                    content=workspace_prompt + self._task_prompt(task),
                ),
            )

            # Automatic compaction under context pressure. Per the
            # release audit (§5.3): the runtime must close the loop —
            # measure pressure, invoke compaction, preserve typed state
            # and transcript artifacts, activate the new context.
            # Compaction blockers (including no-thrash) are surfaced but
            # never fail the task: an oversized context is degraded, not
            # fatal.
            if self._context_builder is not None and self._compaction is not None:
                try:
                    if self._compaction.should_compact(
                        execution_id, context_window, max_output_tokens=model.max_output_tokens
                    ):
                        conversation_messages = [_message_to_record(m) for m in messages]
                        self._compaction.compact(
                            project_id=project_id,
                            execution_id=execution_id,
                            actor_id=actor_id,
                            system_message=(
                                "You are an execution-scoped software development worker. "
                                "Only report actions supported by durable evidence."
                            ),
                            user_prefix=f"Project {project_id.value}",
                            plan_contract=self._task_prompt(task),
                            execution_snapshot=snapshot.graph_state if snapshot else "{}",
                            conversation_messages=conversation_messages,
                            context_window=context_window,
                            model_name=model_name,
                            agent_type_id=policy.type_id,
                            memory_delta_enabled=(
                                policy.agent_type is not None
                                and policy.agent_type.model_policy.get("memory_delta_enabled")
                                == "1"
                            ),
                        )
                except Exception as compact_exc:  # noqa: BLE001 - degraded, not fatal
                    _LOGGER.warning(
                        "automatic compaction skipped for execution %s: %s",
                        execution_id.value,
                        type(compact_exc).__name__,
                    )

            request = CanonicalRequest(
                provider=provider,
                model_name=model_name,
                messages=messages,
                tools=self._tool_declarations(effective_tool_names),
                system_message=system_message,
                # GAP 5: attach a streaming transport exactly when a
                # client-facing stream consumer is connected.
                stream=stream_callback is not None,
            )
        except Exception as exc:
            if worktree is not None:
                assert self._worktrees is not None
                try:
                    self._worktrees.complete_worktree(
                        project_id=project_id,
                        worktree_id=worktree.id,
                        actor_id=actor_id,
                        succeeded=False,
                        source=source,
                    )
                except (OSError, WorktreeError) as cleanup_exc:
                    _LOGGER.debug(
                        "runtime worktree cleanup failed: %s",
                        type(cleanup_exc).__name__,
                    )
            finish_instance("failed")
            self._worker.fail_task(
                execution_id=execution_id,
                project_id=project_id,
                task_id=task.id,
                attempt_id=attempt.id,
                error_message=f"workspace/context setup failed: {type(exc).__name__}"[:500],
                actor_id=actor_id,
                lease_owner=lease_owner,
                source=source,
            )
            raise

        provider_request_id: ProviderRequestId | None = None
        cancel_event = self._worker.get_cancellation_event(execution_id)
        observer = self._stream_observer(execution_id, stream_callback)
        try:
            provider_request, response = self._providers.send_request_with_fallback(
                project_id=task.project_id,
                actor_id=actor_id,
                execution_id=execution_id,
                request=request,
                cancel_event=cancel_event,
                source=source,
                agent_scope=agent_scope,
                stream_observer=observer,
            )
            provider_request_id = provider_request.id
            response, provider_request_id, messages_final = self._run_tool_rounds(
                task=task,
                attempt=attempt,
                actor_id=actor_id,
                execution_id=execution_id,
                project_id=project_id,
                request=request,
                response=response,
                provider_request_id=provider_request_id,
                agent_scope=agent_scope,
                tool_names=effective_tool_names,
                max_tool_rounds=max_tool_rounds,
                cancel_event=cancel_event,
                lease_owner=lease_owner,
                lease_duration_seconds=lease_duration_seconds,
                source=source,
                stream_observer=observer,
            )
            if cancel_event.is_set():
                raise ProviderCancelledError("execution cancelled before evidence acceptance")
            # Post-loop compaction: long tool rounds accumulate real
            # multi-turn history, so pressure is re-measured with the
            # full transcript (linkage preserved via _message_to_record).
            if self._context_builder is not None and self._compaction is not None:
                try:
                    if self._compaction.should_compact(
                        execution_id, context_window, max_output_tokens=model.max_output_tokens
                    ):
                        self._compaction.compact(
                            project_id=project_id,
                            execution_id=execution_id,
                            actor_id=actor_id,
                            system_message=(
                                "You are an execution-scoped software development worker. "
                                "Only report actions supported by durable evidence."
                            ),
                            user_prefix=f"Project {project_id.value}",
                            plan_contract=self._task_prompt(task),
                            execution_snapshot=snapshot.graph_state if snapshot else "{}",
                            conversation_messages=[_message_to_record(m) for m in messages_final],
                            context_window=context_window,
                            model_name=model_name,
                            agent_type_id=policy.type_id,
                            memory_delta_enabled=(
                                policy.agent_type is not None
                                and policy.agent_type.model_policy.get("memory_delta_enabled")
                                == "1"
                            ),
                        )
                except Exception as compact_exc:  # noqa: BLE001 - degraded, not fatal
                    _LOGGER.debug(
                        "post-loop compaction skipped for execution %s: %s",
                        execution_id.value,
                        type(compact_exc).__name__,
                    )
        except ProviderUnknownOutcomeError:
            if worktree is not None:
                assert self._worktrees is not None
                try:
                    self._worktrees.complete_worktree(
                        project_id=project_id,
                        worktree_id=worktree.id,
                        actor_id=actor_id,
                        succeeded=False,
                        source=source,
                    )
                except (OSError, WorktreeError) as cleanup_exc:
                    _LOGGER.debug(
                        "runtime worktree cleanup failed: %s",
                        type(cleanup_exc).__name__,
                    )
            finish_instance("cancelled")
            self._worker.mark_provider_outcome_unknown(
                execution_id=execution_id,
                project_id=project_id,
                task_id=task.id,
                attempt_id=attempt.id,
                error_message="provider outcome unknown; reconciliation required",
                actor_id=actor_id,
                lease_owner=lease_owner,
                source=source,
            )
            raise
        except ProviderCancelledError as exc:
            if worktree is not None:
                assert self._worktrees is not None
                try:
                    self._worktrees.complete_worktree(
                        project_id=project_id,
                        worktree_id=worktree.id,
                        actor_id=actor_id,
                        succeeded=False,
                        source=source,
                    )
                except (OSError, WorktreeError) as cleanup_exc:
                    _LOGGER.debug(
                        "runtime worktree cleanup failed: %s",
                        type(cleanup_exc).__name__,
                    )
            finish_instance("cancelled")
            # Cancellation is a state transition, not a failure: record
            # the task/attempt as cancelled when the lease still allows it.
            try:
                self._worker.cancel_task(
                    execution_id=execution_id,
                    project_id=project_id,
                    task_id=task.id,
                    attempt_id=attempt.id,
                    actor_id=actor_id,
                    lease_owner=lease_owner,
                    reason=f"cancellation requested: {exc}",
                    source=source,
                )
            except Exception as cancel_exc:  # noqa: BLE001 - never mask the cause
                _LOGGER.warning(
                    "cancelled task %s could not be transitioned: %s",
                    task.id.value,
                    type(cancel_exc).__name__,
                )
            raise
        except Exception as exc:
            if worktree is not None:
                assert self._worktrees is not None
                try:
                    self._worktrees.complete_worktree(
                        project_id=project_id,
                        worktree_id=worktree.id,
                        actor_id=actor_id,
                        succeeded=False,
                        source=source,
                    )
                except (OSError, WorktreeError) as cleanup_exc:
                    _LOGGER.debug(
                        "runtime worktree cleanup failed: %s",
                        type(cleanup_exc).__name__,
                    )
            finish_instance("failed")
            self._worker.fail_task(
                execution_id=execution_id,
                project_id=project_id,
                task_id=task.id,
                attempt_id=attempt.id,
                error_message=f"{type(exc).__name__}: runtime execution failed"[:500],
                actor_id=actor_id,
                lease_owner=lease_owner,
                source=source,
            )
            raise

        if provider_request_id is None:  # pragma: no cover - defensive
            raise RuntimeErrorBase("provider did not return a request identity")
        # Postconditions (diff capture, test commands) can outlive the
        # original lease window; renew best-effort so completion fencing
        # judges live work, not an expired clock.
        try:
            self._worker.renew_task_lease(
                execution_id=execution_id,
                task_id=task.id,
                attempt_id=attempt.id,
                project_id=project_id,
                actor_id=actor_id,
                lease_owner=lease_owner,
                lease_duration_seconds=lease_duration_seconds,
                source=source,
            )
        except Exception as renew_exc:  # noqa: BLE001 - completion re-fences
            _LOGGER.debug(
                "pre-evidence lease renewal skipped for task %s: %s",
                task.id.value,
                type(renew_exc).__name__,
            )
        evidence_ids: list[ArtifactId] = []
        try:
            transcript = self._store_evidence(
                task=task,
                attempt=attempt,
                provider_request_id=provider_request_id,
                response=response,
                actor_id=actor_id,
            )
            evidence_ids.append(transcript.id)
            if worktree is not None:
                evidence_ids.extend(
                    self._collect_workspace_evidence(
                        task=task,
                        attempt=attempt,
                        worktree_id=worktree.id,
                        actor_id=actor_id,
                        source=source,
                    )
                )
                assert self._worktrees is not None
                self._worktrees.complete_worktree(
                    project_id=project_id,
                    worktree_id=worktree.id,
                    actor_id=actor_id,
                    succeeded=True,
                    source=source,
                )
            completed_task = self._worker.complete_task(
                execution_id=execution_id,
                project_id=project_id,
                task_id=task.id,
                attempt_id=attempt.id,
                actor_id=actor_id,
                lease_owner=lease_owner,
                evidence=task.expected_evidence,
                evidence_artifact_ids=tuple(evidence_ids),
                source=source,
            )
            # A cancel racing completion surfaces as a terminal task in
            # a non-completed state; never report that as success.
            if completed_task.state != "completed":
                raise RuntimeEvidenceError(
                    f"task reached terminal state {completed_task.state!r} instead of completed"
                )
        except Exception as exc:
            if worktree is not None:
                assert self._worktrees is not None
                try:
                    self._worktrees.complete_worktree(
                        project_id=project_id,
                        worktree_id=worktree.id,
                        actor_id=actor_id,
                        succeeded=False,
                        source=source,
                    )
                except (OSError, WorktreeError) as cleanup_exc:
                    _LOGGER.debug(
                        "runtime worktree cleanup failed: %s",
                        type(cleanup_exc).__name__,
                    )
            finish_instance("failed")
            self._worker.fail_task(
                execution_id=execution_id,
                project_id=project_id,
                task_id=task.id,
                attempt_id=attempt.id,
                error_message=f"evidence/postcondition failed: {type(exc).__name__}"[:500],
                actor_id=actor_id,
                lease_owner=lease_owner,
                source=source,
            )
            raise
        agent_instance_id_for_result = agent_instance_id
        finish_instance("completed")
        final_attempt = self._worker.list_attempts(
            task.id,
            project_id=project_id,
            actor_id=actor_id,
            source=source,
        )[-1]
        return RuntimeTaskResult(
            task=completed_task,
            attempt=final_attempt,
            provider_request_id=provider_request_id,
            evidence_artifact_id=evidence_ids[0],
            response=response,
            evidence_artifact_ids=tuple(evidence_ids),
            worktree_id=worktree.id if worktree is not None else None,
            context_ledger_id=context_ledger_id,
            agent_instance_id=agent_instance_id_for_result,
            agent_type_id=policy.type_id.value if policy.type_id is not None else None,
        )

    @staticmethod
    def _task_prompt(task: Task) -> str:
        scope = ", ".join(task.permitted_scope) or "(none declared)"
        evidence = ", ".join(task.expected_evidence) or "(none declared)"
        return (
            f"Objective: {task.objective}\n"
            f"Permitted scope: {scope}\n"
            f"Required evidence: {evidence}\n"
            "Return a concise completion report and do not claim actions "
            "that you did not perform."
        )

    def _tool_declarations(
        self,
        tool_names: tuple[str, ...],
    ) -> tuple[ToolDeclaration | str, ...]:
        """Build model-facing declarations carrying real argument schemas.

        The registry's input schema travels with every request so the
        model can emit well-typed arguments; unknown names fall back to
        bare-name declarations rather than being dropped.
        """
        if not tool_names and not self._enable_delegation:
            return ()
        if self._tools is None and not self._enable_delegation:
            return tool_names
        by_name: dict[str, Any] = {}
        try:
            by_name = {tool.name: tool for tool in self._tools.list_tools()}
        except Exception:  # noqa: BLE001 - declaration enrichment is best-effort
            _LOGGER.debug("tool registry unavailable for declaration building")
        declarations: list[ToolDeclaration | str] = []
        for name in tool_names:
            tool = by_name.get(name)
            if tool is None:
                declarations.append(name)
                continue
            declarations.append(
                ToolDeclaration(
                    name=name,
                    description=getattr(tool, "description", "") or "",
                    parameters=dict(getattr(tool, "input_schema", None) or {}) or None,
                )
            )
        # GAP 8: expose `delegate` while nesting budget remains.
        from zero.app.delegation import (
            MAX_DELEGATION_DEPTH,
            current_delegation_depth,
            delegate_declaration,
        )

        if self._enable_delegation and current_delegation_depth() < MAX_DELEGATION_DEPTH:
            declarations.append(delegate_declaration())
        return tuple(declarations)

    def _find_task(
        self,
        execution_id: ExecutionId,
        task_id: TaskId,
        *,
        project_id: ProjectId,
        actor_id: UserId,
        source: AuditSource,
    ) -> Task:
        for task in self._worker.list_tasks(
            execution_id,
            project_id=project_id,
            actor_id=actor_id,
            source=source,
        ):
            if task.id == task_id:
                return task
        raise TaskNotFoundError(
            f"Task {task_id.value} does not belong to execution {execution_id.value}"
        )

    @staticmethod
    def _stream_observer(execution_id: ExecutionId, stream_callback: Any):
        """Bind a run-level callback to the execution id (GAP 5)."""
        if stream_callback is None:
            return None
        execution_value = execution_id.value

        def observer(payload: dict) -> None:
            stream_callback(execution_value, payload)

        return observer

    def _execute_delegation(
        self,
        *,
        call_arguments: str,
        parent_allowed_tools: tuple[str, ...],
        execution_id: ExecutionId,
        project_id: ProjectId,
        actor_id: UserId,
        provider: str,
        model_name: str,
    ) -> dict[str, Any]:
        """Run one delegated subtask inline and return its result payload.

        The child runs with a fresh conversation, an intersection-narrowed
        tool set, and provider requests tagged ``sub_agent`` so usage
        accounting keeps whole-tree aggregation correct. Failures return
        structured error payloads — delegation never crashes the parent.
        """
        from zero.app.delegation import (
            _WORKSPACE_TOOLS,
            MAX_DELEGATION_DEPTH,
            current_delegation_depth,
            delegation_depth_increased,
        )

        def _error(message: str) -> dict[str, Any]:
            return {"status": "error", "error": message}

        try:
            input_data = json.loads(call_arguments or "{}")
        except json.JSONDecodeError:
            return _error("delegate arguments were not valid JSON")
        if not isinstance(input_data, dict):
            return _error("delegate arguments must be a JSON object")
        objective = str(input_data.get("objective") or "").strip()
        if not objective or len(objective) > 8192:
            return _error("delegate requires a non-empty objective")
        depth = current_delegation_depth()
        if depth >= MAX_DELEGATION_DEPTH:
            return _error(
                f"delegation depth limit reached ({MAX_DELEGATION_DEPTH}); "
                "complete the task yourself"
            )

        requested_tools = tuple(str(name) for name in (input_data.get("tools") or ())[:32])
        allowed = [n for n in requested_tools if n in set(parent_allowed_tools)]
        if not requested_tools:
            # Default to the parent's non-workspace tools.
            allowed = [n for n in parent_allowed_tools if n not in _WORKSPACE_TOOLS]
        child_model = str(input_data.get("model") or "").strip() or None

        with delegation_depth_increased():
            messages = [CanonicalMessage(role="user", content=objective)]
            current_request = CanonicalRequest(
                provider=provider,
                model_name=child_model or model_name,
                system_message=(
                    "You are a focused sub-agent completing one delegated "
                    "subtask. Return only the final answer."
                ),
                messages=tuple(messages),
                tools=tuple(self._tool_declarations(tuple(allowed))),
                max_tokens=1024,
            )
            final_content = ""
            completed = False
            for _round in range(4):
                _provider_request, response = self._providers.send_request_with_fallback(
                    project_id=project_id,
                    actor_id=actor_id,
                    execution_id=execution_id,
                    request=current_request,
                    agent_scope="sub_agent_type",
                    source="system",
                )
                if not response.tool_calls:
                    final_content = response.content
                    completed = True
                    break
                messages.append(
                    CanonicalMessage(
                        role="assistant",
                        content=response.content,
                        tool_calls=tuple(
                            (c.tool_name, c.tool_call_id, c.arguments) for c in response.tool_calls
                        ),
                    )
                )
                for call in response.tool_calls:
                    payload_text = f"tool {call.tool_name} unavailable to sub-agents"
                    if self._tools is not None and call.tool_name in allowed:
                        try:
                            tool_input = json.loads(call.arguments or "{}")
                            result = self._tools.invoke(
                                project_id=project_id,
                                actor_id=actor_id,
                                agent_scope="main_worker",
                                tool_name=call.tool_name,
                                input_data=tool_input if isinstance(tool_input, dict) else {},
                                execution_id=execution_id.value,
                                source="system",
                            )
                            payload_text = result.model_facing
                        except Exception as exc:  # noqa: BLE001 - tool failure is data
                            payload_text = f"error: {type(exc).__name__}"
                    messages.append(
                        CanonicalMessage(
                            role="tool",
                            content=payload_text[:2000],
                            tool_call_id=call.tool_call_id,
                        )
                    )
                current_request = replace(current_request, messages=tuple(messages))
            if not completed:
                final_content = "(sub-agent exhausted its round budget)"
        return {
            "status": "completed",
            "depth": depth + 1,
            "tools_used": allowed,
            "result": final_content[:4000],
        }

    def _run_tool_rounds(
        self,
        *,
        task: Task,
        attempt: TaskAttempt,
        actor_id: UserId,
        execution_id: ExecutionId,
        project_id: ProjectId,
        request: CanonicalRequest,
        response: CanonicalResponse,
        provider_request_id: ProviderRequestId,
        agent_scope: AgentScope,
        tool_names: tuple[str, ...],
        max_tool_rounds: int,
        cancel_event: Event,
        lease_owner: str,
        lease_duration_seconds: int,
        source: AuditSource,
        stream_observer: Any = None,
    ) -> tuple[CanonicalResponse, ProviderRequestId, list[CanonicalMessage]]:
        """Run a bounded model/tool loop without accepting unresolved calls.

        The attempt lease is renewed before every round so long-running
        tool work cannot silently expire the fencing window mid-loop.
        """
        if max_tool_rounds < 1 or max_tool_rounds > 32:
            raise ValueError("max_tool_rounds must be between 1 and 32")
        messages = list(request.messages)
        if not response.tool_calls:
            return response, provider_request_id, messages
        if self._tools is None:
            raise RuntimeToolError("provider requested tools but no tool service is wired")
        requested_names = set(tool_names)
        current_response = response
        current_request_id = provider_request_id

        def _renew_lease() -> None:
            try:
                self._worker.renew_task_lease(
                    execution_id=execution_id,
                    task_id=task.id,
                    attempt_id=attempt.id,
                    project_id=project_id,
                    actor_id=actor_id,
                    lease_owner=lease_owner,
                    lease_duration_seconds=lease_duration_seconds,
                    source=source,
                )
            except LeaseOwnershipError as lease_exc:
                # The fencing clock won mid-round: surface a precise
                # tool-loop failure. fail_task now tolerates an expired
                # but owner-matched lease, so the terminal state is
                # recorded instead of leaving a zombie running attempt.
                raise RuntimeToolError(
                    f"attempt lease expired during tool round: {lease_exc}"
                ) from lease_exc

        def _synthetic_tool_error(call_id: str, payload: dict[str, object]) -> CanonicalMessage:
            """Hermes parity: never raise on recoverable call defects.

            The model receives a structured error as the paired tool
            message so it can correct course in the next round instead of
            losing the whole attempt to one malformed argument.
            """
            return CanonicalMessage(
                role="tool",
                content=json.dumps(payload, ensure_ascii=False),
                tool_call_id=call_id,
            )

        failure_signatures: dict[str, int] = {}
        boost_budget = _MAX_TRUNCATION_BOOSTS_PER_LOOP
        breaker_tripped = False

        for _round in range(max_tool_rounds):
            _renew_lease()

            # ---- truncation boost ladder (before history append) ------
            # finish_reason=length can truncate arguments mid-JSON. If any
            # call fails to parse AND the cap was hit, discard the broken
            # assistant turn entirely and re-ask once with doubled output
            # budget rather than executing guessed/partial arguments.
            if (
                current_response.finish_reason == "length"
                and current_response.tool_calls
                and boost_budget > 0
                and any(
                    self._tool_argument_error(call) is not None
                    for call in current_response.tool_calls
                )
            ):
                boosted_request = replace(
                    request,
                    messages=tuple(messages),
                    max_tokens=min(request.max_tokens * 2, _MAX_BOOSTED_TOKENS),
                )
                boost_budget -= 1
                if self._metrics is not None:
                    self._metrics.increment(
                        "agent_runtime_truncation_boosts",
                        project_id=task.project_id.value,
                        result="boosted_reask",
                    )
                next_provider_request, boosted_response = (
                    self._providers.send_request_with_fallback(
                        project_id=task.project_id,
                        actor_id=actor_id,
                        execution_id=execution_id,
                        request=boosted_request,
                        cancel_event=cancel_event,
                        source=source,
                        agent_scope=agent_scope,
                        stream_observer=stream_observer,
                    )
                )
                current_request_id = next_provider_request.id
                current_response = boosted_response
                if not current_response.tool_calls:
                    return current_response, current_request_id, messages

            messages.append(
                CanonicalMessage(
                    role="assistant",
                    content=current_response.content,
                    tool_calls=tuple(
                        (call.tool_name, call.tool_call_id, call.arguments)
                        for call in current_response.tool_calls
                    ),
                )
            )
            abort_after_batch = False
            for call in current_response.tool_calls:
                if call.tool_name == DELEGATE_TOOL_NAME and self._enable_delegation:
                    # GAP 8: delegation is runtime-owned; it never flows
                    # through the static tool registry.
                    payload = self._execute_delegation(
                        call_arguments=call.arguments,
                        parent_allowed_tools=tool_names,
                        execution_id=execution_id,
                        project_id=project_id,
                        actor_id=actor_id,
                        provider=request.provider,
                        model_name=request.model_name,
                    )
                    messages.append(
                        CanonicalMessage(
                            role="tool",
                            content=json.dumps(payload, ensure_ascii=False),
                            tool_call_id=call.tool_call_id,
                        )
                    )
                    continue
                arg_error = self._tool_argument_error(call)
                if arg_error is not None:
                    if self._metrics is not None:
                        self._metrics.increment(
                            "agent_runtime_tool_call_defects",
                            project_id=task.project_id.value,
                            result="invalid_arguments",
                        )
                    messages.append(
                        _synthetic_tool_error(
                            call.tool_call_id,
                            {
                                "error": "invalid_tool_arguments",
                                "detail": arg_error,
                                "hint": "Re-issue this tool call with a JSON object "
                                "whose keys match the declared schema.",
                            },
                        )
                    )
                    continue
                if call.tool_name not in requested_names:
                    if self._metrics is not None:
                        self._metrics.increment(
                            "agent_runtime_tool_call_defects",
                            project_id=task.project_id.value,
                            result="undeclared_tool",
                        )
                    messages.append(
                        _synthetic_tool_error(
                            call.tool_call_id,
                            {
                                "error": "undeclared_tool",
                                "detail": (
                                    f"tool {call.tool_name!r} is not declared for this task"
                                ),
                                "declared_tools": sorted(requested_names),
                            },
                        )
                    )
                    continue
                call_arguments = json.loads(call.arguments)
                if self._approval_gate is not None:
                    verdict = self._approval_gate.evaluate(
                        project_id=task.project_id.value,
                        execution_id=execution_id.value,
                        tool_name=call.tool_name,
                        input_data=call_arguments,
                    )
                    if verdict.state != "allowed":
                        if self._metrics is not None:
                            self._metrics.increment(
                                "agent_runtime_tool_call_defects",
                                project_id=task.project_id.value,
                                result=(
                                    "approval_pending"
                                    if verdict.state == "pending"
                                    else "approval_denied"
                                ),
                            )
                        payload: dict[str, object] = {
                            "error": (
                                "approval_pending"
                                if verdict.state == "pending"
                                else "approval_denied"
                            ),
                            "tool": call.tool_name,
                        }
                        if verdict.request is not None and verdict.state == "pending":
                            payload["approval_request_id"] = verdict.request.id
                            payload["hint"] = (
                                "A human must approve this tool call before it runs. "
                                "Continue with other work or finalize your answer."
                            )
                        elif verdict.cause is not None:
                            payload["cause"] = verdict.cause
                        messages.append(_synthetic_tool_error(call.tool_call_id, payload))
                        continue
                result = self._tools.invoke(
                    project_id=task.project_id,
                    actor_id=actor_id,
                    agent_scope=agent_scope,
                    tool_name=call.tool_name,
                    input_data=call_arguments,
                    execution_id=execution_id.value,
                    task_id=task.id.value,
                    source=source,
                )
                messages.append(
                    CanonicalMessage(
                        role="tool",
                        content=result.model_facing,
                        tool_call_id=call.tool_call_id,
                    )
                )
                # ---- identical-failure loop breaker --------------------
                status = getattr(result, "status", "success")
                if status in ("success", "unknown"):
                    continue
                signature = (
                    call.tool_name,
                    (getattr(result, "error", None) or result.model_facing or "")[:120],
                )
                count = failure_signatures.get(signature, 0) + 1
                failure_signatures[signature] = count
                if count == _FAILURE_WARN_THRESHOLD:
                    messages.append(
                        CanonicalMessage(
                            role="user",
                            content=(
                                f"Tool {call.tool_name!r} has failed {count} times with an "
                                "identical error. Change your approach or try a different "
                                "strategy before calling it again."
                            ),
                        )
                    )
                elif count >= _FAILURE_ABORT_THRESHOLD:
                    breaker_tripped = True
                    abort_after_batch = True
            if abort_after_batch:
                break
            # Field-preserving reconstruction: replace() carries any
            # CanonicalRequest field this loop does not manage (notably
            # ``stream``) instead of silently resetting it.
            next_request = replace(request, messages=tuple(messages))
            next_provider_request, next_response = self._providers.send_request_with_fallback(
                project_id=task.project_id,
                actor_id=actor_id,
                execution_id=execution_id,
                request=next_request,
                cancel_event=cancel_event,
                source=source,
                agent_scope=agent_scope,
                stream_observer=stream_observer,
            )
            current_request_id = next_provider_request.id
            current_response = next_response
            if not current_response.tool_calls:
                return current_response, current_request_id, messages
        unresolved = ", ".join(call.tool_name for call in current_response.tool_calls)
        reason = (
            "identical-failure loop breaker tripped"
            if breaker_tripped
            else f"model/tool loop exceeded {max_tool_rounds} rounds"
        )
        # Round budget exhausted (or breaker fired): one final toolless
        # request asking the model for its summary (reference parity). If
        # it STILL returns tool calls, the original failure stands.
        nudge_request = replace(
            request,
            messages=tuple(messages)
            + (CanonicalMessage(role="user", content=MAX_TOOL_ROUNDS_NUDGE_REQUEST),),
            tools=(),
        )
        try:
            nudge_provider_request, nudge_response = self._providers.send_request_with_fallback(
                project_id=task.project_id,
                actor_id=actor_id,
                execution_id=execution_id,
                request=nudge_request,
                cancel_event=cancel_event,
                source=source,
                agent_scope=agent_scope,
            )
        except Exception as nudge_exc:  # noqa: BLE001 - original error is primary
            _LOGGER.warning(
                "final summary request failed for task %s: %s",
                task.id.value,
                type(nudge_exc).__name__,
            )
        else:
            if not nudge_response.tool_calls:
                return nudge_response, nudge_provider_request.id, messages
        raise RuntimeToolError(f"{reason} with unresolved calls: {unresolved}")

    @staticmethod
    def _tool_argument_error(call: Any) -> str | None:
        """Describe a defective tool-call argument payload, or None.

        Hermes parity check used by BOTH the truncation ladder (a cap-hit
        plus unparseable arguments means the call may be half-written)
        and the synthesized-error path. Deliberately refuses coercion:
        guessed arguments are never executed.
        """
        try:
            data = json.loads(call.arguments)
        except json.JSONDecodeError as exc:
            return (
                f"arguments are not valid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})"
            )
        if not isinstance(data, dict):
            return "arguments must decode to a JSON object"
        return None

    def _collect_workspace_evidence(
        self,
        *,
        task: Task,
        attempt: TaskAttempt,
        worktree_id: WorktreeId,
        actor_id: UserId,
        source: AuditSource,
    ) -> list[ArtifactId]:
        """Execute required postconditions and copy task artifacts canonically."""
        if self._worktrees is None:  # pragma: no cover - guarded by caller
            raise RuntimeEvidenceError("workspace evidence requested without a worktree service")
        required = set(task.expected_evidence)
        stored: list[ArtifactId] = []

        def store_task_artifact(
            task_artifact: TaskArtifact,
            *,
            label: str,
            content: str | None = None,
            kind: str | None = None,
        ) -> ArtifactId:
            payload = content if content is not None else task_artifact.content
            if not payload:
                raise RuntimeEvidenceError(f"required {label} evidence is empty")
            artifact = self._artifacts.store_artifact(
                project_id=task.project_id,
                actor_id=actor_id,
                kind=kind or task_artifact.kind,  # type: ignore[arg-type]
                content=payload,
                producer=f"agent-runtime:{task.id.value}",
                provenance=json.dumps(
                    {
                        "execution_id": task.execution_id.value,
                        "task_id": task.id.value,
                        "attempt_id": attempt.id.value,
                        "worktree_id": worktree_id.value,
                        "task_artifact_id": task_artifact.id.value,
                        "evidence_labels": [label],
                    },
                    sort_keys=True,
                ),
                source=source,
            )
            stored.append(artifact.id)
            return artifact.id

        if "diff" in required:
            diff = self._worktrees.capture_diff(
                project_id=task.project_id,
                worktree_id=worktree_id,
                task_id=task.id,
                actor_id=actor_id,
                source=source,
            )
            if not diff.content.strip():
                raise RuntimeEvidenceError("required diff evidence contains no file change")
            store_task_artifact(diff, label="diff", kind="diff")

        if "source_snapshot" in required:
            snapshot = self._worktrees.capture_source_snapshot(
                project_id=task.project_id,
                worktree_id=worktree_id,
                task_id=task.id,
                actor_id=actor_id,
                source=source,
            )
            if not snapshot.content.strip():
                raise RuntimeEvidenceError("required source_snapshot evidence is empty")
            store_task_artifact(snapshot, label="source_snapshot", kind="source_snapshot")

        command_artifacts: list[TaskArtifact] = []
        command_run = None
        if {"test_report", "exit_status", "stdout", "stderr"} & required:
            if not self._test_command:
                raise RuntimeEvidenceError(
                    "test evidence was requested but no test command is configured"
                )
            command_run, command_artifacts = self._worktrees.run_command(
                project_id=task.project_id,
                worktree_id=worktree_id,
                task_id=task.id,
                actor_id=actor_id,
                command=self._test_command[0],
                args=tuple(self._test_command[1:]),
                source=source,
            )
            if command_run.state != "completed" or command_run.exit_code != 0:
                raise RuntimeEvidenceError(
                    f"configured test command did not pass (state={command_run.state}, exit={command_run.exit_code})"
                )
            by_kind = {artifact.kind: artifact for artifact in command_artifacts}
            if "stdout" in required:
                stdout_artifact = by_kind.get("stdout")
                if stdout_artifact is None:
                    raise RuntimeEvidenceError("test command produced no stdout artifact")
                store_task_artifact(stdout_artifact, label="stdout", kind="stdout")
            if "stderr" in required:
                stderr_artifact = by_kind.get("stderr")
                if stderr_artifact is None:
                    raise RuntimeEvidenceError("test command produced no stderr artifact")
                store_task_artifact(stderr_artifact, label="stderr", kind="stderr")
            exit_artifact = by_kind.get("exit_status")
            if "exit_status" in required:
                if exit_artifact is None:
                    raise RuntimeEvidenceError("test command produced no exit-status artifact")
                store_task_artifact(exit_artifact, label="exit_status", kind="exit_status")
            if "test_report" in required:
                if not command_artifacts:
                    # The exit-code gate above guarantees a completed run
                    # produced artifacts; an empty list means the worktree
                    # service contract changed. Fail loudly instead of
                    # fabricating a placeholder artifact identity.
                    raise RuntimeEvidenceError("test command produced no artifacts for the report")
                report_parts = [
                    json.dumps(
                        {
                            "command": self._test_command[0],
                            "args": list(self._test_command[1:]),
                            "state": command_run.state,
                            "exit_code": command_run.exit_code,
                            "timed_out": command_run.timed_out,
                        },
                        sort_keys=True,
                    )
                ]
                report_parts.extend(
                    f"[{artifact.kind}]\n{artifact.content}" for artifact in command_artifacts
                )
                report = TaskArtifact(
                    id=command_artifacts[-1].id,
                    project_id=task.project_id,
                    worktree_id=worktree_id,
                    task_id=task.id,
                    command_run_id=command_run.id,
                    kind="test_report",
                    content="\n".join(report_parts),
                    content_hash="",
                    created_at="",
                )
                store_task_artifact(report, label="test_report", kind="test_report")
        return stored

    def _store_evidence(
        self,
        *,
        task: Task,
        attempt: TaskAttempt,
        provider_request_id: ProviderRequestId,
        response: CanonicalResponse,
        actor_id: UserId,
    ):
        content = json.dumps(
            {
                "task_id": task.id.value,
                "attempt_id": attempt.id.value,
                "provider_request_id": provider_request_id.value,
                "provider_message_id": response.provider_message_id,
                "evidence_labels": list(task.expected_evidence),
                "objective": task.objective,
                "response": {
                    "content": response.content,
                    "finish_reason": response.finish_reason,
                    "tool_calls": [
                        {
                            "tool_name": call.tool_name,
                            "tool_call_id": call.tool_call_id,
                            "arguments": call.arguments,
                        }
                        for call in response.tool_calls
                    ],
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        encoded = content.encode("utf-8")
        if len(encoded) > _MAX_EVIDENCE_BYTES:
            content = encoded[:_MAX_EVIDENCE_BYTES].decode("utf-8", errors="ignore")
        return self._artifacts.store_artifact(
            project_id=task.project_id,
            actor_id=actor_id,
            kind="transcript",
            content=content,
            media_type="application/json",
            producer=f"agent-runtime:{task.id.value}",
            provenance=json.dumps(
                {
                    "execution_id": task.execution_id.value,
                    "task_id": task.id.value,
                    "attempt_id": attempt.id.value,
                    "provider_request_id": provider_request_id.value,
                    "evidence_labels": list(task.expected_evidence),
                },
                sort_keys=True,
            ),
        )
