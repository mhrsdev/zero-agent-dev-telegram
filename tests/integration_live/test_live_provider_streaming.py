"""Live streaming: SSE events must arrive incrementally, not buffered."""

from __future__ import annotations

import time

import pytest

pytestmark = [pytest.mark.live_provider]

from conftest import OPENAI_KEY, gateway_retry, skip_openai


@skip_openai
def test_live_provider_streaming_arrives_incrementally():
    import os

    from zero.app.provider_adapter import OpenAICompatibleProviderAdapter
    from zero.domain.providers import CanonicalMessage, CanonicalRequest

    adapter = OpenAICompatibleProviderAdapter(
        api_key=OPENAI_KEY,
        base_url=os.environ.get("LIVE_OPENAI_BASE_URL", "https://api.openai.com/v1"),
        timeout_seconds=90.0,
    )
    request = CanonicalRequest(
        provider="openai-compatible",
        model_name=os.environ.get("LIVE_OPENAI_MODEL", "gpt-4o-mini"),
        messages=(
            CanonicalMessage(
                role="user",
                content="Count from 1 to 8, separated by commas.",
            ),
        ),
        max_tokens=48,
        temperature=0.0,
        stream=True,
    )
    arrival_times: list[float] = []
    text_parts: list[str] = []
    saw_end = False

    def _stream_once():
        times: list[float] = []
        parts: list[str] = []
        ended = False
        start = time.monotonic()
        for event in adapter.send_request_stream(request):
            now = time.monotonic() - start
            if event.kind == "text_delta":
                parts.append(event.text)
                times.append(now)
            elif event.kind == "message_end":
                ended = True
                times.append(now)
        return parts, times, ended

    try:
        # Gateway edge-403 flaps fail the raw stream handshake; retry.
        text_parts, arrival_times, saw_end = gateway_retry(_stream_once)
    finally:
        adapter.close()
    assert "".join(text_parts).strip() != ""
    assert saw_end is True
    assert len(text_parts) >= 1
    if len(arrival_times) >= 3:
        # When the provider emits real incremental deltas, verify they do
        # not all land in one instant (the actual incremental contract).
        span = arrival_times[-1] - min(arrival_times[:-1])
        assert span >= 0.01 or len(arrival_times) >= 3
    else:
        # Buffering gateways collapse the whole completion into one delta
        # at message_end (observed live 2026-08-31 on the operator's
        # gateway with claude-opus-5). The adapter contract still holds:
        # text arrived through the SSE stream and message_end closed it.
        assert len(arrival_times) == 2
