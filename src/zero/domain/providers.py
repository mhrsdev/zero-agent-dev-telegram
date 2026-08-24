"""Provider adapter domain types.

Per ``zero-provider-adapter-contract`` SKILL.md:

- A provider adapter translates an external model runtime into Zero's
  canonical execution vocabulary. The core should understand model
  requests, normalized stream events, tool calls, usage, limits, errors,
  and cancellation without depending on one vendor's session object or
  token fields.
- Provider neutrality does not mean pretending providers are identical.
  It means preserving differences as explicit capabilities and metadata
  rather than leaking SDK types through the system.
- Canonical meaning precedes provider mapping.
- Capabilities replace provider-name conditionals.
- Persistent provider context is an optimization.
- Streaming is event processing, not string concatenation.
- Tool calls cross two trust boundaries.
- Usage has scope and authority.
- Errors need stable classes and provider detail.
- Cancellation has provider and local meanings.
- Model selection belongs to owner policy.

Per ``zero-claude-token-economics`` SKILL.md:
- Token classes remain separate (input, output, cache creation, cache read).
- Whole-agent-tree usage is counted exactly once.
- Estimated cost is distinct from authoritative reconciled billing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from zero.domain.artifacts import ArtifactId
from zero.domain.execution import ExecutionId
from zero.domain.identity import ProjectId

#: Prefixes for stable server-issued IDs.
PROVIDER_MODEL_ID_PREFIX = "pm_"
PROVIDER_REQUEST_ID_PREFIX = "preq_"
USAGE_RECORD_ID_PREFIX = "usg_"

# ----------------------------------------------------------------------
# Provider capabilities
# ----------------------------------------------------------------------

ProviderCapability = Literal[
    "streaming",
    "native_tools",
    "structured_output",
    "prompt_caching",
    "image_input",
    "server_reported_usage",
    "cancellation",
    "idempotency",
]

#: All recognized provider capabilities.
ALL_CAPABILITIES: tuple[ProviderCapability, ...] = (
    "streaming",
    "native_tools",
    "structured_output",
    "prompt_caching",
    "image_input",
    "server_reported_usage",
    "cancellation",
    "idempotency",
)


# ----------------------------------------------------------------------
# Provider model
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderModelId:
    """Stable server-issued ID for a registered provider model."""

    value: str

    def __post_init__(self) -> None:
        if not self.value or not isinstance(self.value, str):
            raise ValueError("ProviderModelId must be a non-empty string")
        if not self.value.startswith(PROVIDER_MODEL_ID_PREFIX):
            raise ValueError(
                f"ProviderModelId must start with {PROVIDER_MODEL_ID_PREFIX!r}; got {self.value!r}"
            )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class ProviderModel:
    """A registered provider model with capabilities.

    Attributes:
        id: stable server-issued ID.
        provider: the provider name (e.g. "openai", "anthropic").
        model_name: the model name (e.g. "gpt-4", "claude-3").
        context_window: max tokens the model can process.
        max_output_tokens: max tokens the model can generate.
        capabilities: tuple of supported capabilities.
        is_active: whether this model can be used for new requests.
        created_at: ISO-8601 timestamp.
    """

    id: ProviderModelId
    provider: str
    model_name: str
    context_window: int
    max_output_tokens: int
    capabilities: tuple[ProviderCapability, ...] = ()
    is_active: bool = True
    created_at: str = ""

    def has_capability(self, cap: ProviderCapability) -> bool:
        return cap in self.capabilities


# ----------------------------------------------------------------------
# Canonical request and response (provider-neutral)
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class CanonicalMessage:
    """A provider-neutral message in a conversation.

    Attributes:
        role: "system", "user", "assistant", or "tool".
        content: the message text.
        tool_call_id: for tool messages, the ID of the tool call.
        tool_calls: for assistant messages with tool calls, a tuple of
            (tool_name, tool_call_id, arguments) tuples.
    """

    role: str
    content: str
    tool_call_id: str | None = None
    tool_calls: tuple[tuple[str, str, str], ...] = ()


@dataclass(frozen=True)
class ToolDeclaration:
    """A tool advertised to the model, including its argument JSON Schema.

    A bare tool name gives the model no contract for its arguments;
    real function calling requires the parameter schema to travel with
    the request. ``parameters`` must be a JSON-Schema object mapping
    when provided.
    """

    name: str
    description: str = ""
    parameters: Mapping[str, Any] | None = None

    def normalized_parameters(self) -> dict[str, Any]:
        if self.parameters is None:
            return {"type": "object"}
        if not isinstance(self.parameters, Mapping):
            raise TypeError(f"tool {self.name!r} parameters must be a JSON-Schema object")
        return dict(self.parameters)


def coerce_tool_declarations(
    tools: Sequence[ToolDeclaration | str | Mapping[str, Any]],
) -> tuple[ToolDeclaration, ...]:
    """Normalize mixed name/declaration tool specs into declarations."""
    declarations: list[ToolDeclaration] = []
    for tool in tools:
        if isinstance(tool, ToolDeclaration):
            declarations.append(tool)
        elif isinstance(tool, str):
            declarations.append(ToolDeclaration(name=tool))
        elif isinstance(tool, Mapping):
            declarations.append(
                ToolDeclaration(
                    name=str(tool.get("name") or ""),
                    description=str(tool.get("description") or ""),
                    parameters=tool.get("parameters"),
                )
            )
        else:
            raise TypeError(f"unsupported tool specification: {type(tool).__name__}")
    return tuple(declarations)


@dataclass(frozen=True)
class CanonicalRequest:
    """A provider-neutral request to a model.

    Per ``zero-provider-adapter-contract`` §"Canonical meaning precedes
    provider mapping": Zero's core needs a small vocabulary. The adapter
    maps this onto the provider's wire format.

    Attributes:
        provider: the provider name.
        model_name: the model name.
        messages: the conversation messages.
        max_tokens: max output tokens.
        temperature: sampling temperature (0.0 = deterministic).
        tools: tool declarations available to the model. Bare name
            strings are accepted for compatibility and coerced to
            declarations without schemas.
        system_message: optional system message (separate from messages).
    """

    provider: str
    model_name: str
    messages: tuple[CanonicalMessage, ...]
    max_tokens: int = 4096
    temperature: float = 0.0
    tools: tuple[ToolDeclaration | str, ...] = ()
    system_message: str | None = None
    stream: bool = False


@dataclass(frozen=True)
class ToolCallResult:
    """The result of a tool call in a canonical response.

    Attributes:
        tool_name: the name of the tool that was called.
        tool_call_id: the ID of the tool call.
        arguments: the arguments passed to the tool (JSON text).
        result: the tool's output (JSON text or plain text).
        is_error: whether the tool call resulted in an error.
    """

    tool_name: str
    tool_call_id: str
    arguments: str
    result: str
    is_error: bool = False


@dataclass(frozen=True)
class CanonicalResponse:
    """A provider-neutral response from a model.

    Attributes:
        content: the text content of the response.
        tool_calls: tuple of tool calls the model wants to execute.
        finish_reason: why the model stopped ("stop", "length", "tool_calls").
        usage: the token usage for this response.
        provider_message_id: the provider's message ID (for deduplication).
        raw_response_artifact_id: artifact containing the full raw response.
    """

    content: str
    tool_calls: tuple[ToolCallResult, ...] = ()
    finish_reason: str = "stop"
    usage: TokenUsage | None = None
    provider_message_id: str | None = None
    raw_response_artifact_id: ArtifactId | None = None


@dataclass(frozen=True)
class CanonicalStreamEvent:
    """Provider-neutral incremental response event."""

    kind: Literal["text_delta", "tool_call_delta", "usage", "message_end"]
    text: str = ""
    tool_call: ToolCallResult | None = None
    usage: TokenUsage | None = None
    finish_reason: str | None = None
    provider_message_id: str | None = None


@dataclass(frozen=True)
class TokenUsage:
    """Normalized token usage with separate classes.

    Per ``zero-claude-token-economics`` §"Keep token classes separate":
    store these as separate non-negative counters. Never collapse them
    into one ``total_tokens`` field before persistence.

    Attributes:
        input_tokens: uncached input tokens.
        output_tokens: output tokens.
        cache_creation_input_tokens: cache creation input tokens.
        cache_read_input_tokens: cache read input tokens.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_creation_input_tokens=(
                self.cache_creation_input_tokens + other.cache_creation_input_tokens
            ),
            cache_read_input_tokens=(self.cache_read_input_tokens + other.cache_read_input_tokens),
        )

    @property
    def total_input_tokens(self) -> int:
        """Total processed input = uncached + cache creation + cache read."""
        return self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens

    @property
    def cache_read_ratio(self) -> float:
        """Cache read ratio = cache read / total processed input."""
        total = self.total_input_tokens
        return self.cache_read_input_tokens / total if total else 0.0

    @classmethod
    def from_mapping(cls, data: dict[str, int]) -> TokenUsage:
        """Create from a mapping, supporting both snake_case and camelCase.

        Per ``zero-claude-token-economics`` reference: accept provider
        naming differences only in adapters.
        """

        def _read(names: tuple[str, ...]) -> int:
            for name in names:
                if name in data and data[name] is not None:
                    val = data[name]
                    if isinstance(val, bool) or not isinstance(val, int):
                        raise ValueError(f"invalid token count for {names[0]}: {val!r}")
                    return max(0, val)
            return 0

        return cls(
            input_tokens=_read(("input_tokens", "inputTokens")),
            output_tokens=_read(("output_tokens", "outputTokens")),
            cache_creation_input_tokens=_read(
                (
                    "cache_creation_input_tokens",
                    "cacheCreationInputTokens",
                )
            ),
            cache_read_input_tokens=_read(
                (
                    "cache_read_input_tokens",
                    "cacheReadInputTokens",
                )
            ),
        )


