"""Shared pytest fixtures.

Per ADR 0004 §4.2: tests force ``zero_env='test'`` and refuse a
``ZERO_DATABASE_URL`` containing ``prod`` or ``production``. The
default test database is in-memory SQLite, fully isolated.
"""

from __future__ import annotations

import functools
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from zero.app.api import create_app
from zero.config import Settings


@pytest.fixture(scope="session", autouse=True)
def isolated_zero_home(tmp_path_factory):
    """Point ``$ZERO_HOME`` at a throwaway directory for the whole session.

    ``zero_home()`` (and the management-layer policy gate, admin GUI, and
    wizard built on it) resolve ``$ZERO_HOME`` **per call**, so any test
    that does not override the variable itself must never observe the
    operator's real ``~/.zero``. With a real home present, the live
    ``owner_only`` access policy denied every interface intake (the gate
    resolved the real ``owner_project_id`` against the test's empty
    in-memory database → ``ProjectNotFoundError`` → ``denied``), and
    home-writing tests mutated the operator's live config. Tests that
    need a home of their own still ``monkeypatch.setenv("ZERO_HOME",
    ...)`` per test, which overrides this session default.
    """
    import os

    home = tmp_path_factory.mktemp("zero-home")
    saved = os.environ.get("ZERO_HOME")
    os.environ["ZERO_HOME"] = str(home)
    try:
        yield home
    finally:
        if saved is None:
            os.environ.pop("ZERO_HOME", None)
        else:
            os.environ["ZERO_HOME"] = saved


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


@functools.lru_cache(maxsize=1)
def loopback_http_works() -> bool:
    """Probe whether a loopback TCP round-trip actually completes.

    Several suites stand up a real ``ThreadingHTTPServer`` on
    ``127.0.0.1`` and drive the production adapters against it — the
    honest way to test HTTP behavior without live credentials. Some
    hardened/containerized environments accept the connection and
    deliver the request but never return the response, so those tests
    hang until ``ReadTimeout`` and fail for reasons that have nothing to
    do with the code under test. Probing once, with a real request and
    response, tells the difference between "loopback HTTP is unavailable
    here" and "the adapter is broken"; only the former is skippable.
    """
    import http.server
    import threading
    import urllib.error
    import urllib.request

    class _Probe(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):  # noqa: D102 - silence the probe
            pass

        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler contract
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    try:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Probe)
    except OSError:
        return False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/probe"
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status == 200 and response.read() == b"ok"
    except (urllib.error.URLError, OSError, TimeoutError):
        return False
    finally:
        server.shutdown()
        server.server_close()


requires_loopback_http = pytest.mark.skipif(
    not loopback_http_works(),
    reason="loopback HTTP round-trips do not complete in this environment",
)
