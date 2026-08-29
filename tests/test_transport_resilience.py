"""Regression tests for the flaky-network / TransportError-wall session.

Operator report (2026-08-29): ``zero logs`` showed
``worker error: polling:ib_…: TransportError`` every ~4 seconds for 8+
minutes while ``api.telegram.org`` was unreachable through a filtered
network (then sudden 200 OKs when the egress path recovered). Four bugs
made that outage undiagnosable and the log unreadable:

1. ``TransportError`` discarded the underlying httpx cause, so the log
   wall carried zero facts (DNS? connect? read timeout? 409?);
2. generic transport failures had NO per-binding backoff (only 409
   conflicts did) and hot-looped at the 1s polling interval;
3. there was no way to route Telegram traffic through a proxy
   (``ZERO_TELEGRAM_PROXY_URL``) on filtered networks;
4. a healthy gateway was indistinguishable from a dead one in the log.

These tests pin each layer of the fix.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from zero.adapters.messaging import (
    PermanentTransportError,
    RetryPolicy,
    TransportError,
    redact_bot_token,
)
from zero.config import ConfigError, Settings, mask_proxy_credentials


# ----------------------------------------------------------------------
# TransportError carries a sanitized cause summary
# ----------------------------------------------------------------------
class _ExplodingTransport:
    """Transport that fails like a filtered network does."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def request(self, *a, **k):
        raise self._exc

    def close(self) -> None:
        pass


def _adapter_with(transport_exc: Exception):
    from zero.adapters.telegram import TelegramAdapter

    return TelegramAdapter(
        event_handler=lambda e: None,
        transport=_ExplodingTransport(transport_exc),
        bot_token="8753924431:AAHcSECRETVALUE",
        poll_timeout_seconds=0,
        retry_policy=RetryPolicy(attempts=1, backoff_seconds=0.0, timeout_seconds=1.0),
    )


def test_transport_error_carries_cause_type_and_detail() -> None:
    adapter = _adapter_with(
        httpx.ConnectError("All connection attempts failed bot8753924431:AAHcSECRETVALUE/x")
    )
    with pytest.raises(TransportError) as exc_info:
        adapter.poll_once(scope_key="s")
    message = str(exc_info.value)
    assert "ConnectError" in message, "the underlying cause type must be visible"
    assert "All connection attempts failed" in message, "the cause text must be visible"


def test_transport_error_redacts_bot_token() -> None:
    adapter = _adapter_with(httpx.ConnectError("timeout talking to bot8753924431:AAHcSECRETVALUE"))
    with pytest.raises(TransportError) as exc_info:
        adapter.poll_once(scope_key="s")
    message = str(exc_info.value)
    assert "AAHcSECRETVALUE" not in message
    assert "bot[REDACTED]" in message


def test_redact_bot_token_helper() -> None:
    text = "POST https://api.telegram.org/bot123456789:AAABCDEF12345/getUpdates failed"
    assert "AAABCDEF12345" not in redact_bot_token(text)
    assert "bot[REDACTED]" in redact_bot_token(text)
    # A 4xx rejection keeps its actionable status text intact.
    assert redact_bot_token("provider returned HTTP status 409") == (
        "provider returned HTTP status 409"
    )


def test_retryable_status_transport_error_keeps_status_detail() -> None:
    class Transport502:
        status_code = 502

        def request(self, *a, **k):
            return SimpleNamespace(status_code=502)

        def close(self) -> None:
            pass

    from zero.adapters.telegram import TelegramAdapter

    adapter = TelegramAdapter(
        event_handler=lambda e: None,
        transport=Transport502(),
        bot_token="1:t",
        poll_timeout_seconds=0,
    )
    with pytest.raises(TransportError) as exc_info:
        adapter.poll_once(scope_key="s")
    assert "502" in str(exc_info.value)


