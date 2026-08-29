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
    # Not required: a bare `zero-develop` must print the full help (same
    # contract as `zero`), not argparse's terse "error: the following
    # arguments are required: command".
    subparsers = parser.add_subparsers(dest="command")

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
    """True when the effective .env sets ZERO_ENV itself.

    With ``env_file=None`` the effective file is ``$ZERO_HOME/.env``
    (Settings.load now defaults to it), so it must be consulted too —
    otherwise the dev banner claimed development defaults while the
    engine was actually loading a pinned configuration.
    """
    path = Path(env_file) if env_file else (
        Path(os.environ.get("ZERO_HOME", str(Path.home() / ".zero"))) / ".env"
    )
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
        reused = key_file.exists()
        if reused:
            key = key_file.read_text(encoding="utf-8").strip()
            reused = bool(key)
        if not reused:
            key = _secrets.token_urlsafe(48)
            key_file.write_text(key, encoding="utf-8")
            os.chmod(key_file, 0o600)
        # Persist for future processes via the supported .env path — but
        # only when the exact key line is not already there (rewriting
        # the file on every run was needless I/O and made the banner
        # claim a fresh key was "generated" each start).
        env_path = Path(env_file) if env_file else home / ".env"
        key_line = f"ZERO_SECRET_KEY={key}"
        lines: list[str] = []
        if env_path.is_file():
            lines = env_path.read_text(encoding="utf-8").splitlines()
        if key_line not in lines:
            lines = [
                line
                for line in lines
                if not line.startswith("ZERO_SECRET_KEY=")
            ]
            lines.append(key_line)
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
    # Honesty fix: the old message said "generated" on EVERY serve run
    # even when an existing key was merely reloaded — operators reasonably
    # read that as the key being rotated (it is not).
    verb = "reusing the existing" if reused else "generated a"
    print(
        f"[zero] {verb} development encryption key at {key_file} "
        "(local-only; run 'zero setup' for production).",
        file=sys.stderr,
    )
    return Settings.load(env_file=env_file, zero_env_fallback="development")


def _managed_service_pid() -> int | None:
    """PID of the managed service ($ZERO_HOME/zero.pid) when it is alive."""
    home = Path(os.environ.get("ZERO_HOME", str(Path.home() / ".zero")))
    pid_file = home / "zero.pid"
    if not pid_file.is_file():
        return None
    try:
        pid = int(pid_file.read_text().strip())
    except ValueError:
        return None
    from zero.manage.cli import _pid_alive

    return pid if _pid_alive(pid) else None


def _managed_service_bind() -> tuple[str, int]:
    """The (host, port) the managed service ('zero start') listens on.

    Bug fix (port mismatch): the serve pre-check hard-assumed port 8000
    while ``zero start`` honors ``server.host``/``server.port`` from
    config.yaml. Both sides now resolve the managed bind through ONE
    helper (``zero.manage.cli._managed_bind``), so the two CLIs can never
    disagree about where the managed service lives. Defaults:
    127.0.0.1:8000 — an unreadable/missing configuration falls back so a
    fresh host stays startable.
    """
    try:
        from zero.manage.cli import _managed_bind

        return _managed_bind()
    except Exception:  # noqa: BLE001 - never brick serve over config metadata
        return "127.0.0.1", 8000


def _binds_overlap(host_a: str, port_a: int, host_b: str, port_b: int) -> bool:
    """True when two (host, port) binds would fight for the same socket.

    A wildcard host ("", "0.0.0.0", "::") covers every interface —
    including the other side's loopback bind; ``localhost`` is treated as
    ``127.0.0.1``. Different ports never overlap.
    """
    if port_a != port_b:
        return False
    wildcards = {"", "0.0.0.0", "::"}
    if host_a in wildcards or host_b in wildcards:
        return True
    return host_a == host_b or {host_a, host_b} == {"localhost", "127.0.0.1"}


def _suggest_free_port(host: str, port: int, tries: int = 50) -> int:
    """The first bindable port above ``port`` (``port + 1`` as fallback).

    Bug fix (circular hint): the refusal messages used to suggest a
    hardcoded ``--port 8001`` — the exact command the operator had just
    run (and, in the busy-port branch, possibly the port that just
    failed). The suggestion is now verified bindable at the moment it is
    printed.
    """
    for candidate in range(port + 1, port + 1 + tries):
        if _port_available(host, candidate):
            return candidate
    return port + 1


def _port_available(host: str, port: int) -> bool:
    """True when a server could bind (host, port) right now."""
    import socket

    # Something LISTENING there accepts the connection: definitively busy.
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return False
    except OSError:
        pass
    # Not accepting — try a real bind (no SO_REUSEADDR, so a foreign
    # listener or a bound-but-paused socket still reports busy).
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, port))
    except OSError:
        return False
    return True


