"""Tool service — registry, capability grants, and invocation lifecycle.

Per ``zero-tool-capability-runtime`` SKILL.md:

- A Zero tool is a server-owned capability with a name, bounded input,
  authorization policy, execution limits, result policy, and audit
  identity.
- Tool schemas are trust boundaries: model output is untrusted input.
  Validation covers type, shape, length, allowed values, project
  ownership, path normalization, and domain preconditions before side
  effects begin.
- Tool choice and tool permission are separate: a model may reason
  that a tool is relevant, but the control plane still decides
  whether the project and agent type may invoke it.
- A denied tool does not become available through a different
  interface or child agent. Delegation can narrow authority but
  cannot invent authority.

Invocation lifecycle:

1. Resolve the tool by name (registry lookup).
2. Validate the input against the tool's input schema (trust
   boundary).
3. Resolve the capability grant for (project_id, tool_id, agent_scope).
   If no grant exists, deny with a typed error.
4. Invoke the server-side handler. The handler receives the validated
   input and a :class:`ToolContext` carrying project scope, actor,
   and any capability handles (e.g. secret references). The handler
   MUST NOT receive raw secrets in its arguments; secrets are
   resolved by the handler through the capability boundary.
5. Validate the handler's output against the tool's output schema.
6. Construct a :class:`ToolResult` with a bounded, redacted,
   model-facing rendering.
7. Record an audit event with the operation, target, result, and
   correlation ID — never the raw input or output.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import jsonschema

from zero.app.authorization_service import AuthorizationService
from zero.app.tool_runner import (
    IsolatedToolRunner,
    ToolRunnerHandlerError,
    ToolRunnerOutputLimit,
    ToolRunnerTimeout,
)
from zero.domain.audit import AuditEvent, AuditEventId, AuditSource, redact_sensitive_text
from zero.domain.execution import TaskId
from zero.domain.identity import ProjectId, UserId
from zero.domain.ids import generate_audit_event_id, generate_correlation_id
from zero.domain.tools import (
    AgentScope,
    Tool,
    ToolError,
    ToolGrant,
    ToolGrantNotFoundError,
    ToolId,
    ToolInputValidationError,
    ToolInvocationDeniedError,
    ToolNotFoundError,
    ToolOutputValidationError,
    ToolResult,
    ToolResultStatus,
    ToolTimeoutError,
)
from zero.persistence.repositories.audit_repository import AuditRepository
from zero.persistence.repositories.tool_repository import ToolRepository


def _now_utc_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


#: Hermes parity (audit 2026-08-28): tool handler failures surface the
#: underlying reason to the model — bounded and secret-redacted, in the
#: reference shape ``Error executing tool '{name}': {reason}``.
_HANDLER_ERROR_DETAIL_LIMIT = 512


def _handler_failure_message(tool_name: str, exc: BaseException | None) -> str:
    """Render a handler failure with its bounded, redacted cause."""
    if exc is None:
        return f"Tool {tool_name!r} handler failed"
    from zero.domain.audit import redact_sensitive_text

    detail = redact_sensitive_text(str(exc) or type(exc).__name__)
    return (
        f"Error executing tool {tool_name!r}: "
        f"{type(exc).__name__}: {detail[:_HANDLER_ERROR_DETAIL_LIMIT]}"
    )


# ----------------------------------------------------------------------
# Tool context and handler protocol
# ----------------------------------------------------------------------


class ToolContext:
    """Runtime context passed to a tool handler.

    The context carries the project scope, actor, and any capability
    handles the handler may use (e.g. secret references). The handler
    MUST NOT receive raw secrets in its arguments; secrets are
    resolved through the :class:`SecretService` referenced by the
    context.

    Per ``zero-tool-capability-runtime`` §"Secrets resolve at the last
    responsible moment": the handler resolves the credential
    immediately before the external call and excludes it from request
    summaries, errors, logs, and artifacts.
    """

    def __init__(
        self,
        *,
        project_id: ProjectId,
        actor_id: UserId,
        agent_scope: AgentScope,
        execution_id: str | None = None,
        task_id: str | None = None,
        correlation_id: str | None = None,
        secret_service: Any | None = None,
    ) -> None:
        self.project_id = project_id
        self.actor_id = actor_id
        self.agent_scope = agent_scope
        self.execution_id = execution_id
        self.task_id = task_id
        self.correlation_id = correlation_id or generate_correlation_id()
        self.secret_service = secret_service

    def __getstate__(self) -> dict[str, Any]:
        """Prepare the context for a process-boundary crossing.

        Under ``spawn`` (non-POSIX platforms) the context must survive
        pickling. A live :class:`SecretService` holds database
        connections and key material and cannot be inherited by a
        spawned child, so it is dropped in transit: handlers running in
        an isolated spawned process resolve no secrets. ``fork``
        children share the parent image and are unaffected.
        """
        state = dict(self.__dict__)
        if state.get("secret_service") is not None and not hasattr(
            state["secret_service"], "__reduce__"
        ):
            state["secret_service"] = None
            return state
        try:
            import pickle

            pickle.dumps(state["secret_service"])
        except (pickle.PickleError, TypeError, AttributeError):
            state["secret_service"] = None
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.__dict__.update(state)


ToolHandler = Callable[[dict[str, Any], ToolContext], dict[str, Any]]
"""A tool handler is a callable that takes validated input and a
context, and returns a dict matching the tool's output schema.

