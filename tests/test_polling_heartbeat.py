"""Round-5: polling heartbeat + stall watchdog (Hermes parity).

Hermes probes ``getMe`` every 90s and warns after 150s without a
successful round trip. Zero now runs the same budgets inline in the
polling loop: a periodic heartbeat line names the live bot, and a stall
warning fires when no successful Telegram round trip happened for
_POLLING_STALL_SECONDS — even when no hard error fired.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from zero.app.background_workers import (
    _POLLING_HEARTBEAT_SECONDS,
    _POLLING_STALL_SECONDS,
    BackgroundWorkerHost,
)
from zero.config import Settings


def test_heartbeat_budgets_match_hermes_parity() -> None:
    assert _POLLING_HEARTBEAT_SECONDS == 90.0
    assert _POLLING_STALL_SECONDS == 300.0


def _host(monkeypatch, *, fake_adapter):
    settings = Settings.load_for_test()
    settings = settings.model_copy(update={"polling_interval_seconds": 0.01})
    host = BackgroundWorkerHost(settings, SimpleNamespace())
    binding = SimpleNamespace(id=SimpleNamespace(value="b-hb"))
    monkeypatch.setattr(
        host, "_telegram_poll_targets", lambda: [(None, binding, "123:TOKEN")]
    )
    monkeypatch.setattr(
        "zero.app.background_workers._build_binding_adapter",
        lambda **kwargs: fake_adapter,
    )
    return host


class _HealthyAdapter:
    """poll_once returns nothing; getMe succeeds; both recorded."""

    def __init__(self) -> None:
        self.polls = 0
        self.probes = 0

    def poll_once(self, *, scope_key):
        self.polls += 1
        return []

    def get_me(self):
        self.probes += 1
        return {"username": "SandboxEnvironmentBot", "id": 8753924431}


@pytest.mark.asyncio
async def test_heartbeat_fires_and_succeeds(monkeypatch) -> None:
    adapter = _HealthyAdapter()
    host = _host(monkeypatch, fake_adapter=adapter)
    # Shrink the heartbeat budget so it fires inside the test window.
    import zero.app.background_workers as workers_mod

    monkeypatch.setattr(workers_mod, "_POLLING_HEARTBEAT_SECONDS", 0.05)
    task = asyncio.create_task(host._polling_loop())
    await asyncio.sleep(0.3)
    host._stop.set()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert adapter.polls >= 1
    assert adapter.probes >= 1, "the periodic getMe heartbeat must fire"


@pytest.mark.asyncio
async def test_heartbeat_failure_does_not_kill_polling(monkeypatch) -> None:
    adapter = _HealthyAdapter()

    def _failing_probe(self=None):
        raise RuntimeError("gateway down")

    adapter.get_me = _failing_probe  # type: ignore[method-assign]
    host = _host(monkeypatch, fake_adapter=adapter)
    import zero.app.background_workers as workers_mod

    monkeypatch.setattr(workers_mod, "_POLLING_HEARTBEAT_SECONDS", 0.05)
    task = asyncio.create_task(host._polling_loop())
    await asyncio.sleep(0.3)
    host._stop.set()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    # Polling continued past the failed heartbeat probes.
    assert adapter.polls >= 3
