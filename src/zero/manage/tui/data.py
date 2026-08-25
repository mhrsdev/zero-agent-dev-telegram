"""TUI data layer: pure functions gathering screen payloads.

No Textual imports here so everything is unit-testable headlessly.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from zero.manage.core.config import ConfigService, zero_home

# Private alias keeping the module-local call sites short; the canonical
# $ZERO_HOME resolver lives in manage.core.config.
_home = zero_home


def _cfgsvc() -> ConfigService:
    return ConfigService(_home())


def overview() -> dict[str, Any]:
    cfgsvc = _cfgsvc()
    cfg = cfgsvc.load() if cfgsvc.exists() else None
    out: dict[str, Any] = {
        "config_path": str(cfgsvc.path),
        "initialized": cfg is not None,
        "environment": cfg.server.environment if cfg else "-",
        "telegram": {"mode": "-", "bot": "-", "token": "no"},
        "access": {"mode": "-", "groups": 0},
        "providers": [],
        "routing": {},
        "backups": backups_screen(),
    }
    if cfg is not None:
        out["telegram"] = {
            "mode": cfg.telegram.mode,
            "bot": cfg.telegram.bot_username or "-",
            "token": "yes" if cfg.telegram.bot_token_ref else "no",
        }
        out["access"] = {"mode": cfg.access.mode, "groups": len(cfg.access.groups)}
        out["providers"] = [
            {
                "id": p.id,
                "protocol": p.protocol,
                "enabled": p.enabled,
                "models": list(p.models),
                "priority": p.fallback_priority,
            }
            for p in cfg.providers
        ]
        out["routing"] = {
            "primary": cfg.routing.primary_model or "-",
            "fallbacks": list(cfg.routing.fallback_models),
        }
        out["environment"] = cfg.server.environment
    return out


def telegram_screen() -> dict[str, Any]:
    o = overview()
    events_tail: list[str] = []
    return {"telegram": o["telegram"], "events": events_tail[-20:]}


def groups_screen() -> list[dict[str, Any]]:
    cfgsvc = _cfgsvc()
    cfg = cfgsvc.load() if cfgsvc.exists() else None
    if cfg is None:
        return []
    return [g.model_dump() for g in cfg.access.groups]


def providers_screen() -> list[dict[str, Any]]:
    cfgsvc = _cfgsvc()
    cfg = cfgsvc.load() if cfgsvc.exists() else None
    caps_file = _home() / "capabilities.json"
    caps: dict[str, Any] = {}
    try:
        caps = json.loads(caps_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    rows = []
    for p in cfg.providers if cfg else []:
        probe = next((v for v in caps.values() if v.get("provider_id") == p.id), None)
        rows.append(
            {
                "id": p.id,
                "protocol": p.protocol,
                "enabled": p.enabled,
                "models": len(p.models),
                "priority": p.fallback_priority,
                "tool_calls": (probe or {}).get("tool_calls", "unknown"),
                "streaming": (probe or {}).get("streaming", "unknown"),
            }
        )
    return rows


def usage_screen(days: int = 7) -> list[dict[str, Any]]:
    db = _engine_db_path()
    if not db:
        return []
    try:
        conn = __import__("sqlite3").connect(f"file:{db}?mode=ro", uri=True)
        since = time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime(time.time() - days * 86400))
        rows = conn.execute(
            "SELECT substr(created_at,1,10) day, provider, model,"
            " COUNT(*) requests, SUM(input_tokens) it, SUM(output_tokens) ot,"
            " SUM(CAST(estimated_cost_usd AS REAL)) cost"
            " FROM provider_usage WHERE created_at >= ?"
            " GROUP BY day, provider, model ORDER BY day DESC LIMIT 100",
            (since,),
        ).fetchall()
        return [
            {
                "day": r[0],
                "provider": r[1],
                "model": r[2],
                "requests": r[3],
                "input_tokens": r[4],
                "output_tokens": r[5],
                "cost": round(r[6], 4),
            }
            for r in rows
        ]
    except Exception:  # noqa: BLE001 - missing table/db renders empty
        return []


def system_screen() -> dict[str, Any]:
    free = None
    cwd = Path.cwd()
    try:
        _t, _u, fr = shutil.disk_usage(cwd)
        free = fr / 1024**3
    except OSError:
        pass
    svc = {"kind": "systemd"} if shutil.which("systemctl") else {"kind": "process"}
    return {
        "python": f"{os.sys.version_info.major}.{os.sys.version_info.minor}",
        "disk_free_gb": None if free is None else round(free, 2),
        "config_home": str(_home()),
        "service_kind": svc["kind"],
    }


def backups_screen() -> dict[str, Any]:
    home = _home()
    bdir = home / "backups"
    archives = [
        {
            "name": f.name,
            "size": f.stat().st_size,
            "age_h": round((time.time() - f.stat().st_mtime) / 3600, 1),
        }
        for f in sorted(bdir.glob("zero-backup-*"), key=lambda x: x.stat().st_mtime, reverse=True)
    ]
    last = None
    sp = bdir / "last-backup.json"
    if sp.exists():
        try:
            last = json.loads(sp.read_text(encoding="utf-8"))
        except ValueError:
            last = None
    schedule = "-"
    svc = _cfgsvc()
    cfg = svc.load() if svc.exists() else None
    if cfg is not None:
        schedule = cfg.backups.schedule
    return {"schedule": schedule, "archives": archives, "last": last}


def diagnostics_screen() -> dict[str, Any]:
    from zero.manage.services.doctor import DoctorService

    def engine():
        from zero.app.services import build_services
        from zero.config import Settings
        from zero.persistence.connection import Database
        from zero.persistence.migrations import apply_migrations

        s = Settings.load()
        d = Database(s)
        apply_migrations(d)
        return build_services(s, d)

    report = DoctorService(_cfgsvc(), engine).run()
    return report


def _engine_db_path() -> Path | None:
    url = os.environ.get("ZERO_DATABASE_URL", "")
    if url.startswith("sqlite:///"):
        p = Path(url[len("sqlite:///") :])
        return p if p.exists() else None
    dev = Path("zero_develop.db")
    return dev if dev.exists() else None