Handlers MUST NOT:
- raise exceptions for expected domain failures (return a dict with
  an ``error`` field instead, or use a typed ToolError);
- include secrets, raw credentials, or unbounded content in the
  output;
- perform side effects outside the project scope of the context.
"""


# ----------------------------------------------------------------------
# Built-in test tool: echo
# ----------------------------------------------------------------------

#: Upper bound on stdout/stderr text surfaced to the model per
#: ``run_command`` invocation (real-run fix, 2026-08-28: the model used
#: to receive only artifact ids and could not observe command output).
_COMMAND_OUTPUT_CHAR_LIMIT = 8000

#: Upper bound on the model-facing rendering of ANY tool result.
#: Real-run fix (2026-08-28): the historical hard cap of 500 characters
#: made a coding agent effectively blind — read_file returned ~450 chars
#: of a source file and command output was invisible, so the model could
#: not write correct tests or react to failures (its own honest reports
#: documented exactly this blocker). 20k chars ≈ 5k tokens is still a
#: bounded, context-safe render while carrying real file content and
#: bounded test output.
_MODEL_FACING_CHAR_LIMIT = 20000


ECHO_TOOL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "message": {
            "type": "string",
            "minLength": 1,
            "maxLength": 1000,
            "description": "The message to echo back.",
        }
    },
    "required": ["message"],
    "additionalProperties": False,
}

ECHO_TOOL_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "echoed": {
            "type": "string",
            "description": "The echoed message.",
        },
        "length": {
            "type": "integer",
            "minimum": 0,
            "description": "The length of the echoed message in characters.",
        },
    },
    "required": ["echoed", "length"],
    "additionalProperties": False,
}


def echo_handler(input_data: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """A harmless deterministic tool that echoes its input.

    Per ``zero-tool-capability-runtime`` §"Good and bad first slices":
    the first tool slice proves the trust boundary. The echo tool is
    the smallest possible tool that exercises:
    - input validation (the schema enforces ``message`` is a non-empty
      string of bounded length);
    - capability grant resolution (the project + scope must have a
      grant);
    - output validation (the handler must return ``echoed`` and
      ``length``);
    - audit (an event is recorded for every invocation).
    """
    message = input_data["message"]
    return {
        "echoed": message,
        "length": len(message),
    }


# ----------------------------------------------------------------------
# Tool service
# ----------------------------------------------------------------------


class ToolService:
    """Application operations for tool registration and invocation.

    The service is the only place where tools are invoked. HTTP
    handlers, future adapters, and the agent runtime all call
    :meth:`invoke` rather than touching the handler directly.
    """

    def __init__(
        self,
        tool_repo: ToolRepository,
        audit_repo: AuditRepository,
        authorization: AuthorizationService,
        *,
        runner: IsolatedToolRunner | None = None,
        default_timeout_seconds: float = 30.0,
        max_grant_timeout_seconds: float = 300.0,
        max_output_bytes: int = 64 * 1024,
        metrics: Any | None = None,
    ) -> None:
        self._tool_repo = tool_repo
        self._audit_repo = audit_repo
        self._authorization = authorization
        self._metrics = metrics
        if max_grant_timeout_seconds <= 0:
            raise ValueError("max_grant_timeout_seconds must be positive")
        self._max_grant_timeout_seconds = max_grant_timeout_seconds
        self._runner = runner or IsolatedToolRunner(
            default_timeout_seconds=default_timeout_seconds,
            max_output_bytes=max_output_bytes,
        )
        # The handler registry maps handler_key -> callable. This is
        # server-side only; never sent to models.
        self._handlers: dict[str, ToolHandler] = {}
        self._inline_handler_keys: set[str] = set()
        self._register_builtin_handlers()

    def _register_builtin_handlers(self) -> None:
        self._handlers["echo"] = echo_handler

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_tool(
        self,
        *,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        output_schema: dict[str, Any],
        handler_key: str,
        handler: ToolHandler | None = None,
        inline: bool = False,
    ) -> Tool:
        """Register a new tool in the registry.

        If ``handler`` is provided, it is registered for ``handler_key``
        in the in-process handler map. Built-in handlers (like
        ``echo``) are pre-registered and do not need to be passed.
        """
        if handler is not None:
            self._handlers[handler_key] = handler
        elif handler_key not in self._handlers:
            raise ToolError(f"No handler registered for handler_key {handler_key!r}")
        if inline:
            self._inline_handler_keys.add(handler_key)
        from zero.domain.ids import generate_tool_id
        from zero.domain.tools import ToolId

        tool = Tool(
            id=ToolId(generate_tool_id()),
            name=name,
            description=description,
            input_schema=input_schema,
            output_schema=output_schema,
            handler_key=handler_key,
        )
        self._tool_repo.insert_tool(tool)
        return tool

    def register_echo_tool(self) -> Tool:
        """Convenience: register the built-in echo tool."""
        return self.register_tool(
            name="echo",
            description=(
                "Echoes back the input message. A harmless test tool "
                "for exercising the tool capability runtime."
            ),
            input_schema=ECHO_TOOL_INPUT_SCHEMA,
            output_schema=ECHO_TOOL_OUTPUT_SCHEMA,
            handler_key="echo",
        )

    def register_worktree_tools(self, worktree_service: Any) -> tuple[Tool, ...]:
        """Register server-owned coding tools backed by a task worktree.

        These handlers deliberately run in-process because they use the
        authorized WorktreeService/database boundary. They are not arbitrary
        extension code and still require a project-scoped tool grant.
        """
        read_input = {
            "type": "object",
            "properties": {
                "relative_path": {"type": "string", "minLength": 1, "maxLength": 4096},
                "max_bytes": {"type": "integer", "minimum": 1, "maximum": 1048576},
            },
            "required": ["relative_path"],
            "additionalProperties": False,
        }
        read_output = {
            "type": "object",
            "properties": {
                "relative_path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["relative_path", "content"],
            "additionalProperties": False,
        }
        write_input = {
            "type": "object",
            "properties": {
                "relative_path": {"type": "string", "minLength": 1, "maxLength": 4096},
                "content": {"type": "string", "maxLength": 1048576},
            },
            "required": ["relative_path", "content"],
            "additionalProperties": False,
        }
        write_output = {
            "type": "object",
            "properties": {
                "relative_path": {"type": "string"},
                "content_hash": {"type": "string"},
            },
            "required": ["relative_path", "content_hash"],
            "additionalProperties": False,
        }
        # The advertised timeout bound matches the worktree command
        # policy exactly, so models are never invited to request values
        # the policy will refuse.
        command_timeout_cap = int(getattr(worktree_service, "max_command_timeout_seconds", 300))
        command_input = {
            "type": "object",
            "properties": {
                "command": {"type": "string", "minLength": 1, "maxLength": 128},
                "args": {"type": "array", "items": {"type": "string"}, "maxItems": 64},
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": max(1, command_timeout_cap),
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        }
        command_output = {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "state": {"type": "string"},
                "exit_code": {"type": ["integer", "null"]},
                "artifact_ids": {"type": "array", "items": {"type": "string"}},
                # Real-run fix (2026-08-28): the model received only artifact
                # ids and could never see what a command printed — it could
                # not read test failures or probe output and reported the
                # objective as unmet (honestly). Bounded stdout/stderr are
                # now part of the declared result.
                "stdout": {"type": "string"},
                "stderr": {"type": "string"},
            },
            "required": ["run_id", "state", "exit_code", "artifact_ids", "stdout", "stderr"],
            "additionalProperties": False,
        }
        diff_output = {
            "type": "object",
            "properties": {
                "artifact_id": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["artifact_id", "content"],
            "additionalProperties": False,
        }

        def context_ids(context: ToolContext) -> tuple[TaskId, Any]:
            if not context.task_id:
                # Real-run fix (2026-08-28, bug 11a): name the actual
                # policy so a delegation sub-agent (which has no worktree
                # context by design) produces a self-explanatory denial
                # instead of a mystery.
                raise ToolError(
                    "coding tools require a task worktree context; delegation "
                    "sub-agents cannot call worktree tools — the parent task "
                    "must invoke them directly"
                )
            return TaskId(context.task_id), context

        def get_worktree(context: ToolContext):
            task_id, _ = context_ids(context)
            if not context.execution_id:
                raise ToolError("coding tools require an execution context")
            worktree = worktree_service.get_worktree_for_task(
                context.project_id,
                task_id,
                actor_id=context.actor_id,
                source="system",
            )
            if worktree is None or str(worktree.execution_id) != context.execution_id:
                raise ToolError("the task has no matching owned worktree")
            return task_id, worktree

        def read_handler(input_data: dict[str, Any], context: ToolContext) -> dict[str, Any]:
            task_id, worktree = get_worktree(context)
            return {
                "relative_path": input_data["relative_path"],
                "content": worktree_service.read_file(
                    project_id=context.project_id,
                    worktree_id=worktree.id,
                    task_id=task_id,
                    actor_id=context.actor_id,
                    relative_path=input_data["relative_path"],
                    max_bytes=input_data.get("max_bytes", 256 * 1024),
                ),
            }

        def write_handler(input_data: dict[str, Any], context: ToolContext) -> dict[str, Any]:
            task_id, worktree = get_worktree(context)
            return {
                "relative_path": input_data["relative_path"],
                "content_hash": worktree_service.write_file(
                    project_id=context.project_id,
                    worktree_id=worktree.id,
                    task_id=task_id,
                    actor_id=context.actor_id,
                    relative_path=input_data["relative_path"],
                    content=input_data["content"],
                ),
            }

        def command_handler(input_data: dict[str, Any], context: ToolContext) -> dict[str, Any]:
            task_id, worktree = get_worktree(context)
            run, artifacts = worktree_service.run_command(
                project_id=context.project_id,
                worktree_id=worktree.id,
                task_id=task_id,
                actor_id=context.actor_id,
                command=input_data["command"],
                args=tuple(input_data.get("args", ())),
                timeout_seconds=input_data.get("timeout_seconds", 300),
            )

            def _bounded(kind: str) -> str:
                for artifact in artifacts:
                    if artifact.kind == kind:
                        text = artifact.content or ""
                        if len(text) > _COMMAND_OUTPUT_CHAR_LIMIT:
                            return (
                                text[:_COMMAND_OUTPUT_CHAR_LIMIT]
                                + f"\n…[{kind} truncated at {_COMMAND_OUTPUT_CHAR_LIMIT} chars]"
                            )
                        return text
                return ""

            return {
                "run_id": run.id.value,
                "state": run.state,
                "exit_code": run.exit_code,
                "artifact_ids": [artifact.id.value for artifact in artifacts],
                "stdout": _bounded("stdout"),
                "stderr": _bounded("stderr"),
            }

        def diff_handler(_input_data: dict[str, Any], context: ToolContext) -> dict[str, Any]:
            task_id, worktree = get_worktree(context)
            artifact = worktree_service.capture_diff(
                project_id=context.project_id,
                worktree_id=worktree.id,
                task_id=task_id,
                actor_id=context.actor_id,
            )
            return {"artifact_id": artifact.id.value, "content": artifact.content}

        # Bug fix (real run, 2026-08-28): the description used to keep the
        # allowlist secret ("allowlisted, bounded command"), so a frontier
        # model naturally asked for `bash -c "…"` and got a policy
        # rejection it could not have predicted. Declare the exact
        # permitted binaries and the no-shell rule in the description the
        # model sees, and read them from the enforcing service so the
        # advertisement can never drift from the policy.
        allowed = sorted(getattr(worktree_service, "allowed_commands", ()) or ())
        if allowed:
            run_command_description = (
                "Run one bounded command in the current task worktree. "
                "There is NO shell: only these exact binaries are permitted: "
                + ", ".join(allowed)
                + ". Shell composition (bash/sh -c), pipes and redirects are "
                "refused; pass file lists via args instead."
            )
        else:
            run_command_description = (
                "Run one allowlisted, bounded command in the current task worktree."
            )

        definitions = (
            (
                "read_file",
                "Read a bounded UTF-8 file from the current task worktree.",
                read_input,
                read_output,
                "zero.workspace.read_file",
                read_handler,
            ),
            (
                "write_file",
                "Atomically write a bounded UTF-8 file in the current task worktree.",
                write_input,
                write_output,
                "zero.workspace.write_file",
                write_handler,
            ),
            (
                "run_command",
                run_command_description,
                command_input,
                command_output,
                "zero.workspace.run_command",
                command_handler,
            ),
            (
                # Real-run fix (2026-08-28, bug 11a): the previous schema
                # declared a zero-property object with
                # additionalProperties=False, so a frontier model that
                # naturally passed arguments ("base", "paths", …) failed
                # input validation FIVE times in a row in a real run
                # before giving up on the tool. capture_diff is a
                # read-only, no-argument tool: declare that explicitly in
                # the description and tolerate (ignore) extra keys instead
                # of failing the call.
                "capture_diff",
                (
                    "Capture the current Git diff and status for the current "
                    "task worktree. Takes NO arguments — call it with an "
                    "empty object {}; any extra keys are ignored."
                ),
                {"type": "object", "properties": {}, "additionalProperties": True},
                diff_output,
                "zero.workspace.capture_diff",
                diff_handler,
            ),
        )
        registered: list[Tool] = []
        existing = {tool.name for tool in self.list_tools()}
        for name, description, input_schema, output_schema, handler_key, handler in definitions:
            if name in existing:
                existing_tool = self.get_tool_by_name(name)
                if existing_tool.handler_key != handler_key:
                    raise ToolError(
                        f"persistent tool {name!r} has unexpected handler key "
                        f"{existing_tool.handler_key!r}"
                    )
                # Tool rows persist across process restarts, but handlers are
                # intentionally process-local callables. Rebind the trusted
                # server-owned handler every time the composition root starts.
                self._handlers[handler_key] = handler
                self._inline_handler_keys.add(handler_key)
                # Real-run fix (2026-08-28): the persisted row also carried
                # the ORIGINAL declared schemas. When a server-owned tool's
                # declaration evolves (run_command gained stdout/stderr
                # result fields), invocations kept failing output validation
                # against the stale contract. The declaration is server-
                # owned metadata — refresh it in lockstep with the handler.
                if (
                    existing_tool.description != description
                    or existing_tool.input_schema != input_schema
                    or existing_tool.output_schema != output_schema
                ):
                    self._tool_repo.update_tool_declaration(
                        existing_tool.id,
                        description=description,
                        input_schema=input_schema,
                        output_schema=output_schema,
                    )
                    existing_tool = self.get_tool_by_name(name)
                registered.append(existing_tool)
                continue
            registered.append(
                self.register_tool(
                    name=name,
                    description=description,
                    input_schema=input_schema,
                    output_schema=output_schema,
                    handler_key=handler_key,
                    handler=handler,
                    inline=True,
                )
            )
        return tuple(registered)

    def get_tool(self, tool_id: ToolId) -> Tool:
        return self._tool_repo.get_tool_by_id(tool_id)

    def get_tool_by_name(self, name: str) -> Tool:
        return self._tool_repo.get_tool_by_name(name)

    def list_tools(self) -> list[Tool]:
        return self._tool_repo.list_tools()

    # ------------------------------------------------------------------
    # Capability grants
    # ------------------------------------------------------------------

    def grant_tool(
        self,
        *,
        project_id: ProjectId,
        actor_id: UserId,
        tool_id: ToolId,
        agent_scope: AgentScope,
        max_invocations: int | None = None,
        timeout_seconds: float | None = None,
        source: AuditSource = "system",
    ) -> ToolGrant:
        """Grant a tool to an agent scope in a project.

        Per ``zero-tool-capability-runtime`` §"Tool choice and tool
        permission are separate": a grant is required; without one,
        invocation is denied.
        """
        if max_invocations is not None and max_invocations < 1:
            raise ValueError("max_invocations must be at least 1")
        if timeout_seconds is not None:
            if timeout_seconds <= 0:
                raise ValueError("timeout_seconds must be positive")
            if timeout_seconds > self._max_grant_timeout_seconds:
                raise ValueError(
                    f"timeout_seconds exceeds maximum {self._max_grant_timeout_seconds:g}"
                )
        self._authorization.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="tool.manage",
            source=source,
        )

        from zero.domain.ids import generate_tool_grant_id
        from zero.domain.tools import ToolGrantId

        # Verify the tool exists.
        self._tool_repo.get_tool_by_id(tool_id)
        grant = ToolGrant(
            id=ToolGrantId(generate_tool_grant_id()),
            project_id=project_id,
            tool_id=tool_id,
            agent_scope=agent_scope,
            max_invocations=max_invocations,
            timeout_seconds=timeout_seconds,
        )
        self._tool_repo.insert_grant(grant)
        return grant

    def revoke_tool_grant(
        self,
        *,
        project_id: ProjectId,
        actor_id: UserId,
        tool_id: ToolId,
        agent_scope: AgentScope,
        source: AuditSource = "system",
    ) -> None:
        """Revoke a tool grant. Takes effect immediately."""
        self._authorization.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="tool.manage",
            source=source,
        )
        self._tool_repo.delete_grant(project_id, tool_id, agent_scope)

    def list_grants_for_project(
        self,
        project_id: ProjectId,
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> list[ToolGrant]:
        self._authorization.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="tool.manage",
            source=source,
        )
        return self._tool_repo.list_grants_for_project(project_id)

    # ------------------------------------------------------------------
    # Invocation
    # ------------------------------------------------------------------

    def invoke(
        self,
        *,
        project_id: ProjectId,
        actor_id: UserId,
        agent_scope: AgentScope,
        tool_name: str,
        input_data: dict[str, Any],
        source: AuditSource = "system",
        secret_service: Any | None = None,
        execution_id: str | None = None,
        task_id: str | None = None,
    ) -> ToolResult:
        """Invoke a tool by name.

        This is the canonical entry point for tool invocation. It:

        1. Resolves the tool by name.
        2. Validates the input against the tool's input schema.
        3. Resolves the capability grant. Denies if no grant.
        4. Invokes the handler with a :class:`ToolContext`.
        5. Validates the output against the tool's output schema.
        6. Constructs a :class:`ToolResult` with bounded rendering.
        7. Records an audit event.

        Per ``zero-tool-capability-runtime`` §"Model intent ->
        capability resolution -> authorization + validation -> bounded
        server-side execution -> redacted result/artifact -> usage +
        audit evidence -> durable terminal state".
        """
        self._authorization.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="execution.start",
            source=source,
        )
        correlation_id = generate_correlation_id()
        started_at = time.monotonic()

        # 1. Resolve tool by name.
        try:
            tool = self._tool_repo.get_tool_by_name(tool_name)
        except ToolNotFoundError:
            self._audit_invocation(
                project_id=project_id,
                actor_id=actor_id,
                source=source,
                tool_name=tool_name,
                status="failure",
                error="tool not found",
                correlation_id=correlation_id,
                duration_ms=int((time.monotonic() - started_at) * 1000),
            )
            raise

        # 2. Validate input against schema (trust boundary).
        try:
            jsonschema.validate(instance=input_data, schema=tool.input_schema)
        except jsonschema.ValidationError as exc:
            self._audit_invocation(
                project_id=project_id,
                actor_id=actor_id,
                source=source,
                tool_name=tool_name,
                status="failure",
                error="input validation failed",
                correlation_id=correlation_id,
                duration_ms=int((time.monotonic() - started_at) * 1000),
            )
            # Real-run fix (2026-08-28, bug 11a class): a zero-argument
            # tool must say so plainly, or the model keeps re-sending
            # arguments it invented (observed: 5 consecutive validation
            # failures on capture_diff in one real task).
            _decl = tool.input_schema if isinstance(tool.input_schema, dict) else {}
            _no_arg_tool = not _decl.get("properties") and not _decl.get(
                "additionalProperties"
            )
            _hint = (
                " This tool takes NO arguments — call it with an empty object {}."
                if _no_arg_tool
                else ""
            )
            raise ToolInputValidationError(
                f"Input for tool {tool.name!r} failed validation: {exc.message}{_hint}",
                errors=[{"path": list(exc.path), "message": exc.message}],
            ) from exc

        # 3. Resolve capability grant.
        try:
            grant = self._tool_repo.get_grant(project_id, tool.id, agent_scope)
        except ToolGrantNotFoundError:
            self._audit_invocation(
                project_id=project_id,
                actor_id=actor_id,
                source=source,
                tool_name=tool.name,
                status="denied",
                error="no capability grant",
                correlation_id=correlation_id,
                duration_ms=int((time.monotonic() - started_at) * 1000),
            )
            raise ToolInvocationDeniedError(
                f"No grant for tool {tool.name!r} in scope {agent_scope} in project {project_id}"
            )

        if not self._tool_repo.reserve_invocation(grant.id):
            self._audit_invocation(
                project_id=project_id,
                actor_id=actor_id,
                source=source,
                tool_name=tool.name,
                status="denied",
                error="invocation limit reached",
                correlation_id=correlation_id,
                duration_ms=int((time.monotonic() - started_at) * 1000),
            )
            raise ToolInvocationDeniedError(f"Invocation limit reached for tool {tool.name!r}")

        # 4. Invoke handler.
        handler = self._handlers.get(tool.handler_key)
        if handler is None:
            self._audit_invocation(
                project_id=project_id,
                actor_id=actor_id,
                source=source,
                tool_name=tool.name,
                status="error",
                error=f"no handler for key {tool.handler_key!r}",
                correlation_id=correlation_id,
                duration_ms=int((time.monotonic() - started_at) * 1000),
            )
            raise ToolError(f"No handler registered for tool {tool.name!r}")

        context = ToolContext(
            project_id=project_id,
            actor_id=actor_id,
            agent_scope=agent_scope,
            execution_id=execution_id,
            task_id=task_id,
            correlation_id=correlation_id,
            secret_service=secret_service,
        )

        try:
            if tool.handler_key in self._inline_handler_keys:
                # Server-owned handlers may need durable service objects
                # (worktree repositories, cancellation registries, etc.).
                # They still pass through the same schema, grant, audit, and
                # output-validation boundary; only arbitrary extensions use
                # the isolated child-process runner.
                output = handler(input_data, context)
            else:
                output = self._runner.run(
                    handler,
                    input_data,
                    context,
                    timeout_seconds=grant.timeout_seconds,
                )
        except ToolRunnerTimeout as exc:
            self._audit_invocation(
                project_id=project_id,
                actor_id=actor_id,
                source=source,
                tool_name=tool.name,
                status="error",
                error="handler timed out",
                correlation_id=correlation_id,
                duration_ms=int((time.monotonic() - started_at) * 1000),
            )
            raise ToolTimeoutError(f"Tool {tool.name!r} handler timed out") from exc
        except ToolRunnerOutputLimit as exc:
            self._audit_invocation(
                project_id=project_id,
                actor_id=actor_id,
                source=source,
                tool_name=tool.name,
                status="error",
                error="handler output exceeded limit",
                correlation_id=correlation_id,
                duration_ms=int((time.monotonic() - started_at) * 1000),
            )
            raise ToolError(f"Tool {tool.name!r} output exceeds the configured limit") from exc
        except ToolRunnerHandlerError as exc:
            self._audit_invocation(
                project_id=project_id,
                actor_id=actor_id,
                source=source,
                tool_name=tool.name,
                status="error",
                error="handler failed in isolated runner",
                correlation_id=correlation_id,
                duration_ms=int((time.monotonic() - started_at) * 1000),
            )
            # Hermes parity (audit 2026-08-28): the model-facing error must
            # carry the underlying reason or the loop cannot self-correct.
            raise ToolError(_handler_failure_message(tool.name, exc.__cause__ or exc)) from exc
        except ToolError:
            self._audit_invocation(
                project_id=project_id,
                actor_id=actor_id,
                source=source,
                tool_name=tool.name,
                status="error",
                error="handler rejected the request",
                correlation_id=correlation_id,
                duration_ms=int((time.monotonic() - started_at) * 1000),
            )
            raise
        except Exception as exc:
            self._audit_invocation(
                project_id=project_id,
                actor_id=actor_id,
                source=source,
                tool_name=tool.name,
                status="error",
                error="server-owned handler failed",
                correlation_id=correlation_id,
                duration_ms=int((time.monotonic() - started_at) * 1000),
            )
            # Hermes parity (audit 2026-08-28): include the bounded,
            # redacted underlying reason. A bare "handler failed" made the
            # model blind to e.g. which path was unreadable.
            raise ToolError(_handler_failure_message(tool.name, exc)) from exc

        # 5. Validate output against schema.
        try:
            jsonschema.validate(instance=output, schema=tool.output_schema)
        except jsonschema.ValidationError as exc:
            self._audit_invocation(
                project_id=project_id,
                actor_id=actor_id,
                source=source,
                tool_name=tool.name,
                status="error",
                error="output validation failed",
                correlation_id=correlation_id,
                duration_ms=int((time.monotonic() - started_at) * 1000),
            )
            raise ToolOutputValidationError(
                f"Output of tool {tool.name!r} failed validation: {exc.message}"
            ) from exc

        # 6. Construct bounded, redacted model-facing rendering.
        duration_ms = int((time.monotonic() - started_at) * 1000)
        safe_output = self._redact_output(output)
        model_facing = self._render_model_facing(tool, safe_output)

        result = ToolResult(
            tool_id=tool.id,
            status="success",
            output=safe_output,
            model_facing=model_facing,
            duration_ms=duration_ms,
        )

        # 7. Audit.
        self._audit_invocation(
            project_id=project_id,
            actor_id=actor_id,
            source=source,
            tool_name=tool.name,
            status="success",
            error=None,
            correlation_id=correlation_id,
            duration_ms=duration_ms,
        )

        return result

    @staticmethod
    def _redact_output(value: Any) -> Any:
        """Redact credential-shaped values before exposing tool output.

        Tool handlers are extension points and may read files or return
        subprocess output. Schema validation is not a confidentiality
        boundary, so both structured secret-looking fields and embedded
        key/value material are sanitized before a ``ToolResult`` leaves the
        service.
        """
        if isinstance(value, dict):
            safe: dict[Any, Any] = {}
            sensitive_key_fragments = (
                "password",
                "secret",
                "token",
                "api_key",
                "apikey",
                "authorization",
            )
            for key, item in value.items():
                key_text = str(key).lower().replace("-", "_")
                if any(fragment in key_text for fragment in sensitive_key_fragments):
                    safe[key] = "[REDACTED]"
                else:
                    safe[key] = ToolService._redact_output(item)
            return safe
        if isinstance(value, list):
            return [ToolService._redact_output(item) for item in value]
        if isinstance(value, tuple):
            return tuple(ToolService._redact_output(item) for item in value)
        if isinstance(value, str):
            return redact_sensitive_text(value)
        return value

    def _render_model_facing(self, tool: Tool, output: dict[str, Any]) -> str:
        """Render a compact, redacted, model-facing summary.

        Per ``zero-tool-capability-runtime`` §"Output policy is part
        of the tool": the model-facing rendering is bounded and
        redacted. Raw output is preserved in the :class:`ToolResult`
        for the caller; the model only sees the summary.
        """
        import json

        text = redact_sensitive_text(json.dumps(output, ensure_ascii=False, sort_keys=True))
        if len(text) > _MODEL_FACING_CHAR_LIMIT:
            text = (
                text[:_MODEL_FACING_CHAR_LIMIT]
                + f"…[model-facing output truncated at {_MODEL_FACING_CHAR_LIMIT} chars]"
            )
        return text

    def _audit_invocation(
        self,
        *,
        project_id: ProjectId,
        actor_id: UserId,
        source: AuditSource,
        tool_name: str,
        status: ToolResultStatus,
        error: str | None,
        correlation_id: str,
        duration_ms: int,
    ) -> None:
        """Record an audit event for a tool invocation.

        Per ``zero-tool-capability-runtime`` §"Audit describes the
        operation without copying payloads": the event contains the
        tool name, project, actor, result, and timing — never the
        raw input or output.
        """
        result_map = {
            "success": "success",
            "failure": "failure",
            "denied": "denied",
            "unknown": "error",
        }
        mapped_result = result_map.get(status, "error")
        # Metrics describe aggregates: low-cardinality outcome counters
        # only (per zero-observability-evidence §"Metrics describe
        # aggregates").
        if self._metrics is not None:
            self._metrics.increment("tool_invocations_total", result=mapped_result, source=source)
            self._metrics.observe_duration("tool_invocation_duration_ms", duration_ms)
        self._audit_repo.insert(
            AuditEvent(
                id=AuditEventId(generate_audit_event_id()),
                project_id=project_id,
                actor_id=actor_id,
                source=source,
                operation="tool.invoke",
                target_type="tool",
                target_id=tool_name,
                result=mapped_result,  # type: ignore[arg-type]
                correlation_id=correlation_id,
                redacted_summary=(
                    f"Invoked tool {tool_name!r} "
                    f"(status={status}, duration_ms={duration_ms}"
                    + (f", error={error}" if error else "")
                    + ")"
                ),
                created_at=_now_utc_iso(),
            )
        )
