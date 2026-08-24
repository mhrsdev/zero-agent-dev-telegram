"""ASGI entry point.

Run with:

    uvicorn zero.main:app --reload

Or via the console script installed by ``pip install -e .[dev]``:

    zero-develop

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
    non-zero, which is the correct fail-closed behavior. On development
    and test it auto-selects safe defaults.
    """
    return Settings.load()


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
# If configuration is invalid (e.g. ZERO_ENV missing), we let the
# ConfigError propagate so the process exits non-zero with a clear
# message. This is the fail-closed behavior required by ADR 0004.

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


def run() -> None:
    """Console-script entry point: run uvicorn with sensible defaults."""
    import uvicorn

    uvicorn.run(
        "zero.main:app",
        host="127.0.0.1",
        port=8000,
        reload=_settings.is_development,
        log_level=_settings.log_level.lower(),
    )


__all__ = ["app", "create_app", "run"]
