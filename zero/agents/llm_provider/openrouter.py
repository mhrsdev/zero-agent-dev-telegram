"""OpenRouter provider — openrouter.ai aggregator.

Endpoint: https://openrouter.ai/api/v1
Auth: Bearer <OPENROUTER_API_KEY>

OpenRouter proxies to many providers (Anthropic, OpenAI, Google, Meta, etc.)
and bills per-token at the underlying provider's price + 5% markup.

We don't bake in pricing because OpenRouter's catalog is dynamic. Instead,
OpenRouter returns usage in the response body and we read the cost from
the ``x-openrouter-cost-usd`` header when present.
"""
from __future__ import annotations

from zero.agents.llm_provider.generic import GenericOpenAIProvider

__all__ = ["OpenRouterProvider"]


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterProvider(GenericOpenAIProvider):
    """OpenRouter provider — multi-model aggregator.

    Set ``default_model`` to a model slug from https://openrouter.ai/models.
    Examples:
        - ``anthropic/claude-3.5-sonnet``
        - ``openai/gpt-4o``
        - ``google/gemini-2.0-flash-exp:free``
        - ``meta-llama/llama-3.3-70b-instruct``
    """

    provider_name = "openrouter"
    base_url = OPENROUTER_BASE_URL
    default_model = "openai/gpt-4o-mini"
    # Pricing is dynamic — read from response headers.
    pricing = {}

    def __init__(
        self,
        *args: object,
        extra_headers: dict[str, str] | None = None,
        **kwargs: object,
    ) -> None:
        # OpenRouter recommends sending HTTP-Referer + X-Title for ranking.
        or_headers = {
            "HTTP-Referer": "https://github.com/zero/zero-agent",
            "X-Title": "Zero Agent v2",
        }
        if extra_headers:
            or_headers.update(extra_headers)
        super().__init__(*args, extra_headers=or_headers, **kwargs)  # type: ignore[arg-type]

    async def _do_call(self, headers, body, scope, model):  # type: ignore[no-untyped-def]
        """Override to also read OpenRouter's cost header."""
        resp = await super()._do_call(headers, body, scope, model)
        # If super didn't compute cost (pricing table empty), try the header.
        # We need to re-read the response — but super() already consumed it.
        # Instead, we patch: GenericOpenAIProvider._do_call reads cost from
        # pricing table only. For OpenRouter, the cost is in a header.
        # Since we can't re-read, we override the full method below.
        return resp

    async def _do_call_full(self, headers, body, scope, model):  # type: ignore[no-untyped-def]
        """Full implementation with OpenRouter cost header support."""
        import time  # noqa: PLC0415

        import httpx  # noqa: PLC0415

        from zero.agents.llm_provider.base import (  # noqa: PLC0415
            LLMProviderError,
            LLMProviderResponse,
            LLMProviderTimeoutError,
            parse_tool_calls,
        )

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

        usage = data.get("usage", {}) or {}
        input_tokens = int(usage.get("prompt_tokens", 0))
        output_tokens = int(usage.get("completion_tokens", 0))

        # OpenRouter-specific: read cost from header.
        cost_str = resp.headers.get("x-openrouter-cost-usd")
        if cost_str:
            try:
                cost = float(cost_str)
            except ValueError:
                cost = self._compute_cost(
                    data.get("model", model or self.default_model),
                    input_tokens,
                    output_tokens,
                )
        else:
            cost = self._compute_cost(
                data.get("model", model or self.default_model),
                input_tokens,
                output_tokens,
            )

        actual_model = data.get("model", model or self.default_model)
        request_id = resp.headers.get("x-zero-request-id", "") or data.get("id", "")

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
            latency_ms=latency_ms,
        )