def test_permanent_4xx_error_is_typed_and_detailed() -> None:
    class Transport401:
        status_code = 401

        def request(self, *a, **k):
            return SimpleNamespace(status_code=401)

        def close(self) -> None:
            pass

    from zero.adapters.telegram import TelegramAdapter

    adapter = TelegramAdapter(
        event_handler=lambda e: None,
        transport=Transport401(),
        bot_token="1:t",
        poll_timeout_seconds=0,
    )
    with pytest.raises(PermanentTransportError) as exc_info:
        adapter.poll_once(scope_key="s")
    assert "401" in str(exc_info.value)


# ----------------------------------------------------------------------
# Polling loop: backoff + detail + hint + recovery + identity
# ----------------------------------------------------------------------
def _host_for(monkeypatch, *, poll_interval: float = 0.01):
    from zero.app.background_workers import BackgroundWorkerHost

    settings = Settings.load_for_test()
    settings = settings.model_copy(update={"polling_interval_seconds": poll_interval})
    services = SimpleNamespace()
    host = BackgroundWorkerHost(settings, services)
    binding = SimpleNamespace(id=SimpleNamespace(value="b-flaky"))
    monkeypatch.setattr(host, "_telegram_poll_targets", lambda: [(None, binding, "123:TOKEN")])
    return host, binding


async def _run_loop_for(host, *, seconds: float) -> None:
    """Run the polling loop for a bounded real-time window, then stop it."""
    task = asyncio.create_task(host._polling_loop())
    await asyncio.sleep(seconds)
    host._stop.set()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_polling_loop_backs_off_on_transport_errors(monkeypatch) -> None:
    """A dead network must NOT hot-loop at the polling interval."""
    host, _binding = _host_for(monkeypatch)
    calls = {"n": 0}

    class _FakeAdapter:
        def poll_once(self, *, scope_key: str):
            calls["n"] += 1
            raise TransportError(
                "provider transport failed after retries — "
                "ConnectError: All connection attempts failed"
            )

    monkeypatch.setattr(
        "zero.app.background_workers._build_binding_adapter",
        lambda **kwargs: _FakeAdapter(),
    )

    task = asyncio.create_task(host._polling_loop())
    await asyncio.sleep(0.3)
    host._stop.set()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    # First failure earns a 2s backoff — a hot loop at the 0.01s interval
    # would have produced dozens of calls in the 0.3s window.
    assert 1 <= calls["n"] <= 2, f"transport-error backoff violated: {calls['n']} polls"


@pytest.mark.asyncio
async def test_polling_loop_first_error_logs_full_cause(monkeypatch, caplog) -> None:
    host, _binding = _host_for(monkeypatch)

    class _FakeAdapter:
        def poll_once(self, *, scope_key: str):
            raise TransportError(
                "provider transport failed after retries — "
                "ConnectError: [Errno 10061] connection refused"
            )

    monkeypatch.setattr(
        "zero.app.background_workers._build_binding_adapter",
        lambda **kwargs: _FakeAdapter(),
    )
    with caplog.at_level("WARNING", logger="zero.workers"):
        task = asyncio.create_task(host._polling_loop())
        await asyncio.sleep(0.15)
        host._stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    detail_lines = [r for r in caplog.records if "ConnectError" in r.getMessage()]
    assert detail_lines, "first failure must log the sanitized cause detail"


@pytest.mark.asyncio
async def test_polling_loop_hint_logged_once_and_compact_repeats(monkeypatch, caplog) -> None:
    """After 3 consecutive failures: one proxy hint; repeats stay compact."""
    host, _binding = _host_for(monkeypatch)

    clock = {"t": 0.0}

    def _fake_monotonic() -> float:
        clock["t"] += 3.0  # virtual time jumps past every backoff window
        return clock["t"]

    monkeypatch.setattr("zero.app.background_workers._loop_monotonic", _fake_monotonic)

    class _FakeAdapter:
        def poll_once(self, *, scope_key: str):
            raise TransportError("provider transport failed — ConnectError: down")

    monkeypatch.setattr(
        "zero.app.background_workers._build_binding_adapter",
        lambda **kwargs: _FakeAdapter(),
    )
    with caplog.at_level("WARNING", logger="zero.workers"):
        task = asyncio.create_task(host._polling_loop())
        # Wait until the hint appears (bounded real-time deadline).
        for _ in range(60):
            if any("ZERO_TELEGRAM_PROXY_URL" in r.getMessage() for r in caplog.records):
                break
            await asyncio.sleep(0.05)
        host._stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    hints = [r for r in caplog.records if "ZERO_TELEGRAM_PROXY_URL" in r.getMessage()]
    assert hints, "persistent failure must surface the proxy hint"
    assert len(hints) == 1, "the hint must be logged ONCE per failure streak"


