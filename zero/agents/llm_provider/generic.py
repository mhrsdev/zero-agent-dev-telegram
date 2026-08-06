"""Generic OpenAI-compatible provider.

This is the base implementation that all concrete providers (Gemini, OpenAI,
OpenRouter) extend. It speaks the OpenAI Chat Completions API protocol and
parses the standard response shape.

Concrete providers only override:
    - ``provider_name`` (for logging / metrics)
    - ``base_url`` (endpoint URL)
    - ``default_model`` (fallback model when caller doesn't specify)
    - ``pricing`` (per-token cost table)
"""
from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from zero.agents.llm_provider.base import (
    LLMProviderError,
    LLMProviderResponse,
    LLMProviderTimeoutError,
    ProviderMessage,
    ProviderToolCall,
    ProviderToolDef,
    messages_to_openai_format,
    now_ms,
    parse_tool_calls,
    scope_headers,
    sleep_with_backoff,
)
from zero.core.scope import Scope
from zero.core.secret import SecretResolver

__all__ = ["GenericOpenAIProvider", "PricingTable"]


# ---------------------------------------------------------------------- pricing

PricingTable = dict[str, tuple[float, float]]
"""Model name → (input_price_per_1m_tokens, output_price_per_1m_tokens)."""


# ---------------------------------------------------------------------- provider

