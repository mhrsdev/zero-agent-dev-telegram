"""Zero Develop command-line interface.

Per the release audit (Phase 6, Hermes parity): a real operational
runtime needs a CLI surface. This module provides the minimum honest
operational commands:

- ``serve``: run the ASGI app (managed workers included);
- ``migrate``: apply pending database migrations and exit;
- ``check-config``: load and validate configuration, print the redacted
  settings plus the capability report, and exit non-zero on failure;
- ``reconcile``: run the startup recovery procedures once (including
  merge crash-window reconciliation) and print the report.

The CLI never prints secret values; all reporting goes through the same
redaction helpers as the application.
"""

from __future__ import annotations

import argparse
import json
import sys

from zero import __version__
from zero.config import ConfigError, Settings


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zero-develop",
        description="Zero Develop multi-agent control plane",
    )
    parser.add_argument("--version", action="version", version=f"zero-develop {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="run the ASGI app with managed workers")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument(
        "--env-file",
        default=None,
        help="optional .env path for local development overrides",
    )

    migrate = subparsers.add_parser("migrate", help="apply pending migrations and exit")
    migrate.add_argument(
        "--env-file",
        default=None,
        help="optional .env path for local development overrides",
    )

    check = subparsers.add_parser(
        "check-config", help="validate configuration and print capabilities"
    )
    check.add_argument(
        "--env-file",
        default=None,
        help="optional .env path for local development overrides",
    )

    reconcile = subparsers.add_parser(
        "reconcile", help="run recovery/reconciliation procedures once"
    )
    reconcile.add_argument(
        "--env-file",
        default=None,
        help="optional .env path for local development overrides",
    )
    return parser


def _load(env_file: str | None) -> Settings:
    return Settings.load(env_file=env_file)


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from zero.app.api import create_app
    from zero.app.observability_service import configure_logging

    settings = _load(args.env_file)
    configure_logging(settings.log_level)
    uvicorn.run(
        create_app(settings),
        host=args.host,
        port=args.port,
        reload=False,
        log_level=settings.log_level.lower(),
    )
    return 0


def _cmd_migrate(args: argparse.Namespace) -> int:
    from zero.persistence.connection import open_database
    from zero.persistence.migrations import apply_migrations

    settings = _load(args.env_file)
    database = open_database(settings)
    applied = apply_migrations(database)
    print(json.dumps({"status": "ok", "applied": applied}))
    return 0


def _cmd_check_config(args: argparse.Namespace) -> int:
    from zero.app.capabilities import compute_capabilities

    try:
        settings = _load(args.env_file)
    except ConfigError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}))
        return 1
    print(
        json.dumps(
            {
                "status": "ok",
                "settings": settings.safe_repr(),
                "capabilities": compute_capabilities(settings),
            },
            indent=2,
        )
    )
    return 0


def _cmd_reconcile(args: argparse.Namespace) -> int:
    from zero.app.api import build_application_services
    from zero.persistence.connection import open_database
    from zero.persistence.migrations import count_applied_migrations

    settings = _load(args.env_file)
    database = open_database(settings)
    # build_application_services returns (database, services) and runs
    # startup recovery as part of composition.
    _database, services = build_application_services(settings, database)
    report = services.recovery.run_all_recovery()
    print(
        json.dumps(
            {"status": "ok", "migrations": count_applied_migrations(database), **report},
            indent=2,
            default=str,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "serve": _cmd_serve,
        "migrate": _cmd_migrate,
        "check-config": _cmd_check_config,
        "reconcile": _cmd_reconcile,
    }
    handler = handlers[args.command]
    try:
        return handler(args)
    except ConfigError as exc:
        print(f"zero: configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
