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
from zero.domain.audit import AuditEvent, AuditEventId, AuditSource
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
)
from zero.persistence.repositories.audit_repository import AuditRepository
from zero.persistence.repositories.tool_repository import ToolRepository


def _now_utc_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


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


def echo_handler(
    input_data: dict[str, Any], context: ToolContext
) -> dict[str, Any]:
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
    ) -> None:
        self._tool_repo = tool_repo
        self._audit_repo = audit_repo
        self._authorization = authorization
        # The handler registry maps handler_key -> callable. This is
        # server-side only; never sent to models.
        self._handlers: dict[str, ToolHandler] = {}
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
    ) -> Tool:
        """Register a new tool in the registry.

        If ``handler`` is provided, it is registered for ``handler_key``
        in the in-process handler map. Built-in handlers (like
        ``echo``) are pre-registered and do not need to be passed.
        """
        if handler is not None:
            self._handlers[handler_key] = handler
        elif handler_key not in self._handlers:
            raise ToolError(
                f"No handler registered for handler_key {handler_key!r}"
            )
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
        timeout_seconds: int | None = None,
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
            raise ValueError(
                "timeout_seconds requires an isolated tool runner, which is not available"
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
            jsonschema.validate(
                instance=input_data, schema=tool.input_schema
            )
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
            raise ToolInputValidationError(
                f"Input for tool {tool.name!r} failed validation: "
                f"{exc.message}",
                errors=[{"path": list(exc.path), "message": exc.message}],
            ) from exc

        # 3. Resolve capability grant.
        try:
            grant = self._tool_repo.get_grant(
                project_id, tool.id, agent_scope
            )
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
                f"No grant for tool {tool.name!r} in scope {agent_scope} "
                f"in project {project_id}"
            )

        if grant.timeout_seconds is not None:
            raise ToolInvocationDeniedError(
                "Timeout-constrained grants require an isolated tool runner"
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
            raise ToolInvocationDeniedError(
                f"Invocation limit reached for tool {tool.name!r}"
            )

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
            raise ToolError(
                f"No handler registered for tool {tool.name!r}"
            )

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
            output = handler(input_data, context)
        except Exception as exc:
            # Don't leak the traceback to the model.
            self._audit_invocation(
                project_id=project_id,
                actor_id=actor_id,
                source=source,
                tool_name=tool.name,
                status="error",
                error=f"handler raised: {type(exc).__name__}",
                correlation_id=correlation_id,
                duration_ms=int((time.monotonic() - started_at) * 1000),
            )
            raise ToolError(
                f"Tool {tool.name!r} handler raised an exception"
            ) from exc

        # 5. Validate output against schema.
        try:
            jsonschema.validate(
                instance=output, schema=tool.output_schema
            )
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
                f"Output of tool {tool.name!r} failed validation: "
                f"{exc.message}"
            ) from exc

        # 6. Construct bounded, redacted model-facing rendering.
        duration_ms = int((time.monotonic() - started_at) * 1000)
        model_facing = self._render_model_facing(tool, output)

        result = ToolResult(
            tool_id=tool.id,
            status="success",
            output=output,
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

    def _render_model_facing(
        self, tool: Tool, output: dict[str, Any]
    ) -> str:
        """Render a compact, redacted, model-facing summary.

        Per ``zero-tool-capability-runtime`` §"Output policy is part
        of the tool": the model-facing rendering is bounded and
        redacted. Raw output is preserved in the :class:`ToolResult`
        for the caller; the model only sees the summary.
        """
        # For the echo tool and similar simple tools, we render the
        # output as compact JSON. For tools with large outputs, the
        # handler should provide a custom rendering; for now we cap
        # the length to a safe bound.
        import json

        text = json.dumps(output, ensure_ascii=False, sort_keys=True)
        if len(text) > 500:
            text = text[:497] + "..."
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
        self._audit_repo.insert(
            AuditEvent(
                id=AuditEventId(generate_audit_event_id()),
                project_id=project_id,
                actor_id=actor_id,
                source=source,
                operation="tool.invoke",
                target_type="tool",
                target_id=tool_name,
                result=result_map.get(status, "error"),  # type: ignore[arg-type]
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
