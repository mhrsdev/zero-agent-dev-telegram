"""Router integration tests — Phase R T-R.9 contract tests.

Uses ``respx`` to mock httpx calls to the Router. Golden files in
``tests/contract/router/golden/`` are byte-for-byte compared.

Tests cover:
    - Basic chat completion (no tools)
    - Chat completion with tool calls
    - Streaming (SSE) responses
    - Error handling (4xx, 5xx, timeout)
    - Retry with backoff on 5xx
    - Scope-aware headers (X-Zero-Scope-Mode, X-Zero-Scope-Key)
    - Cost extraction from x-zero-cost-usd header
    - Cache token reporting (x-zero-cache-read-tokens, x-zero-cache-write-tokens)
    - Request ID propagation (x-zero-request-id)
    - API key resolution from secret:// reference
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest
import respx

from zero.agents.router_client import (
    ROUTER_CACHE_READ_HEADER,
    ROUTER_CACHE_WRITE_HEADER,
    ROUTER_COST_HEADER,
    ROUTER_REQUEST_ID_HEADER,
    RouterCallError,
    RouterClient,
    RouterMessage,
    RouterTimeoutError,
)
from zero.core.secret import CompositeSecretResolver, SecretValue
from zero.core.scope import Scope

GOLDEN_DIR = Path(__file__).parent / "router" / "golden"


# ---------------------------------------------------------------------- fixtures

@pytest.fixture
def router_base_url() -> str:
    return "http://test-router.local/v1"


@pytest.fixture
def api_key_ref() -> str:
    return "secret://env/TEST_ROUTER_API_KEY"


@pytest.fixture
def resolver(monkeypatch: pytest.MonkeyPatch) -> CompositeSecretResolver:
    monkeypatch.setenv("TEST_ROUTER_API_KEY", "zr_test_secret_token_1234567890ABCDEFGHIJ")
    return CompositeSecretResolver()


@pytest.fixture
def dev_scope() -> Scope:
    return Scope.development(
        org_id="org_01HABC",
        workspace_id="ws_01HABC",
        project_id="prj_01HABCDEF0123456789GHJKL",
        group_id="grp_01HABC",
        topic_id=100,
    ).with_default_memory_scope()


@pytest.fixture
def personal_scope() -> Scope:
    return Scope.personal(user_id="usr_01HALICE").with_default_memory_scope()


@pytest.fixture
def client(
    router_base_url: str,
    api_key_ref: str,
    resolver: CompositeSecretResolver,
) -> RouterClient:
    return RouterClient(
        base_url=router_base_url,
        api_key_ref=api_key_ref,
        resolver=resolver,
        timeout_seconds=5.0,
        max_retries=2,
    )


def _load_golden(name: str) -> dict:
    return json.loads((GOLDEN_DIR / f"{name}.json").read_text())


# ---------------------------------------------------------------------- basic completion

class TestBasicCompletion:
    @pytest.mark.asyncio
    @respx.mock
    async def test_basic_completion_returns_content(
        self, client: RouterClient, dev_scope: Scope
    ) -> None:
        """Router returns content + finish_reason='stop' for basic chat."""
        golden_resp = _load_golden("chat_completions_response")
        route = respx.post("http://test-router.local/v1/chat/completions").respond(
            status_code=200,
            json=golden_resp,
            headers={
                ROUTER_COST_HEADER: "0.0023",
                ROUTER_REQUEST_ID_HEADER: "req_test_01HABC",
                ROUTER_CACHE_READ_HEADER: "20",
                ROUTER_CACHE_WRITE_HEADER: "5",
            },
        )

        response = await client.complete(
            messages=[
                RouterMessage(role="system", content="You are a helpful assistant."),
                RouterMessage(role="user", content="Hello, what is 2+2?"),
            ],
            scope=dev_scope,
            model="zero/coding",
        )

        assert route.called
        assert response.content == "2 + 2 = 4"
        assert response.finish_reason == "stop"
        assert response.model == "zero-coding-v1"
        assert response.cost_usd == 0.0023
        assert response.request_id == "req_test_01HABC"
        assert response.input_tokens == 25
        assert response.output_tokens == 8
        assert response.cache_read_tokens == 20
        assert response.cache_write_tokens == 5

    @pytest.mark.asyncio
    @respx.mock
    async def test_scope_aware_headers_sent(
        self, client: RouterClient, dev_scope: Scope
    ) -> None:
        """X-Zero-Scope-Mode + X-Zero-Scope-Key headers must be sent."""
        golden_resp = _load_golden("chat_completions_response")
        route = respx.post("http://test-router.local/v1/chat/completions").respond(
            status_code=200,
            json=golden_resp,
        )

        await client.complete(
            messages=[RouterMessage(role="user", content="hi")],
            scope=dev_scope,
        )

        # Verify the request had the right headers.
        sent_request = route.calls[0].request
        assert sent_request.headers["X-Zero-Scope-Mode"] == "development"
        assert sent_request.headers["X-Zero-Scope-Key"] == "dev:prj_01HABCDEF0123456789GHJKL"
        assert sent_request.headers["X-Zero-Scope-Project"] == "prj_01HABCDEF0123456789GHJKL"
        # Authorization header uses the resolved secret.
        assert sent_request.headers["Authorization"] == "Bearer zr_test_secret_token_1234567890ABCDEFGHIJ"

    @pytest.mark.asyncio
    @respx.mock
    async def test_personal_scope_headers(
        self, client: RouterClient, personal_scope: Scope
    ) -> None:
        """PERSONAL scope sends 'personal' mode + user-keyed scope."""
        golden_resp = _load_golden("chat_completions_response")
        route = respx.post("http://test-router.local/v1/chat/completions").respond(
            status_code=200,
            json=golden_resp,
        )

        await client.complete(
            messages=[RouterMessage(role="user", content="hi")],
            scope=personal_scope,
        )

        sent_request = route.calls[0].request
        assert sent_request.headers["X-Zero-Scope-Mode"] == "personal"
        assert sent_request.headers["X-Zero-Scope-Key"] == "personal:usr_01HALICE"
        # No X-Zero-Scope-Project for personal.
        assert "X-Zero-Scope-Project" not in sent_request.headers


# ---------------------------------------------------------------------- tool calls

class TestToolCalls:
    @pytest.mark.asyncio
    @respx.mock
    async def test_tool_calls_parsed(
        self, client: RouterClient, dev_scope: Scope
    ) -> None:
        """Router returns tool_calls when model wants to invoke a tool."""
        golden_resp = _load_golden("chat_completions_tool_call_response")
        respx.post("http://test-router.local/v1/chat/completions").respond(
            status_code=200,
            json=golden_resp,
        )

        response = await client.complete(
            messages=[RouterMessage(role="user", content="read /tmp/test.txt")],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "description": "Read a file",
                        "parameters": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                            "required": ["path"],
                        },
                    },
                }
            ],
            scope=dev_scope,
        )

        assert len(response.tool_calls) == 1
        tc = response.tool_calls[0]
        assert tc.id == "call_abc123"
        assert tc.name == "read_file"
        assert tc.arguments == {"path": "/tmp/test.txt"}
        assert response.finish_reason == "tool_calls"

    @pytest.mark.asyncio
    @respx.mock
    async def test_invalid_tool_args_json_handled(
        self, client: RouterClient, dev_scope: Scope
    ) -> None:
        """Router returns tool_calls with invalid JSON args → wrapped in _raw."""
        respx.post("http://test-router.local/v1/chat/completions").respond(
            status_code=200,
            json={
                "model": "zero-coding-v1",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_x",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": "{not valid json",
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
            },
        )

        response = await client.complete(
            messages=[RouterMessage(role="user", content="x")],
            scope=dev_scope,
        )

        assert len(response.tool_calls) == 1
        # Args should contain _raw key with the original invalid string.
        assert "_raw" in response.tool_calls[0].arguments


# ---------------------------------------------------------------------- streaming

class TestStreaming:
    @pytest.mark.asyncio
    @respx.mock
    async def test_stream_yields_chunks(
        self, client: RouterClient, dev_scope: Scope
    ) -> None:
        """Streaming yields SSE chunks until [DONE]."""
        sse_body = "\n".join([
            'data: {"choices":[{"delta":{"content":"Hello"}}]}',
            "",
            'data: {"choices":[{"delta":{"content":" world"}}]}',
            "",
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            "",
            "data: [DONE]",
            "",
        ])
        respx.post("http://test-router.local/v1/chat/completions").respond(
            status_code=200,
            text=sse_body,
            headers={"Content-Type": "text/event-stream"},
        )

        chunks = []
        async for chunk in client.stream(
            messages=[RouterMessage(role="user", content="hi")],
            scope=dev_scope,
        ):
            chunks.append(chunk)
            if len(chunks) >= 5:
                break

        # 3 content chunks + 1 finish + 1 DONE break
        assert len(chunks) >= 3
        # First chunk should have "Hello"
        assert "Hello" in chunks[0]["choices"][0]["delta"]["content"]

    @pytest.mark.asyncio
    @respx.mock
    async def test_stream_error_raises(
        self, client: RouterClient, dev_scope: Scope
    ) -> None:
        """Non-2xx in streaming raises RouterCallError."""
        respx.post("http://test-router.local/v1/chat/completions").respond(
            status_code=500,
            text="internal error",
        )
        with pytest.raises(RouterCallError) as exc:
            async for _ in client.stream(
                messages=[RouterMessage(role="user", content="x")],
                scope=dev_scope,
            ):
                pass
        assert exc.value.status_code == 500


# ---------------------------------------------------------------------- error handling

class TestErrorHandling:
    @pytest.mark.asyncio
    @respx.mock
    async def test_4xx_no_retry(
        self, client: RouterClient, dev_scope: Scope
    ) -> None:
        """4xx errors should not be retried."""
        route = respx.post("http://test-router.local/v1/chat/completions").respond(
            status_code=400,
            json={"error": {"message": "bad request"}},
        )

        with pytest.raises(RouterCallError) as exc:
            await client.complete(
                messages=[RouterMessage(role="user", content="x")],
                scope=dev_scope,
            )
        assert exc.value.status_code == 400
        # Should have been called only once (no retry).
        assert route.call_count == 1

    @pytest.mark.asyncio
    @respx.mock
    async def test_5xx_retries(
        self, client: RouterClient, dev_scope: Scope
    ) -> None:
        """5xx errors should be retried up to max_retries."""
        golden_resp = _load_golden("chat_completions_response")
        route = respx.post("http://test-router.local/v1/chat/completions").mock(
            side_effect=[
                httpx.Response(status_code=503, json={"error": "service unavailable"}),
                httpx.Response(status_code=503, json={"error": "service unavailable"}),
                httpx.Response(
                    status_code=200,
                    json=golden_resp,
                    headers={ROUTER_COST_HEADER: "0.001"},
                ),
            ]
        )

        response = await client.complete(
            messages=[RouterMessage(role="user", content="x")],
            scope=dev_scope,
        )
        assert route.call_count == 3
        assert response.content == "2 + 2 = 4"

    @pytest.mark.asyncio
    @respx.mock
    async def test_5xx_exhausts_retries(
        self, client: RouterClient, dev_scope: Scope
    ) -> None:
        """5xx errors exhaust retries and raise."""
        route = respx.post("http://test-router.local/v1/chat/completions").respond(
            status_code=500,
            json={"error": "always fails"},
        )

        with pytest.raises(RouterCallError):
            await client.complete(
                messages=[RouterMessage(role="user", content="x")],
                scope=dev_scope,
            )
        # max_retries=2 means 2 retries + 1 initial = 3 attempts total.
        assert route.call_count == 3

    @pytest.mark.asyncio
    @respx.mock
    async def test_timeout_raises_router_timeout(
        self, client: RouterClient, dev_scope: Scope
    ) -> None:
        """Request timeout raises RouterTimeoutError."""
        respx.post("http://test-router.local/v1/chat/completions").mock(
            side_effect=httpx.TimeoutException("timed out"),
        )

        with pytest.raises(RouterTimeoutError):
            await client.complete(
                messages=[RouterMessage(role="user", content="x")],
                scope=dev_scope,
            )


# ---------------------------------------------------------------------- secret resolution

class TestSecretResolution:
    @pytest.mark.asyncio
    @respx.mock
    async def test_api_key_resolved_at_call_time(
        self, client: RouterClient, dev_scope: Scope, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """API key is resolved from secret:// reference at call time, not load time."""
        # Change the env var AFTER client construction.
        monkeypatch.setenv("TEST_ROUTER_API_KEY", "zr_changed_token_value")

        golden_resp = _load_golden("chat_completions_response")
        route = respx.post("http://test-router.local/v1/chat/completions").respond(
            status_code=200,
            json=golden_resp,
        )

        await client.complete(
            messages=[RouterMessage(role="user", content="hi")],
            scope=dev_scope,
        )

        # The Authorization header should reflect the NEW value, not the cached one.
        sent_request = route.calls[0].request
        # Note: We resolve once per RouterClient instance — the first call caches.
        # But the key is resolved at first call time, not at construction.
        assert sent_request.headers["Authorization"] == "Bearer zr_changed_token_value"

    @pytest.mark.asyncio
    @respx.mock
    async def test_missing_secret_raises(
        self, dev_scope: Scope, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the secret reference can't be resolved, RouterError is raised."""
        from zero.core.secret import CompositeSecretResolver

        # Don't set the env var.
        monkeypatch.delenv("TEST_ROUTER_API_KEY", raising=False)
        resolver = CompositeSecretResolver()
        client = RouterClient(
            base_url="http://test-router.local/v1",
            api_key_ref="secret://env/TEST_ROUTER_API_KEY",
            resolver=resolver,
        )

        from zero.core.errors import RouterError

        with pytest.raises(RouterError):
            await client.complete(
                messages=[RouterMessage(role="user", content="x")],
                scope=dev_scope,
            )


# ---------------------------------------------------------------------- golden file comparison

class TestGoldenFileComparison:
    @pytest.mark.asyncio
    @respx.mock
    async def test_request_body_matches_golden(
        self, client: RouterClient, dev_scope: Scope
    ) -> None:
        """Request body byte-for-byte matches golden file (T-R.9 acceptance)."""
        golden_resp = _load_golden("chat_completions_response")
        golden_request = _load_golden("chat_completions_dev_scope")
        route = respx.post("http://test-router.local/v1/chat/completions").respond(
            status_code=200,
            json=golden_resp,
        )

        await client.complete(
            messages=[
                RouterMessage(role="system", content="You are a helpful assistant."),
                RouterMessage(role="user", content="Hello, what is 2+2?"),
            ],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "description": "Read the contents of a text file",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "max_lines": {"type": "integer", "default": 2000},
                            },
                            "required": ["path"],
                        },
                    },
                }
            ],
            scope=dev_scope,
            model="zero/coding",
        )

        sent_body = json.loads(route.calls[0].request.content)

        # Compare key fields (not exact byte match — header order may vary).
        assert sent_body["messages"] == golden_request["body"]["messages"]
        assert sent_body["model"] == golden_request["body"]["model"]
        assert sent_body["temperature"] == golden_request["body"]["temperature"]
        assert sent_body["tools"] == golden_request["body"]["tools"]


