"""Diagnostics service powering `zero doctor`."""

from __future__ import annotations

import os
import shutil
import socket
from pathlib import Path
from typing import Any

from zero.manage.core import probes
from zero.manage.core.config import ConfigError, ConfigService


class DoctorService:
    def __init__(self, cfgsvc: ConfigService, engine_factory) -> None:
        self.cfgsvc = cfgsvc
        self._engine = engine_factory

    def run(self) -> dict[str, Any]:
        checks: list[dict[str, str]] = []
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
        cfg_err = ""
        cfg = None
        if cfg_ok:
            try:
                cfg = self.cfgsvc.load()
                add("config", True, str(self.cfgsvc.path))
            except ConfigError as exc:
                cfg_err = str(exc)
                add("config", False, cfg_err)
            except Exception as exc:  # noqa: BLE001 - audit fix: corrupted
                # YAML used to raise raw parser errors here, crashing
                # `zero doctor` on the most common breakage it exists
                # to report.
                cfg_err = f"unreadable ({type(exc).__name__})"
                add("config", False, cfg_err)
        else:
            add("config", False, "not initialized — run: zero setup")

        # database (engine env)
        db_detail = "skipped"
        db_ok = True
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
            db_detail = f"{url.split('///')[-1]} migrations={applied}"
            add("database", db_ok, db_detail)
            if not settings.is_test:
                fk = services.database.connect().execute("PRAGMA foreign_key_check").fetchall()
                add("db-integrity", not fk, f"{len(fk)} violations")
        except Exception as exc:  # noqa: BLE001 - diagnostics must survive
            add("database", False, type(exc).__name__)
        del db_ok

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
                tg_detail = type(exc).__name__
        add("telegram", tg_ok, tg_detail)

        # provider reachability (base host DNS/TCP only — no auth call)
        if cfg is not None:
            for p in cfg.providers:
                host = (p.base_url.split("//")[-1]).split("/")[0]
                port = 443
                try:
                    with socket.create_connection((host, port), timeout=3):
                        reachable = True
                    detail = f"{host}:{port} reachable"
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
        }


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
