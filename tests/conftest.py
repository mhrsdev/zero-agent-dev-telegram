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