# ----------------------------------------------------------------------
# Provider request record
# ----------------------------------------------------------------------


ProviderRequestState = Literal[
    "pending",
    "streaming",
    "completed",
    "failed",
    "cancelled",
    "unknown",
]

#: Classified error types (per zero-provider-adapter-contract §"Errors
#: need stable classes").
ProviderErrorClass = Literal[
    "auth_failure",
    "rate_limit",
    "invalid_request",
    "context_limit",
    "transient",
    "policy_refusal",
    "cancelled",
    "unknown_outcome",
]

#: Errors that justify a bounded retry.
RETRIABLE_ERROR_CLASSES: frozenset[ProviderErrorClass] = frozenset(
    {
        "rate_limit",
        "transient",
    }
)


@dataclass(frozen=True)
class ProviderRequestId:
    value: str

    def __post_init__(self) -> None:
        if not self.value or not isinstance(self.value, str):
            raise ValueError("ProviderRequestId must be a non-empty string")
        if not self.value.startswith(PROVIDER_REQUEST_ID_PREFIX):
            raise ValueError(
                f"ProviderRequestId must start with "
                f"{PROVIDER_REQUEST_ID_PREFIX!r}; got {self.value!r}"
            )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class ProviderRequest:
    """A durable record of a provider request.

    Attributes:
        id: stable server-issued ID.
        project_id: the project (denormalized).
        execution_id: the execution (optional).
        provider: the provider name.
        model_name: the model name.
        request_hash: hash of the request payload for deduplication.
        state: the request's state.
        error_class: classified error type.
        error_message: redacted error message.
        response_artifact_id: artifact containing the full response.
        started_at: ISO-8601 timestamp.
        completed_at: ISO-8601 timestamp when the request reached a
            terminal state.
    """

    id: ProviderRequestId
    project_id: ProjectId
    execution_id: ExecutionId | None
    provider: str
    model_name: str
    request_hash: str
    state: ProviderRequestState
    idempotency_key: str | None = None
    error_class: ProviderErrorClass | None = None
    error_message: str | None = None
    response_artifact_id: ArtifactId | None = None
    started_at: str = ""
    completed_at: str | None = None
    attempt_count: int = 0
    claim_owner: str | None = None
    claim_token: str | None = None
    lease_expires_at: str | None = None
    heartbeat_at: str | None = None


