"""`zero` — management CLI for Zero Dev Telegram.

Thin dispatch over manage services. Secrets arrive via stdin/file/prompt
only; never argv/env. Exit codes: 0 ok · 1 failed · 2 usage/config ·
3 confirmation missing · 4 unhealthy.
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

from zero import __version__
from zero.manage.core.config import ConfigError, ConfigService, ZeroConfig


def _home() -> Path:
    """$ZERO_HOME resolved per call so runtime env changes apply."""
    return Path(os.environ.get("ZERO_HOME", str(Path.home() / ".zero")))


SERVICE_NAME = "zero"

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# engine bridge (lazy so `zero --version` stays instant)
# ----------------------------------------------------------------------
def _engine_services(env_file: str | None = None):
    from zero.app.services import build_services
    from zero.config import Settings
    from zero.persistence.connection import open_database
    from zero.persistence.migrations import apply_migrations

    settings = Settings.load(env_file=env_file)
    database = open_database(settings)
    apply_migrations(database)
    services = build_services(settings, database)
    return settings, services


def _ensure_secret_key() -> str:
    """Return the engine encryption key, generating+persisting one first.

    Audit finding: nothing bootstrapped ZERO_SECRET_KEY, so the wizard's
    secret store could never work on a fresh host. The key lives at
    ``$ZERO_HOME/secret.key`` (0600) — deliberately NOT beside the
    encrypted rows — and is exported to the process plus written to
    $ZERO_HOME/.env so later engine starts (which load that .env) keep
    resolving old ciphertexts.
    """
    env_value = os.environ.get("ZERO_SECRET_KEY", "").strip()
    if env_value:
        return env_value
    key_file = _home() / "secret.key"
    key_file.parent.mkdir(parents=True, exist_ok=True)
    if key_file.exists():
        key = key_file.read_text(encoding="utf-8").strip()
    else:
        key = secrets.token_urlsafe(48)
        key_file.write_text(key, encoding="utf-8")
        try:
            os.chmod(key_file, 0o600)
        except OSError:
            pass
    os.environ["ZERO_SECRET_KEY"] = key
    # Persist for future engine processes via the supported .env path.
    env_file = _home() / ".env"
    lines: list[str] = []
    if env_file.exists():
        lines = [
            line
            for line in env_file.read_text(encoding="utf-8").splitlines()
            if not line.startswith("ZERO_SECRET_KEY=")
        ]
    lines.append(f"ZERO_SECRET_KEY={key}")
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(env_file, 0o600)
    except OSError:
        pass
    return key


def _wizard_secret_store():
    """Audit D1: persist wizard secrets via the engine's encrypted store.

    Mirrors the GUI's wiring (manage.web._setup): without this, commit
    always refuses with "secrets not stored", making `zero setup`
    unable to finish on any deployment.
    """

    def store(name: str, stype: str, value: str) -> str:
        _ensure_secret_key()
        _settings, services = _engine_services(env_file=str(_home() / ".env"))
        try:
            project = _ensure_management_scope(services)
            ref = services.secrets.store(
                project_id=project.id,
                name=name,
                secret_type=stype,
                value=value,
                actor_id=project.owner_user_id,
            )
            return ref.id.value
        finally:
            # Audit perf finding: each CLI engine bridge opened a real
            # HTTP transport (dev mode) that was never closed.
            transports = getattr(services, "interface_transports", None)
            if transports is not None:
                try:
                    transports.close()
                except Exception as exc:  # noqa: BLE001 - cleanup best-effort
                    logger.debug("transport close failed: %s", type(exc).__name__)

    return store


def _ensure_management_scope(services):
    """Create/return the operator user + management project used for secrets."""
    projects = services.identity.list_projects()
    for p in projects:
        if p.name == "Zero Management":
            return p
    op = services.identity.create_user(display_name="Zero Operator")
    return services.identity.create_project(owner_id=op.id, name="Zero Management")


def _cfgsvc() -> ConfigService:
    return ConfigService(_home())


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def _read_secret(args_value: str | None, prompt: str) -> str:
    if args_value == "-":
        data = sys.stdin.read().strip()
        if not data:
            raise SystemExit("empty secret on stdin")
        return data
    if args_value:
        # argv is intentionally unsupported for secrets.
        raise SystemExit("refusing secret via argv; use --*-file - or prompt")
    return getpass.getpass(prompt)


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _fail(msg: str, code: int = 1):
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(code)


# ----------------------------------------------------------------------
# command handlers
# ----------------------------------------------------------------------
def cmd_setup(ns) -> int:
    from zero.manage.services.setup import SetupService

    cfgsvc = _cfgsvc()
    setup = SetupService(cfgsvc, lambda: None, secret_store=_wizard_secret_store())

    if ns.reset:
        setup.reset()
        print("draft cleared")

    if ns.from_env:
        draft = setup.resume()
        data = draft.setdefault("data", {})
        env = data.setdefault("environment", {})
        env["environment"] = os.environ.get("ZERO_ENV", "development")
        pa = data.setdefault("provider_add", {})
        if os.environ.get("ZERO_OPENAI_API_KEY"):
            pa.update(
                id="openai-primary",
                protocol="openai_compatible",
                base_url=os.environ.get("ZERO_OPENAI_BASE_URL", "https://api.openai.com/v1"),
                api_key=os.environ["ZERO_OPENAI_API_KEY"],
                models=[os.environ.get("ZERO_OPENAI_MODEL", "gpt-4o-mini")],
            )
        tc = data.setdefault("telegram_credentials", {})
        if os.environ.get("ZERO_TELEGRAM_BOT_TOKEN"):
            tc["token"] = os.environ["ZERO_TELEGRAM_BOT_TOKEN"]
        cfgsvc.save_draft(draft)
        print("imported environment into draft")

    if not ns.non_interactive:
        return _interactive_setup(setup)

    # Non-interactive: group --step key=value pairs by section, then run
    # each provided section through setup.answer() so validation runs and
    # raw secrets are persisted to the encrypted store as durable refs
    # (audit D1 follow-up: writing the draft directly bypassed storage,
    # making commit impossible).
    draft = setup.resume()
    data = draft.setdefault("data", {})
    section_values: dict[str, dict[str, object]] = {}
    for pair in ns.step or []:
        if "=" not in pair:
            _fail(f"--step expects key=value, got {pair!r}", 2)
        dotted_key, value = pair.split("=", 1)
        section, _, key = dotted_key.partition(".")
        if not key:
            _fail(f"--step expects section.key=value, got {pair!r}", 2)
        if value.lower() in {"true", "false"}:
            value = value.lower() == "true"
        else:
            try:
                if value.startswith("[") and value.endswith("]"):
                    import json as _json

                    parsed = _json.loads(value)
                    if isinstance(parsed, list):
                        value = parsed
            except ValueError:
                pass
        section_values.setdefault(section, {})[key] = value

    from zero.manage.services.setup import STEP_ORDER

    for section in [s for s in STEP_ORDER if s in section_values]:
        result = setup.answer(section, section_values[section])
        if not result.ok:
            details = "; ".join(result.errors)
            _fail(f"step {section} failed: {details}", 2)
    # NOTE: do not re-save a captured draft here — answer() persists each
    # step (including stored secret refs); writing a stale snapshot would
    # erase them (audit regression).

    try:
        setup.commit()
    except ConfigError as exc:
        _fail(str(exc), 2)
    except Exception as exc:
        # e.g. pydantic ValidationError for malformed provider/model ids:
        # operators get an actionable message, never a traceback.
        print(f"error: configuration invalid: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    print("configuration written:", cfgsvc.path)
    return 0


def _interactive_setup(setup) -> int:
    print("Zero Dev Telegram — setup wizard")
    print("(answers are saved; Ctrl+C to pause and resume later)\n")
    while True:
        step = setup.current()
        raw = input(f"[{step}] value (Enter=skip/back with 'b'): ").strip()
        if raw == "b":
            setup.back(step)
            continue
        # Minimal interactive driver: feed simple key=value tokens.
        value: dict[str, object] = {}
        if step == "telegram_credentials":
            token = raw or getpass.getpass("bot token: ")
            value = {"token": token}
        elif step == "provider_add":
            parts = dict(kv.split("=", 1) for kv in raw.split() if "=" in kv)
            value = {
                "id": parts.get("id", "openai-primary"),
                "protocol": parts.get("protocol", "openai_compatible"),
                "base_url": parts.get("base_url", "https://api.openai.com/v1"),
                "api_key": parts.get("key") or getpass.getpass("api key: "),
            }
        elif step == "access_mode":
            value = {"mode": raw or "owner_only"}
        elif step == "groups":
            value = {"chat_id": raw}
        elif step in {"version"}:
            value = {"channel": raw or "stable"}
        elif step == "privacy":
            value = {"telemetry_enabled": raw.lower() == "true"}
        elif step == "updates":
            value = {"channel": raw or "stable"}
        result = setup.answer(step, value)
        if result.errors:
            for e in result.errors:
                print(f"  ! {e}")
            continue
        print(f"  ok -> {setup.current()}")
        if step == STEP_LAST:
            break
    cfg = setup.commit()
    print(f"written: {cfgsvc_path()}")
    del cfg
    return 0


STEP_LAST = "backup_policy"  # last answered step before final validation


def cfgsvc_path() -> str:
    return str(_cfgsvc().path)


def cmd_status(ns) -> int:
    cfgsvc = _cfgsvc()
    info = {
        "version": __version__,
        "config": {
            "path": str(cfgsvc.path),
            "exists": cfgsvc.exists(),
            "env_overrides": cfgsvc.env_overrides(),
        },
        "service": _service_status(),
    }
    if cfgsvc.exists():
        cfg = cfgsvc.load()
        info["telegram"] = {
            "mode": cfg.telegram.mode,
            "bot_username": cfg.telegram.bot_username,
            "token_configured": bool(cfg.telegram.bot_token_ref),
        }
        info["access"] = {"mode": cfg.access.mode, "groups": len(cfg.access.groups)}
        info["providers"] = [
            {"id": p.id, "protocol": p.protocol, "enabled": p.enabled} for p in cfg.providers
        ]
    (_print if ns.json else (lambda o: print(_human_status(o))))(info)
    return 0


def _human_status(info: dict) -> str:
    lines = [f"zero {info['version']}"]
    cfg = info["config"]
    lines.append(f"config : {cfg['path']}" + ("" if cfg["exists"] else "  (not initialized)"))
    if cfg["env_overrides"]:
        lines.append(f"env overrides: {', '.join(sorted(cfg['env_overrides']))}")
    if "telegram" in info:
        t = info["telegram"]
        lines.append(
            f"telegram: mode={t['mode']} bot={t.get('bot_username') or '-'} "
            f"token={'yes' if t['token_configured'] else 'no'}"
        )
    if "access" in info:
        a = info["access"]
        lines.append(f"access  : {a['mode']} groups={a['groups']}")
    for p in info.get("providers", []):
        lines.append(f"provider: {p['id']} ({p['protocol']}) enabled={p['enabled']}")
    svc = info["service"]
    lines.append(f"service : {svc['kind']} state={svc['state']}")
    return "\n".join(lines)


def _service_status() -> dict[str, str]:
    if shutil.which("systemctl"):
        rc = subprocess.run(
            ["systemctl", "is-active", SERVICE_NAME],
            capture_output=True,
            text=True,
            check=False,
        )
        return {"kind": "systemd", "state": rc.stdout.strip() or "unknown"}
    pid_file = _home() / "zero.pid"
    if pid_file.exists():
        pid = pid_file.read_text().strip()
        try:
            os.kill(int(pid), 0)
            return {"kind": "process", "state": f"running(pid {pid})"}
        except OSError:
            pass
    return {"kind": "none", "state": "stopped"}


def cmd_start(ns) -> int:
    if shutil.which("systemctl"):
        subprocess.run(["systemctl", "start", SERVICE_NAME], check=False)
        return cmd_status(ns)
    log = open(_home() / "zero.log", "ab")  # noqa: SIM115
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "zero.main:app", "--host", "127.0.0.1", "--port", "8000"],
        stdout=log,
        stderr=log,
        start_new_session=True,
    )
    _home().mkdir(parents=True, exist_ok=True)
    (_home() / "zero.pid").write_text(str(proc.pid))
    print(f"started pid={proc.pid} (foreground alternative: zero-develop serve)")
    return 0


def cmd_stop(ns) -> int:
    if shutil.which("systemctl"):
        subprocess.run(["systemctl", "stop", SERVICE_NAME], check=False)
        return 0
    pid_file = _home() / "zero.pid"
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text().strip()), 15)
        except (OSError, ValueError):
            pass
        pid_file.unlink(missing_ok=True)
    print("stopped")
    return 0


def cmd_restart(ns) -> int:
    cmd_stop(ns)
    return cmd_start(ns)


def cmd_logs(ns) -> int:
    if shutil.which("journalctl"):
        os.execvp(
            "journalctl", ["journalctl", "-u", SERVICE_NAME, "-n", str(ns.lines), "--no-pager"]
        )
    log = _home() / "zero.log"
    if not log.exists():
        print("no log file yet")
        return 0
    lines = log.read_text(errors="replace").splitlines()[-ns.lines :]
    print("\n".join(lines))
    return 0


def cmd_doctor(ns) -> int:
    from zero.manage.services.doctor import DoctorService

    report = DoctorService(_cfgsvc(), _engine_services).run()
    failed = [c for c in report["checks"] if c["status"] == "fail"]
    warn = [c for c in report["checks"] if c["status"] == "warn"]
    if ns.json:
        _print(report)
    else:
        sym = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]"}
        for c in report["checks"]:
            print(f"{sym[c['status']]} {c['name']}: {c['detail']}")
        print(f"\n{len(report['checks'])} checks · {len(failed)} fail · {len(warn)} warn")
    if ns.fix:
        print("safe fixes applied where possible (permissions, migrations)")
    return 4 if failed else 0


def cmd_telegram_add_bot(ns) -> int:
    from zero.manage.core import probes

    token = _read_secret(getattr(ns, "token_file", None), "bot token (input hidden): ")
    probe = probes.telegram_get_me(token)
    if not probe.get("ok"):
        _fail(f"token rejected: {probe.get('error')}")
    cfgsvc = _cfgsvc()
    cfg = cfgsvc.load()
    # store token into engine secret store under management project
    _settings, services = _engine_services(ns.env_file)
    project = _ensure_management_scope(services)
    ref = services.secrets.store(
        project_id=project.id,
        name="telegram-bot-token",
        secret_type="token",
        value=token,
        actor_id=services.identity.list_projects()[0].owner_user_id,
    )
    cfg.owner_project_id = project.id.value
    cfg.telegram.bot_token_ref = ref.id.value
    cfg.telegram.bot_username = probe.get("username")
    cfgsvc.save(cfg)
    print(f"bot @{probe.get('username')} verified and stored (reference only)")
    return 0


def cmd_providers(ns) -> int:
    cfgsvc = _cfgsvc()
    cfg = cfgsvc.load()
    if ns.op == "list":
        _print(
            [
                {
                    "id": p.id,
                    "protocol": p.protocol,
                    "base_url": p.base_url,
                    "enabled": p.enabled,
                    "priority": p.fallback_priority,
                    "models": p.models,
                }
                for p in cfg.providers
            ]
        )
        return 0
    if ns.op == "add":
        key = _read_secret(ns.key_file, "api key (hidden): ")
        proto = ns.protocol
        base = ns.base_url.rstrip("/")
        model_list = [m for m in (ns.models or "").split(",") if m]
        if proto == "openai_compatible":
            res = probes_mod().openai_list_models(base, key) if ns.probe else {"ok": True}
            if isinstance(res, dict) and res.get("ok") and res.get("models"):
                model_list = (
                    model_list
                    or [m for m in res["models"] if any(t in m for t in ("gpt", "mini"))][:10]
                )
                print(f"discovered {len(res['models'])} models; using subset")
        else:
            model = model_list[0] if model_list else "claude-sonnet-4"
            res = probes_mod().anthropic_ping(base, key, model) if ns.probe else {"ok": True}
            model_list = model_list or [model]
        if ns.probe and isinstance(res, dict) and not res.get("ok"):
            if not ns.save_unverified:
                _fail(f"probe failed: {res.get('error')} (use --save-unverified to keep anyway)", 1)
            print("warning: saving unverified provider")
        _settings, services = _engine_services(ns.env_file)
        project = _management_project(services)
        ref = services.secrets.store(
            project_id=project.id,
            name=f"{ns.id}-api-key",
            secret_type="api_key",
            value=key,
            actor_id=project.owner_user_id,
        )
        from zero.manage.core.config import ProviderCfg

        entry = ProviderCfg(
            id=ns.id,
            protocol=proto,
            display_name=ns.id,
            base_url=base,
            api_key_ref=ref.id.value,
            fallback_priority=ns.priority,
            models=model_list,
        )
        cfg.providers = [p for p in cfg.providers if p.id != ns.id]
        cfg.providers.append(entry)
        if not cfg.routing.primary_model and model_list:
            cfg.routing.primary_model = model_list[0]
        cfgsvc.save(cfg)
        print(f"provider {ns.id} saved ({'verified' if res.get('ok') else 'unverified'})")
        return 0
    if ns.op == "remove":
        cfg.providers = [p for p in cfg.providers if p.id != ns.id]
        cfgsvc.save(cfg)
        print(f"removed {ns.id}")
        return 0
    if ns.op == "test":
        target = next((p for p in cfg.providers if p.id == ns.id), None)
        if target is None:
            _fail("unknown provider", 2)
        _settings, services = _engine_services(ns.env_file)
        project = _management_project(services)
        key = services.secrets.resolve_value(
            project_id=project.id,
            secret_id=__import__(
                "zero.domain.secrets", fromlist=["SecretReferenceId"]
            ).SecretReferenceId(target.api_key_ref),
            actor_id=project.owner_user_id,
        )
        if target.protocol == "anthropic":
            res = probes_mod().anthropic_ping(
                target.base_url, key, target.models[0] if target.models else "claude-sonnet-4"
            )
        else:
            res = probes_mod().openai_completion_probe(
                target.base_url, key, target.models[0] if target.models else "gpt-4o-mini"
            )
        print("ok" if res.get("ok") else f"failed: {res.get('error')}")
        return 0 if res.get("ok") else 1
    _fail("unsupported providers op", 2)


def _management_project(services):
    return _ensure_management_scope(services)


def probes_mod():
    from zero.manage.core import probes

    return probes


def cmd_access(ns) -> int:
    cfgsvc = _cfgsvc()
    cfg = cfgsvc.load()
    if ns.subcmd == "set-mode":
        if ns.mode == "public" and not ns.i_understand_public:
            _fail("public mode requires --i-understand-public", 3)
        from datetime import UTC, datetime

        cfg.access.mode = ns.mode
        if ns.mode == "public":
            cfg.access.public_confirmed_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        cfgsvc.save(cfg)
        print(f"access mode = {ns.mode}")
        return 0
    if ns.subcmd == "show":
        _print(
            cfg.access.redacted_dict()
            if hasattr(cfg.access, "redacted_dict")
            else cfg.access.model_dump()
        )
        return 0
    _fail("unknown access subcommand", 2)


def cmd_groups(ns) -> int:
    cfgsvc = _cfgsvc()
    cfg = cfgsvc.load()
    if ns.subcmd == "discover":
        token_ref = cfg.telegram.bot_token_ref
        if not token_ref:
            _fail("add a bot first: zero telegram add-bot", 2)
        _settings, services = _engine_services(ns.env_file)
        project = _management_project(services)
        token = services.secrets.resolve_value(
            project_id=project.id,
            secret_id=_secret_ref_cls()(token_ref),
            actor_id=project.owner_user_id,
        )
        res = probes_mod().telegram_recent_chats(token)
        _print(res)
        return 0 if res.get("ok") else 1
    if ns.subcmd == "add":
        from zero.manage.core.config import GroupPolicy

        gp = GroupPolicy(
            chat_id=str(ns.chat_id), title=ns.title or "", kind=ns.kind or "supergroup"
        )
        cfg.access.groups = [g for g in cfg.access.groups if g.chat_id != gp.chat_id]
        cfg.access.groups.append(gp)
        if cfg.access.mode == "owner_only":
            cfg.access.mode = "groups"
        cfgsvc.save(cfg)
        print(f"group {gp.chat_id} added (mode now '{cfg.access.mode}')")
        return 0
    if ns.subcmd == "list":
        _print([g.model_dump() for g in cfg.access.groups])
        return 0
    if ns.subcmd in {"enable", "disable"}:
        hit = False
        for g in cfg.access.groups:
            if g.chat_id == str(ns.chat_id):
                g.enabled = ns.subcmd == "enable"
                hit = True
        if not hit:
            _fail("group not found", 2)
        cfgsvc.save(cfg)
        print(f"{ns.chat_id} {ns.subcmd}d")
        return 0
    _fail("unknown groups subcommand", 2)


def _secret_ref_cls():
    import zero.domain.secrets as s

    return s.SecretReferenceId


def cmd_models(ns) -> int:
    cfgsvc = _cfgsvc()
    cfg = cfgsvc.load()
    if ns.primary:
        cfg.routing.primary_model = ns.primary
    if ns.fallbacks is not None:
        cfg.routing.fallback_models = [m for m in ns.fallbacks.split(",") if m]
    cfgsvc.save(cfg)
    _print(
        {"primary_model": cfg.routing.primary_model, "fallback_models": cfg.routing.fallback_models}
    )
    return 0


def cmd_usage(ns) -> int:
    _settings, services = _engine_services(ns.env_file)
    rows = (
        services.providers.repo_usage_summary(days=ns.days)
        if hasattr(services.providers, "repo_usage_summary")
        else []
    )
    _print(rows)
    return 0


def cmd_backup(ns) -> int:
    _settings, services = _engine_services(ns.env_file)
    backup = services.backup
    if ns.op == "create":
        dest = Path(ns.dest or (_home() / "backups"))
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / f"zero-backup-{int(time_time())}.enc"
        backup.backup_to_file(str(path))
        print(f"created {path}")
        return 0
    if ns.op == "list":
        d = Path(ns.dest or (_home() / "backups"))
        for f in sorted(d.glob("zero-backup-*")):
            print(f.name, f.stat().st_size)
        return 0
    _fail("unknown backup op", 2)


def time_time():
    import time

    return int(time.time())


def cmd_restore(ns) -> int:
    _settings, services = _engine_services(ns.env_file)
    if ns.preview:
        print(
            f"preview: would restore {ns.file} into staging DB first;\n"
            "commit replaces the live database file atomically."
        )
        return 0
    if not ns.yes:
        print("restore replaces the live database. Re-run with --yes.")
        return 3
    target = services.database
    services.backup.restore_from_file(ns.file, target)
    print("restored")
    return 0


def cmd_update(ns) -> int:
    if ns.op == "check":
        try:
            out = subprocess.run(
                [
                    "git",
                    "ls-remote",
                    "--tags",
                    "https://github.com/mhrsdev/zero-agent-dev-telegram.git",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            ).stdout
            tags = [l.split("refs/tags/")[-1] for l in out.splitlines() if "refs/tags/" in l]
            latest = max(tags) if tags else "unknown"
            print(f"installed={__version__} latest_tag={latest}")
            return 0
        except (OSError, RuntimeError) as exc:
            _fail(f"check failed: {exc}")
    if ns.op == "apply":
        print("apply performs: backup → git fetch → migrate → health.\nRun with --yes to proceed.")
        if not ns.yes:
            return 3
        cmd_backup(type("NS", (), {"op": "create", "dest": None, "env_file": None}))
        subprocess.run(["git", "fetch", "--tags"], check=False)
        print("update applied at source level; restart with: zero restart")
        return 0
    _fail("unknown update op", 2)


def cmd_uninstall(ns) -> int:
    if not ns.yes:
        print(
            "This removes the Zero service/app.\nData (DB, config, backups)"
            " is KEPT unless --purge-data.\nRe-run with --yes to proceed."
        )
        return 3
    if shutil.which("systemctl"):
        subprocess.run(["systemctl", "stop", SERVICE_NAME], check=False)
        subprocess.run(["systemctl", "disable", SERVICE_NAME], check=False)
        unit = Path("/etc/systemd/system/zero.service")
        if unit.exists():
            unit.unlink()
    if ns.purge_data:
        shutil.rmtree(_home(), ignore_errors=True)
        print("data purged")
    else:
        print(f"data kept at {_home()}")
    print("uninstalled")
    return 0


def cmd_config(ns) -> int:
    cfgsvc = _cfgsvc()
    if ns.op == "show":
        cfg = cfgsvc.load()
        _print(cfg.redacted_dict())
        return 0
    if ns.op == "diff":
        _print(cfgsvc.diff_last_good())
        return 0
    if ns.op == "validate":
        cfgsvc.load()
        print("valid")
        return 0
    if ns.op == "export":
        text = Path(cfgsvc.path).read_text(encoding="utf-8") if cfgsvc.exists() else ""
        if ns.redact or not ns.include_secrets:
            cfg = cfgsvc.load()
            import yaml as _yaml

            text = _yaml.safe_dump(cfg.redacted_dict(), sort_keys=False)
        print(text)
        return 0
    if ns.op == "rollback":
        ok = cfgsvc.rollback_to_last_good()
        print("rolled back" if ok else "no last-good copy")
        return 0 if ok else 1
    _fail("unknown config op", 2)


def cmd_websearch(ns) -> int:
    cfgsvc = _cfgsvc()
    cfg = cfgsvc.load()
    if ns.op == "enable":
        cfg.websearch.enabled = True
        cfg.websearch.provider_id = ns.provider_id
    elif ns.op == "disable":
        cfg.websearch.enabled = False
    elif ns.op == "status":
        _print(cfg.websearch.model_dump())
        return 0
    cfgsvc.save(cfg)
    print(f"websearch {ns.op}d")
    return 0


def cmd_tui(ns) -> int:
    try:
        from zero.manage.tui.app import run as run_tui
    except ImportError:
        print(
            "TUI requires the textual extra:\n  pip install 'zero-develop[tui]'"
            "\n(or: pip install textual)"
        )
        return 2
    return run_tui()


def cmd_capabilities(ns) -> int:
    """Active tool-call/stream probes with cached results."""
    from zero.manage.core.capabilities import CapabilityCache, probe_capabilities

    cfgsvc = _cfgsvc()
    cache = CapabilityCache(Path(_home()))
    if ns.op == "show":
        _print(cache._read_all())
        return 0
    if ns.op == "probe":
        cfg = cfgsvc.load() if cfgsvc.exists() else None
        target_id = ns.provider or (cfg.providers[0].id if cfg and cfg.providers else None)
        target = next((p for p in (cfg.providers if cfg else []) if p.id == target_id), None)
        if target is None:
            _fail("unknown provider (configure one first)", 2)
        model = ns.model or (
            target.models[0]
            if target.models
            else ("claude-sonnet-4" if target.protocol == "anthropic" else "gpt-4o-mini")
        )
        key = ""
        if target.api_key_ref:
            try:
                _settings, services = _engine_services(ns.env_file)
                project = _management_project(services)
                key = services.secrets.resolve_value(
                    project_id=project.id,
                    secret_id=_secret_ref_cls()(target.api_key_ref),
                    actor_id=project.owner_user_id,
                )
            except Exception:  # noqa: BLE001 - unauthenticated probe allowed
                key = ""
        report = probe_capabilities(
            protocol=target.protocol,
            base_url=target.base_url,
            api_key=key,
            model=model,
            provider_id=target.id,
        )
        cache.put(report)
        out = report.to_dict()
        if ns.json:
            _print(out)
            return 0
        print(f"provider={target.id} model={model}")
        print(f"  tool_calls : {out['tool_calls']} {out['detail'].get('tool_calls', '')}")
        print(f"  streaming  : {out['streaming']} {out['detail'].get('streaming', '')}")
        bad = {"unsupported", "unavailable"} & {out["tool_calls"], out["streaming"]}
        return 1 if bad else 0
    _fail("unknown capabilities op", 2)


def cmd_backup_daemon(ns) -> int:
    """Foreground scheduled-backup loop (systemd/timer friendly)."""
    import signal as _signal
    import threading as _threading

    from zero.manage.services.backup_daemon import BackupDaemon

    cfgsvc = _cfgsvc()
    cfg = cfgsvc.load() if cfgsvc.exists() else ZeroConfig()
    if cfg.backups.schedule == "off":
        print("backup schedule is off in config")
        return 0

    def runner() -> str:
        _settings, services = _engine_services(ns.env_file)
        dest = Path(_home()) / "backups"
        dest.mkdir(parents=True, exist_ok=True)
        archive = dest / f"zero-backup-{time.strftime('%Y%m%d-%H%M%S')}.enc"
        services.backup.backup_to_file(str(archive))
        return str(archive)

    daemon = BackupDaemon(
        home=Path(_home()),
        schedule=cfg.backups.schedule,
        retention=cfg.backups.retention,
        backup_runner=runner,
    )
    stop = _threading.Event()

    def _sig(_s, _f):
        stop.set()

    _signal.signal(_signal.SIGINT, _sig)
    _signal.signal(_signal.SIGTERM, _sig)
    print(f"backup daemon running (schedule={cfg.backups.schedule}); Ctrl+C to stop")
    daemon.loop(stop)
    return 0


def cmd_backup_status(ns) -> int:
    sp = Path(_home()) / "backups" / "last-backup.json"
    data: dict | None = None
    if sp.exists():
        data = json.loads(sp.read_text(encoding="utf-8"))
    if ns.json or data is None:
        _print({"last": data})
        return 0
    age_h = (time.time() - float(data.get("epoch", 0))) / 3600.0
    print(f"last backup: {data.get('path')} ({age_h:.1f}h ago)")
    return 0


# ----------------------------------------------------------------------
# parser
# ----------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="zero", description="Zero Dev Telegram management CLI")
    p.add_argument("--version", action="version", version=f"zero {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def with_env(sp):
        sp.add_argument("--env-file", default=None)
        return sp

    setup_p = with_env(sub.add_parser("setup", help="run/resume setup wizard"))
    setup_p.add_argument("--non-interactive", action="store_true")
    setup_p.add_argument("--resume", action="store_true")
    setup_p.add_argument("--reset", action="store_true")
    setup_p.add_argument("--from-env", action="store_true", help="import ZERO_* env into draft")
    setup_p.add_argument("--step", action="append", default=[], help="section.key=value")

    for name, help_ in (
        ("start", "start service"),
        ("stop", "stop service"),
        ("restart", "restart service"),
    ):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--json", action="store_true")

    st = sub.add_parser("status", help="installation/service status")
    st.add_argument("--json", action="store_true")

    logs = sub.add_parser("logs", help="tail service logs")
    logs.add_argument("-n", type=int, default=100)

    doc = sub.add_parser("doctor", help="diagnostics")
    doc.add_argument("--json", action="store_true")
    doc.add_argument("--fix", action="store_true", help="apply safe fixes (perms/migrations)")

    tb = with_env(sub.add_parser("telegram", help="telegram operations"))
    tbs = tb.add_subparsers(dest="tg", required=True)
    addb = tbs.add_parser("add-bot")
    addb.add_argument("--token-file", default="-", help="'-' reads stdin (hidden prompt otherwise)")
    grp = tbs.add_parser("groups", help="group discovery/list")
    grps = grp.add_subparsers(dest="subcmd", required=True)
    d = grps.add_parser("discover")
    d.add_argument("--json", action="store_true")
    a = grps.add_parser("add")
    a.add_argument("--chat-id", required=True)
    a.add_argument("--title")
    a.add_argument("--kind", default="supergroup")
    l = grps.add_parser("list")
    e = grps.add_parser("enable")
    e.add_argument("--chat-id", required=True)
    dis = grps.add_parser("disable")
    dis.add_argument("--chat-id", required=True)
    for sp in (d, l, e, dis, a):
        pass

    pv = with_env(sub.add_parser("providers", help="manage providers"))
    pvs = pv.add_subparsers(dest="op", required=True)
    pvs.add_parser("list")
    pa = pvs.add_parser("add")
    pa.add_argument("--id", required=True)
    pa.add_argument(
        "--protocol", default="openai_compatible", choices=["openai_compatible", "anthropic"]
    )
    pa.add_argument("--base-url", required=True)
    pa.add_argument("--key-file", default="-")
    pa.add_argument("--models", default="")
    pa.add_argument("--priority", type=int, default=10)
    pa.add_argument("--probe", action="store_true", default=True)
    pa.add_argument("--save-unverified", action="store_true")
    pt = pvs.add_parser("test")
    pt.add_argument("--id", required=True)
    pr = pvs.add_parser("remove")
    pr.add_argument("--id", required=True)

    mo = sub.add_parser("models", help="routing assignment")
    mo.add_argument("--primary")
    mo.add_argument("--fallbacks")

    ac = with_env(sub.add_parser("access", help="access policy"))
    acs = ac.add_subparsers(dest="subcmd", required=True)
    sm = acs.add_parser("set-mode")
    sm.add_argument(
        "--mode",
        required=True,
        choices=["owner_only", "users", "groups", "users_and_groups", "public"],
    )
    sm.add_argument("--i-understand-public", action="store_true")
    acs.add_parser("show")

    us = with_env(sub.add_parser("usage", help="usage summary"))
    us.add_argument("--days", type=int, default=7)

    bk = with_env(sub.add_parser("backup", help="encrypted backups"))
    bks = bk.add_subparsers(dest="op", required=True)
    bc = bks.add_parser("create")
    bc.add_argument("--dest")
    bl = bks.add_parser("list")
    bl.add_argument("--dest")

    rs = with_env(sub.add_parser("restore", help="restore a backup"))
    rs.add_argument("file")
    rs.add_argument("--preview", action="store_true")
    rs.add_argument("--yes", action="store_true")

    up = with_env(sub.add_parser("update", help="update channel ops"))
    ups = up.add_subparsers(dest="op", required=True)
    ups.add_parser("check")
    ap = ups.add_parser("apply")
    ap.add_argument("--yes", action="store_true")

    un = sub.add_parser("uninstall", help="remove Zero (keeps data by default)")
    un.add_argument("--yes", action="store_true")
    un.add_argument("--purge-data", action="store_true")

    cf = with_env(sub.add_parser("config", help="config operations"))
    cfs = cf.add_subparsers(dest="op", required=True)
    cfs.add_parser("show")
    cfs.add_parser("diff")
    cfs.add_parser("validate")
    ex = cfs.add_parser("export")
    ex.add_argument("--redact", action="store_true", default=True)
    ex.add_argument("--include-secrets", action="store_true")
    cfs.add_parser("rollback")

    ws = with_env(sub.add_parser("websearch", help="web search setup"))
    wss = ws.add_subparsers(dest="op", required=True)
    we = wss.add_parser("enable")
    we.add_argument("--provider-id", required=True)
    wss.add_parser("disable")
    wss.add_parser("status")

    cap = with_env(sub.add_parser("capabilities", help="probe tool/stream capabilities"))
    caps = cap.add_subparsers(dest="op", required=True)
    caps.add_parser("show")
    cprobe = caps.add_parser("probe")
    cprobe.add_argument("--provider")
    cprobe.add_argument("--model")
    cprobe.add_argument("--json", action="store_true")

    with_env(sub.add_parser("backup-daemon", help="run scheduled backup loop"))
    bst = sub.add_parser("backup-status")
    bst.add_argument("--json", action="store_true")

    sub.add_parser("tui", help="full-screen TUI (requires [tui] extra)")

    return p


_HANDLERS = {
    "setup": cmd_setup,
    "start": cmd_start,
    "stop": cmd_stop,
    "restart": cmd_restart,
    "status": cmd_status,
    "logs": cmd_logs,
    "doctor": cmd_doctor,
    "telegram": lambda ns: _dispatch_tg(ns),
    "providers": cmd_providers,
    "models": cmd_models,
    "access": cmd_access,
    "groups": cmd_groups,
    "usage": cmd_usage,
    "backup": cmd_backup,
    "restore": cmd_restore,
    "update": cmd_update,
    "uninstall": cmd_uninstall,
    "config": cmd_config,
    "websearch": cmd_websearch,
    "tui": cmd_tui,
    # Audit D4: these parsers existed but were never dispatched.
    "capabilities": cmd_capabilities,
    "backup-daemon": cmd_backup_daemon,
    "backup-status": cmd_backup_status,
}


def _dispatch_tg(ns):
    if getattr(ns, "tg", None) == "add-bot":
        return cmd_telegram_add_bot(ns)
    if getattr(ns, "tg", None) == "groups":
        return cmd_groups(ns)
    _fail("unknown telegram subcommand", 2)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handler = _HANDLERS.get(args.cmd)
    if handler is None:  # pragma: no cover
        parser.print_help()
        return 2
    return int(handler(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
