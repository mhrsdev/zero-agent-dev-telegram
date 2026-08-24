"""Tool registry and capability domain types.

Per ``zero-tool-capability-runtime`` SKILL.md:

- A Zero tool is a server-owned capability with a name, bounded input,
  authorization policy, execution limits, result policy, and audit
  identity.
- The registry describes what a tool can do. A capability grant
  describes who may invoke one bounded part of it in one context.
- Tool schemas are trust boundaries: model output is untrusted input.
  Validation covers type, shape, length, allowed values, project
  ownership, path normalization, and domain preconditions before side
  effects begin.
- Secrets resolve at the last responsible moment.
- Output policy is part of the tool: canonical artifact storage,
  redacted structured metadata, bounded model-facing rendering,
  human-visible download or inspection under authorization.
- Tool choice and tool permission are separate.
- A denied tool does not become available through a different
  interface or child agent. Delegation can narrow authority but
  cannot invent authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from zero.domain.identity import ProjectId

#: Prefix for Tool IDs.
TOOL_ID_PREFIX = "tool_"
#: Prefix for Tool Grant IDs.
TOOL_GRANT_ID_PREFIX = "tg_"

#: Agent scopes that can receive a tool grant. Per ``zero-context-memory``
#: SKILL.md §"Main Agent mapping": ``main_planner``, ``main_worker``,
#: and dynamic ``sub_agent_type`` (and the integration checker).
AgentScope = Literal["main_planner", "main_worker", "sub_agent_type", "integration"]


@dataclass(frozen=True)
class ToolId:
    """Stable server-issued ID for a registered tool."""

    value: str

    def __post_init__(self) -> None:
        if not self.value or not isinstance(self.value, str):
            raise ValueError("ToolId must be a non-empty string")
        if not self.value.startswith(TOOL_ID_PREFIX):
            raise ValueError(f"ToolId must start with {TOOL_ID_PREFIX!r}; got {self.value!r}")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class ToolGrantId:
    """Stable server-issued ID for a tool capability grant."""

    value: str

    def __post_init__(self) -> None:
        if not self.value or not isinstance(self.value, str):
            raise ValueError("ToolGrantId must be a non-empty string")
        if not self.value.startswith(TOOL_GRANT_ID_PREFIX):
            raise ValueError(
                f"ToolGrantId must start with {TOOL_GRANT_ID_PREFIX!r}; got {self.value!r}"
            )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class Tool:
    """A registered tool in the registry.

    The registry describes what a tool can do. A capability grant
    (see :class:`ToolGrant`) describes who may invoke it in a given
    context.

    Attributes:
        id: stable server-issued ID.
        name: unique tool name (e.g. ``"echo"``).
        description: human-readable description. MUST NOT contain
            secrets or credentials.
        input_schema: JSON Schema (as a Python dict) describing valid
            input. Used to validate model output before invocation.
        output_schema: JSON Schema (as a Python dict) describing the
            shape of the tool's result. Used to validate the result
            before it is returned to the caller.
        handler_key: server-side key identifying the Python handler
            that implements this tool. Never sent to models.
    """

    id: ToolId
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    handler_key: str


@dataclass(frozen=True)
class ToolGrant:
    """A capability grant: who may invoke a tool, in what context.

    Per ``zero-tool-capability-runtime`` §"Registry metadata and
    runtime capability differ": a grant describes a bounded part of a
    tool in one context. Boolean access (``tool=true``) hides
    operation, target, duration, and limits; we explicitly reject it.

    Attributes:
        id: stable server-issued ID.
        project_id: the project this grant belongs to.
        tool_id: the tool being granted.
        agent_scope: the agent scope that may use this grant.
        max_invocations: optional cap on total invocations.
        timeout_seconds: optional per-invocation timeout.
    """

    id: ToolGrantId
    project_id: ProjectId
    tool_id: ToolId
    agent_scope: AgentScope
    max_invocations: int | None = None
    timeout_seconds: float | None = None


# ----------------------------------------------------------------------
# Tool result
# ----------------------------------------------------------------------

ToolResultStatus = Literal["success", "failure", "cancelled", "unknown", "error"]


@dataclass(frozen=True)
class ToolResult:
    """The bounded result of a tool invocation.

    Per ``zero-tool-capability-runtime`` §"Output policy is part of
    the tool": tool output may contain secrets, private data, huge
    logs, binary content, or prompt injection. A safe result path
    distinguishes:

    - canonical artifact storage (deferred to M8);
    - redacted structured metadata (this object);
    - bounded model-facing rendering (the ``model_facing`` field);
    - human-visible download or inspection under authorization
      (deferred to M12).

    Attributes:
        tool_id: the tool that produced this result.
        status: terminal status of the invocation.
        output: the validated output payload (matches
            :attr:`Tool.output_schema`). May be ``None`` on failure.
        model_facing: a compact, redacted rendering suitable for
            inclusion in a model prompt. MUST NOT contain secrets,
            raw credentials, or unbounded content.
        error: optional error message on failure. MUST NOT contain
            secrets or credentials.
        duration_ms: wall-clock duration of the invocation.
    """

    tool_id: ToolId
    status: ToolResultStatus
    output: dict[str, Any] | None = None
    model_facing: str = ""
    error: str | None = None
    duration_ms: int = 0


# ----------------------------------------------------------------------
# Typed failures
# ----------------------------------------------------------------------


class ToolError(RuntimeError):
    """Base class for tool-domain typed failures."""


class ToolNotFoundError(ToolError):
    """No tool is registered with the given ID or name."""


class ToolAlreadyExistsError(ToolError):
    """A tool with the same name is already registered."""


class ToolGrantNotFoundError(ToolError):
    """No tool grant exists for the given project + tool + scope."""


class ToolInputValidationError(ToolError):
    """The model-supplied tool input failed schema validation.

    Per ``zero-tool-capability-runtime`` §"Tool schemas are trust
    boundaries": model output is untrusted input. Validation covers
    type, shape, length, allowed values, project ownership, path
    normalization, and domain preconditions before side effects begin.
    """

    def __init__(self, message: str, *, errors: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []


class ToolOutputValidationError(ToolError):
    """The tool handler returned output that did not match its schema.

    This indicates a bug in the tool handler, not a model error.
    """


class ToolInvocationDeniedError(ToolError):
    """The actor is not authorized to invoke this tool in this context."""


class ToolTimeoutError(ToolError):
    """The tool invocation exceeded its timeout.

    Per ``zero-tool-capability-runtime`` §"Retries depend on
    side-effect semantics": a timeout does not reveal whether a remote
    side effect occurred. Safe retry requires an idempotency key or a
    read-after-write reconciliation path.
    """
