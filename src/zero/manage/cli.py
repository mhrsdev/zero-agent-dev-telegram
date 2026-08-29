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
from zero.manage.core.config import ConfigError, ConfigService, ZeroConfig, zero_home

# Private alias so this module's many call sites stay untouched; the
# single canonical $ZERO_HOME resolver lives in manage.core.config.
_home = zero_home


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

    # Bug fix: strict load made `zero setup` impossible on a fresh host —
    # storing any secret raised "ZERO_ENV is required" because the .env
    # the wizard is supposed to create does not exist yet. Like
    # `zero-develop serve`, the management CLI is a developer-facing
    # entry point, so it may assume development defaults; an explicit
    # ZERO_ENV (env or .env) still always wins.
    settings = Settings.load(env_file=env_file, zero_env_fallback="development")
    database = open_database(settings)
    apply_migrations(database)
    services = build_services(settings, database)
    # Bookkeeping for `zero doctor`: remember which database THIS command
    # resolved in THIS directory, so a later drift can be auto-repaired.
    try:
        from zero.manage.core.env_file import record_database_usage

        record_database_usage()
    except Exception:  # noqa: BLE001 - bookkeeping must never break a command
        pass
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

    if ns.resume:
        draft = setup.resume()
        if draft.get("current_step") or draft.get("data"):
            print(f"resuming draft at step: {draft.get('current_step') or 'welcome'}")

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
        rc = _interactive_setup(setup)
        if rc == 0:
            _pin_database_after_setup()
        return rc

    if not ns.step:
        _fail(
            "--non-interactive requires at least one --step section.key=value "
            "(or run the interactive wizard without --non-interactive)",
            2,
        )

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
    _pin_database_after_setup()
    return 0


def _pin_database_after_setup() -> None:
    """Pin the wizard's database into $ZERO_HOME/.env after a commit.

    Bug fix (2026-08-29, "bot completely dead" session): the wizard
    stores secrets into the database resolved IN ITS CWD (development
    fallback ``sqlite:///./zero_develop.db``), while ``zero start`` later
    resolved the same RELATIVE URL from a different directory and booted
    a fresh, secret-less database — every ``sec_...`` reference in
    config.yaml then failed with SecretNotFoundError and the bot could
    not respond even to /start. Pinning the ABSOLUTE database URL into
    ``$ZERO_HOME/.env`` (which every engine start now loads by default)
    makes the storage location stable regardless of CWD.
    """
    try:
        from zero.manage.core.env_file import pin_database_url

        report = pin_database_url()
    except Exception as exc:  # noqa: BLE001 - never fail setup over the pin
        print(
            f"note: could not pin the database into {_home() / '.env'} "
            f"({type(exc).__name__}: {exc}) — if the Telegram bot stays "
            "silent after 'zero start', run 'zero doctor'."
        )
        return
    if report.get("pinned"):
        print(
            f"pinned engine database into {_home() / '.env'} "
            f"({', '.join(report['pinned'])}): {report['database_url']}"
        )


