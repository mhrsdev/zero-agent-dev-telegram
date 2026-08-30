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


async def _wait_until(predicate, *, timeout: float = 10.0) -> bool:
    """Await ``predicate`` becoming true, polling cooperatively.

    The original tests slept a fixed 0.3s and asserted a poll count.
    That count is a function of how much CPU the event loop actually got:
    under full-suite load the loop completed fewer iterations and the
    assertion failed while the behavior under test — polling surviving a
    failed heartbeat — was intact. Waiting for the condition with a
    generous deadline tests the behavior instead of the machine's speed.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


async def _drain(host, task) -> None:
    host._stop.set()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_heartbeat_fires_and_succeeds(monkeypatch) -> None:
    adapter = _HealthyAdapter()
    host = _host(monkeypatch, fake_adapter=adapter)
    # Shrink the heartbeat budget so it fires inside the test window.
    import zero.app.background_workers as workers_mod

    monkeypatch.setattr(workers_mod, "_POLLING_HEARTBEAT_SECONDS", 0.05)
    task = asyncio.create_task(host._polling_loop())
    try:
        fired = await _wait_until(lambda: adapter.polls >= 1 and adapter.probes >= 1)
    finally:
        await _drain(host, task)
    assert fired, (
        f"the periodic getMe heartbeat must fire "
        f"(polls={adapter.polls}, probes={adapter.probes})"
    )


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
    try:
        # Polling must continue past the failed heartbeat probes.
        survived = await _wait_until(lambda: adapter.polls >= 3)
    finally:
        await _drain(host, task)
    assert survived, (
        f"a failing heartbeat probe must not stop polling (polls={adapter.polls})"
    )
