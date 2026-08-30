"""Round-8 hardening wave — pins for every defect fixed in this pass.

Each test below fails on the pre-fix tree:

1. ``$ZERO_HOME`` leaked into tests, so the operator's live
   ``owner_only`` access policy denied every interface intake and
   home-writing tests mutated the real ``~/.zero``.
2. ``Database`` never released its cached SQLite connections, so
   CPython >= 3.13 raised ``ResourceWarning: unclosed database`` during
   finalization; under the repo's warnings-as-errors policy that failed
   unrelated tests.
3. The auth middleware ran two synchronous database reads directly on
   the event loop for every authenticated request.
4. ``DockerExecutor`` handed the full host environment to the ``docker``
   CLI process.
5. The admin CSRF token was ``sha256("csrf:" + session_id)`` — derivable
   by anyone who learned a session id.
6. Live Telegram/provider credentials were embedded in tracked scripts.
"""

from __future__ import annotations

import inspect
import os
import re
import sqlite3
import subprocess
from pathlib import Path

import pytest

from zero.app.executors.sandbox import DockerExecutor, docker_cli_env
from zero.config import Settings
from zero.manage.core.config import zero_home
from zero.persistence.connection import Database

REPO_ROOT = Path(__file__).resolve().parents[1]


# ----------------------------------------------------------------------
# 1. Test-session home isolation
# ----------------------------------------------------------------------


def test_tests_never_resolve_the_operator_real_home() -> None:
    """``zero_home()`` must point at a throwaway directory under pytest.

    The management config lives at ``$ZERO_HOME/config.yaml`` and the
    policy gate live-reloads it on every intake. With the real home
    visible, a configured ``owner_only`` policy denied every inbound
    Telegram event in the test suite (``processing_result == "denied"``)
    because the gate resolved the operator's real ``owner_project_id``
    against the test's empty in-memory database.
    """
    resolved = zero_home().resolve()
    real_home = (Path.home() / ".zero").resolve()
    assert resolved != real_home, "tests must not resolve the operator's live $ZERO_HOME"
    assert os.environ.get("ZERO_HOME"), "the session fixture must export ZERO_HOME"


def test_home_isolation_survives_per_test_override(monkeypatch, tmp_path) -> None:
    """A test that sets its own home still wins over the session default."""
    own = tmp_path / "own-home"
    monkeypatch.setenv("ZERO_HOME", str(own))
    assert zero_home() == own


# ----------------------------------------------------------------------
# 2. Database connection lifecycle
# ----------------------------------------------------------------------


def test_database_releases_connections_when_dropped(tmp_db_path) -> None:
    """An abandoned ``Database`` must not leak an open SQLite handle.

    Before the fix the cached connections were left to
    ``sqlite3.Connection`` finalization, which raises
    ``ResourceWarning: unclosed database`` on CPython >= 3.13.
    """
    settings = Settings.load_for_test(database_url=f"sqlite:///{tmp_db_path}")
    database = Database(settings)
    conn = database.connect()
    conn.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY)")
    conn.commit()
    tracked = tuple(database._connections)
    assert tracked, "the wrapper must track the connections it caches"

    database.__del__()

    for tracked_conn in tracked:
        with pytest.raises(sqlite3.ProgrammingError):
            tracked_conn.execute("SELECT 1")


def test_database_close_is_idempotent(tmp_db_path) -> None:
    settings = Settings.load_for_test(database_url=f"sqlite:///{tmp_db_path}")
    database = Database(settings)
    database.connect().execute("SELECT 1")
    database.close()
    database.close()  # must not raise
    # A fresh connect() after close() still works (no poisoned state).
    assert database.ping() is True
    database.close()


def test_no_resource_warning_escapes_a_dropped_database(tmp_db_path, recwarn) -> None:
    import gc

    settings = Settings.load_for_test(database_url=f"sqlite:///{tmp_db_path}")
    database = Database(settings)
    database.connect().execute("SELECT 1")
    del database
    gc.collect()
    leaked = [w for w in recwarn.list if "unclosed database" in str(w.message)]
    assert leaked == [], f"a dropped Database leaked connections: {leaked}"


# ----------------------------------------------------------------------
# 3. Auth middleware must not block the event loop
# ----------------------------------------------------------------------


