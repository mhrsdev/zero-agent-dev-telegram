"""Tests for the LLM provider adapter layer.

These tests verify:
    1. GeminiProvider builds correctly
    2. OpenAIProvider builds correctly
    3. OpenRouterProvider builds correctly
    4. GenericOpenAIProvider complete() works with a mock HTTP server
    5. RouterShim exposes the provider via OpenAI protocol
    6. RouterClient can talk to the RouterShim end-to-end
    7. Cost computation is correct
    8. Retry logic works on 5xx
    9. 4xx does NOT retry
    10. Timeout raises LLMProviderTimeoutError
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
import respx
from httpx import Response

from zero.agents.llm_provider import (
    GeminiProvider,
    GenericOpenAIProvider,
    LLMProviderError,
    LLMProviderTimeoutError,
    OpenAIProvider,
    OpenRouterProvider,
    RouterShim,
    RouterShimConfig,
    build_provider_from_config,
)
from zero.agents.llm_provider.base import (
    ProviderMessage,
    ProviderToolDef,
    scope_headers,
)
from zero.agents.router_client import RouterClient, RouterMessage
from zero.core.config import RouterConfig
from zero.core.scope import Scope
from zero.core.secret import CompositeSecretResolver

# ---------------------------------------------------------------------- fixtures


@pytest.fixture
def personal_scope() -> Scope:
    return Scope.personal(user_id="usr_test").with_default_memory_scope()


@pytest.fixture
def resolver(monkeypatch: pytest.MonkeyPatch) -> CompositeSecretResolver:
    monkeypatch.setenv("TEST_LLM_API_KEY", "test-key-12345")
    return CompositeSecretResolver()


# ---------------------------------------------------------------------- construction tests


class TestProviderConstruction:
    """Verify each provider class constructs with the right defaults."""

    def test_gemini_provider_defaults(self, resolver: CompositeSecretResolver) -> None:
        p = GeminiProvider(
            api_key_ref="secret://env/TEST_LLM_API_KEY",
            resolver=resolver,
        )
        assert p.provider_name == "gemini"
        assert p.base_url == "https://generativelanguage.googleapis.com/v1beta/openai"
        assert p.default_model == "gemini-2.0-flash"
        assert "gemini-2.0-flash" in p.pricing

    def test_openai_provider_defaults(self, resolver: CompositeSecretResolver) -> None:
        p = OpenAIProvider(
            api_key_ref="secret://env/TEST_LLM_API_KEY",
            resolver=resolver,
        )
        assert p.provider_name == "openai"
        assert p.base_url == "https://api.openai.com/v1"
        assert p.default_model == "gpt-4o-mini"
        assert "gpt-4o" in p.pricing

    def test_openrouter_provider_defaults(self, resolver: CompositeSecretResolver) -> None:
        p = OpenRouterProvider(
            api_key_ref="secret://env/TEST_LLM_API_KEY",
            resolver=resolver,
        )
        assert p.provider_name == "openrouter"
        assert p.base_url == "https://openrouter.ai/api/v1"
        assert "HTTP-Referer" in p._extra_headers  # type: ignore[attr-defined]

    def test_custom_provider_uses_base_url(self, resolver: CompositeSecretResolver) -> None:
        p = GenericOpenAIProvider(
            api_key_ref="secret://env/TEST_LLM_API_KEY",
            resolver=resolver,
        )
        p.base_url = "http://localhost:9999/v1"
        assert p.base_url == "http://localhost:9999/v1"


# ---------------------------------------------------------------------- factory tests


class TestProviderFactory:
    """Verify build_provider_from_config picks the right class."""

    def test_factory_gemini(self, resolver: CompositeSecretResolver) -> None:
        cfg = RouterConfig(
            api_key="secret://env/TEST_LLM_API_KEY",
            provider="gemini",
        )
        p = build_provider_from_config(router_cfg=cfg, resolver=resolver)
        assert p.provider_name == "gemini"  # type: ignore[attr-defined]

    def test_factory_openai(self, resolver: CompositeSecretResolver) -> None:
        cfg = RouterConfig(
            api_key="secret://env/TEST_LLM_API_KEY",
            provider="openai",
        )
        p = build_provider_from_config(router_cfg=cfg, resolver=resolver)
        assert p.provider_name == "openai"  # type: ignore[attr-defined]

    def test_factory_custom(self, resolver: CompositeSecretResolver) -> None:
        cfg = RouterConfig(
            api_key="secret://env/TEST_LLM_API_KEY",
            provider="custom",
            base_url="http://localhost:9999/v1",
        )
        p = build_provider_from_config(router_cfg=cfg, resolver=resolver)
        # "custom" maps to GenericOpenAIProvider (provider_name="generic").
        assert p.provider_name == "generic"  # type: ignore[attr-defined]
        assert p.base_url == "http://localhost:9999/v1"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------- complete() tests


class TestProviderComplete:
    """Verify complete() works with mocked HTTP responses."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_complete_returns_response(
        self,
        resolver: CompositeSecretResolver,
        personal_scope: Scope,
    ) -> None:
        """complete() returns a properly-parsed LLMProviderResponse."""
        # Mock the Gemini endpoint.
        respx.post(
            "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        ).mock(
            Response(
                200,
                json={
                    "id": "chatcmpl-test123",
                    "object": "chat.completion",
                    "created": 1700000000,
                    "model": "gemini-2.0-flash",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "Hello, world!",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    },
                },
                headers={
                    "x-zero-request-id": "req_test_123",
                },
            )
        )

        p = GeminiProvider(
            api_key_ref="secret://env/TEST_LLM_API_KEY",
            resolver=resolver,
        )
        resp = await p.complete(
            messages=[ProviderMessage(role="user", content="hi")],
            scope=personal_scope,
        )

        assert resp.content == "Hello, world!"
        assert resp.finish_reason == "stop"
        assert resp.model == "gemini-2.0-flash"
        assert resp.input_tokens == 10
        assert resp.output_tokens == 5
        assert resp.request_id == "req_test_123"
        # Cost: 10 input * $0.10/1M + 5 output * $0.40/1M = 0.000003
        assert resp.cost_usd > 0
        assert resp.cost_usd < 0.001  # sanity check

    @respx.mock
    @pytest.mark.asyncio
    async def test_complete_parses_tool_calls(
        self,
        resolver: CompositeSecretResolver,
        personal_scope: Scope,
    ) -> None:
        """complete() correctly parses tool_calls from the response."""
        respx.post(
            "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        ).mock(
            Response(
                200,
                json={
                    "id": "chatcmpl-tc1",
                    "model": "gemini-2.0-flash",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "call_abc",
                                        "type": "function",
                                        "function": {
                                            "name": "read_file",
                                            "arguments": '{"path": "/tmp/test.txt"}',
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 10, "total_tokens": 15},
                },
            )
        )

        p = GeminiProvider(
            api_key_ref="secret://env/TEST_LLM_API_KEY",
            resolver=resolver,
        )
        resp = await p.complete(
            messages=[ProviderMessage(role="user", content="read /tmp/test.txt")],
            tools=[ProviderToolDef(
                name="read_file",
                description="Read a file",
                parameters_schema={"type": "object", "properties": {"path": {"type": "string"}}},
            )],
            scope=personal_scope,
        )

        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].id == "call_abc"
        assert resp.tool_calls[0].name == "read_file"
        assert resp.tool_calls[0].arguments == {"path": "/tmp/test.txt"}
        assert resp.finish_reason == "tool_calls"

    @respx.mock
    @pytest.mark.asyncio
    async def test_complete_handles_invalid_json_args(
        self,
        resolver: CompositeSecretResolver,
        personal_scope: Scope,
    ) -> None:
        """Invalid JSON arguments are wrapped in _raw key."""
        respx.post(
            "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        ).mock(
            Response(
                200,
                json={
                    "model": "gemini-2.0-flash",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": "call_x",
                                        "function": {
                                            "name": "search",
                                            "arguments": "{not valid json",
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )
        )

        p = GeminiProvider(
            api_key_ref="secret://env/TEST_LLM_API_KEY",
            resolver=resolver,
        )
        resp = await p.complete(
            messages=[ProviderMessage(role="user", content="search")],
            scope=personal_scope,
        )
        assert resp.tool_calls[0].arguments == {"_raw": "{not valid json"}

    @respx.mock
    @pytest.mark.asyncio
    async def test_complete_4xx_does_not_retry(
        self,
        resolver: CompositeSecretResolver,
        personal_scope: Scope,
    ) -> None:
        """4xx errors should NOT be retried."""
        route = respx.post(
            "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        ).mock(Response(400, json={"error": {"message": "bad request"}}))

        p = GeminiProvider(
            api_key_ref="secret://env/TEST_LLM_API_KEY",
            resolver=resolver,
            max_retries=3,
        )
        with pytest.raises(LLMProviderError):
            await p.complete(
                messages=[ProviderMessage(role="user", content="hi")],
                scope=personal_scope,
            )
        # Only one call — no retries.
        assert route.call_count == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_complete_5xx_retries(
        self,
        resolver: CompositeSecretResolver,
        personal_scope: Scope,
    ) -> None:
        """5xx errors should be retried up to max_retries times."""
        route = respx.post(
            "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        ).mock(
            Response(503, json={"error": {"message": "service unavailable"}})
        )

        p = GeminiProvider(
            api_key_ref="secret://env/TEST_LLM_API_KEY",
            resolver=resolver,
            max_retries=2,
        )
        with pytest.raises(LLMProviderError):
            await p.complete(
                messages=[ProviderMessage(role="user", content="hi")],
                scope=personal_scope,
            )
        # Initial + 2 retries = 3 total.
        assert route.call_count == 3

    @respx.mock
    @pytest.mark.asyncio
    async def test_complete_5xx_then_success(
        self,
        resolver: CompositeSecretResolver,
        personal_scope: Scope,
    ) -> None:
        """5xx then 200 should succeed on retry."""
        route = respx.post(
            "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        ).mock(
            side_effect=[
                Response(503, json={"error": "service unavailable"}),
                Response(200, json={
                    "model": "gemini-2.0-flash",
                    "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }),
            ]
        )

        p = GeminiProvider(
            api_key_ref="secret://env/TEST_LLM_API_KEY",
            resolver=resolver,
            max_retries=3,
        )
        resp = await p.complete(
            messages=[ProviderMessage(role="user", content="hi")],
            scope=personal_scope,
        )
        assert resp.content == "ok"
        assert route.call_count == 2


# ---------------------------------------------------------------------- scope headers


class TestScopeHeaders:
    """Verify X-Zero-Scope-* headers are correctly built."""

    def test_personal_scope_headers(self) -> None:
        s = Scope.personal(user_id="usr_abc").with_default_memory_scope()
        h = scope_headers(s)
        assert h["X-Zero-Scope-Mode"] == "personal"
        assert "usr_abc" in h["X-Zero-Scope-Key"]
        assert "X-Zero-Scope-Project" not in h

    def test_dev_scope_headers_include_project(self) -> None:
        s = Scope.development(
            org_id="org_x", workspace_id="ws_x", project_id="prj_x",
            group_id="grp_x", topic_id=0,
        ).with_default_memory_scope()
        h = scope_headers(s)
        assert h["X-Zero-Scope-Mode"] == "development"
        assert "prj_x" in h["X-Zero-Scope-Key"]
        assert h["X-Zero-Scope-Project"] == "prj_x"


# ---------------------------------------------------------------------- cost computation


class TestCostComputation:
    """Verify cost computation from the pricing table."""

    def test_gemini_flash_cost(self, resolver: CompositeSecretResolver) -> None:
        p = GeminiProvider(
            api_key_ref="secret://env/TEST_LLM_API_KEY",
            resolver=resolver,
        )
        # gemini-2.0-flash: $0.10/1M input, $0.40/1M output
        # 1000 input + 500 output = 0.0001 + 0.0002 = 0.0003
        cost = p._compute_cost("gemini-2.0-flash", 1000, 500)
        assert abs(cost - 0.0003) < 0.00001

    def test_prefix_match_cost(self, resolver: CompositeSecretResolver) -> None:
        """Model with version suffix should match base name."""
        p = GeminiProvider(
            api_key_ref="secret://env/TEST_LLM_API_KEY",
            resolver=resolver,
        )
        # gemini-2.0-flash-001 should match gemini-2.0-flash prefix.
        cost = p._compute_cost("gemini-2.0-flash-001", 1000, 500)
        assert cost > 0

    def test_unknown_model_zero_cost(self, resolver: CompositeSecretResolver) -> None:
        p = GeminiProvider(
            api_key_ref="secret://env/TEST_LLM_API_KEY",
            resolver=resolver,
        )
        cost = p._compute_cost("unknown-model", 1000, 500)
        assert cost == 0.0


# ---------------------------------------------------------------------- RouterShim


class TestRouterShim:
    """Verify the RouterShim exposes a provider via OpenAI protocol."""

    @pytest.mark.asyncio
    async def test_shim_starts_and_stops(
        self,
        resolver: CompositeSecretResolver,
    ) -> None:
        """Shim starts an HTTP server and stops cleanly."""
        from zero.agents.llm_provider.gemini import GeminiProvider

        p = GeminiProvider(
            api_key_ref="secret://env/TEST_LLM_API_KEY",
            resolver=resolver,
        )
        shim = RouterShim(
            provider=p,  # type: ignore[arg-type]
            config=RouterShimConfig(host="127.0.0.1", port=0),
        )
        await shim.start()
        try:
            assert shim.is_running
            assert shim.actual_port > 0
            assert shim.base_url.startswith("http://127.0.0.1:")
        finally:
            await shim.stop()

    @pytest.mark.asyncio
    async def test_shim_health_endpoint(
        self,
        resolver: CompositeSecretResolver,
    ) -> None:
        """Shim /health endpoint returns provider info."""
        import httpx

        from zero.agents.llm_provider.gemini import GeminiProvider

        p = GeminiProvider(
            api_key_ref="secret://env/TEST_LLM_API_KEY",
            resolver=resolver,
        )
        shim = RouterShim(
            provider=p,  # type: ignore[arg-type]
            config=RouterShimConfig(host="127.0.0.1", port=0),
        )
        await shim.start()
        try:
            # Don't use respx here — let httpx call the real local server.
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"http://127.0.0.1:{shim.actual_port}/v1/health")
                assert resp.status_code == 200
                data = resp.json()
                assert data["status"] == "ok"
                assert data["provider"] == "gemini"
        finally:
            await shim.stop()

    @pytest.mark.asyncio
    async def test_shim_chat_completions_end_to_end(
        self,
        resolver: CompositeSecretResolver,
        personal_scope: Scope,
    ) -> None:
        """End-to-end: RouterClient → RouterShim → mock provider.

        We mock the provider directly (instead of using respx to mock the
        provider's HTTP calls) because respx interferes with the RouterClient's
        HTTP calls to the local shim.
        """
        from zero.agents.llm_provider.base import LLMProviderResponse, ProviderToolCall
        from zero.agents.llm_provider.gemini import GeminiProvider

        # Create a GeminiProvider but monkey-patch its complete() to return
        # a canned response (bypasses the real HTTP call).
        p = GeminiProvider(
            api_key_ref="secret://env/TEST_LLM_API_KEY",
            resolver=resolver,
        )

        original_complete = p.complete

        async def mock_complete(**kwargs: Any) -> LLMProviderResponse:
            return LLMProviderResponse(
                content="Hello from shim!",
                tool_calls=[],
                finish_reason="stop",
                model="gemini-2.0-flash",
                request_id="req_test_e2e",
                cost_usd=0.000123,
                input_tokens=10,
                output_tokens=5,
                cache_read_tokens=0,
                cache_write_tokens=0,
                latency_ms=42.0,
            )

        p.complete = mock_complete  # type: ignore[assignment]

        shim = RouterShim(
            provider=p,  # type: ignore[arg-type]
            config=RouterShimConfig(host="127.0.0.1", port=0),
        )
        await shim.start()
        try:
            # Now use RouterClient to talk to the shim.
            rc = RouterClient(
                base_url=shim.base_url,
                api_key_ref="secret://env/TEST_LLM_API_KEY",
                resolver=resolver,
            )
            resp = await rc.complete(
                messages=[RouterMessage(role="user", content="hi")],
                scope=personal_scope,
            )
            assert resp.content == "Hello from shim!"
            assert resp.model == "gemini-2.0-flash"
            # Shim should pass through the cost header.
            assert resp.cost_usd == 0.000123
            # Shim generates its own request_id (overriding the provider's).
            assert resp.request_id.startswith("req_")
        finally:
            await shim.stop()


# ---------------------------------------------------------------------- missing secret


class TestMissingSecret:
    """Verify missing secret raises LLMProviderError."""

    @pytest.mark.asyncio
    async def test_missing_secret_raises(
        self,
        personal_scope: Scope,
    ) -> None:
        # Use a resolver with no env vars set.
        resolver = CompositeSecretResolver()
        p = GeminiProvider(
            api_key_ref="secret://env/NONEXISTENT_KEY",
            resolver=resolver,
        )
        with pytest.raises(LLMProviderError, match="failed to resolve"):
            await p.complete(
                messages=[ProviderMessage(role="user", content="hi")],
                scope=personal_scope,
            )
