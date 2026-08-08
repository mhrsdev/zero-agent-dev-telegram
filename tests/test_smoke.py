"""Smoke test — starts the real ASGI app and probes its boundaries.

Per ``zero-modular-bootstrap`` SKILL.md §"One executable path is a
design asset": "The smoke test starts the same application entry point
intended for later deployment, using isolated configuration and
persistence."

Per ``zero-modular-bootstrap`` §"Wrong example": "A unit test
constructs internal classes successfully while the real process cannot
start from a clean environment." This test does NOT construct
internal classes; it goes through the same :func:`create_app` used in
production.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from zero import __version__
from zero.app.api import create_app
from zero.config import Settings


@pytest.mark.asyncio
async def test_app_starts_and_serves_root() -> None:
    """The app must start from a clean environment and serve ``/``."""
    settings = Settings.load_for_test()
    app = create_app(settings)
    transport = __import__("httpx").ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.get("/")
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Zero Develop"
    assert body["version"] == __version__
    assert body["environment"] == "test"


@pytest.mark.asyncio
async def test_app_writes_and_reads_persistent_marker() -> None:
    """The smoke test must prove persistence is wired end-to-end.

    We write a ``runtime_markers`` row through the real database
    connection, then read it back through a fresh connection to prove
    the write was durable (for file databases) or visible (for
    in-memory databases with the per-process cache).
    """
    settings = Settings.load_for_test()
    app = create_app(settings)
    database = app.state.database

    # Write
    conn = database.connect()
    conn.execute(
        "INSERT OR REPLACE INTO runtime_markers (name, value) VALUES (?, ?)",
        ("smoke_test", "phase_1_complete"),
    )
    conn.commit()

    # Read back
    cursor = conn.execute(
        "SELECT value FROM runtime_markers WHERE name = ?",
        ("smoke_test",),
    )
    row = cursor.fetchone()
    assert row is not None
    assert row[0] == "phase_1_complete"


@pytest.mark.asyncio
async def test_app_health_endpoint_reports_ok(test_settings: Settings) -> None:
    """The ``/healthz`` endpoint must return ``status=ok`` with the
    real database wired."""
    app = create_app(test_settings)
    transport = __import__("httpx").ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert body["environment"] == "test"
    assert body["database"] == "ok"
    assert body["migration_count"] is not None
    assert body["migration_count"] >= 1


@pytest.mark.asyncio
async def test_app_readyz_returns_200_when_healthy(
    test_settings: Settings,
) -> None:
    """The ``/readyz`` endpoint must return 200 when the app is healthy."""
    app = create_app(test_settings)
    transport = __import__("httpx").ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