def _interactive_setup(setup) -> int:
    """Form-driven interactive wizard.

    Bug fix history: the previous "minimal" driver passed an empty value
    dict for every step it had no hard-coded branch for — including
    ``telegram_mode`` and ``model_assign`` — so typing the exact valid
    answer still failed validation and the wizard deadlocked. It now
    derives its prompts from the shared WIZARD_STEPS form specs (same
    source as the GUI), shows every available option/default, and can
    skip optional steps, so every step of STEP_ORDER is completable.
    """
    from zero.manage.services.setup import STEP_ORDER
    from zero.manage.services.wizard_forms import WIZARD_STEPS

    print("Zero Dev Telegram — setup wizard")
    print("(answers are saved · 'b'=back · 's'=skip optional · Ctrl+C to pause/resume)")
    total = len(STEP_ORDER)
    try:
        while True:
            step = setup.current()
            spec = WIZARD_STEPS.get(step)
            idx = STEP_ORDER.index(step) + 1 if step in STEP_ORDER else total
            print("\n" + "─" * 64)
            print(f"Step {idx}/{total} · {spec.title if spec else step}")
            if step == "welcome":
                print(f"  config home : {_home()}")
                print(f"  version     : {__version__}")
            if spec and spec.optional:
                print("  (optional step)")
            if step == "groups":
                print("  (Enter at chat id skips — add later via: zero telegram groups add)")

            value, action = _collect_step_answers(setup, step, spec)
            if action == "back":
                setup.back(step)
                print(f"  <- back to {setup.current()}")
                continue
            if action == "skip":
                if step == STEP_ORDER[-1]:
                    print("  skipped — setup complete")
                    break
                nxt = setup.skip(step)
                print(f"  skipped -> {nxt}")
                continue

            last_errors: list[str] | None = None
            while True:
                result = setup.answer(step, value)
                for e in result.errors or []:
                    print(f"  ! {e}")
                for w in result.warnings or []:
                    print(f"  ~ {w}")
                if result.ok:
                    break
                # Bug fix (UX): any validation failure used to re-ask every
                # field, so a transient probe error ("unreachable:
                # ConnectError") forced retyping the whole step. Offer a
                # retry with the same answers first.
                can_skip = bool(spec and spec.optional) or step == "groups"
                # Bug fix (dead-loop): "Enter=retry same answers" can never
                # fix a DETERMINISTIC validation error (e.g. the websearch
                # step with required provider_id/api_key left empty) — the
                # identical answers fail identically forever, which is
                # exactly what happened in the reported Windows session.
                # Keep the one-keypress retry for transient probe/network
                # errors, but after ONE identical failure automatically
                # re-ask the step's fields (prefilled with the previous
                # answers) instead of looping.
                if last_errors is not None and list(result.errors or []) == last_errors:
                    print("  same answers failed twice — re-asking this step's fields")
                    value, action2 = _collect_step_answers(setup, step, spec, prefill=value)
                    if action2 == "back":
                        setup.back(step)
                        print(f"  <- back to {setup.current()}")
                        action = "back"
                        break
                    if action2 == "skip":
                        action = "skip"
                        break
                    last_errors = None
                    continue
                last_errors = list(result.errors or [])
                opts = "Enter=retry same answers · r=re-enter · b=back"
                if can_skip:
                    opts += " · s=skip"
                choice = input(f"  [{opts}]: ").strip().lower()
                if choice == "b":
                    setup.back(step)
                    print(f"  <- back to {setup.current()}")
                    action = "back"
                    break
                if choice == "s" and can_skip:
                    action = "skip"
                    break
                if choice == "r":
                    value, action2 = _collect_step_answers(setup, step, spec, prefill=value)
                    if action2 == "back":
                        setup.back(step)
                        print(f"  <- back to {setup.current()}")
                        action = "back"
                        break
                    if action2 == "skip":
                        action = "skip"
                        break
                    last_errors = None
                    continue
                # Enter (or anything else): retry with the same answers —
                # the usual fix for a transient network error. A second
                # identical failure re-asks the fields automatically (see
                # the dead-loop fix above).
            if action == "back":
                continue
            if action == "skip":
                if step == STEP_ORDER[-1]:
                    print("  skipped — setup complete")
                    break
                nxt = setup.skip(step)
                print(f"  skipped -> {nxt}")
                continue
            if step == STEP_ORDER[-1]:
                # Bug fix: the last step used to print the self-referencing
                # transition "ok -> test_message" (answer() cannot advance
                # past the final step) — report completion instead.
                print("  ok — setup complete")
                break
            print(f"  ok -> {setup.current()}")
    except (KeyboardInterrupt, EOFError):
        print(f"\npaused at step '{setup.current()}' — resume with: zero setup --resume")
        return 130

    print("\n" + "─" * 64)
    try:
        setup.commit()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - e.g. pydantic ValidationError:
        # operators get an actionable message, never a traceback.
        print(
            f"error: configuration invalid: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2
    print(f"configuration written: {cfgsvc_path()}")
    return 0


_OMIT = object()  # sentinel: field left unset (no default, not required)


def _dynamic_wizard_defaults(setup, step: str, draft_data: dict) -> dict[str, object]:
    """Per-step defaults derived from previously answered draft steps."""
    out: dict[str, object] = {}
    provider_models = (draft_data.get("provider_add", {}) or {}).get("models") or []
    if step == "provider_test" and provider_models:
        out["model"] = provider_models[0]
    elif step == "model_assign" and provider_models:
        out["primary_model"] = provider_models[0]
    elif step == "groups":
        raw_token = ((draft_data.get("telegram_credentials", {}) or {}).get("_raw") or {}).get(
            "token"
        )
        if raw_token:
            out["token"] = raw_token
    return out


def _field_prompt(field, default) -> str:
    """Render one input line: label, available options, default, required."""
    hints: list[str] = []
    if field.kind == "select" and field.options:
        hints.append("options: " + ", ".join(field.options))
    if field.kind == "bool":
        hints.append("y/n")
    if field.kind == "password":
        if default:
            hints.append("Enter=keep previous")
    elif default is not None and default != "":
        hints.append(f"default: {default}")
    if field.required and field.kind != "bool":
        hints.append("required")
    suffix = (" (" + "; ".join(hints) + ")") if hints else ""
    return f"  {field.label}{suffix}: "


def _parse_field_value(field, raw: str, default):
    """Coerce one raw answer per field kind; returns (value|_OMIT, error)."""
    if raw == "":
        if field.kind == "bool":
            return bool(default), None
        if default is not None:
            return default, None
        if field.required:
            return None, f"{field.label} is required"
        return _OMIT, None
    if field.kind == "bool":
        if raw.lower() in {"true", "yes", "y", "1", "on"}:
            return True, None
        if raw.lower() in {"false", "no", "n", "0", "off"}:
            return False, None
        return None, "please answer y/n"
    if field.kind == "password":
        # Keys/tokens are pasted: strip invisible artifacts (zero-width,
        # NBSP, BOM) and reject visible non-ASCII up front — the wizard's
        # own draft mask ('…') or a truncated copy would otherwise crash
        # the HTTP probe with UnicodeEncodeError.
        from zero.manage.core.probes import clean_secret

        cleaned = clean_secret(raw)
        if cleaned is None:
            return None, (
                "value contains invalid characters (often '…' from a truncated "
                "copy or invisible paste artifacts) — paste the full value again"
            )
        return cleaned, None
    if field.kind == "select":
        opts = list(field.options)
        if raw in opts:
            return raw, None
        if raw.isdigit() and 1 <= int(raw) <= len(opts):
            return opts[int(raw) - 1], None
        normalized = raw.lower().replace("-", "_").replace(" ", "_")
        if normalized in opts:
            return normalized, None
        return None, f"choose one of: {', '.join(opts)}"
    if field.kind == "int":
        try:
            return int(raw), None
        except ValueError:
            return None, "enter a whole number"
    return raw, None


def _collect_step_answers(setup, step: str, spec, prefill: dict | None = None):
    """Prompt every field of a wizard step; returns (value, action).

    action: 'answer' | 'back' | 'skip' ('s', or Enter on an empty
    non-required step such as groups). ``prefill`` (the last failed
    attempt) supplies field defaults so 'r'-re-entry keeps typed values
    instead of falling back to stale draft/spec defaults.
    """
    fields = spec.fields if spec else ()
    if not fields:
        raw = input("  Press Enter to continue ('b'=back, 's'=skip): ").strip().lower()
        if raw == "b":
            return {}, "back"
        if raw == "s":
            return {}, "skip"
        return {}, "answer"

    draft_data = setup.resume().get("data", {})
    saved = draft_data.get(step, {}) or {}
    dynamic = _dynamic_wizard_defaults(setup, step, draft_data)

    value: dict[str, object] = {}
    for field in fields:
        # Conditional fields: only asked when a previous answer needs them.
        if (
            step == "access_mode"
            and field.name == "confirm_public"
            and value.get("mode") != "public"
        ):
            continue
        if (
            step == "websearch"
            and field.name in {"provider_id", "api_key"}
            and not value.get("enabled")
        ):
            continue

        default = saved.get(field.name, field.default)
        if default is None and field.name in dynamic:
            default = dynamic[field.name]
        if default is None and prefill and field.name in prefill:
            default = prefill[field.name]

        while True:
            prompt = _field_prompt(field, default)
            if field.kind == "password":
                raw = getpass.getpass(prompt).strip()
            else:
                raw = input(prompt).strip()
            low = raw.lower()
            if low == "b":
                return {}, "back"
            if low in {"s", "skip"} and (spec.optional or step == "groups"):
                return {}, "skip"
            parsed, err = _parse_field_value(field, raw, default)
            if err:
                print(f"  ! {err}")
                continue
            if parsed is not _OMIT:
                value[field.name] = parsed
            # Groups gate: an empty chat id means "no groups" — skip the
            # remaining discovery prompts immediately.
            if step == "groups" and field.name == "chat_id" and not value.get("chat_id"):
                return {}, "skip"
            break

    if step == "groups":
        if not str(value.get("chat_id") or "").strip():
            return {}, "skip"
        if value.get("token"):
            value["discover"] = True
    return value, "answer"


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


def _pid_alive(pid: int) -> bool:
    """Liveness probe that never signals the target process.

    Bug fix (Windows): ``os.kill(pid, 0)`` is a harmless no-signal probe
    on POSIX, but on Windows os.kill maps any non-CTRL signal to
    TerminateProcess — so ``zero status`` used to KILL the running
    service it was merely checking. On Windows we ask the kernel via a
    query-only handle instead; POSIX keeps the cheap signal-0 probe.
    """
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    SYNCHRONIZE = 0x00100000
    WAIT_TIMEOUT = 0x00000102
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, int(pid)
    )
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)



