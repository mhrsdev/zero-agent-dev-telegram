"""Diagnostics service powering `zero doctor`.

2026-08-29 extension ("Telegram bot completely dead" session): the
original checks probed version/config/database/telegram, but they all
inspected the database the CURRENT CWD resolves to — the exact value
that had drifted between `zero setup` and `zero start`. The doctor
thereby reported a healthy installation while the engine was looking
into a secret-less database. The new checks close that gap:

- ``secret-key``      — encryption key material available?
- ``secret-references`` — does EVERY ``sec_...`` reference in
  config.yaml actually resolve inside the engine database?
- ``database-drift``  — when references do not resolve, scan the
  known candidate databases ($ZERO_HOME, $ZERO_HOME/state, CWD) for
  the one that DOES contain them; `zero doctor --fix` pins that
  database into ``$ZERO_HOME/.env`` (backed up first) and re-verifies.
"""

from __future__ import annotations

import os
import shutil
import socket
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from zero.manage.core import probes
from zero.manage.core.config import ConfigError, ConfigService


def _candidate_databases(home: Path) -> list[Path]:
    """Known locations a Zero SQLite database may live in.

    Ordered by likelihood: locations recorded in the CLI usage history,
    the resolved $ZERO_HOME (plus its state dir and parent — the
    historical drift source: running `zero start` from the user home),
    and the current working directory INCLUDING its one-level
    subdirectories (the other half of the drift: `zero setup` ran from
    the repo folder). Duplicates removed; missing files skipped.
    """
    from zero.manage.core.env_file import history_database_files

    candidates: list[Path] = []
    seen: set[Path] = set()

    def consider(path: Path) -> None:
        try:
            real = path.resolve()
        except OSError:
            return
        if real not in seen and real.is_file():
            seen.add(real)
            candidates.append(real)

    for p in history_database_files(home):
        consider(p)
    dirs = [home, home / "state", home.parent, Path.cwd()]
    try:
        dirs.extend(sorted(d for d in Path.cwd().iterdir() if d.is_dir()))
    except OSError:
        pass
    for directory in dirs:
        try:
            files = sorted(directory.glob("*.db")) if directory.is_dir() else []
        except OSError:
            files = []
        for f in files:
            consider(f)
    return candidates


