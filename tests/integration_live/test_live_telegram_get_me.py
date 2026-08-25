"""Live getMe: the configured bot token is valid and identifies a bot."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.live_telegram]

from conftest import skip_telegram


@skip_telegram
def test_live_telegram_get_me(telegram_adapter):
    me = telegram_adapter.get_me()
    assert isinstance(me, dict)
    assert str(me.get("username") or "").strip() != ""
    assert me.get("is_bot") is True