class GenericOpenAIProvider:
    """Generic OpenAI-compatible LLM provider.

    This is the concrete base — subclasses (GeminiProvider, OpenAIProvider,
    OpenRouterProvider) override ``provider_name``, ``base_url``,
    ``default_model``, and ``pricing``.
    """

    provider_name: str = "generic"
    base_url: str = "https://api.openai.com/v1"
    default_model: str = "gpt-4o-mini"
    pricing: PricingTable = {}

    def __init__(
        self,
        *,
        api_key_ref: str,
        resolver: SecretResolver,
        timeout_seconds: float = 60.0,
        max_retries: int = 3,
        default_model: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self._api_key_ref = api_key_ref
        self._resolver = resolver
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        if default_model is not None:
            self.default_model = default_model
        self._extra_headers = extra_headers or {}

    # ------------------------------------------------------------------ complete

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
    ) -> LLMProviderResponse:
        """Call the provider's /chat/completions endpoint."""
        body = self._build_body(
            messages=messages,
            tools=tools,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            effort_tier=effort_tier,
            stream=False,
        )
        headers = await self._build_headers(scope)

        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return await self._do_call(headers, body, scope, model)
            except LLMProviderTimeoutError as e:
                last_exc = e
                if attempt >= self._max_retries:
                    break
                await sleep_with_backoff(attempt)
            except LLMProviderError as e:
                last_exc = e
                # 4xx → do not retry
                status = getattr(e, "status_code", None)
                if status is not None and 400 <= status < 500:
                    raise
                if attempt >= self._max_retries:
                    break
                await sleep_with_backoff(attempt)

        assert last_exc is not None
        raise last_exc

    # ------------------------------------------------------------------ stream

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
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream the response as SSE chunks."""
        body = self._build_body(
            messages=messages,
            tools=tools,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            effort_tier=effort_tier,
            stream=True,
        )
        headers = await self._build_headers(scope)
        headers["Accept"] = "text/event-stream"

        timeout = httpx.Timeout(self._timeout, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                json=body,
                headers=headers,
            ) as resp:
                if resp.status_code >= 400:
                    text_bytes = await resp.aread()
                    text = text_bytes.decode("utf-8", errors="replace")
                    raise LLMProviderError(
                        f"{self.provider_name} returned {resp.status_code}: {text[:200]}",
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

    # ------------------------------------------------------------------ internals

    def _build_body(
        self,
        *,
        messages: list[ProviderMessage],
        tools: list[ProviderToolDef] | None,
        model: str | None,
        temperature: float,
        max_tokens: int | None,
        effort_tier: str | None,
        stream: bool,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model or self.default_model,
            "messages": messages_to_openai_format(messages),
            "temperature": temperature,
        }
        if stream:
            body["stream"] = True
        if tools:
            body["tools"] = [t.to_openai_dict() for t in tools]
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if effort_tier is not None:
            # OpenRouter uses "reasoning.effort", OpenAI uses "reasoning_effort"
            # We pass both — providers ignore unknown fields.
            body["effort_tier"] = effort_tier
            body["reasoning"] = {"effort": effort_tier}
        return body

    async def _build_headers(self, scope: Scope) -> dict[str, str]:
        """Resolve the API key + build auth headers."""
        try:
            secret = self._resolver.resolve(self._api_key_ref)
        except Exception as e:
            raise LLMProviderError(
                f"failed to resolve {self.provider_name} API key from {self._api_key_ref!r}",
                internal=str(e),
            ) from e

        headers = {
            "Authorization": f"Bearer {secret.reveal()}",
            "Content-Type": "application/json",
            **scope_headers(scope),
            **self._extra_headers,
        }
        return headers

    async def _do_call(
        self,
        headers: dict[str, str],
        body: dict[str, Any],
        scope: Scope,
        model: str | None,
    ) -> LLMProviderResponse:
        """Single HTTP call to the provider. Raises on non-2xx."""
        start = time.monotonic()
        timeout = httpx.Timeout(self._timeout, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    json=body,
                    headers=headers,
                )
            except httpx.TimeoutException as e:
                raise LLMProviderTimeoutError(
                    f"{self.provider_name} call timed out after {self._timeout}s",
                    internal=str(e),
                ) from e
            except httpx.HTTPError as e:
                raise LLMProviderError(
                    f"{self.provider_name} HTTP error: {e}",
                    internal=str(e),
                ) from e

        latency_ms = (time.monotonic() - start) * 1000.0

        if resp.status_code >= 400:
            text = resp.text
            err = LLMProviderError(
                f"{self.provider_name} returned {resp.status_code}: {text[:200]}",
                internal=f"status={resp.status_code} body={text[:500]!r}",
            )
            err.status_code = resp.status_code  # type: ignore[attr-defined]
            raise err

        try:
            data = resp.json()
        except Exception as e:
            raise LLMProviderError(
                f"{self.provider_name} returned non-JSON: {resp.text[:200]}",
                internal=str(e),
            ) from e

        # Extract usage + cost.
        usage = data.get("usage", {}) or {}
        input_tokens = int(usage.get("prompt_tokens", 0))
        output_tokens = int(usage.get("completion_tokens", 0))
        cache_read = int(
            usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)
            if isinstance(usage.get("prompt_tokens_details"), dict)
            else 0
        )
        cache_write = int(
            usage.get("completion_tokens_details", {}).get("reasoning_tokens", 0)
            if isinstance(usage.get("completion_tokens_details"), dict)
            else 0
        )

        # Compute cost from pricing table.
        actual_model = data.get("model", model or self.default_model)
        cost = self._compute_cost(actual_model, input_tokens, output_tokens)

        # Request ID from header (preferred) or response body.
        request_id = resp.headers.get("x-zero-request-id", "") or data.get("id", "")

        # Parse content + tool calls.
        choices = data.get("choices") or []
        if not choices:
            raise LLMProviderError(f"{self.provider_name} response missing 'choices'")
        choice = choices[0]
        msg = choice.get("message", {}) or {}
        content = msg.get("content") or ""
        finish_reason = choice.get("finish_reason", "stop")
        tool_calls = parse_tool_calls(msg.get("tool_calls"))

        return LLMProviderResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            model=actual_model,
            request_id=request_id,
            cost_usd=cost,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            latency_ms=latency_ms,
        )

    def _compute_cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        """Compute USD cost from the pricing table.

        Falls back to exact-model match, then prefix match (e.g.
        ``gemini-2.0-flash-lite-001`` matches ``gemini-2.0-flash-lite``).
        Returns 0.0 if no pricing is known.
        """
        if not self.pricing:
            return 0.0
        # Exact match.
        if model in self.pricing:
            in_p, out_p = self.pricing[model]
            return (input_tokens / 1_000_000.0) * in_p + (output_tokens / 1_000_000.0) * out_p
        # Prefix match (longest prefix wins).
        best_match: str | None = None
        for priced_model in self.pricing:
            if model.startswith(priced_model) and (
                best_match is None or len(priced_model) > len(best_match)
            ):
                best_match = priced_model
        if best_match is not None:
            in_p, out_p = self.pricing[best_match]
            return (input_tokens / 1_000_000.0) * in_p + (output_tokens / 1_000_000.0) * out_p
        return 0.0
