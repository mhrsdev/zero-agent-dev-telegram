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
import os
import sys
from pathlib import Path

from zero import __version__
from zero.config import ConfigError, Settings, _parse_dotenv


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
    # Strict load: operational/validation commands must not assume an
    # environment the operator never chose.
    return Settings.load(env_file=env_file)


def _env_file_declares_zero_env(env_file: str | None) -> bool:
    """True when the provided .env file sets ZERO_ENV itself."""
    if not env_file:
        return False
    path = Path(env_file)
    if not path.is_file():
        return False
    return any(key == "ZERO_ENV" for key, _ in _parse_dotenv(path))


def _ensure_development_secret_key(settings: Settings, env_file: str | None) -> Settings:
    """Auto-provision an encryption key for a bare development server.

    The encrypted secret store fails closed without key material
    (``secret_service._key_material``), which on a fresh host turned
    every ``POST /secrets`` into an opaque 500. The management CLI
    solved this for its wizard (``$ZERO_HOME/secret.key``, 0600); this
    applies the identical bootstrap to ``zero-develop serve`` when — and
    only when — running with development defaults and no explicit key.
    Production and test keep their strict, fail-closed requirements.
    """
    if not settings.is_development or settings.secret_key is not None:
        return settings
    import secrets as _secrets

    home = Path(os.environ.get("ZERO_HOME", str(Path.home() / ".zero")))
    key_file = home / "secret.key"
    try:
        home.mkdir(parents=True, exist_ok=True)
        if key_file.exists():
            key = key_file.read_text(encoding="utf-8").strip()
        else:
            key = _secrets.token_urlsafe(48)
            key_file.write_text(key, encoding="utf-8")
            os.chmod(key_file, 0o600)
        # Persist for future processes via the supported .env path.
        env_path = Path(env_file) if env_file else home / ".env"
        lines: list[str] = []
        if env_path.is_file():
            lines = [
                line
                for line in env_path.read_text(encoding="utf-8").splitlines()
                if not line.startswith("ZERO_SECRET_KEY=")
            ]
        lines.append(f"ZERO_SECRET_KEY={key}")
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.chmod(env_path, 0o600)
    except OSError as exc:
        print(
            f"[zero] could not persist a development secret key ({exc}); "
            "the encrypted secret store will refuse writes until "
            "ZERO_SECRET_KEY is configured.",
            file=sys.stderr,
        )
        return settings
    os.environ["ZERO_SECRET_KEY"] = key
    print(
        f"[zero] generated a development encryption key at {key_file} "
        "(local-only; run 'zero setup' for production).",
        file=sys.stderr,
    )
    return Settings.load(env_file=env_file, zero_env_fallback="development")


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from zero.app.api import create_app
    from zero.app.observability_service import configure_logging

    # Bare-start usability (installation audit R2): `zero-develop serve`
    # with no configuration starts a local development server instead of
    # failing closed without guidance. Production keeps requiring an
    # explicit ZERO_ENV=production plus its secrets; only the explicit,
    # safe development defaults may be assumed automatically.
    dev_default = not ("ZERO_ENV" in os.environ or _env_file_declares_zero_env(args.env_file))
    if dev_default:
        print(
            "[zero] ZERO_ENV is not set; assuming 'development' "
            "(local SQLite at ./zero_develop.db).",
            file=sys.stderr,
        )
        print(
            "[zero] For production run 'zero setup' or export ZERO_ENV=production "
            "with its required secrets.",
            file=sys.stderr,
        )
        settings = Settings.load(
            env_file=args.env_file,
            zero_env_fallback="development",
        )
        settings = _ensure_development_secret_key(settings, args.env_file)
    else:
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
        # Every failure prints a safe next action (per the installation
        # audit acceptance criteria).
        print(f"zero: configuration error: {exc}", file=sys.stderr)
        print(
            "zero: next action — see .env.example and docs/USAGE.md, "
            "or run 'zero setup' for guided configuration.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
