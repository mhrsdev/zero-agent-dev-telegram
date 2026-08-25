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
    from zero.adapters.telegram import TelegramAdapter

    adapter = TelegramAdapter(
        lambda event: event,
        bot_token=TELEGRAM_TOKEN,
        poll_timeout_seconds=0,
    )
    return adapter
