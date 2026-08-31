"""Concrete approved-task agent runtime.

This module is intentionally small but real: it owns the model/tool loop at
one task boundary and delegates all durable state transitions to the existing
worker, provider, tool, and artifact services. It does not treat a database
row as proof that work happened.
"""

from __future__ import annotations

import json
import logging
import re
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
from zero.domain.tools import AgentScope, ToolError, ToolInvocationDeniedError
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

# B10 refinement (live run 2026-08-31): capture_diff marks the cumulative
# fallback with this stable marker; a generative objective whose diff
# evidence contains ONLY the fallback recorded no work of its own.
_TASK_MADE_NO_CHANGES_MARKER = "(this task made no changes on top of its dependency"

_OBJECTIVE_CHANGE_VERBS = re.compile(
    r"\b(create|write|add|implement|update|fix|repair|refactor|remove"
    r"|delete|migrate|extend|modify|generate|produce)\b",
    re.IGNORECASE,
)


def _objective_expects_changes(objective: str) -> bool:
    """Whether the objective's wording implies the agent must change files."""
    return bool(_OBJECTIVE_CHANGE_VERBS.search(objective or ""))
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
#: Hermes parity (audit 2026-08-28): a no-content/no-tool-call response
#: is retried with a bounded nudge instead of silently completing the
#: task with an empty deliverable. Mirrors the reference empty-response
#: ladder (post-tool nudge → bounded retries → terminal), right-sized
#: for the task loop.
_MAX_EMPTY_RESPONSE_RETRIES = 2
_EMPTY_RESPONSE_NUDGE = (
    "Your previous response was empty (no content and no tool calls). "
    "Process the conversation above and produce your final answer now."
)
#: Hermes parity: identical-failure warnings ride ON the tool result as
#: a bracketed suffix. Injecting a bare ``user`` message between the
#: tool results of one assistant batch breaks tool-call/result pairing
#: on strict provider wire formats (tool messages must directly follow
#: the assistant tool_calls turn).
_FAILURE_WARN_SUFFIX = (
    "\n\n[Tool loop warning: identical failure; count={count}; tool={tool}. "
    "Change your approach or try a different strategy before calling it again.]"
)
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