def test_auth_middleware_offloads_database_reads_to_the_threadpool() -> None:
    """Authentication and the project scope check must not run inline.

    Both are synchronous SQLite reads. Running them in the middleware
    coroutine serialized the event loop on database I/O for every
    authenticated request, while every router and the webhook path
    already used the threadpool.
    """
    from zero.app import api

    source = inspect.getsource(api._register_auth_middleware)
    assert "run_in_threadpool(services.auth.authenticate" in source, (
        "token authentication must be offloaded to the threadpool"
    )
    assert "run_in_threadpool(_check_project_scope)" in source, (
        "the project-scope permission check must be offloaded to the threadpool"
    )
    assert not re.search(r"^\s+actor_id = services\.auth\.authenticate\(", source, re.M), (
        "the blocking inline authenticate() call must be gone"
    )


@pytest.mark.asyncio
async def test_authenticated_request_still_works_through_the_threadpool() -> None:
    """Behavior parity: the offloaded path authenticates and scopes as before."""
    from httpx import ASGITransport, AsyncClient
    from pydantic import SecretStr

    from zero.app.api import create_app

    settings = Settings.load_for_test(
        auth_required=True,
        secret_key=SecretStr("test-only-auth-key-material-0123456789"),
    )
    app = create_app(settings)
    services = app.state.services

    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(owner_id=owner.id, name="P")
    owner_token, _ = services.auth.issue_access_token(owner.id)
    stranger = services.identity.create_user(display_name="Stranger")
    stranger_token, _ = services.auth.issue_access_token(stranger.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        anonymous = await client.get(f"/projects/{project.id.value}")
        assert anonymous.status_code == 401

        authorized = await client.get(
            f"/projects/{project.id.value}",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert authorized.status_code == 200

        denied = await client.get(
            f"/projects/{project.id.value}",
            headers={"Authorization": f"Bearer {stranger_token}"},
        )
        assert denied.status_code == 404, "a non-member must not learn the project exists"

    app.state.database.close()


# ----------------------------------------------------------------------
# 4. Docker CLI environment scrubbing
# ----------------------------------------------------------------------


def test_docker_cli_env_excludes_host_secrets(monkeypatch) -> None:
    """The ``docker`` client process gets no host credentials.

    The container environment was already built from explicit ``-e``
    flags, but the wrapper process received ``dict(os.environ)`` — every
    provider key, bot token, and secret in the engine's environment.
    """
    monkeypatch.setenv("ZERO_OPENAI_API_KEY", "sk-must-not-leak")
    monkeypatch.setenv("ZERO_SECRET_KEY", "k" * 64)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-must-not-leak")

    env = docker_cli_env()

    assert "ZERO_OPENAI_API_KEY" not in env
    assert "ZERO_SECRET_KEY" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "must-not-leak" not in "".join(env.values())
    assert env["PATH"], "the docker client still needs PATH to be found"


def test_docker_cli_env_passes_daemon_locators(monkeypatch) -> None:
    """Docker's own connection settings must survive the scrub."""
    monkeypatch.setenv("DOCKER_HOST", "tcp://127.0.0.1:2375")
    monkeypatch.setenv("DOCKER_CONTEXT", "rootless")
    env = docker_cli_env()
    assert env["DOCKER_HOST"] == "tcp://127.0.0.1:2375"
    assert env["DOCKER_CONTEXT"] == "rootless"


def test_docker_executor_uses_the_scrubbed_cli_env(monkeypatch, tmp_path) -> None:
    """``DockerExecutor.execute`` must not pass ``os.environ`` through."""
    monkeypatch.setenv("ZERO_OPENAI_API_KEY", "sk-must-not-leak")
    captured: dict[str, dict[str, str]] = {}

    def fake_popen(argv, **kwargs):
        captured["env"] = dict(kwargs.get("env") or {})
        raise FileNotFoundError("probe only")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    DockerExecutor().execute(["true"], cwd=str(tmp_path), timeout_seconds=5, output_limit=1024)

    assert "env" in captured, "the executor must pass an explicit environment"
    assert "ZERO_OPENAI_API_KEY" not in captured["env"]


# ----------------------------------------------------------------------
# 5. Admin CSRF tokens are random, not derived
# ----------------------------------------------------------------------


def test_admin_csrf_token_is_not_derivable_from_the_session_id() -> None:
    """Knowing a session id must not yield the matching CSRF token."""
    import hashlib

    from zero.manage import web

    web._sessions.clear()
    web._csrf_tokens.clear()
    try:
        sid, _ = web._new_session()
        token = web._csrf(sid)
        derived = hashlib.sha256(f"csrf:{sid}".encode()).hexdigest()[:32]
        assert token, "a live session must have a CSRF token"
        assert token != derived, "the CSRF token must not be a hash of the session id"
        assert len(token) >= 32, "the CSRF token must carry real entropy"
        assert web._check_csrf(sid, token) is True
        assert web._check_csrf(sid, derived) is False
    finally:
        web._sessions.clear()
        web._csrf_tokens.clear()


def test_admin_csrf_tokens_are_unique_per_session() -> None:
    from zero.manage import web

    web._sessions.clear()
    web._csrf_tokens.clear()
    try:
        first, _ = web._new_session()
        second, _ = web._new_session()
        assert web._csrf(first) != web._csrf(second)
        # A token from one session must never validate for another.
        assert web._check_csrf(second, web._csrf(first)) is False
    finally:
        web._sessions.clear()
        web._csrf_tokens.clear()


def test_admin_csrf_rejects_unknown_and_empty_sessions() -> None:
    from zero.manage import web

    web._sessions.clear()
    web._csrf_tokens.clear()
    try:
        assert web._csrf("") == ""
        assert web._check_csrf("", "") is False
        assert web._check_csrf("not-a-session", "anything") is False
    finally:
        web._sessions.clear()
        web._csrf_tokens.clear()


def test_admin_logout_and_password_change_drop_the_csrf_token() -> None:
    """A dead session must not keep a usable CSRF token behind."""
    from zero.manage import web

    source = inspect.getsource(web.register_admin)
    assert "_csrf_tokens.pop(sid, None)" in source, "logout must drop the session's CSRF token"
    assert "_csrf_tokens.clear()" in source, (
        "a password change must invalidate every CSRF token with its sessions"
    )


# ----------------------------------------------------------------------
# 6. No live credentials in tracked files
# ----------------------------------------------------------------------

_CREDENTIAL_PATTERNS = (
    re.compile(r"\b\d{8,}:[A-Za-z0-9_-]{30,}\b"),  # Telegram bot token
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),  # OpenAI-style API key
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\bghp_[A-Za-z0-9]{30,}\b"),  # GitHub PAT
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),  # Slack token
)