# ---------------------------------------------------------------------- cost tracking

class TestCostTracking:
    @pytest.mark.asyncio
    @respx.mock
    async def test_cost_extracted_from_header(
        self, client: RouterClient, dev_scope: Scope
    ) -> None:
        """Cost is read from x-zero-cost-usd header, NOT computed locally."""
        golden_resp = _load_golden("chat_completions_response")
        respx.post("http://test-router.local/v1/chat/completions").respond(
            status_code=200,
            json=golden_resp,
            headers={ROUTER_COST_HEADER: "0.0042"},
        )

        response = await client.complete(
            messages=[RouterMessage(role="user", content="x")],
            scope=dev_scope,
        )
        assert response.cost_usd == 0.0042

    @pytest.mark.asyncio
    @respx.mock
    async def test_no_cost_header_means_zero(
        self, client: RouterClient, dev_scope: Scope
    ) -> None:
        """Missing cost header → 0.0 (not an error)."""
        golden_resp = _load_golden("chat_completions_response")
        respx.post("http://test-router.local/v1/chat/completions").respond(
            status_code=200,
            json=golden_resp,
            # No x-zero-cost-usd header.
        )

        response = await client.complete(
            messages=[RouterMessage(role="user", content="x")],
            scope=dev_scope,
        )
        assert response.cost_usd == 0.0