@pytest.mark.asyncio
async def test_polling_loop_recovers_and_verifies_identity_once(monkeypatch, caplog) -> None:
    host, _binding = _host_for(monkeypatch)

    # Virtual clock: each read jumps 3s so the 2s error backoff elapses
    # between loop iterations without real waiting.
    clock = {"t": 0.0}

    def _fake_monotonic() -> float:
        clock["t"] += 3.0
        return clock["t"]

    monkeypatch.setattr("zero.app.background_workers._loop_monotonic", _fake_monotonic)

    state = {"polls": 0, "get_me": 0}

    class _FakeAdapter:
        def poll_once(self, *, scope_key: str):
            state["polls"] += 1
            if state["polls"] == 1:
                raise TransportError("provider transport failed — ConnectError: down")
            return []

        def get_me(self):
            state["get_me"] += 1
            return {"username": "my_zero_bot", "id": 8753924431}

    monkeypatch.setattr(
        "zero.app.background_workers._build_binding_adapter",
        lambda **kwargs: _FakeAdapter(),
    )
    with caplog.at_level("INFO", logger="zero.workers"):
        task = asyncio.create_task(host._polling_loop())
        # Wait until the identity probe has run (bounded real-time deadline).
        for _ in range(60):
            if state["get_me"] >= 1 and state["polls"] >= 2:
                break
            await asyncio.sleep(0.05)
        host._stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert state["polls"] >= 2, "recovery requires a successful poll after the failure"
    recovered = [r for r in caplog.records if "recovered" in r.getMessage()]
    assert recovered, "recovery must be visible at INFO level"
    identity = [r for r in caplog.records if "@my_zero_bot" in r.getMessage()]
    assert identity, "first success must verify and log the bot identity"
    assert state["get_me"] == 1, "identity probe must run exactly once per token"


# ----------------------------------------------------------------------
# Settings: ZERO_TELEGRAM_PROXY_URL validation + masking
# ----------------------------------------------------------------------
def test_proxy_accepts_http_and_masks_credentials() -> None:
    from zero.config import _validate_telegram_proxy

    raw = "http://alice:secret-pw@10.0.0.8:8080"
    assert _validate_telegram_proxy(raw) == raw
    masked = mask_proxy_credentials(raw)
    assert masked == "http://alice:***@10.0.0.8:8080"
    assert "secret-pw" not in masked


def test_proxy_rejects_unknown_scheme() -> None:
    from zero.config import _validate_telegram_proxy

    with pytest.raises(ConfigError) as exc_info:
        _validate_telegram_proxy("ftp://127.0.0.1:21")
    assert "ZERO_TELEGRAM_PROXY_URL" in str(exc_info.value)


def test_proxy_rejects_hostless_value() -> None:
    from zero.config import _validate_telegram_proxy

    with pytest.raises(ConfigError):
        _validate_telegram_proxy("socks5://")


def test_proxy_empty_is_none() -> None:
    from zero.config import _validate_telegram_proxy

    assert _validate_telegram_proxy(None) is None
    assert _validate_telegram_proxy("   ") is None


