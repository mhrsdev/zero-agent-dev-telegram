"""Shared pytest fixtures.

Per ADR 0004 §4.2: tests force ``zero_env='test'`` and refuse a
``ZERO_DATABASE_URL`` containing ``prod`` or ``production``. The
default test database is in-memory SQLite, fully isolated.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from zero.app.api import create_app
from zero.config import Settings


@pytest.fixture
def test_settings() -> Settings:
    """Return a forced-test Settings instance backed by in-memory SQLite."""
    return Settings.load_for_test()


@pytest.fixture
def app(test_settings: Settings) -> FastAPI:
    """Create the real ASGI app with test settings.

    This is the same :func:`create_app` used in production; only the
    configuration differs. Tests therefore exercise the same executable
    path intended for later milestones.
    """
    return create_app(test_settings)


@pytest.fixture
async def client(app: FastAPI) -> Iterator[AsyncClient]:
    """Async HTTP client wired to the ASGI app (no network port)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    """Return a path for a temporary file-based SQLite database.

    Used by tests that need to verify file-database behavior (e.g.
    restart survival).
    """
    return tmp_path / "test_zero.db"


@pytest.fixture
def env_snapshot():
    """Snapshot the whole process env; restore it after the test.

    Unlike ``monkeypatch.delenv`` — whose undo is a NO-OP when the
    variable was already absent — this fixture also rolls back RAW
    ``os.environ[...] = ...`` assignments made by product code under
    test (e.g. ``_ensure_development_secret_key`` persisting the
    bootstrapped encryption key into the process env). Without it,
    such an assignment leaks into every subsequent test in the
    session and silently changes their behavior (observed 2026-08-29:
    the wizard e2e skipped writing ``$ZERO_HOME/.env`` because a
    leaked ``ZERO_SECRET_KEY`` made ``_ensure_secret_key`` early-return).
    """
    import os

    saved = dict(os.environ)
    try:
        yield
    finally:
        for key in list(os.environ):
            if key not in saved:
                del os.environ[key]
        os.environ.update(saved)