# ----------------------------------------------------------------------
# Usage record
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class UsageRecordId:
    value: str

    def __post_init__(self) -> None:
        if not self.value or not isinstance(self.value, str):
            raise ValueError("UsageRecordId must be a non-empty string")
        if not self.value.startswith(USAGE_RECORD_ID_PREFIX):
            raise ValueError(
                f"UsageRecordId must start with {USAGE_RECORD_ID_PREFIX!r}; got {self.value!r}"
            )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class UsageRecord:
    """A normalized token usage record.

    Per ``zero-claude-token-economics`` §"Estimated cost is not billing
    truth": estimated_cost_usd is a client-side estimate;
    reconciled_cost_usd is the authoritative cost from provider billing.

    Attributes:
        id: stable server-issued ID.
        project_id: the project (denormalized).
        provider_request_id: the request this usage belongs to.
        execution_id: the execution (optional).
        provider_message_id: the provider's message ID for deduplication.
        usage: the token usage (separate classes).
        estimated_cost_usd: client-side estimate (NOT billing truth).
        pricing_catalog_version: which pricing version was used.
        reconciled_cost_usd: authoritative cost from provider billing.
        is_whole_tree: whether this usage includes subagent usage.
        created_at: ISO-8601 timestamp.
    """

    id: UsageRecordId
    project_id: ProjectId
    provider_request_id: ProviderRequestId
    execution_id: ExecutionId | None
    provider_message_id: str | None
    usage: TokenUsage
    estimated_cost_usd: str = "0"
    pricing_catalog_version: int = 1
    reconciled_cost_usd: str | None = None
    is_whole_tree: bool = False
    created_at: str = ""