class DoctorService:
    def __init__(self, cfgsvc: ConfigService, engine_factory) -> None:
        self.cfgsvc = cfgsvc
        self._engine = engine_factory

    # ------------------------------------------------------------------
    # Reference collection / resolution
    # ------------------------------------------------------------------
    @staticmethod
    def _collect_refs(cfg) -> list[tuple[str, str]]:
        """All ``(label, sec_id)`` references configured in config.yaml."""
        refs: list[tuple[str, str]] = []
        if cfg.telegram.bot_token_ref:
            refs.append(("telegram bot token", cfg.telegram.bot_token_ref))
        for p in cfg.providers:
            if p.api_key_ref:
                refs.append((f"provider {p.id} api key", p.api_key_ref))
        if cfg.websearch.enabled and cfg.websearch.api_key_ref:
            refs.append(("websearch api key", cfg.websearch.api_key_ref))
        return refs

    def _resolve_refs(self, cfg) -> list[tuple[str, str, str]]:
        """Try resolving every reference; returns ``(label, ref, status)``.

        status is "ok" or the exception type name (e.g.
        "SecretNotFoundError") — never the secret value itself.
        """
        import zero.domain.secrets as secrets_domain

        results: list[tuple[str, str, str]] = []
        _settings, services = self._engine()
        projects = services.identity.list_projects()
        project = next(
            (
                p
                for p in projects
                if p.id.value == (cfg.owner_project_id or "")
            ),
            None,
        ) or next(
            (p for p in projects if p.name == "Zero Management"), None
        ) or (projects[0] if projects else None)
        if project is None:
            return [
                (label, ref, "NoManagementProject")
                for label, ref in self._collect_refs(cfg)
            ]
        for label, ref in self._collect_refs(cfg):
            try:
                services.secrets.resolve_value(
                    project_id=project.id,
                    secret_id=secrets_domain.SecretReferenceId(ref),
                    actor_id=project.owner_user_id,
                    source="system",
                )
                results.append((label, ref, "ok"))
            except Exception as exc:  # noqa: BLE001 - report, never crash
                results.append((label, ref, type(exc).__name__))
        return results

    # ------------------------------------------------------------------
    # Drift scan + repair
    # ------------------------------------------------------------------
    @staticmethod
    def _refs_in_database(db_path: Path, ref_ids: list[str]) -> set[str]:
        """The subset of ``ref_ids`` present in this SQLite database."""
        found: set[str] = set()
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                rows = conn.execute(
                    f"SELECT id FROM secret_references "
                    f"WHERE id IN ({', '.join('?' for _ in ref_ids)})",
                    ref_ids,
                ).fetchall()
            finally:
                conn.close()
            found = {r[0] for r in rows}
        except (sqlite3.Error, OSError):
            return set()
        return found

    def _scan_drift(self, ref_ids: list[str]) -> dict[str, Any]:
        """Locate which candidate database actually holds the secrets."""
        from zero.manage.core.env_file import absolutize_sqlite_url, home_dotenv_path

        report: dict[str, Any] = {"scan": []}
        try:
            settings, _services = self._engine()
            resolved_url = str(settings.database_url)
        except Exception:  # noqa: BLE001 - diagnostics must survive
            resolved_url = "sqlite:///./zero_develop.db"
        resolved_abs = absolutize_sqlite_url(resolved_url, Path.cwd())
        report["resolved_url"] = resolved_abs

        home = home_dotenv_path().parent
        hits: dict[str, set[str]] = {}
        for candidate in _candidate_databases(home):
            current = str(candidate) in resolved_abs
            found = self._refs_in_database(candidate, ref_ids)
            report["scan"].append(
                {"path": str(candidate), "current": current, "holds": sorted(found)}
            )
            if found:
                hits[str(candidate)] = found
        report["hits"] = hits
        complete = [p for p, f in hits.items() if set(ref_ids) <= f]
        report["complete_match"] = complete[0] if len(complete) == 1 else None
        report["ambiguous"] = len(complete) > 1
        return report

    def _apply_drift_repair(self, target_db: str) -> dict[str, Any]:
        """Pin ``target_db`` as the engine database (with .env backup)."""
        from zero.manage.core.env_file import (
            home_dotenv_path,
            read_dotenv,
            upsert_dotenv,
        )

        env_path = home_dotenv_path()
        backup = None
        if env_path.is_file():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = env_path.with_name(f".env.bak-{stamp}")
            shutil.copyfile(env_path, backup)
        updates = {"ZERO_DATABASE_URL": f"sqlite:///{target_db}"}
        if not read_dotenv(env_path).get("ZERO_ENV") and not os.environ.get("ZERO_ENV"):
            updates["ZERO_ENV"] = "development"
        changed = upsert_dotenv(env_path, updates)
        return {"changed": changed, "backup": str(backup) if backup else None}

    # ------------------------------------------------------------------
    # Main entry
    # ------------------------------------------------------------------
    def run(self) -> dict[str, Any]:
        checks: list[dict[str, str]] = []
        extras: dict[str, Any] = {}
        add = lambda name, ok, detail, warn=False: checks.append(
            {
                "name": name,
                "status": ("ok" if ok else ("warn" if warn else "fail")),
                "detail": detail,
            }
        )

        # version / runtime
        from zero import __version__

        add(
            "version",
            True,
            f"zero {__version__} on python {os.sys.version_info.major}.{os.sys.version_info.minor}",
        )
        add(
            "git",
            bool(shutil.which("git")),
            shutil.which("git") or "not found",
            warn=not shutil.which("git"),
        )

        # config
        cfg_ok = self.cfgsvc.exists()
        cfg = None
        if cfg_ok:
            try:
                cfg = self.cfgsvc.load()
                add("config", True, str(self.cfgsvc.path))
            except ConfigError as exc:
                add("config", False, str(exc))
            except Exception as exc:  # noqa: BLE001 - audit fix: corrupted
                # YAML used to raise raw parser errors here, crashing
                # `zero doctor` on the most common breakage it exists
                # to report.
                add("config", False, f"unreadable ({type(exc).__name__})")
        else:
            add("config", False, "not initialized — run: zero setup")

        # encryption key material (engine settings / secret.key / .env / env)
        key_locations: list[str] = []
        try:
            engine_settings, _svc = self._engine()
            if engine_settings is not None and getattr(
                engine_settings, "secret_key", None
            ) is not None:
                key_locations.append("engine settings")
        except Exception:  # noqa: BLE001 - diagnostics must survive
            pass
        if os.environ.get("ZERO_SECRET_KEY"):
            key_locations.append("process environment")
        env_file = self.cfgsvc.home / ".env"
        if env_file.is_file():
            from zero.manage.core.env_file import read_dotenv

            if read_dotenv(env_file).get("ZERO_SECRET_KEY"):
                key_locations.append(str(env_file))
        key_file = self.cfgsvc.home / "secret.key"
        if key_file.is_file() and key_file.read_text(encoding="utf-8").strip():
            key_locations.append(str(key_file))
        add(
            "secret-key",
            bool(key_locations),
            "available via: " + ", ".join(key_locations)
            if key_locations
            else "missing — secret resolution would fail; run 'zero setup' "
            "or 'zero-develop serve' once to bootstrap it",
            warn=False,
        )

        # database (engine env)
        db_detail = "skipped"
        try:
            settings, services = self._engine()
            url = str(settings.database_url)
            applied = (
                services.database.connect()
                .execute("SELECT COUNT(*) FROM schema_migrations")
                .fetchone()[0]
                if not settings.is_test
                else -1
            )
            db_ok = applied in (-1,) or applied >= 29
            db_detail = f"{url} migrations={applied}"
            add("database", db_ok, db_detail)
            if not settings.is_test:
                fk = services.database.connect().execute("PRAGMA foreign_key_check").fetchall()
                add("db-integrity", not fk, f"{len(fk)} violations")
        except Exception as exc:  # noqa: BLE001 - diagnostics must survive
            add("database", False, type(exc).__name__)

        # secret references resolve inside the engine database?
        drift_report: dict[str, Any] | None = None
        if cfg is not None:
            refs = self._collect_refs(cfg)
            if not refs:
                add(
                    "secret-references",
                    True,
                    "no secret references configured",
                    warn=True,
                )
            else:
                resolved = self._resolve_refs(cfg)
                failed = [(l, r, s) for l, r, s in resolved if s != "ok"]
                if not failed:
                    add(
                        "secret-references",
                        True,
                        f"all {len(resolved)} reference(s) resolve",
                    )
                else:
                    names = ", ".join(f"{l} ({s})" for l, _r, s in failed)
                    add(
                        "secret-references",
                        False,
                        f"{len(failed)}/{len(resolved)} failed: {names} — "
                        "run 'zero doctor --fix' to locate and pin the "
                        "database that holds them",
                    )
                    drift_report = self._scan_drift([r for _l, r, _s in resolved])
                    extras["database_drift"] = drift_report
                    scan = drift_report.get("scan") or []
                    scan_lines = [
                        f"{entry['path']} (current={entry['current']}, "
                        f"holds {len(entry['holds'])}/{len(refs)} refs)"
                        for entry in scan
                    ]
                    add(
                        "database-drift",
                        False,
                        "; ".join(scan_lines) or "no candidate databases found",
                    )

        # telegram probe using stored token reference
        tg_ok, tg_detail = False, "no bot configured"
        if cfg is not None and cfg.telegram.bot_token_ref:
            try:
                _settings, services = self._engine()
                project = (
                    next(
                        (
                            p
                            for p in services.identity.list_projects()
                            if p.id.value == (cfg.owner_project_id or "")
                        ),
                        None,
                    )
                    or (services.identity.list_projects() or [None])[0]
                )
                token = services.secrets.resolve_value(
                    project_id=project.id,
                    secret_id=_ref_cls()(cfg.telegram.bot_token_ref),
                    actor_id=project.owner_user_id,
                )
                res = probes.telegram_get_me(token)
                tg_ok = bool(res.get("ok"))
                tg_detail = f"bot @{res.get('username')}" if tg_ok else str(res.get("error"))
            except Exception as exc:  # noqa: BLE001
                tg_detail = (
                    f"{type(exc).__name__} — the bot cannot poll until the "
                    "secret resolves (see secret-references check)"
                )
        add("telegram", tg_ok, tg_detail)

        # provider reachability (base host DNS/TCP only — no auth call)
        if cfg is not None:
            from urllib.parse import urlparse

            for p in cfg.providers:
                parsed = urlparse(p.base_url or "")
                host = parsed.hostname or ""
                if not host:
                    add(f"provider:{p.id}", False, "provider has no usable base_url")
                    continue
                # Honor the URL's scheme and port: a self-hosted gateway on
                # a custom port (or plain http) is reachable — the old probe
                # hardcoded 443 and failed every such deployment.
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
                try:
                    with socket.create_connection((host, port), timeout=3):
                        reachable = True
                    detail = f"{host}:{port} reachable ({parsed.scheme})"
                except OSError as exc:
                    reachable = False
                    detail = f"{host}:{port}: {exc.__class__.__name__}"
                add(f"provider:{p.id}", reachable, detail)

        # websearch consistency
        if cfg is not None:
            ws = cfg.websearch
            add(
                "websearch",
                (not ws.enabled) or bool(ws.provider_id),
                "enabled" if ws.enabled else "disabled",
            )

        # disk
        free = _disk_free_gb(Path.cwd())
        if free is not None:
            add("disk", free > 1.0, f"{free:.2f} GB free", warn=free < 5.0)

        return {
            "checks": checks,
            "summary": {
                "total": len(checks),
                "fail": sum(1 for c in checks if c["status"] == "fail"),
                "warn": sum(1 for c in checks if c["status"] == "warn"),
            },
            **extras,
        }

    def fix(self) -> dict[str, Any]:
        """Apply safe automated remediation; returns a report dict.

        Today's automatable repair (the one that resurrected dead bots):
        when the configured secret references live in exactly one
        candidate database that is NOT the database the engine resolves,
        pin that database into ``$ZERO_HOME/.env`` and re-verify.
        """
        result: dict[str, Any] = {"fixed": [], "recheck": None}
        cfg = None
        if self.cfgsvc.exists():
            try:
                cfg = self.cfgsvc.load()
            except Exception:  # noqa: BLE001 - nothing to repair then
                return result
        if cfg is None:
            return result
        refs = self._collect_refs(cfg)
        if not refs:
            return result
        resolved = self._resolve_refs(cfg)
        if all(status == "ok" for _l, _r, status in resolved):
            result["fixed"].append("secret-references already resolve — nothing to fix")
            return result
        drift = self._scan_drift([r for _l, r, _s in resolved])
        target = drift.get("complete_match")
        if not target:
            result["fixed"].append(
                "no single database contains all configured secrets — "
                "automatic repair is not possible; re-run 'zero setup' "
                "(it re-stores your credentials), or export "
                "ZERO_TELEGRAM_BOT_TOKEN / ZERO_OPENAI_API_KEY and run "
                "'zero restart' (config sync will store them for you)"
            )
            return result
        if drift.get("ambiguous"):
            result["fixed"].append(
                "multiple databases contain all secrets — refusing to "
                "guess; delete or rename the stale ones and re-run "
                "'zero doctor --fix'"
            )
            return result
        repair = self._apply_drift_repair(target)
        result["fixed"].append(
            f"pinned engine database to {target} in $ZERO_HOME/.env "
            f"(backup: {repair['backup'] or 'none needed'}; "
            f"updated: {', '.join(repair['changed'])})"
        )
        # Re-verify through a FRESH engine (new settings load).
        resolved2 = self._resolve_refs(cfg)
        ok = all(status == "ok" for _l, _r, status in resolved2)
        result["recheck"] = {
            "ok": ok,
            "details": [(l, s) for l, _r, s in resolved2],
        }
        if ok:
            result["fixed"].append(
                "verified: every secret reference now resolves — run "
                "'zero restart' to bring the bot back"
            )
        else:
            result["fixed"].append(
                "warning: references still do not resolve after pinning — "
                "check the encryption key ('secret-key' check) and the "
                "zero.log output"
            )
        return result


def _ref_cls():
    import zero.domain.secrets as s

    return s.SecretReferenceId


def _disk_free_gb(path: Path) -> float | None:
    try:
        import shutil

        _t, _u, free = shutil.disk_usage(path)
        return free / 1024**3
    except OSError:
        return None
