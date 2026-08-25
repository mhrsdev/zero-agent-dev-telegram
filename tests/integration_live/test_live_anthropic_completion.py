"""Live Anthropic completion through the production adapter."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.live_provider]

from conftest import ANTHROPIC_KEY, skip_anthropic


@skip_anthropic
def test_live_anthropic_completion():
    import os
    from threading import Event

    from zero.app.provider_adapter import AnthropicMessagesProviderAdapter
    from zero.domain.providers import CanonicalMessage, CanonicalRequest

    adapter = AnthropicMessagesProviderAdapter(
        api_key=ANTHROPIC_KEY,
        base_url=os.environ.get("LIVE_ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
        timeout_seconds=60.0,
    )
    request = CanonicalRequest(
        provider="anthropic",
        model_name="claude-3-5-haiku",
        messages=(CanonicalMessage(role="user", content="Reply with the word: ok"),),
        max_tokens=16,
        temperature=0.0,
    )
    response = adapter.send_request(request, cancel_event=Event())
    try:
        assert response.content.strip() != ""
        usage = response.usage
        assert usage is not None
        assert (usage.input_tokens + usage.cache_read_input_tokens) > 0
        assert usage.output_tokens > 0
    finally:
        adapter.close()