_SCANNED_SUFFIXES = {".py", ".sh", ".md", ".txt", ".yml", ".yaml", ".json", ".jsonl", ".log"}
# Synthetic fixtures whose "tokens" are literal test data, not secrets.
_ALLOWED_FIXTURE_TOKENS = ("AAHcSECRETVALUE", "AAABCDEF12345")


def _tracked_files() -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=REPO_ROOT,
            capture_output=True,
            check=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        pytest.skip("git is unavailable; credential scan needs the tracked file list")
    return [REPO_ROOT / name for name in out.stdout.decode().split("\0") if name]


def test_no_live_credentials_in_tracked_files() -> None:
    """Tracked files must never carry a real bot token or API key.

    The pre-fix tree embedded a live Telegram bot token and a live
    provider key in e2e scripts and shipped them again inside captured
    evidence logs (1,342 occurrences in ``realrun-evidence/server.log``).
    """
    offenders: list[str] = []
    for path in _tracked_files():
        if path.suffix.lower() not in _SCANNED_SUFFIXES or not path.is_file():
            continue
        if path.name == Path(__file__).name:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for pattern in _CREDENTIAL_PATTERNS:
            for match in pattern.finditer(text):
                found = match.group(0)
                if any(fixture in found for fixture in _ALLOWED_FIXTURE_TOKENS):
                    continue
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {found[:12]}…")
    assert offenders == [], "live credentials found in tracked files: " + "; ".join(
        sorted(set(offenders))[:20]
    )


def test_e2e_scripts_read_credentials_from_the_environment() -> None:
    """The e2e drivers must source secrets from env, not literals."""
    setup = (REPO_ROOT / "scripts" / "e2e_round5_setup.py").read_text(encoding="utf-8")
    assert 'environ.get("E2E_BOT_TOKEN"' in setup
    assert 'environ.get("E2E_PROVIDER_KEY"' in setup
    assert 'environ.get("E2E_WEBHOOK_SECRET"' in setup
    assert "missing required environment variables" in setup, (
        "the setup script must fail closed when credentials are absent"
    )

    engine = (REPO_ROOT / "scripts" / "e2e_round5_engine.sh").read_text(encoding="utf-8")
    assert "E2E_WEBHOOK_SECRET:?" in engine, "the engine script must require the secret from env"
