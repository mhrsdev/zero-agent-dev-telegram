"""Base classes and protocols for LLM providers.

Every provider returns an :class:`LLMProviderResponse` with the same fields the
RouterClient parses from the Router response. This keeps the abstraction
leak-proof: a provider looks exactly like a Router to RouterClient.
"""
from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from zero.core.errors import RouterError
from zero.core.scope import Scope

__all__ = [
    "LLMProvider",
    "LLMProviderError",
    "LLMProviderResponse",
    "LLMProviderTimeoutError",
    "ProviderMessage",
    "ProviderToolCall",
    "ProviderToolDef",
]


class LLMProviderError(RouterError):
    """Raised when an LLM provider call fails."""


class LLMProviderTimeoutError(LLMProviderError):
    """Raised when an LLM provider call times out."""


@dataclass(frozen=True, slots=True)
class ProviderMessage:
    """OpenAI-format chat message (mirrors RouterMessage)."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    name: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderToolCall:
    """A single tool call requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ProviderToolDef:
    """OpenAI-format tool definition (function calling)."""

    name: str
    description: str
    parameters_schema: dict[str, Any]

    def to_openai_dict(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema,
            },
        }


@dataclass(frozen=True, slots=True)
class LLMProviderResponse:
    """Response from an LLM provider call.

    Mirrors :class:`zero.agents.router_client.RouterResponse` so providers
    are drop-in replacements for the Router.
    """

    content: str
    tool_calls: list[ProviderToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    model: str = ""
    request_id: str = ""
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    latency_ms: float = 0.0


class LLMProvider(Protocol):
    """Protocol every LLM provider implements.

    A provider is a *stateless* HTTP client that knows how to:
        1. Translate RouterMessage list → provider-native request body
        2. Call the provider's API
        3. Translate the provider-native response → LLMProviderResponse
        4. Compute cost (per-token pricing) and emit Router-compatible headers

    Providers MUST:
        - Resolve their API key from a ``secret://`` ref at call time
        - Send ``X-Zero-Scope-*`` headers (for telemetry / future routing)
        - Honor ``timeout_seconds``
        - Retry 5xx with exponential backoff (max_retries times)
        - NOT retry 4xx (client error — caller must fix the request)
    """

    provider_name: str
    default_model: str

    async def complete(
        self,
        *,
        messages: list[ProviderMessage],
        tools: list[ProviderToolDef] | None = None,
        scope: Scope,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        effort_tier: str | None = None,
    ) -> LLMProviderResponse: ...

    async def stream(
        self,
        *,
        messages: list[ProviderMessage],
        tools: list[ProviderToolDef] | None = None,
        scope: Scope,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        effort_tier: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]: ...


# ---------------------------------------------------------------------- helpers

async def sleep_with_backoff(attempt: int) -> None:
    """Exponential backoff: 0.5 * 2^attempt seconds (capped at 8s)."""
    import asyncio  # noqa: PLC0415

    await asyncio.sleep(min(8.0, 0.5 * (2**attempt)))


def now_ms() -> float:
    """Monotonic milliseconds (for latency measurement)."""
    return time.monotonic() * 1000.0


def parse_tool_calls(raw: list[dict[str, Any]] | None) -> list[ProviderToolCall]:
    """Parse OpenAI-format tool_calls list into ProviderToolCall list.

    Handles invalid JSON arguments gracefully (wraps in ``_raw`` key) per
    the RouterClient contract.
    """
    import json  # noqa: PLC0415

    if not raw:
        return []
    out: list[ProviderToolCall] = []
    for tc in raw:
        func = tc.get("function", {}) or {}
        args_str = func.get("arguments", "{}")
        try:
            args = json.loads(args_str) if isinstance(args_str, str) else args_str
        except json.JSONDecodeError:
            args = {"_raw": args_str}
        if not isinstance(args, dict):
            args = {"_raw": str(args)}
        out.append(
            ProviderToolCall(
                id=tc.get("id", "") or f"call_{len(out)}",
                name=func.get("name", ""),
                arguments=args,
            )
        )
    return out


def messages_to_openai_format(messages: list[ProviderMessage]) -> list[dict[str, Any]]:
    """Convert ProviderMessage list to OpenAI dict list."""
    out: list[dict[str, Any]] = []
    for m in messages:
        d: dict[str, Any] = {"role": m.role}
        if m.content is not None:
            d["content"] = m.content
        if m.tool_calls is not None:
            d["tool_calls"] = m.tool_calls
        if m.tool_call_id is not None:
            d["tool_call_id"] = m.tool_call_id
        if m.name is not None:
            d["name"] = m.name
        out.append(d)
    return out


def scope_headers(scope: Scope) -> dict[str, str]:
    """Build the X-Zero-Scope-* headers (same as RouterClient)."""
    headers: dict[str, str] = {
        "X-Zero-Scope-Mode": scope.mode.value,
        "X-Zero-Scope-Key": scope.retrieval_key(),
    }
    if scope.is_development() and scope.project_id is not None:
        headers["X-Zero-Scope-Project"] = scope.project_id
    return headers