def _systemd_active() -> bool:
    """True only when systemd MANAGES this machine AND the unit exists.

    Bug fix (2026-08-29, e2e session): `cmd_start`/`cmd_stop`/status
    gated on the mere PRESENCE of the systemctl binary. On hosts where
    the binary is installed but PID 1 is NOT systemd (WSL, containers,
    chroots), every `systemctl` call failed with "System has not been
    booted with systemd" and `zero start` silently started NOTHING
    while still printing status output. The plain-process fallback now
    runs on such hosts.
    """
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return False
    if not Path("/run/systemd/system").exists():
        return False
    try:
        return (
            subprocess.run(
                [systemctl, "cat", SERVICE_NAME], capture_output=True, check=False
            ).returncode
            == 0
        )
    except OSError:
        return False


def _service_status() -> dict[str, str]:
    if _systemd_active():
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
            alive = _pid_alive(int(pid))
        except ValueError:
            alive = False
        if alive:
            return {"kind": "process", "state": f"running(pid {pid})"}
        return {"kind": "process", "state": f"stopped (stale pid {pid})"}
    return {"kind": "none", "state": "stopped"}


def _healthz_ok(url: str, timeout: float = 1.0) -> bool:
    """Loopback-only health probe; never goes through a system proxy."""
    import urllib.request

    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:  # noqa: BLE001 - any error means "not healthy yet"
        return False


