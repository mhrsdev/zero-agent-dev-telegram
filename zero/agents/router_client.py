"""Zero v2 Router client — ADR 0004, Phase R.

Zero is a **pure HTTP consumer** of Router via OpenAI-compatible protocol.
Zero NEVER picks models — structural test (T-R.2 acceptance) enforces this.

Key design (per ADR T-R.2):
    - Single ``complete()`` method that takes messages + tools
    - Streaming with mid-stream failure handling
    - Timeout always set
    - API key only via ``secret://`` reference (resolved at call time)
    - Cost read from Router response header ``x-zero-cost-usd``
    - ``cache_read_tokens`` / ``cache_write_tokens`` read and reported
    - ``request_id`` stored for traceability
"""
from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from zero.core.errors import RouterError
from zero.core.scope import Scope
from zero.core.secret import SecretResolver, SecretValue

__all__ = [
    "ROUTER_COST_HEADER",
    "ROUTER_REQUEST_ID_HEADER",
    "RouterCallError",
    "RouterClient",
    "RouterMessage",
    "RouterResponse",
    "RouterTimeoutError",
    "RouterToolCall",
]


# Headers (Phase R contract)
ROUTER_COST_HEADER = "x-zero-cost-usd"
ROUTER_REQUEST_ID_HEADER = "x-zero-request-id"
ROUTER_CACHE_READ_HEADER = "x-zero-cache-read-tokens"
ROUTER_CACHE_WRITE_HEADER = "x-zero-cache-write-tokens"


# ---------------------------------------------------------------------- types

@dataclass(frozen=True, slots=True)
class RouterMessage:
    """OpenAI-format chat message."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    name: str | None = None  # tool name (for role=tool)


@dataclass(frozen=True, slots=True)
class RouterToolCall:
    """A single tool call requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RouterResponse:
    """Response from a Router ``complete()`` call."""

    content: str
    tool_calls: list[RouterToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    model: str = ""  # filled by Router (Zero doesn't pick)
    request_id: str = ""
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    latency_ms: float = 0.0

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "request_id": self.request_id,
            "cost_usd": self.cost_usd,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "latency_ms": self.latency_ms,
            "finish_reason": self.finish_reason,
            "tool_call_count": len(self.tool_calls),
        }


class RouterCallError(RouterError):
    """Raised when Router returns a non-2xx response."""

    status_code: int | None = None

    def __init__(self, message: str, *, status_code: int | None = None, internal: str | None = None) -> None:
        super().__init__(message, internal=internal)
        self.status_code = status_code


class RouterTimeoutError(RouterError):
    """Raised when Router call times out."""


# ---------------------------------------------------------------------- client

