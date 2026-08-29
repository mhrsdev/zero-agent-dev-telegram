"""``$ZERO_HOME/.env`` helpers — the machine-stable engine anchor.

Root-cause fix (2026-08-29, "Telegram bot completely dead" session): the
engine resolves its database from ``ZERO_*`` environment values, and the
development fallback is ``sqlite:///./zero_develop.db`` — RELATIVE TO THE
PROCESS CWD. ``zero setup`` stored operator secrets into the database it
saw from ITS directory, while ``zero start`` later booted the engine from
ANOTHER directory and silently created a fresh database with a new
"Zero Management" project. Every ``sec_...`` reference in config.yaml
then failed with ``SecretNotFoundError``, no Telegram binding was ever
created, and the bot could not respond even to ``/start`` (the web UI,
which needs no bot token, kept working — matching the report exactly).

The cure is to make ``$ZERO_HOME/.env`` the single machine-stable anchor:

- the engine (:mod:`zero.main`, every CLI entry point) now LOADS that
  file by default (see ``Settings.load`` in :mod:`zero.config`); and
- ``zero setup`` / ``zero doctor`` PIN the exact database the wizard
  used into the file as an ABSOLUTE ``ZERO_DATABASE_URL`` so every
  later engine start — from any directory — opens the same database.

This module is deliberately dependency-free (stdlib only) so both the
management CLI and the engine can import it cheaply.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Keys this toolchain owns inside ``$ZERO_HOME/.env``.
ENV_FILE_KEYS = ("ZERO_ENV", "ZERO_DATABASE_URL", "ZERO_SECRET_KEY")


def home_dotenv_path(home: Path | None = None) -> Path:
    """The canonical ``$ZERO_HOME/.env`` path."""
    base = Path(home) if home is not None else Path(
        os.environ.get("ZERO_HOME", str(Path.home() / ".zero"))
    )
    return base / ".env"


def read_dotenv(path: Path) -> dict[str, str]:
    """Parse a dotenv file into a dict (same tiny format as Settings)."""
    pairs: dict[str, str] = {}
    if not path.is_file():
        return pairs
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        if key:
            pairs[key] = value
    return pairs


def upsert_dotenv(path: Path, updates: dict[str, str]) -> list[str]:
    """Set ``updates`` in the dotenv file, preserving unrelated lines.

    Returns the keys that were actually ADDED or CHANGED (a key already
    holding the target value is left untouched and not reported).
    The file is created if missing. Existing permissions are preserved;
    new files get 0600 (best-effort — Windows ignores the mode bits).
    """
    lines: list[str] = []
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()
    changed: list[str] = []
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        key = stripped.partition("=")[0].strip() if "=" in stripped else None
        if key and key in remaining:
            new_value = remaining.pop(key)
            if stripped != f"{key}={new_value}":
                changed.append(key)
                out.append(f"{key}={new_value}")
            else:
                out.append(line)
        else:
            out.append(line)
    for key, value in remaining.items():
        changed.append(key)
        out.append(f"{key}={value}")
    if not changed:
        return []
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return changed


def absolutize_sqlite_url(url: str, cwd: Path) -> str:
    """Rewrite a RELATIVE sqlite URL into an absolute one for ``cwd``.

    ``sqlite:///./zero_develop.db`` → ``sqlite:///<cwd>/zero_develop.db``.
    Absolute URLs, ``:memory:`` and non-sqlite URLs are returned as-is.
    """
    if not url.startswith("sqlite") or ":memory:" in url:
        return url
    if url.startswith("sqlite:////"):
        return url  # already an absolute filesystem path
    if url.startswith("sqlite:///"):
        raw = url[len("sqlite:///") :]
    elif url.startswith("sqlite://"):
        raw = url[len("sqlite://") :]
    else:
        return url
    if not raw or raw.startswith("/"):
        return url
    return "sqlite:///" + str((Path(cwd) / raw).resolve())


def pin_database_url(cwd: Path | None = None, home: Path | None = None) -> dict[str, object]:
    """Pin the CURRENT process's resolved database into ``$ZERO_HOME/.env``.

    Called right after a successful ``zero setup`` commit (and by
    ``zero doctor --repair`` with an explicit target): whatever database
    the wizard just wrote secrets into becomes THE database every later
    engine start opens, regardless of the directory the operator runs
    from. Existing explicit operator configuration wins:

    - a real ``ZERO_DATABASE_URL`` process-env var → no file write at all;
    - a ``ZERO_ENV`` already present in the file → ``ZERO_ENV`` not added.

    Returns a small report dict: ``{"pinned": [keys...], "database_url":
    <abs-url-or-None>, "skipped": <reason-or-None>}``.
    """
    cwd = Path(cwd) if cwd is not None else Path.cwd()

    if os.environ.get("ZERO_DATABASE_URL"):
        return {
            "pinned": [],
            "database_url": None,
            "skipped": "ZERO_DATABASE_URL is set in the process environment",
        }

    from zero.config import Settings

    # Resolve exactly what the engine bridge in THIS directory would use.
    env_path = home_dotenv_path(home)
    settings = Settings.load(
        env_file=str(env_path), zero_env_fallback="development"
    )
    abs_url = absolutize_sqlite_url(settings.database_url, cwd)

    updates: dict[str, str] = {"ZERO_DATABASE_URL": abs_url}
    file_values = read_dotenv(env_path)
    if not file_values.get("ZERO_ENV") and not os.environ.get("ZERO_ENV"):
        updates["ZERO_ENV"] = "development"

    changed = upsert_dotenv(env_path, updates)
    return {
        "pinned": changed,
        "database_url": abs_url,
        "skipped": None if changed else "already up to date",
    }


def record_database_usage(cwd: Path | None = None, home: Path | None = None) -> None:
    """Append the current process's resolved database to the usage history.

    Best-effort bookkeeping for ``zero doctor``: when the operator later
    boots the engine from a different directory, the doctor reads this
    history and can locate the database that holds the secrets even if
    it sits in a directory no heuristic would guess. Capped at the 12
    most recent entries; silently ignored on any I/O problem.
    """
    try:
        cwd = Path(cwd) if cwd is not None else Path.cwd()
        from zero.config import Settings

        env_path = home_dotenv_path(home)
        settings = Settings.load(
            env_file=str(env_path), zero_env_fallback="development"
        )
        abs_url = absolutize_sqlite_url(settings.database_url, cwd)
        base = env_path.parent
        state_dir = base / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        history_path = state_dir / "db-history.json"
        entries: list[dict[str, object]] = []
        if history_path.is_file():
            import json

            try:
                entries = json.loads(history_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                entries = []
        entry = {"url": abs_url, "cwd": str(cwd)}
        entries = [e for e in entries if e.get("url") != abs_url] + [entry]
        entries = entries[-12:]
        import json

        history_path.write_text(
            json.dumps(entries, indent=2), encoding="utf-8"
        )
    except Exception:  # noqa: BLE001 - pure bookkeeping, never break a command
        return


def history_database_files(home: Path | None = None) -> list[Path]:
    """Database files previously resolved by CLI entry points."""
    base = home_dotenv_path(home).parent / "state" / "db-history.json"
    out: list[Path] = []
    if not base.is_file():
        return out
    import json

    try:
        entries = json.loads(base.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return out
    for e in entries:
        url = str(e.get("url", ""))
        if url.startswith("sqlite:////"):
            raw = url[len("sqlite:////"):]
        elif url.startswith("sqlite:///"):
            raw = url[len("sqlite:///"):]
        else:
            continue
        if not raw:
            continue
        p = Path(raw if raw.startswith("/") else "/" + raw)
        if p.is_file() and p not in out:
            out.append(p)
    return out


__all__ = [
    "ENV_FILE_KEYS",
    "absolutize_sqlite_url",
    "history_database_files",
    "home_dotenv_path",
    "pin_database_url",
    "read_dotenv",
    "record_database_usage",
    "upsert_dotenv",
]