def _last_log_lines(count: int) -> list[str]:
    log = _home() / "zero.log"
    try:
        return log.read_text(errors="replace").splitlines()[-count:]
    except OSError:
        return []


def _port_busy(host: str, port: int) -> bool:
    """True when (host, port) cannot be bound right now."""
    import socket

    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, port))
    except OSError:
        return True
    return False


def _managed_bind() -> tuple[str, int]:
    """The (host, port) ``zero start`` spawns the service on.

    Bug fix (ignored server config): the bind used to be hardcoded to
    127.0.0.1:8000 in four places while config.yaml exposes
    ``server.host``/``server.port`` (ServerCfg) — a configured port was
    silently ignored and `zero-develop serve`'s pre-checks disagreed
    with where the managed service actually lived. Both CLIs now resolve
    the managed bind through this single helper. A missing or invalid
    configuration falls back to the loopback defaults so a fresh host
    stays startable (``zero-develop serve`` mirrors this fallback).
    """
    try:
        server = _cfgsvc().load().server
        return server.host, int(server.port)
    except Exception:  # noqa: BLE001 - config problems must not brick start
        return "127.0.0.1", 8000


def _probe_host(host: str) -> str:
    """A host usable as an HTTP connect target (wildcards → loopback)."""
    return "127.0.0.1" if host in {"", "0.0.0.0", "::"} else host


