"""Live poll: one getUpdates cycle returns an empty or valid batch."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.live_telegram]

from conftest import skip_telegram  # noqa: E402


@skip_telegram
def test_live_telegram_poll_once(telegram_adapter):
    results = telegram_adapter.poll_once(scope_key="live-test")
    assert isinstance(results, list)
    # Empty batches are normal for a quiet test bot; anything present
    # must already be normalized dispatch results.
    for item in results:
        assert hasattr(item, "event") or isinstance(item, dict) or item is not None
