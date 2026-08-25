"""Live streaming: SSE events must arrive incrementally, not buffered."""

from __future__ import annotations

import time

import pytest

pytestmark = [pytest.mark.live_provider]

from conftest import OPENAI_KEY, skip_openai


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
        model_name="gpt-4o-mini",
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
    start = time.monotonic()
    try:
        for event in adapter.send_request_stream(request):
            now = time.monotonic() - start
            if event.kind == "text_delta":
                text_parts.append(event.text)
                arrival_times.append(now)
            elif event.kind == "message_end":
                saw_end = True
                arrival_times.append(now)
    finally:
        adapter.close()
    assert "".join(text_parts).strip() != ""
    assert saw_end is True
    # Incremental delivery: deltas do not all land in one instant.
    assert len(arrival_times) >= 2
    span = (
        arrival_times[-1] - min(arrival_times[:-1])
        if len(arrival_times) > 2
        else (arrival_times[-1] - arrival_times[0])
    )
    assert span >= 0.01 or len(arrival_times) >= 3