def _failure_detail(exc: BaseException) -> str:
    """Redacted ``Class: message`` detail for durable failure records.

    Live-run fix (2026-08-31): the runtime's failure wrappers used to
    record only ``type(exc).__name__`` (e.g. "evidence/postcondition
    failed: RuntimeEvidenceError"), which hid the actual cause an
    operator needed. The message is redacted (the same gateway key that
    once leaked into a public repo proves messages can carry secrets)
    and bounded before it reaches the durable task error.
    """
    from zero.domain.audit import redact_sensitive_text

    detail = str(exc).strip() or "(no detail)"
    return f"{type(exc).__name__}: {redact_sensitive_text(detail)}"[:400]


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
        test_command: tuple[str, ...] | None = None,
        enable_delegation: bool = False,
        approval_gate: Any | None = None,
        metrics: Any | None = None,
        audit_repo: Any | None = None,
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
        # GAP L (round-9 live find): optional AuditRepository so delegate
        # invocations leave the SAME durable `tool.invoke` audit trail as
        # every registry tool. ``None`` (legacy test compositions) skips
        # the audit write without changing behavior.
        self._audit_repo = audit_repo

    def _audit_delegation(
        self,
        *,
        project_id: ProjectId,
        actor_id: UserId,
        execution_id: ExecutionId,
        result: str,
        detail: str,
    ) -> None:
        """Write the durable ``tool.invoke`` audit row for one delegate
        call (GAP L). Failures never affect the delegation outcome."""
        audit_repo = getattr(self, "_audit_repo", None)
        if audit_repo is None:
            return
        try:
            # Bug fix (regression found 2026-08-31): this import named a
            # private symbol that does not exist (`tool_service` exports
            # `now_utc_iso` via zero.app.clock), so EVERY delegation audit
            # write died on ImportError and was swallowed by the broad
            # handler below — delegate calls left ZERO durable trace and
            # the round-9 GAP L regressions failed.
            from zero.app.clock import now_utc_iso
            from zero.domain.audit import AuditEvent, AuditEventId, redact_sensitive_text
            from zero.domain.ids import generate_audit_event_id

            audit_repo.insert(
                AuditEvent(
                    id=AuditEventId(generate_audit_event_id()),
                    project_id=project_id,
                    actor_id=actor_id,
                    source="system",
                    operation="tool.invoke",
                    target_type="tool",
                    target_id=DELEGATE_TOOL_NAME,
                    result=result,  # type: ignore[arg-type]
                    correlation_id=execution_id.value,
                    redacted_summary=(
                        f"Invoked tool {DELEGATE_TOOL_NAME!r} "
                        f"(status={result}, {redact_sensitive_text(detail)[:400]})"
                    ),
                    created_at=now_utc_iso(),
                )
            )
        except Exception as exc:  # noqa: BLE001 - audit loss must not crash delegation
            import logging

            logging.getLogger(__name__).debug(
                "delegation audit write skipped: %s", type(exc).__name__
            )

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

    def agent_type_at_capacity(
        self,
        *,
        project_id: ProjectId,
        task: Task,
        explicit_agent_type_id: AgentTypeId | None = None,
    ) -> bool:
        """True when the task's agent type has no free instance slot.

        Live-run fix (2026-08-31): ``run_ready_tasks`` used to claim and
        then instantly FAIL any task whose agent type was at its
        ``max_concurrent_instances`` limit — one busy worker of a
        max_concurrent_instances=1 type meant every sibling task died
        with "agent type concurrency limit reached" and blocked the
        whole graph. Capacity is now checked BEFORE the claim: an
        at-capacity task is left ``ready`` for a later tick instead of
        being consumed. The atomic lease inside ``run_task`` remains the
        race-safety net for the rare claim-time overlap.
        """
        if self._agent_type_repo is None:
            return False
        try:
            policy = self.resolve_agent_policy(
                project_id=project_id,
                task=task,
                explicit_agent_type_id=explicit_agent_type_id,
            )
        except RuntimeEvidenceError:
            # Policy problems are surfaced by run_task itself; capacity
            # pre-checking must never change that failure mode.
            return False
        if policy.type_id is None:
            return False
        try:
            running = self._agent_type_repo.count_running_instances(policy.type_id)
        except Exception:  # noqa: BLE001 - degraded: let run_task decide
            return False
        return running >= policy.agent_type.max_concurrent_instances

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
        task_event_callback: Any = None,
    ) -> list[RuntimeTaskResult]:
        """Claim and run a bounded snapshot of currently ready tasks.

        ``task_event_callback`` (Hermes live-report parity, gap C) is an
        optional ``callback(event: dict)`` invoked with
        ``task_started`` / ``task_completed`` / ``task_failed`` events
        (``task_id``, ``objective``, optional ``detail``) around each
        run. Callback failures never affect execution outcomes.
        """
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

        def emit_task_event(payload: dict) -> None:
            if task_event_callback is None:
                return
            try:
                task_event_callback(payload)
            except Exception:  # noqa: BLE001 - progress is observability
                _LOGGER.debug("task event dropped", exc_info=True)

        # Per-task fault isolation: one poisoned task must not starve its
        # siblings in the same batch. Failures are logged with bounded
        # context; if nothing succeeded the first error propagates so
        # callers keep their existing single-task failure semantics.
        results: list[RuntimeTaskResult] = []
        errors: list[BaseException] = []
        for task in ready:
            # Live-run fix (2026-08-31): defer at-capacity tasks BEFORE
            # claiming. Claim-then-fail turned a full instance slot into
            # a terminal task failure ("agent type concurrency limit
            # reached"); waiting for a later tick is the correct
            # queueing semantics.
            if self.agent_type_at_capacity(
                project_id=project_id,
                task=task,
                explicit_agent_type_id=agent_type_id,
            ):
                emit_task_event(
                    {
                        "type": "task_deferred",
                        "task_id": task.id.value,
                        "objective": task.objective or "",
                        "detail": "agent type at capacity; task stays ready",
                    }
                )
                continue
            emit_task_event(
                {
                    "type": "task_started",
                    "task_id": task.id.value,
                    "objective": task.objective or "",
                }
            )
            try:
                outcome = self.run_task(
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
                results.append(outcome)
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
                from zero.domain.audit import redact_sensitive_text

                emit_task_event(
                    {
                        "type": "task_failed",
                        "task_id": task.id.value,
                        "objective": task.objective or "",
                        "detail": redact_sensitive_text(
                            str(exc) or type(exc).__name__
                        )[:300],
                    }
                )
            else:
                emit_task_event(
                    {
                        "type": "task_completed",
                        "task_id": task.id.value,
                        "objective": task.objective or "",
                        "detail": (outcome.response.content or "")[:300],
                    }
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
                # Bug fix (real run, 2026-08-28): task worktrees used to
                # branch only from the repository default revision, so a
                # task could never see the files its dependency tasks
                # produced — a "run the tests" task failed because its
                # worktree had no test suite. A task now branches from its
                # SUCCEEDED dependency worktrees' branches (last commit =
                # that task's evidence checkpoint), with clean merges for
                # multiple dependencies. Diamond DAGs resolve through
                # normal git merges; conflicts fail the task with a clear
                # reason instead of silently missing files.
                base_revision, merge_bases = self._dependency_worktree_bases(
                    project_id=project_id,
                    execution_id=execution_id,
                    task_id=task.id,
                    actor_id=actor_id,
                    source=source,
                )
                worktree = self._worktrees.create_worktree(
                    project_id=project_id,
                    repository_id=repository_id,
                    execution_id=execution_id,
                    task_id=task.id,
                    actor_id=actor_id,
                    base_revision=base_revision,
                    source=source,
                )
                for merge_base in merge_bases:
                    self._merge_worktree_base(
                        worktree=worktree,
                        branch=merge_base,
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
                    plan_contract=self._task_prompt_with_retry(task, actor_id=actor_id),
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
                    content=workspace_prompt
                    + self._task_prompt_with_retry(task, actor_id=actor_id),
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
                            plan_contract=self._task_prompt_with_retry(task, actor_id=actor_id),
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
                # GAP 5: a client-facing stream consumer taps the token
                # stream when connected. Real-run fix: stream from the
                # provider even without a consumer. Background tasks
                # previously used non-streaming POSTs, so a long
                # completion (deep sub-agent reviews routinely exceed a
                # gateway's ~100s edge timeout) never returned response
                # headers and died with HTTP 524; both same-provider
                # retry attempts 524'd identically and the task failed.
                # Streaming receives headers immediately, the gateway
                # never times out, and the lease is kept alive by the
                # collect-path heartbeat. The collected CanonicalResponse
                # is identical either way, and every adapter (including
                # the test fake via the base-class default) implements
                # send_request_stream.
                stream=True,
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
            # Live-run fix (2026-08-31): the wrapper used to record only
            # the exception CLASS ("workspace/context setup failed:
            # IntegrityError"), which hid the actual constraint/message
            # an operator needed to diagnose the failure.
            self._worker.fail_task(
                execution_id=execution_id,
                project_id=project_id,
                task_id=task.id,
                attempt_id=attempt.id,
                error_message=(
                    "workspace/context setup failed: "
                    f"{_failure_detail(exc)}"
                )[:500],
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
                            plan_contract=self._task_prompt_with_retry(task, actor_id=actor_id),
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
                error_message=(
                    f"runtime execution failed: {_failure_detail(exc)}"
                )[:500],
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
            # Live-run fix (2026-08-31): "evidence/postcondition failed:
            # RuntimeEvidenceError" carried no cause — the durable record
            # now includes the redacted exception message so the failure
            # is diagnosable from the task error alone.
            self._worker.fail_task(
                execution_id=execution_id,
                project_id=project_id,
                task_id=task.id,
                attempt_id=attempt.id,
                error_message=(
                    f"evidence/postcondition failed: {_failure_detail(exc)}"
                )[:500],
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

    def _task_prompt_with_retry(self, task: Task, *, actor_id) -> str:
        """Task prompt plus retry context when prior attempts failed.

        Live-run gap B13 (2026-08-31): retries re-ran with the IDENTICAL
        prompt, so the agent could not correct course — the live
        "create the test module" agent repeatedly produced no file
        changes and the evidence gate failed it with "required diff
        evidence contains no file change" with no way to learn why.
        The last failed attempt's redacted error is now part of the
        prompt so bounded retries are informative, not blind.
        """
        prompt = self._task_prompt(task)
        dep_context = self._dependency_output_context(task, actor_id=actor_id)
        base = prompt + dep_context
        if self._worker is None:
            return base
        try:
            attempts = self._worker.list_attempts(
                task.id,
                project_id=task.project_id,
                actor_id=actor_id,
                source="system",
            )
        except Exception:  # noqa: BLE001 - prompt context is best-effort
            return base
        last_failed = max(
            (a for a in attempts if a.state == "failed"),
            key=lambda a: a.attempt_number,
            default=None,
        )
        if last_failed is None or not last_failed.error_message:
            return base
        reason = last_failed.error_message.strip()
        if len(reason) > 500:
            reason = reason[:500] + "…"
        guidance = (
            "Correct the previous approach. If the failure says no file"
            " change was recorded, you must actually create or modify the"
            " required files with the workspace tools before finalizing."
        )
        # Live-run B13 refinement (2026-08-31): pattern-specific,
        # actionable guidance beats generic "correct course" — the suite
        # agent kept re-running pytest against an EMPTY repository
        # without ever creating the missing tests.
        lowered = reason.lower()
        if "no tests ran" in lowered or "exit=5" in lowered:
            guidance = (
                "The test command collected NO tests because the repository"
                " has no test files yet. You MUST create the required test"
                " file(s) yourself with the write_file tool (e.g."
                " tests/test_greeting.py covering the task objective), then"
                " run the suite via run_command and make it pass before"
                " finalizing. Do not just report that no tests exist."
            )
        elif "required diff evidence contains no file change" in lowered:
            guidance = (
                "Your previous attempt produced zero file changes. You MUST"
                " create or modify the required files with the write_file"
                " tool before finalizing — a report without changes cannot"
                " satisfy diff evidence."
            )
        return (
            base
            + f"\n\nPrevious attempt #{last_failed.attempt_number} FAILED:\n"
            + f"{reason}\n"
            + guidance
        )

    def _dependency_output_context(self, task: Task, *, actor_id) -> str:
        """M18 (2026-08-31, mega-run live-found): completed dependency
        tasks whose evidence is ``provider_response`` produce TEXT
        artifacts (API contracts, decisions, summaries) that live ONLY in
        the database. Downstream objectives routinely reference them
        ("the documented rules") — but the agent had no access: it
        searched the workspace, found nothing, and honestly reported it
        could not proceed (text-only answer → failed diff gate, attempt
        after attempt). Completed dependencies' text outputs are now
        injected into the task prompt, bounded per dependency and in
        total. Failure to build the context is silent — the prompt falls
        back to the historical shape.
        """
        if self._worker is None or self._artifacts is None:
            return ""
        try:
            deps = self._worker.list_dependencies(
                task.execution_id,
                project_id=task.project_id,
                actor_id=actor_id,
                source="system",
            )
            upstream_ids = [
                d.depends_on_task_id for d in deps if d.task_id == task.id
            ]
            if not upstream_ids:
                return ""
            tasks_by_id = {
                t.id: t
                for t in self._worker.list_tasks(
                    task.execution_id,
                    project_id=task.project_id,
                    actor_id=actor_id,
                    source="system",
                )
            }
        except Exception:  # noqa: BLE001 - context is best-effort
            return ""
        sections: list[str] = []
        total = 0
        max_per_dep = 1800
        max_total = 6000
        for upstream_id in upstream_ids:
            dep = tasks_by_id.get(upstream_id)
            if dep is None or dep.state != "completed":
                continue
            text = self._dependency_report_text(dep, actor_id=actor_id)
            if not text:
                continue
            remaining = max_total - total
            if remaining <= 0:
                break
            if len(text) > max_per_dep:
                text = text[:max_per_dep] + "…"
            text = text[:remaining]
            total += len(text)
            objective = (dep.objective or "").strip()
            label = objective[:110] + ("…" if len(objective) > 110 else "")
            sections.append(
                f"--- Output of dependency task ({label}):\n{text}"
            )
        if not sections:
            return ""
        return (
            "\n\nOutputs produced by your completed dependency tasks"
            " (normative wherever your objective references them):\n"
            + "\n".join(sections)
            + "\nUse them, but DO THE WORK in the workspace with the tools;"
            " a text-only answer fails the diff-evidence gate."
        )

    def _dependency_report_text(self, dep: Task, *, actor_id) -> str:
        """Extract bounded text output(s) of a completed dependency task."""
        parts: list[str] = []
        for artifact_id in dep.completion_evidence or ():
            try:
                artifact = self._artifacts.get_artifact(
                    project_id=dep.project_id,
                    artifact_id=ArtifactId(artifact_id),
                    actor_id=actor_id,
                    source="system",
                )
            except Exception:  # noqa: BLE001 - context is best-effort
                continue
            content = getattr(artifact, "content", None)
            if not content:
                continue
            text = self._provider_response_text(content)
            if text:
                parts.append(text)
        return "\n".join(parts)[:4000]

    @staticmethod
    def _provider_response_text(content: Any) -> str:
        """provider_response artifacts store a JSON envelope; extract the
        model's text content when parseable, else the raw string."""
        if not isinstance(content, str):
            return ""
        try:
            payload = json.loads(content)
        except Exception:
            return content
        if not isinstance(payload, dict):
            return ""
        response = payload.get("response")
        if isinstance(response, dict):
            inner = response.get("content")
            if isinstance(inner, str) and inner.strip():
                return inner
        for key in ("text", "content"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return ""

    @staticmethod
    def _task_prompt(task: Task) -> str:
        scope = ", ".join(task.permitted_scope) or "(none declared)"
        evidence = ", ".join(task.expected_evidence) or "(none declared)"
        lines = [
            f"Objective: {task.objective}",
            f"Permitted scope: {scope}",
            f"Required evidence: {evidence}",
        ]
        # M17 (2026-08-31, mega-run live-found): the historical single
        # closing line — "Return a concise completion report..." — read as
        # the FINAL instruction. For read-heavy coding tasks (objectives
        # that reference documents produced by dependency tasks) the model
        # obeyed it literally: it read the referenced material and returned
        # a text REPORT without ever calling write_file, failing the diff
        # gate attempt after attempt. Diff-evidence tasks are HANDS-ON:
        # the prompt must say so explicitly, in the imperative, AFTER the
        # objective — while keeping the honesty clause.
        if "diff" in [e.strip().lower() for e in (task.expected_evidence or ())]:
            lines.append(
                "This is a hands-on coding task: use the workspace tools "
                "(read_file to inspect, write_file to create/modify files, "
                "run_command to verify) to ACTUALLY implement the objective "
                "in the current workspace before you finish. A text-only "
                "answer with no file changes FAILS the diff-evidence gate. "
                "Do the work first; only then return a concise completion "
                "report, and do not claim actions that you did not perform."
            )
        else:
            lines.append(
                "Return a concise completion report and do not claim actions "
                "that you did not perform."
            )
        return "\n".join(lines)

    def _dependency_worktree_bases(
        self,
        *,
        project_id: ProjectId,
        execution_id: ExecutionId,
        task_id: TaskId,
        actor_id: UserId,
        source: AuditSource,
    ) -> tuple[str | None, tuple[str, ...]]:
        """Resolve the git base for this task's worktree from dependencies.

        Returns ``(base_revision, extra_branches_to_merge)``. Each
        succeeded dependency worktree branch carries that task's
        committed evidence checkpoint (see
        ``WorktreeService.complete_worktree``). One dependency becomes
        the branch base directly; several dependencies use the first as
        the base and queue the rest for clean merges right after
        creation. No usable dependency state falls back to the
        repository default (``None``), preserving historical behavior.
        """
        assert self._worktrees is not None and self._worker is not None
        try:
            dependencies = self._worker.list_dependencies(
                execution_id=execution_id,
                project_id=project_id,
                actor_id=actor_id,
                source=source,
            )
        except Exception as dep_exc:  # noqa: BLE001 - degraded: default base
            _LOGGER.debug(
                "dependency base resolution unavailable for task %s: %s",
                task_id.value,
                type(dep_exc).__name__,
            )
            return None, ()
        mine = [
            dep
            for dep in dependencies
            if dep.task_id == task_id and dep.depends_on_task_id != task_id
        ]
        if not mine:
            return None, ()
        # list_worktrees_for_execution returns every worktree of the
        # execution regardless of state; get_worktree_for_task only finds
        # ACTIVE ones, which by definition never includes a succeeded
        # (completed) dependency worktree.
        try:
            execution_worktrees = self._worktrees.list_worktrees_for_execution(
                project_id,
                execution_id,
                actor_id=actor_id,
                source=source,
            )
        except Exception as wt_exc:  # noqa: BLE001 - degraded: default base
            _LOGGER.debug(
                "execution worktree listing failed for %s: %s",
                execution_id.value,
                type(wt_exc).__name__,
            )
            return None, ()
        branches: list[str] = []
        for dep in mine:
            dep_branches = [
                wt.branch_name
                for wt in execution_worktrees
                if wt.task_id == dep.depends_on_task_id
                and wt.state == "succeeded"
                and wt.branch_name
            ]
            if not dep_branches:
                continue
            branch = dep_branches[-1]  # latest worktree of that task
            if branch not in branches:
                branches.append(branch)
        if not branches:
            return None, ()
        return branches[0], tuple(branches[1:])

    def _merge_worktree_base(self, *, worktree, branch: str) -> None:
        """Merge one dependency branch into a freshly created worktree."""
        assert self._worktrees is not None
        merge_argv = (
            "git",
            "-c", "user.name=Zero Runtime",
            "-c", "user.email=zero@internal",
            "merge",
            "--no-edit",
            "-q",
            branch,
        )
        import subprocess as _sp

        try:
            proc = _sp.run(
                list(merge_argv),
                cwd=str(worktree.worktree_path),
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except (OSError, _sp.TimeoutExpired) as merge_exc:
            raise RuntimeEvidenceError(
                f"dependency branch {branch!r} could not be merged: {merge_exc}"
            ) from merge_exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[:300]
            raise RuntimeEvidenceError(
                f"dependency branch {branch!r} merge conflict — the "
                f"decomposition created overlapping parallel edits: {detail}"
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
            self._audit_delegation(
                project_id=project_id,
                actor_id=actor_id,
                execution_id=execution_id,
                result="error",
                detail="arguments were not valid JSON",
            )
            return _error("delegate arguments were not valid JSON")
        if not isinstance(input_data, dict):
            self._audit_delegation(
                project_id=project_id,
                actor_id=actor_id,
                execution_id=execution_id,
                result="error",
                detail="arguments must decode to a JSON object",
            )
            return _error("delegate arguments must be a JSON object")
        objective = str(input_data.get("objective") or "").strip()
        if not objective or len(objective) > 8192:
            self._audit_delegation(
                project_id=project_id,
                actor_id=actor_id,
                execution_id=execution_id,
                result="error",
                detail="requires a non-empty objective (<=8192 chars)",
            )
            return _error("delegate requires a non-empty objective")
        depth = current_delegation_depth()
        if depth >= MAX_DELEGATION_DEPTH:
            self._audit_delegation(
                project_id=project_id,
                actor_id=actor_id,
                execution_id=execution_id,
                result="error",
                detail=f"delegation depth limit reached ({MAX_DELEGATION_DEPTH})",
            )
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
                # Real-run fix: stream this request too. The non-streaming
                # sub-agent POST was the exact r5 failure — a deep review
                # exceeded the gateway's edge timeout before response
                # headers arrived (HTTP 524) and exhausted both retry
                # attempts with identical 524s. Streaming returns headers
                # immediately; the collect path reassembles the same
                # CanonicalResponse.
                stream=True,
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
                            # Hermes parity (audit 2026-08-28): the sub-agent
                            # needs the failure REASON to self-correct; the
                            # bare exception class hid e.g. which binary the
                            # command policy refused. Bounded + redacted.
                            from zero.domain.audit import redact_sensitive_text

                            detail = redact_sensitive_text(
                                str(exc) or type(exc).__name__
                            )[:512]
                            payload_text = f"error executing tool {call.tool_name!r}: {detail}"
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
        self._audit_delegation(
            project_id=project_id,
            actor_id=actor_id,
            execution_id=execution_id,
            result="success",
            detail=(
                f"objective={objective[:160]} depth={depth + 1} "
                f"tools={allowed} model={child_model or model_name}"
            ),
        )
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
        if not response.tool_calls and (response.content or "").strip():
            return response, provider_request_id, messages
        if self._tools is None and response.tool_calls:
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

        # ---- empty-response ladder (Hermes parity, audit 2026-08-28) ----
        # A response with neither content nor tool calls used to complete
        # the task silently with an empty deliverable (its transcript
        # evidence dutifully recorded "content": """). Bounded nudge
        # retries now ask the model to actually produce its answer; if it
        # still returns nothing, the empty response stands (degraded,
        # not fatal — evidence gates still apply).
        empty_retries = 0
        while (
            not current_response.tool_calls
            and not (current_response.content or "").strip()
            and empty_retries < _MAX_EMPTY_RESPONSE_RETRIES
        ):
            empty_retries += 1
            _renew_lease()
            if self._metrics is not None:
                self._metrics.increment(
                    "agent_runtime_empty_response_retries",
                    project_id=task.project_id.value,
                    result="nudged",
                )
            messages.append(CanonicalMessage(role="assistant", content="(empty)"))
            messages.append(CanonicalMessage(role="user", content=_EMPTY_RESPONSE_NUDGE))
            _LOGGER.warning(
                "empty response for task %s (retry %d/%d); nudging the model",
                task.id.value,
                empty_retries,
                _MAX_EMPTY_RESPONSE_RETRIES,
            )
            empty_request = replace(request, messages=tuple(messages))
            empty_provider_request, current_response = self._providers.send_request_with_fallback(
                project_id=task.project_id,
                actor_id=actor_id,
                execution_id=execution_id,
                request=empty_request,
                cancel_event=cancel_event,
                source=source,
                agent_scope=agent_scope,
                stream_observer=stream_observer,
            )
            current_request_id = empty_provider_request.id
        if not current_response.tool_calls:
            # Text-protocol fallback (live-run 2026-08-30): some gateways
            # silently strip the native ``tools`` parameter — the model
            # then hallucinates tool-like text instead of calling tools.
            # When the probe confirms native tools are unavailable and
            # the task declares tools, run the SAME bounded loop through
            # the text protocol so real tool work stays possible.
            probe = getattr(self._providers, "tool_call_support", None)
            if tool_names and callable(probe) and probe(
                request.provider, request.model_name
            ) is False:
                return self._run_text_protocol_tool_rounds(
                    task=task,
                    attempt=attempt,
                    actor_id=actor_id,
                    execution_id=execution_id,
                    project_id=project_id,
                    request=request,
                    response=current_response,
                    provider_request_id=current_request_id,
                    agent_scope=agent_scope,
                    tool_names=tool_names,
                    max_tool_rounds=max_tool_rounds,
                    cancel_event=cancel_event,
                    lease_owner=lease_owner,
                    lease_duration_seconds=lease_duration_seconds,
                    source=source,
                    stream_observer=stream_observer,
                    messages=messages,
                )
            return current_response, current_request_id, messages

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
                try:
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
                except ToolInvocationDeniedError:
                    # Denials are approvals-adjacent: feed back, never abort.
                    if self._metrics is not None:
                        self._metrics.increment(
                            "agent_runtime_tool_call_defects",
                            project_id=task.project_id.value,
                            result="tool_denied",
                        )
                    messages.append(
                        _synthetic_tool_error(
                            call.tool_call_id,
                            {
                                "error": "tool_denied",
                                "tool": call.tool_name,
                                "hint": (
                                    "This invocation was denied by policy. Use a "
                                    "permitted alternative or continue without it."
                                ),
                            },
                        )
                    )
                    continue
                except ToolError as tool_exc:
                    # Bug fix (real run, 2026-08-28): a raised ToolError —
                    # e.g. run_command refusing a non-allowlisted binary —
                    # used to propagate and fail the WHOLE task, even
                    # though every other defect class (bad arguments,
                    # undeclared tool, approval denial) is recovered by
                    # feeding the model a structured error. Hermes parity:
                    # the model gets the failure as its tool result and can
                    # change approach next round; the identical-failure
                    # breaker below still bounds repeated bad retries.
                    # Fail-closed policy is unchanged: the command did not
                    # run.
                    if self._metrics is not None:
                        self._metrics.increment(
                            "agent_runtime_tool_call_defects",
                            project_id=task.project_id.value,
                            result="tool_error_recovered",
                        )
                    detail = str(tool_exc) or type(tool_exc).__name__
                    messages.append(
                        _synthetic_tool_error(
                            call.tool_call_id,
                            {
                                "error": "tool_execution_failed",
                                "tool": call.tool_name,
                                "detail": detail,
                                "hint": (
                                    "The tool handler refused this call and nothing "
                                    "was executed. Adjust the arguments to satisfy "
                                    "the tool's declared constraints, or use another "
                                    "declared tool."
                                ),
                            },
                        )
                    )
                    signature = (call.tool_name, detail[:120])
                    count = failure_signatures.get(signature, 0) + 1
                    failure_signatures[signature] = count
                    if count >= _FAILURE_ABORT_THRESHOLD:
                        breaker_tripped = True
                        abort_after_batch = True
                    continue
                # ---- identical-failure loop breaker --------------------
                # Hermes parity (audit 2026-08-28): the warning rides ON
                # the tool result as a bracketed suffix instead of being
                # injected as a bare user message between the batch's
                # tool results (tool messages must directly follow the
                # assistant tool_calls turn on strict wire formats).
                status = getattr(result, "status", "success")
                if status in ("success", "unknown"):
                    result_content = result.model_facing
                else:
                    signature = (
                        call.tool_name,
                        (getattr(result, "error", None) or result.model_facing or "")[:120],
                    )
                    count = failure_signatures.get(signature, 0) + 1
                    failure_signatures[signature] = count
                    result_content = result.model_facing
                    if count >= _FAILURE_ABORT_THRESHOLD:
                        breaker_tripped = True
                        abort_after_batch = True
                    elif count == _FAILURE_WARN_THRESHOLD:
                        result_content += _FAILURE_WARN_SUFFIX.format(
                            count=count, tool=call.tool_name
                        )
                messages.append(
                    CanonicalMessage(
                        role="tool",
                        content=result_content,
                        tool_call_id=call.tool_call_id,
                    )
                )
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

    def _run_text_protocol_tool_rounds(
        self,
        *,
        task: Task,
        attempt: TaskAttempt,
        actor_id: UserId,
        execution_id: ExecutionId,
        project_id: ProjectId,
        request: CanonicalRequest,
        response: CanonicalResponse,
        provider_request_id: ProviderRequestId | None,
        agent_scope: AgentScope,
        tool_names: tuple[str, ...],
        max_tool_rounds: int,
        cancel_event: Any,
        lease_owner: str,
        lease_duration_seconds: int,
        source: AuditSource,
        stream_observer: Any,
        messages: list[CanonicalMessage],
    ) -> tuple[CanonicalResponse, ProviderRequestId | None, list[CanonicalMessage]]:
        """Bounded tool loop through the TEXT protocol (no native tools).

        Same invariants as the native loop: lease renewed per round,
        undeclared tools rejected, the approval gate consulted, tool
        failures fed back instead of aborting, and identical-failure
        breakers. Tool results ride user-role ``tool_result`` blocks
        because the model (gateway) never sees the native tool role.
        """
        from zero.app.text_tool_protocol import (
            parse_tool_call,
            render_text_tool_instructions,
            render_tool_error_message,
            render_tool_result_message,
            strip_tool_call_markers,
        )

        declarations = [
            {
                "name": d.name,
                "description": d.description,
                "parameters": d.normalized_parameters(),
            }
            for d in request.tools
            if hasattr(d, "name")
        ]
        protocol_system = (request.system_message or "").rstrip()
        protocol_system = (
            protocol_system
            + "\n\n"
            + render_text_tool_instructions(declarations)
        ).strip()
        requested_names = set(tool_names)
        current_response = response
        current_request_id = provider_request_id
        failure_signatures: dict[str, int] = {}

        def _observe(payload: dict) -> None:
            if stream_observer is not None:
                try:
                    stream_observer(payload)
                except Exception:  # noqa: BLE001 - progress is observability
                    pass

        for _round in range(max_tool_rounds):
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
                raise RuntimeToolError(
                    f"attempt lease expired during tool round: {lease_exc}"
                ) from lease_exc
            if cancel_event is not None and cancel_event.is_set():
                raise ProviderCancelledError("task cancelled during tool rounds")

            call = parse_tool_call(current_response.content)
            if call is None:
                # No further tool calls: this response IS the deliverable.
                clean = strip_tool_call_markers(current_response.content)
                if clean != current_response.content:
                    current_response = replace(current_response, content=clean)
                return current_response, current_request_id, messages

            tool_name = call.get("tool")
            _observe({"type": "text_reset"})
            _observe(
                {
                    "type": "tool_call",
                    "name": tool_name or "unknown",
                    "arguments": call.get("arguments") or {},
                }
            )
            raw_text = current_response.content or ""

            if tool_name is None:
                result_block = render_tool_error_message(
                    None, call.get("error") or "malformed tool call"
                )
                status = "invalid_arguments"
            elif tool_name == DELEGATE_TOOL_NAME and self._enable_delegation:
                payload = self._execute_delegation(
                    call_arguments=json.dumps(call.get("arguments") or {}),
                    parent_allowed_tools=tool_names,
                    execution_id=execution_id,
                    project_id=project_id,
                    actor_id=actor_id,
                    provider=request.provider,
                    model_name=request.model_name,
                )
                result_block = render_tool_result_message(
                    str(tool_name), json.dumps(payload, ensure_ascii=False)
                )
                status = "ok"
            elif tool_name not in requested_names:
                if self._metrics is not None:
                    self._metrics.increment(
                        "agent_runtime_tool_call_defects",
                        project_id=task.project_id.value,
                        result="undeclared_tool",
                    )
                result_block = render_tool_error_message(
                    str(tool_name),
                    f"tool {tool_name!r} is not declared for this task; "
                    f"declared: {sorted(requested_names)}",
                )
                status = "undeclared_tool"
            else:
                # Approval gate (same contract as the native loop).
                arguments = dict(call.get("arguments") or {})
                if self._approval_gate is not None:
                    verdict = self._approval_gate.evaluate(
                        project_id=task.project_id.value,
                        execution_id=execution_id.value,
                        tool_name=str(tool_name),
                        input_data=arguments,
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
                        hint = (
                            "A human must approve this tool call before it runs. "
                            "Continue with other work or finalize your answer."
                            if verdict.state == "pending"
                            else "This invocation was denied by policy."
                        )
                        result_block = render_tool_error_message(str(tool_name), hint)
                        status = f"approval_{verdict.state}"
                    else:
                        result_block = None
                else:
                    result_block = None
                if result_block is None:
                    try:
                        result = self._tools.invoke(  # type: ignore[union-attr]
                            project_id=task.project_id,
                            actor_id=actor_id,
                            agent_scope=agent_scope,
                            tool_name=str(tool_name),
                            input_data=arguments,
                            execution_id=execution_id.value,
                            task_id=task.id.value,
                            source=source,
                        )
                        result_block = render_tool_result_message(
                            str(tool_name), result.model_facing
                        )
                        status = result.status
                    except ToolInvocationDeniedError as denied:
                        result_block = render_tool_error_message(
                            str(tool_name),
                            f"This invocation was denied by policy: {denied}",
                        )
                        status = "tool_denied"
                    except Exception as tool_exc:  # noqa: BLE001 - failures feed back
                        from zero.domain.audit import redact_sensitive_text

                        result_block = render_tool_error_message(
                            str(tool_name),
                            redact_sensitive_text(
                                f"{type(tool_exc).__name__}: {tool_exc}"
                            )[:512],
                        )
                        status = "error"

            # Identical-failure breaker (Hermes parity): the same tool
            # failing the same way repeatedly must not burn the budget.
            signature = f"{tool_name}:{status}"
            failure_signatures[signature] = failure_signatures.get(signature, 0) + 1
            if failure_signatures[signature] >= _FAILURE_ABORT_THRESHOLD:
                raise RuntimeToolError(
                    f"tool {tool_name!r} failed identically "
                    f"{failure_signatures[signature]} times ({status}); loop aborted"
                )

            messages.append(
                CanonicalMessage(
                    role="assistant",
                    content=strip_tool_call_markers(raw_text),
                )
            )
            messages.append(CanonicalMessage(role="user", content=result_block))
            _observe(
                {
                    "type": "tool_result",
                    "name": str(tool_name or "tool"),
                    "ok": status == "ok",
                }
            )

            next_request = replace(
                request,
                messages=tuple(messages),
                tools=(),
                system_message=protocol_system,
            )
            next_provider_request, current_response = (
                self._providers.send_request_with_fallback(
                    project_id=task.project_id,
                    actor_id=actor_id,
                    execution_id=execution_id,
                    request=next_request,
                    cancel_event=cancel_event,
                    source=source,
                    agent_scope=agent_scope,
                    stream_observer=stream_observer,
                )
            )
            current_request_id = next_provider_request.id

        # Round budget exhausted: one final toolless summary request.
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
                stream_observer=stream_observer,
            )
        except Exception as nudge_exc:  # noqa: BLE001
            _LOGGER.warning(
                "text-protocol final summary request failed for task %s: %s",
                task.id.value,
                type(nudge_exc).__name__,
            )
        else:
            return (
                replace(
                    nudge_response,
                    content=strip_tool_call_markers(nudge_response.content or ""),
                ),
                nudge_provider_request.id,
                messages,
            )
        raise RuntimeToolError(
            f"text-protocol tool loop exceeded {max_tool_rounds} rounds"
        )

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
                raise RuntimeEvidenceError(
                    "required diff evidence contains no file change"
                )
            if (
                _TASK_MADE_NO_CHANGES_MARKER in diff.content
                and _objective_expects_changes(task.objective)
            ):
                # B10 refinement (live run 2026-08-31): the cumulative
                # fallback exists for AGGREGATION tasks ("capture the
                # final diff"), whose incremental work is legitimately
                # empty because dependency branches carry the change set.
                # But a GENERATIVE objective (create/write/fix/...) that
                # recorded no change OF ITS OWN must not pass on its
                # dependencies' work — the live "create the test module"
                # task completed without creating any file because the
                # cumulative diff contained greeting.py.
                raise RuntimeEvidenceError(
                    "task objective expects file changes but the attempt "
                    "recorded none of its own on top of its dependency "
                    "branches (diff evidence contains only the execution's "
                    "cumulative change set)"
                )
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
                    "test evidence was requested but no evidence test command is "
                    "configured; set ZERO_EVIDENCE_TEST_COMMAND (e.g. "
                    "'python3 -m unittest discover -s tests -v') and make sure the "
                    "binary is allowlisted by ZERO_WORKTREE_ALLOWED_COMMANDS"
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
                # B13/B3 (live run 2026-08-31): "exit=5" alone told the
                # retrying agent nothing — it kept re-running pytest
                # without ever creating the missing tests because the
                # durable error never said "no tests ran". Attach a
                # bounded output excerpt so the failure is actionable.
                excerpt = ""
                by_kind_now = {a.kind: a.content for a in command_artifacts}
                for kind in ("stderr", "stdout"):
                    text = (by_kind_now.get(kind) or "").strip()
                    if text:
                        excerpt = text[-500:]
                        break
                suffix = f"\ncommand output tail:\n{excerpt}" if excerpt else ""
                raise RuntimeEvidenceError(
                    f"configured test command did not pass "
                    f"(state={command_run.state}, exit={command_run.exit_code})"
                    f"{suffix}"
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
