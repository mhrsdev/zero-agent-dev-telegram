"""Regression: management layer (admin GUI + backup daemon) must boot.

Real server run on 2026-08-28 showed "management layer init skipped:
TypeError" on every `uvicorn zero.main:app` boot. Root cause: the backup
daemon shutdown hook used the decorator form `@app.router.on_shutdown`,
but Starlette's ``Router.on_shutdown`` is a plain list — "decorating" it
raised ``TypeError('list' object is not callable)``, the broad except
swallowed it, and the just-started daemon thread leaked on every boot
(never stopped on shutdown).

These tests pin:
1. a development create_app() with a management home boots WITHOUT
   the "management layer init skipped" warning;
2. the backup daemon is exposed on app.state and its shutdown hook is
   registered on the router;
3. decorating `app.router.on_shutdown` raises TypeError (guards against
   accidentally reintroducing the pattern);
4. the swallow-all warning logs the error MESSAGE, not just its type.
"""

from __future__ import annotations

import logging
from pathlib import Path

import fastapi
import pytest
from starlette.routing import Router

from zero.app.api import create_app
from zero.config import Settings


def _dev_settings(tmp_path: Path, config_yaml: str) -> Settings:
    home = tmp_path / "zero-home"
    home.mkdir(exist_ok=True)
    (home / "config.yaml").write_text(config_yaml, encoding="utf-8")
    # load_for_test() pins zero_env="test" by design; the management block
    # is development-only, so construct development Settings directly.
    return Settings(
        zero_env="development",
        database_url="sqlite::memory:",
        log_level="WARNING",
        workers_enabled=False,
    )


def test_router_on_shutdown_is_not_decoratable() -> None:
    """The historical bug pattern: `@app.router.on_shutdown` on a list."""
    app = fastapi.FastAPI()
    assert isinstance(app.router.on_shutdown, list)  # instance attr: a list
    with pytest.raises(TypeError):

        @app.router.on_shutdown  # type: ignore[operator]
        async def _boom() -> None: ...


def test_starlette_router_class_has_no_callable_on_shutdown() -> None:
    """Starlette >= 0.37 removed the class attribute entirely."""
    attr = getattr(Router, "on_shutdown", None)
    assert not callable(attr), "Router.on_shutdown became callable again; review api.py"


def test_create_app_boots_management_layer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """No 'management layer init skipped' warning on a normal dev boot."""
    settings = _dev_settings(
        tmp_path,
        "schema_version: 1\ntelegram:\n  mode: bot_api\n"
        "backups:\n  schedule: daily\n  retention: 3\n",
    )
    monkeypatch.setenv("ZERO_HOME", str(tmp_path / "zero-home"))
    with caplog.at_level(logging.WARNING, logger="zero"):
        app = create_app(settings)

    warnings = [
        r.getMessage()
        for r in caplog.records
        if "management layer init skipped" in r.getMessage()
    ]
    assert warnings == [], f"management layer failed to boot: {warnings}"
    assert type(app.state.backup_daemon).__name__ == "BackupDaemon"
    assert app.router.on_shutdown, "no shutdown handlers registered at all"


def test_management_layer_failure_includes_exception_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """The swallow-all warning must log the error MESSAGE, not just type."""
    settings = _dev_settings(
        tmp_path, "schema_version: 1\nbackups:\n  schedule: daily\n"
    )
    monkeypatch.setenv("ZERO_HOME", str(tmp_path / "zero-home"))

    def _boom(*a, **k):
        raise ValueError("seed-keyword-failure")

    monkeypatch.setattr(
        "zero.manage.services.backup_daemon.BackupDaemon.start_thread", _boom
    )
    with caplog.at_level(logging.WARNING, logger="zero"):
        create_app(settings)

    msgs = [
        r.getMessage()
        for r in caplog.records
        if "management layer init skipped" in r.getMessage()
    ]
    assert msgs, "expected the management warning"
    assert "seed-keyword-failure" in msgs[0], msgs[0]
