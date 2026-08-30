"""Live OpenAI completion through the production adapter."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.live_provider]

from conftest import OPENAI_KEY, gateway_retry, skip_openai


@skip_openai
def test_live_openai_completion():
    import os
    from threading import Event

    from zero.app.provider_adapter import OpenAICompatibleProviderAdapter
    from zero.domain.providers import CanonicalMessage, CanonicalRequest

    adapter = OpenAICompatibleProviderAdapter(
        api_key=OPENAI_KEY,
        base_url=os.environ.get("LIVE_OPENAI_BASE_URL", "https://api.openai.com/v1"),
        timeout_seconds=60.0,
    )
    request = CanonicalRequest(
        provider="openai-compatible",
        model_name=os.environ.get("LIVE_OPENAI_MODEL", "gpt-4o-mini"),
        messages=(CanonicalMessage(role="user", content="Reply with the word: ok"),),
        max_tokens=16,
        temperature=0.0,
    )
    response = gateway_retry(lambda: adapter.send_request(request, cancel_event=Event()))
    try:
        assert response.content.strip() != ""
        usage = response.usage
        assert usage is not None
        assert (usage.input_tokens + usage.cache_read_input_tokens) > 0
        assert usage.output_tokens > 0
    finally:
        adapter.close()