def _dev_serve_banner(home: Path) -> list[str]:
    """The two guidance lines printed when ZERO_ENV is not explicit.

    Bug fix: the second line always said "run 'zero setup'" even when a
    configured installation already existed — precisely the operator
    state in the reported session (setup done, then confused about why
    `zero-develop serve` still assumed development). The first line no
    longer hardcodes ``./zero_develop.db``: with ``$ZERO_HOME/.env`` now
    loaded by default the resolved database may be a pinned absolute
    path (the real path is printed separately from loaded settings).
    """
    first = "[zero] ZERO_ENV is not explicitly set; assuming 'development'."
    if (home / "config.yaml").is_file():
        second = (
            f"[zero] A configured installation exists at {home / 'config.yaml'} — "
            "run 'zero start' for that service, or export ZERO_ENV=production "
            "with its required secrets."
        )
    else:
        second = (
            "[zero] For production run 'zero setup' or export ZERO_ENV=production "
            "with its required secrets."
        )
    return [first, second]


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from zero.app.api import create_app
    from zero.app.observability_service import configure_logging

    # Bug fix (WinError 10048 / EADDRINUSE): serve used to walk into
    # uvicorn and die on an ugly bind traceback whenever the managed
    # service (or anything else) already held the port — and the process
    # still exited 0. Detect both cases first and fail with guidance.
    #
    # Bug fix (port-blind refusal): the managed-service check used to fire
    # BEFORE looking at the requested port, so `zero-develop serve --port
    # 8001` was refused while the managed service ran on 8000 — with the
    # false claim that the foreground server "cannot bind the same port".
    # The pre-checks are now port-aware: only a genuine bind conflict is
    # refused; a free port runs alongside the managed service (with an
    # honest note), and both refusals suggest a port verified free NOW.
    managed_pid = _managed_service_pid()
    if managed_pid is not None:
        managed_host, managed_port = _managed_service_bind()
        if _binds_overlap(args.host, args.port, managed_host, managed_port):
            print(
                f"[zero] the Zero service is already running (pid {managed_pid}, "
                f"from 'zero start') on {managed_host}:{managed_port} — "
                "the port you asked for.",
                file=sys.stderr,
            )
            free_port = _suggest_free_port(args.host, args.port)
            print(
                f"[zero] stop it first ('zero stop') or pick a free port, e.g.: "
                f"zero-develop serve --port {free_port}",
                file=sys.stderr,
            )
            return 1
    if not _port_available(args.host, args.port):
        print(
            f"[zero] {args.host}:{args.port} is already in use by another process.",
            file=sys.stderr,
        )
        free_port = _suggest_free_port(args.host, args.port)
        print(
            f"[zero] stop that process or pick a free port, e.g.: "
            f"zero-develop serve --port {free_port}",
            file=sys.stderr,
        )
        return 1
    if managed_pid is not None:
        # A different, free port: running alongside is the operator's
        # explicit choice (e.g. a dev server next to the managed service).
        # Say so honestly instead of refusing — note the shared $ZERO_HOME
        # (same database, same Telegram poller) so the tradeoff is visible.
        managed_host, managed_port = _managed_service_bind()
        print(
            f"[zero] note: the managed service (pid {managed_pid}) keeps running "
            f"on {managed_host}:{managed_port}; this foreground server runs "
            f"alongside it on {args.host}:{args.port} (shared ZERO_HOME state).",
            file=sys.stderr,
        )

    # Bare-start usability (installation audit R2): `zero-develop serve`
    # with no configuration starts a local development server instead of
    # failing closed without guidance. Production keeps requiring an
    # explicit ZERO_ENV=production plus its secrets; only the explicit,
    # safe development defaults may be assumed automatically.
    dev_default = not ("ZERO_ENV" in os.environ or _env_file_declares_zero_env(args.env_file))
    if dev_default:
        settings = Settings.load(
            env_file=args.env_file,
            zero_env_fallback="development",
        )
        settings = _ensure_development_secret_key(settings, args.env_file)
        # Honest banner (bug fix): printed from the RESOLVED settings —
        # the old banner hardcoded "local SQLite at ./zero_develop.db"
        # even when $ZERO_HOME/.env pinned a different, absolute path.
        home = Path(os.environ.get("ZERO_HOME", str(Path.home() / ".zero")))
        for line in _dev_serve_banner(home):
            print(line, file=sys.stderr)
        try:
            from zero.manage.core.env_file import absolutize_sqlite_url

            db_display = absolutize_sqlite_url(settings.database_url, Path.cwd())
        except Exception:  # noqa: BLE001 - banner must never crash serve
            db_display = str(settings.database_url)
        print(f"[zero] database: {db_display}", file=sys.stderr)
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
    if not getattr(args, "command", None):
        # Mirror `zero`: bare invocation prints the full help (exit 2 =
        # usage, never a traceback).
        parser.print_help()
        return 2
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