def test_settings_load_reads_proxy_env(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("ZERO_TELEGRAM_PROXY_URL", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ZERO_ENV=development\n"
        "ZERO_TELEGRAM_PROXY_URL=socks5://127.0.0.1:1080\n",
        encoding="utf-8",
    )
    settings = Settings.load(env_file=str(env_file), zero_env_fallback="development")
    assert settings.telegram_proxy_url == "socks5://127.0.0.1:1080"
    # No credentials in the URL → repr shows it verbatim (no secret).
    assert "socks5://127.0.0.1:1080" in repr(settings)


def test_settings_proxy_url_never_leaks_credentials_in_repr(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("ZERO_TELEGRAM_PROXY_URL", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ZERO_ENV=development\n"
        "ZERO_TELEGRAM_PROXY_URL=http://bob:hunter2@10.1.1.1:9090\n",
        encoding="utf-8",
    )
    settings = Settings.load(env_file=str(env_file), zero_env_fallback="development")
    assert "hunter2" not in repr(settings)
    assert "bob:***@10.1.1.1:9090" in repr(settings)


# ----------------------------------------------------------------------
# Shared messaging client honors the proxy + a real timeout budget
# ----------------------------------------------------------------------
def test_messaging_client_gets_proxy_and_timeout(monkeypatch) -> None:
    import zero.app.services as services_module

    captured: dict[str, object] = {}

    class _FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def close(self) -> None:
            pass

    monkeypatch.setattr(services_module.httpx, "Client", _FakeClient)
    settings = Settings.load_for_test().model_copy(
        update={"telegram_proxy_url": "socks5://127.0.0.1:1080"}
    )
    client = services_module._build_messaging_http_client(settings)
    client.close()
    assert captured.get("proxy") == "socks5://127.0.0.1:1080"
    timeout = captured.get("timeout")
    assert timeout is not None and timeout.connect == 15.0


def test_messaging_client_without_proxy_is_plain(monkeypatch) -> None:
    import zero.app.services as services_module

    captured: dict[str, object] = {}

    class _FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def close(self) -> None:
            pass

    monkeypatch.setattr(services_module.httpx, "Client", _FakeClient)
    settings = Settings.load_for_test().model_copy(update={"telegram_proxy_url": None})
    client = services_module._build_messaging_http_client(settings)
    client.close()
    assert "proxy" not in captured


# ----------------------------------------------------------------------
# Doctor/wizard probes honor the same proxy
# ----------------------------------------------------------------------
class _FakeProbeResponse:
    status_code = 200

    def json(self):
        return {"ok": True, "result": {"id": 1, "username": "probe_bot"}}


class _FakeProbeClient:
    last_kwargs: dict[str, object] = {}

    def __init__(self, **kwargs):
        _FakeProbeClient.last_kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, *a, **k):
        return _FakeProbeResponse()

    def post(self, *a, **k):
        return _FakeProbeResponse()


def test_telegram_probe_honors_proxy_env(monkeypatch) -> None:
    import zero.manage.core.probes as probes_module

    monkeypatch.setattr(probes_module.httpx, "Client", _FakeProbeClient)
    monkeypatch.setenv("ZERO_TELEGRAM_PROXY_URL", "socks5://127.0.0.1:1080")
    result = probes_module.telegram_get_me("123:abc")
    assert result["ok"] is True
    assert _FakeProbeClient.last_kwargs.get("proxy") == "socks5://127.0.0.1:1080"


def test_telegram_probe_without_proxy_env(monkeypatch) -> None:
    import zero.manage.core.probes as probes_module

    monkeypatch.setattr(probes_module.httpx, "Client", _FakeProbeClient)
    monkeypatch.delenv("ZERO_TELEGRAM_PROXY_URL", raising=False)
    result = probes_module.telegram_get_me("123:abc")
    assert result["ok"] is True
    assert _FakeProbeClient.last_kwargs.get("proxy") is None


# ----------------------------------------------------------------------
# Outbound send: one clean attempt, real budget, diagnosable errors
# ----------------------------------------------------------------------
def test_outbound_send_policy_is_single_attempt_with_real_budget() -> None:
    from zero.adapters.telegram import TelegramAdapter

    adapter = TelegramAdapter(
        event_handler=lambda e: None,
        transport=_ExplodingTransport(httpx.ConnectError("down")),
        bot_token="1:t",
        retry_policy=RetryPolicy(attempts=1, backoff_seconds=0.5, timeout_seconds=30.0),
    )
    assert adapter._retry_policy.attempts == 1
    assert adapter._retry_policy.timeout_seconds == 30.0