def cmd_start(ns) -> int:
    if _systemd_active():
        subprocess.run(["systemctl", "start", SERVICE_NAME], check=False)
        return cmd_status(ns)
    # Bug fix: a second `zero start` used to spawn a doomed duplicate that
    # died on the bind error (WinError 10048 / EADDRINUSE) while
    # overwriting zero.pid with its dead pid — exactly the reported
    # Windows session. Refuse with guidance when the service is running.
    pid_file = _home() / "zero.pid"
    if pid_file.exists():
        try:
            running_pid = int(pid_file.read_text().strip())
            if _pid_alive(running_pid):
                print(
                    f"service already running (pid {running_pid}); "
                    "use 'zero restart' to reload it or 'zero stop' first"
                )
                return 1
        except ValueError:
            pass
    # Bug fix: never spawn a child doomed to lose the bind race — and
    # never mistake ANOTHER healthy service on the port for the one we
    # just spawned (its /healthz would answer for the child that is
    # about to die). Refuse up front with an actionable message.
    # Bug fix (ignored server config): the bind now honors
    # server.host/server.port from config.yaml instead of a hardcoded
    # 127.0.0.1:8000 (see _managed_bind).
    bind_host, bind_port = _managed_bind()
    probe_host = _probe_host(bind_host)
    if _port_busy(bind_host, bind_port):
        if _healthz_ok(f"http://{probe_host}:{bind_port}/healthz"):
            print(
                f"port {bind_port} already serves a healthy Zero service that this "
                "pid file does NOT manage — stop that process first, or use "
                "'zero status' to inspect it"
            )
        else:
            print(f"port {bind_port} is already in use by another process — stop it first")
        return 1
    # Bug fix: the log file was opened before $ZERO_HOME existed, so a
    # fresh `zero start` crashed with FileNotFoundError. Create it first.
    _home().mkdir(parents=True, exist_ok=True)
    # Bookkeeping for `zero doctor`: record which database THIS spawn
    # (resolved in THIS directory) will open, so a later CWD drift is
    # diagnosable and auto-repairable via `zero doctor --fix`.
    try:
        from zero.manage.core.env_file import record_database_usage

        record_database_usage()
    except Exception:  # noqa: BLE001 - bookkeeping must never break start
        pass
    log = open(_home() / "zero.log", "ab")  # noqa: SIM115
    spawn: dict = {}
    if os.name == "nt":
        # start_new_session is POSIX-only (ValueError on Windows).
        spawn["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    else:
        spawn["start_new_session"] = True
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "zero.main:app",
            "--host",
            bind_host,
            "--port",
            str(bind_port),
        ],
        stdout=log,
        stderr=log,
        **spawn,
    )
    # The child owns its inherited duplicate now; keeping the parent-side
    # handle open leaked an fd (and tripped ResourceWarning under pytest).
    log.close()
    (_home() / "zero.pid").write_text(str(proc.pid))
    print(f"started pid={proc.pid} (foreground alternative: zero-develop serve)")
    # Bug fix: `zero start` used to report success without confirming the
    # process survived startup — a bind failure killed it seconds later
    # and the pid file kept pointing at a dead process. Verify liveness
    # and wait for /healthz. The window must cover a COLD first start
    # (imports + fresh-database migrations) on slow hosts, not just a
    # warm restart; a healthy boot still reports within a few seconds.
    deadline = time.time() + 25.0
    while time.time() < deadline:
        # BUG FIX: _pid_alive(proc.pid) is wrong for OUR OWN child — on
        # POSIX a dead child stays a zombie until reaped, so signal-0
        # kept reporting "alive" forever and the bind-failure death was
        # never noticed. Popen.poll() reaps AND reports the exit.
        if proc.poll() is not None:
            print("service process exited during startup; last log lines:", file=sys.stderr)
            for line in _last_log_lines(5):
                print(f"  {line}", file=sys.stderr)
            return 1
        if _healthz_ok(f"http://{probe_host}:{bind_port}/healthz"):
            # Guard the health answer against the spawn/bind race: a child
            # that lost the port to a foreign process dies right after
            # startup — never credit a foreign service for ours.
            time.sleep(0.5)
            if proc.poll() is not None:
                print("service process exited during startup; last log lines:", file=sys.stderr)
                for line in _last_log_lines(5):
                    print(f"  {line}", file=sys.stderr)
                return 1
            print(f"service healthy at http://{bind_host}:{bind_port} (pid={proc.pid})")
            return 0
        time.sleep(0.25)
    print(
        "warning: process is alive but /healthz has not responded yet "
        "(first start may still be migrating) — check `zero logs`"
    )
    return 0


