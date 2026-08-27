"""ASGI entry point.

Run with:

    uvicorn zero.main:app --reload

Or via the console script installed by ``pip install -e .[dev]``:

    zero-develop serve

The :data:`app` module-level global is constructed eagerly from process
configuration so that ``uvicorn zero.main:app`` and ``zero-develop`` use
one fail-closed startup path.
Tests call :func:`create_app` directly with isolated test settings.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from zero.app.api import create_app
from zero.app.observability_service import configure_logging
from zero.config import ConfigError, Settings

_logger = logging.getLogger("zero")


def _load_settings() -> Settings:
    """Load settings; raise :class:`ConfigError` if invalid.

    On a real production deploy this raises and the process exits
    non-zero, which is the correct fail-closed behavior. When ZERO_ENV
    is not configured anywhere at all (process env or env file), a
    local development server with safe defaults is assumed — matching
    ``zero-develop serve`` and ``scripts/run_dev.sh``. Explicit but
    invalid or incomplete production/test configuration still fails
    closed exactly as before.
    """
    from zero.cli import _ensure_development_secret_key

    settings = Settings.load(zero_env_fallback="development")
    # The uvicorn entry point shares the CLI's dev-only key bootstrap so
    # the encrypted secret store works identically on both paths.
    return _ensure_development_secret_key(settings, None)


def _configure_logging(settings: Settings) -> None:
    configure_logging(settings.log_level)
    _logger.debug("settings: %s", settings.safe_repr())


# ----------------------------------------------------------------------
# Module-level ASGI app
# ----------------------------------------------------------------------
#
# We construct the app eagerly at import time so that `uvicorn
# zero.main:app` works without extra ceremony. Tests do NOT use this
# module-level app; they call `create_app` directly with test settings.
#
# If configuration is invalid (for example an explicit production
# config missing its secrets), we let the ConfigError propagate so the
# process exits non-zero with a clear message. This is the fail-closed
# behavior required by ADR 0004. A completely unset environment is not
# an error: it falls back to development defaults (see _load_settings).

try:
    _settings = _load_settings()
    _configure_logging(_settings)
    app: FastAPI = create_app(_settings)
except ConfigError as exc:
    # Print to stderr because logging may not be configured yet.
    import sys

    print(f"zero: configuration error: {exc}", file=sys.stderr)
    # Re-raise so the process exits non-zero. We do not assign `app`
    # so uvicorn's import will fail loudly.
    raise


def resolve_bind() -> tuple[str, int]:
    """Loopback-first bind address; ZERO_PANEL_PORT overrides the port.

    Audit D6: the TUI reads the same variable so its admin links point
    at the panel actually running.
    """
    import os

    port = int(os.environ.get("ZERO_PANEL_PORT", "8000"))
    return "127.0.0.1", port


def run() -> None:
    """Console-script entry point: run uvicorn with sensible defaults."""
    import uvicorn

    host, port = resolve_bind()
    uvicorn.run(
        "zero.main:app",
        host=host,
        port=port,
        reload=_settings.is_development,
        log_level=_settings.log_level.lower(),
    )


__all__ = ["app", "create_app", "resolve_bind", "run"]