class RouterClient:
    """Async HTTP client for the LLM Router.

    Construction:
        >>> from zero.core.secret import CompositeSecretResolver
        >>> client = RouterClient(
        ...     base_url="http://127.0.0.1:8080/v1",
        ...     api_key_ref="secret://env/ZERO_ROUTER_API_KEY",
        ...     resolver=CompositeSecretResolver(),
        ... )

    Usage:
        >>> resp = await client.complete(
        ...     messages=[RouterMessage(role="user", content="hello")],
        ...     tools=[],
        ...     scope=project_scope,
        ... )
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key_ref: str,
        resolver: SecretResolver,
        timeout_seconds: float = 60.0,
        max_retries: int = 3,
        default_model: str | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key_ref = api_key_ref
        self._resolver = resolver
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._default_model = default_model

    async def complete(
        self,
        *,
        messages: list[RouterMessage],
        tools: list[dict[str, Any]] | None = None,
        scope: Scope,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        effort_tier: str | None = None,
    ) -> RouterResponse:
        """Call Router's ``/chat/completions`` endpoint.

        Per ADR T-R.4, send ``X-Zero-Scope-*`` and ``X-Zero-Policy`` headers
        so Router can apply scope-aware routing.

        Zero NEVER picks the model — ``model`` is passed through only if
        caller (typically AgentDefinition.effort_tier) provided one.
        """
        # Resolve API key at call time (ADR 0007 §2).
        try:
            secret: SecretValue = self._resolver.resolve(self._api_key_ref)
        except Exception as e:
            raise RouterError(
                f"failed to resolve Router API key from {self._api_key_ref!r}",
                internal=str(e),
            ) from e

        body: dict[str, Any] = {
            "messages": [_msg_to_dict(m) for m in messages],
            "temperature": temperature,
        }
        # Model: pass through if caller provided one. If not, let Router pick.
        if model is not None:
            body["model"] = model
        elif self._default_model is not None:
            body["model"] = self._default_model
        # else: Router picks based on its policy + our X-Zero-Policy header.

        if tools:
            body["tools"] = tools
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if effort_tier is not None:
            body["effort_tier"] = effort_tier  # Router-specific extension

        # Scope-aware headers (Phase R T-R.4).
        headers = {
            "Authorization": f"Bearer {secret.reveal()}",
            "Content-Type": "application/json",
            "X-Zero-Scope-Mode": scope.mode.value,
            "X-Zero-Scope-Key": scope.retrieval_key(),
        }
        if scope.is_development() and scope.project_id is not None:
            headers["X-Zero-Scope-Project"] = scope.project_id

        # Retry with exponential backoff.
        # max_retries is the number of RETRIES (so total attempts = max_retries + 1).
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return await self._do_call(headers, body, scope)
            except RouterTimeoutError as e:
                last_exc = e
                if attempt >= self._max_retries:
                    break
                # Timeout: retry with backoff
                await _sleep(2**attempt * 0.5)
            except RouterCallError as e:
                last_exc = e
                if e.status_code is not None and 400 <= e.status_code < 500:
                    # Client error: do not retry
                    raise
                if attempt >= self._max_retries:
                    break
                # 5xx: retry
                await _sleep(2**attempt * 0.5)

        assert last_exc is not None
        raise last_exc

    async def stream(
        self,
        *,
        messages: list[RouterMessage],
        tools: list[dict[str, Any]] | None = None,
        scope: Scope,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        effort_tier: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream Router response as Server-Sent Events chunks.

        Each yielded dict has shape ``{"delta": ..., "finish_reason": ...}``.
        On mid-stream failure, raises ``RouterCallError`` from the iterator.
        """
        try:
            secret = self._resolver.resolve(self._api_key_ref)
        except Exception as e:
            raise RouterError(
                "failed to resolve Router API key",
                internal=str(e),
            ) from e

        body: dict[str, Any] = {
            "messages": [_msg_to_dict(m) for m in messages],
            "temperature": temperature,
            "stream": True,
        }
        if model is not None:
            body["model"] = model
        elif self._default_model is not None:
            body["model"] = self._default_model
        if tools:
            body["tools"] = tools
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if effort_tier is not None:
            body["effort_tier"] = effort_tier

        headers = {
            "Authorization": f"Bearer {secret.reveal()}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "X-Zero-Scope-Mode": scope.mode.value,
            "X-Zero-Scope-Key": scope.retrieval_key(),
        }

        timeout = httpx.Timeout(self._timeout, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client, client.stream(
            "POST",
            f"{self._base_url}/chat/completions",
            json=body,
            headers=headers,
        ) as resp:
            if resp.status_code >= 400:
                text_bytes = await resp.aread()
                text = text_bytes.decode("utf-8", errors="replace")
                raise RouterCallError(
                    f"Router returned {resp.status_code}: {text[:200]}",
                    status_code=resp.status_code,
                    internal=f"status={resp.status_code} body={text[:500]!r}",
                )
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line[len("data: "):]
                if payload == "[DONE]":
                    break
                import json  # noqa: PLC0415

                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                yield chunk

    async def _do_call(
        self,
        headers: dict[str, str],
        body: dict[str, Any],
        scope: Scope,
    ) -> RouterResponse:
        """Single HTTP call to Router. Raises on non-2xx."""
        start = time.monotonic()
        timeout = httpx.Timeout(self._timeout, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                resp = await client.post(
                    f"{self._base_url}/chat/completions",
                    json=body,
                    headers=headers,
                )
            except httpx.TimeoutException as e:
                raise RouterTimeoutError(
                    f"Router call timed out after {self._timeout}s",
                    internal=str(e),
                ) from e
            except httpx.HTTPError as e:
                raise RouterCallError(
                    f"Router HTTP error: {e}",
                    internal=str(e),
                ) from e

        latency_ms = (time.monotonic() - start) * 1000.0

        if resp.status_code >= 400:
            text = resp.text
            err = RouterCallError(
                f"Router returned {resp.status_code}: {text[:200]}",
                status_code=resp.status_code,
                internal=f"status={resp.status_code} body={text[:500]!r}",
            )
            raise err

        try:
            data = resp.json()
        except Exception as e:
            raise RouterCallError(
                f"Router returned non-JSON response: {resp.text[:200]}",
                internal=str(e),
            ) from e

        # Extract usage + cost from headers / body.
        usage = data.get("usage", {}) or {}
        cost_str = resp.headers.get(ROUTER_COST_HEADER)
        cost = float(cost_str) if cost_str else 0.0
        request_id = resp.headers.get(ROUTER_REQUEST_ID_HEADER, "")
        cache_read = int(resp.headers.get(ROUTER_CACHE_READ_HEADER, "0") or "0")
        cache_write = int(resp.headers.get(ROUTER_CACHE_WRITE_HEADER, "0") or "0")

        # Parse content + tool calls from choices[0].
        choices = data.get("choices") or []
        if not choices:
            raise RouterCallError("Router response missing 'choices'")
        choice = choices[0]
        msg = choice.get("message", {}) or {}
        content = msg.get("content") or ""
        finish_reason = choice.get("finish_reason", "stop")
        raw_tool_calls = msg.get("tool_calls") or []
        tool_calls: list[RouterToolCall] = []
        for tc in raw_tool_calls:
            args_str = tc.get("function", {}).get("arguments", "{}")
            import json  # noqa: PLC0415

            try:
                args = json.loads(args_str) if isinstance(args_str, str) else args_str
            except json.JSONDecodeError:
                args = {"_raw": args_str}
            tool_calls.append(
                RouterToolCall(
                    id=tc.get("id", ""),
                    name=tc.get("function", {}).get("name", ""),
                    arguments=args,
                )
            )

        return RouterResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            model=data.get("model", ""),
            request_id=request_id,
            cost_usd=cost,
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            latency_ms=latency_ms,
        )


# ---------------------------------------------------------------------- helpers

def _msg_to_dict(m: RouterMessage) -> dict[str, Any]:
    """Convert RouterMessage to OpenAI-format dict."""
    out: dict[str, Any] = {"role": m.role}
    if m.content is not None:
        out["content"] = m.content
    if m.tool_calls is not None:
        out["tool_calls"] = m.tool_calls
    if m.tool_call_id is not None:
        out["tool_call_id"] = m.tool_call_id
    if m.name is not None:
        out["name"] = m.name
    return out


async def _sleep(seconds: float) -> None:
    import asyncio  # noqa: PLC0415

    await asyncio.sleep(seconds)
