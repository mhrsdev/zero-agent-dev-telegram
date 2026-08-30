"""Shared gating and fixtures for the live integration package (GAP 1).

Every test here is double-gated: ``ZERO_ENABLE_LIVE_TESTS=1`` AND the
relevant credentials must be present, otherwise the test skips. The
deterministic suite never touches the network.
"""

from __future__ import annotations

import os

import pytest

LIVE_ENABLED = os.environ.get("ZERO_ENABLE_LIVE_TESTS") == "1"
TELEGRAM_TOKEN = os.environ.get("LIVE_TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT = os.environ.get("LIVE_TELEGRAM_CHAT_ID", "")
OPENAI_KEY = os.environ.get("LIVE_OPENAI_API_KEY", "")
ANTHROPIC_KEY = os.environ.get("LIVE_ANTHROPIC_API_KEY", "")

TELEGRAM_READY = LIVE_ENABLED and bool(TELEGRAM_TOKEN)
TELEGRAM_CHAT_READY = TELEGRAM_READY and bool(TELEGRAM_CHAT)
OPENAI_READY = LIVE_ENABLED and bool(OPENAI_KEY)
ANTHROPIC_READY = LIVE_ENABLED and bool(ANTHROPIC_KEY)

skip_telegram = pytest.mark.skipif(
    not TELEGRAM_READY,
    reason="set ZERO_ENABLE_LIVE_TESTS=1 and LIVE_TELEGRAM_BOT_TOKEN",
)
skip_telegram_chat = pytest.mark.skipif(
    not TELEGRAM_CHAT_READY,
    reason="also set LIVE_TELEGRAM_CHAT_ID for send tests",
)
skip_openai = pytest.mark.skipif(
    not OPENAI_READY, reason="set ZERO_ENABLE_LIVE_TESTS=1 and LIVE_OPENAI_API_KEY"
)
skip_anthropic = pytest.mark.skipif(
    not ANTHROPIC_READY,
    reason="set ZERO_ENABLE_LIVE_TESTS=1 and LIVE_ANTHROPIC_API_KEY",
)


@pytest.fixture(scope="module")
def telegram_adapter():
    from zero.adapters.messaging import RetryPolicy
    from zero.adapters.telegram import TelegramAdapter

    # Drift fix (2026-08-31): the adapter's HTTP transport became an
    # injected dependency; these live tests were skipped for so long they
    # still built the adapter without one, so every live test failed with
    # "an injected HTTP transport is required" instead of exercising the
    # real Bot API. Inject the shared httpx transport with a poll-safe
    # budget (long-poll hold 0s here, but keep real timeouts).
    import httpx

    transport = httpx.Client(timeout=35.0)
    adapter = TelegramAdapter(
        lambda event: event,
        transport=transport,
        bot_token=TELEGRAM_TOKEN,
        poll_timeout_seconds=0,
        retry_policy=RetryPolicy(attempts=1, backoff_seconds=0.5, timeout_seconds=30.0),
    )
    yield adapter
    transport.close()


def gateway_retry(fn, *, attempts: int = 4, pause: float = 3.0):
    """Bounded retry for the operator gateway's intermittent edge-403s.

    The gateway flaps per-request 403s on BOTH streaming and
    non-streaming calls (observed live 2026-08-31: 3 consecutive 200s,
    then a 403, then a 200 on the identical payload). The PRODUCT
    handles this in ProviderService.send_request_with_fallback; these
    raw-adapter live tests bypass that layer, so they retry the edge
    flap locally instead of failing on gateway weather.
    """
    import time as _time

    from zero.domain.providers import ProviderError

    last = None
    for attempt in range(attempts):
        try:
            return fn()
        except ProviderError as exc:
            last = exc
            if "auth" not in str(exc).lower() or attempt == attempts - 1:
                raise
            _time.sleep(pause)
    raise last
