"""Live sendMessage: a test message reaches the configured chat."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.live_telegram]

from conftest import skip_telegram_chat


@skip_telegram_chat
def test_live_telegram_send_message(telegram_adapter):
    response = telegram_adapter._call_api(
        "sendMessage",
        {
            "chat_id": __import__("os").environ["LIVE_TELEGRAM_CHAT_ID"],
            "text": "zero-develop live test",
        },
    )
    data = telegram_adapter._response_json(response)
    assert isinstance(data, dict) and data.get("ok") is True
    result = data.get("result")
    assert isinstance(result, dict)
    assert isinstance(result.get("message_id"), int)
