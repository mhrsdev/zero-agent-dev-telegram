"""Regression: the polling worker's long-poll HTTP budget must exceed the
Telegram long-poll hold.

Real server run (2026-08-28): `_build_binding_adapter` used the default
``RetryPolicy(timeout_seconds=10)`` while asking Telegram to hold the
getUpdates request open for 25 seconds. httpx aborted every long poll at
10s (TransportError), the retry wrapper re-sent it twice (3 doomed
requests per iteration, ~30s), and the gateway could never receive a
real group message. Observed log cadence: `polling:ib_…: TransportError`
every ~33s.
"""

from __future__ import annotations

from types import SimpleNamespace

from zero.adapters.messaging import RetryPolicy
from zero.app.background_workers import _build_binding_adapter


def _services_with_transport():
    transport = SimpleNamespace(name="fake-http-transport")
    interfaces = SimpleNamespace(process_inbound_event=lambda event: event)
    transports = SimpleNamespace(http_transport=transport)
    return SimpleNamespace(interface_transports=transports, interfaces=interfaces)


def test_polling_adapter_request_timeout_exceeds_long_poll_hold() -> None:
    services = _services_with_transport()
    adapter = _build_binding_adapter(
        services=services, chat_token="123:abc", cursor_store=None
    )
    assert adapter._poll_timeout_seconds == 25
    policy = adapter._retry_policy
    assert isinstance(policy, RetryPolicy)
    assert policy.timeout_seconds > adapter._poll_timeout_seconds, (
        "per-request HTTP timeout must exceed the long-poll hold, "
        "otherwise every long poll aborts client-side"
    )


def test_polling_adapter_does_not_retry_long_polls() -> None:
    """A completed long poll IS the wait; the outer loop is the retry."""
    services = _services_with_transport()
    adapter = _build_binding_adapter(
        services=services, chat_token="123:abc", cursor_store=None
    )
    assert adapter._retry_policy.attempts == 1


def test_polling_adapter_uses_real_transport_and_intake() -> None:
    services = _services_with_transport()
    adapter = _build_binding_adapter(
        services=services, chat_token="123:abc", cursor_store=None
    )
    assert adapter._transport is services.interface_transports.http_transport
    assert adapter._event_handler is services.interfaces.process_inbound_event
    assert adapter._bot_token == "123:abc"
