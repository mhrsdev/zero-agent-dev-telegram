"""Regression tests: a stream break BEFORE any consumed SSE event is a
retryable transport failure, not ``unknown_outcome``.

Real-run bug (r7): the gateway dropped the connection ~28s into a
sub-agent review request, before any SSE data had arrived. The adapter
raised ``ProviderUnknownOutcomeError``, the scheduler paused the whole
execution with "provider outcome unknown; reconciliation required", and
a single network blip wedged an otherwise healthy 7/11-completed run.
Nothing had been consumed, so there is nothing to double-deliver — the
failure is equivalent to ConnectError (retryable transient).
"""

from __future__ import annotations

import httpx
import pytest

from zero.app.provider_adapter import OpenAICompatibleProviderAdapter
from zero.app.provider_service import ProviderService
from zero.domain.providers import (
    CanonicalMessage,
    CanonicalRequest,
    ProviderError,
    ProviderUnknownOutcomeError,
)

SSE_BODY = (
    'data: {"id":"chatcmpl_1","choices":[{"delta":{"content":"partial"},"finish_reason":null}]}\n\n'
    'data: {"id":"chatcmpl_1","choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
    "data: [DONE]\n\n"
)


def _request() -> CanonicalRequest:
    return CanonicalRequest(
        provider="openai-compatible",
        model_name="test-model",
        messages=(CanonicalMessage(role="user", content="Hello"),),
        max_tokens=128,
    )


def _adapter(handler) -> OpenAICompatibleProviderAdapter:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OpenAICompatibleProviderAdapter(
        api_key="synthetic-test-key",
        base_url="https://provider.invalid/v1",
        client=client,
    )


def test_stream_break_before_first_event_is_retryable_transient() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        # The response HEADERS arrive, then the connection drops before
        # a single SSE data line is sent.
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b"",
        )

    adapter = _adapter(handler)
    with pytest.raises(ProviderError) as excinfo:
        list(adapter.send_request_stream(_request()))
    assert "before any stream data" in str(excinfo.value)
    assert "unknown" not in str(excinfo.value)
    assert calls["n"] == 1


def test_stream_break_midway_stays_unknown_outcome() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # One data event is delivered, then the body truncates.
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                'data: {"id":"chatcmpl_1","choices":[{"delta":{"content":"partial"}}]}\n\n'
            ).encode(),
        )

    adapter = _adapter(handler)
    with pytest.raises(ProviderUnknownOutcomeError):
        list(adapter.send_request_stream(_request()))


def test_healthy_stream_still_yields_events() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=SSE_BODY.encode(),
        )

    adapter = _adapter(handler)
    events = list(adapter.send_request_stream(_request()))
    kinds = [event.kind for event in events]
    assert "text_delta" in kinds
    assert kinds[-1] == "message_end"


def test_terminal_marker_break_is_classified_transient_for_retry() -> None:
    """The service layer must classify a no-terminal-marker stream break
    as transient so the bounded same-provider retry handles it, instead
    of pausing the whole execution for human reconciliation."""
    exc = ProviderUnknownOutcomeError("provider stream ended without a terminal message marker")
    assert ProviderService._classify_error(None, exc) == "transient"

    # Other unknown-outcome causes keep their conservative class.
    lease_exc = ProviderUnknownOutcomeError("provider request outcome is unknown")
    assert ProviderService._classify_error(None, lease_exc) == "unknown_outcome"