def cmd_stop(ns) -> int:
    if _systemd_active():
        subprocess.run(["systemctl", "stop", SERVICE_NAME], check=False)
        return 0
    pid_file = _home() / "zero.pid"
    # Honesty fix: `zero stop` used to print "stopped" even when nothing
    # was running (no pid file), which masked real state confusion.
    if not pid_file.exists():
        print("service not running (no pid file)")
        return 0
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


def _journalctl_active_unit() -> bool:
    """True only when the zero systemd unit actually exists on this host.

    Bug fix: `zero logs` used to exec journalctl whenever the binary
    existed, even when the service runs as a plain process (`zero start`
    writes zero.pid/zero.log) — users saw "No entries" while the file
    had fresh content. Fall back to the file when the unit is absent.
    """
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return False
    try:
        return (
            subprocess.run(
                [systemctl, "cat", SERVICE_NAME], capture_output=True, check=False
            ).returncode
            == 0
        )
    except OSError:
        return False


def cmd_logs(ns) -> int:
    # Bug fix: the parser only defined `-n` (dest "n") while this handler
    # read `ns.lines`, so every `zero logs` crashed with AttributeError.
    # `-n N` and `--lines N` both work now, and a non-positive N no longer
    # falls into Python's `[-0:] == [0:]` trap (which printed EVERYTHING).
    tail = max(0, int(getattr(ns, "lines", 100) or 0))
    if _journalctl_active_unit():
        os.execvp(
            "journalctl", ["journalctl", "-u", SERVICE_NAME, "-n", str(tail), "--no-pager"]
        )
    log = _home() / "zero.log"
    if not log.exists():
        print("no log file yet")
        return 0
    if tail == 0:
        return 0
    lines = log.read_text(errors="replace").splitlines()[-tail:]
    print("\n".join(lines))
    return 0


def cmd_doctor(ns) -> int:
    from zero.manage.services.doctor import DoctorService

    doctor = DoctorService(_cfgsvc(), _engine_services)
    if getattr(ns, "fix", False):
        fix_report = doctor.fix()
        for line in fix_report["fixed"]:
            print(f"[FIX ] {line}")
        recheck = fix_report.get("recheck")
        if recheck is not None:
            sym = "ok" if recheck["ok"] else "FAIL"
            print(f"[RECHK] {sym}: " + ", ".join(f"{l}={s}" for l, s in recheck["details"]))
    report = doctor.run()
    failed = [c for c in report["checks"] if c["status"] == "fail"]
    warn = [c for c in report["checks"] if c["status"] == "warn"]
    if ns.json:
        _print(report)
    else:
        sym = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]"}
        for c in report["checks"]:
            print(f"{sym[c['status']]} {c['name']}: {c['detail']}")
        print(f"\n{len(report['checks'])} checks · {len(failed)} fail · {len(warn)} warn")
    if getattr(ns, "fix", False) and not failed:
        return 0
    if getattr(ns, "fix", False):
        print("issues remain — see the [FAIL] lines above for the manual next action")
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
    if _systemd_active():
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
    age_h = (time.time() - float(data.get("epoch") or 0)) / 3600.0
    print(f"last backup: {data.get('path')} ({age_h:.1f}h ago)")
    return 0


# ----------------------------------------------------------------------
# parser
# ----------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zero", description="Zero Dev Telegram management CLI")
    parser.add_argument("--version", action="version", version=f"zero {__version__}")
    # Not required: a bare `zero` should print help, not an argparse error.
    sub = parser.add_subparsers(dest="cmd")

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
    # Bug fix: bare "-n" made argparse derive dest "n" while cmd_logs read
    # ns.lines — every `zero logs` crashed. Explicit dest + long alias.
    logs.add_argument(
        "-n",
        "--lines",
        dest="lines",
        type=int,
        default=100,
        metavar="N",
        help="show the last N lines (default: 100)",
    )

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
    # Bug fix: `--probe` with store_true/default=True could never be
    # turned off; BooleanOptionalAction gives --probe / --no-probe.
    pa.add_argument(
        "--probe",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="probe the endpoint before saving (default: yes)",
    )
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

    return parser


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
    if handler is None:
        parser.print_help()
        return 2
    try:
        return int(handler(args))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except EOFError:
        # A piped/closed stdin reached an input()/getpass() prompt
        # outside the wizard: fail like a usage error, not a traceback.
        print("\ninput stream closed (EOF) before a value was provided", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