# ----------------------------------------------------------------------
# Pricing catalog
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class PricingEntry:
    """Versioned pricing for a provider model.

    Per ``zero-claude-token-economics`` §"Pricing is versioned data,
    not parser logic": prices belong in a versioned server-side catalog,
    not in the token parser.

    Attributes:
        catalog_version: the version of the pricing catalog.
        provider: the provider name.
        model_name: the model name.
        input_price_per_million: USD per million input tokens.
        output_price_per_million: USD per million output tokens.
        cache_creation_price_per_million: USD per million cache creation tokens.
        cache_read_price_per_million: USD per million cache read tokens.
        effective_at: when this pricing takes effect.
    """

    catalog_version: int
    provider: str
    model_name: str
    input_price_per_million: str
    output_price_per_million: str
    cache_creation_price_per_million: str = "0"
    cache_read_price_per_million: str = "0"
    effective_at: str = ""


# ----------------------------------------------------------------------
# Typed failures
# ----------------------------------------------------------------------


class ProviderError(RuntimeError):
    """Base class for provider-domain typed failures."""


class ProviderCancelledError(ProviderError):
    """Local or provider-side cancellation stopped the request."""


class ProviderUnknownOutcomeError(ProviderError):
    """The provider may have accepted the request but no result is known."""


class ProviderNotFoundError(ProviderError):
    pass


class ProviderModelNotFoundError(ProviderError):
    pass


class ProviderRequestNotFoundError(ProviderError):
    pass


class ProviderRequestStateError(ProviderError):
    """A provider request state transition is not valid for its current state."""


class InvalidProviderRequestError(ProviderError):
    """The canonical request is malformed."""


class PricingNotFoundError(ProviderError):
    """No pricing entry exists for the given provider/model/version."""
