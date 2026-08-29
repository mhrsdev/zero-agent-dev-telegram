"""SetupService — the durable wizard engine (single source for CLI/TUI/GUI)."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from zero.manage.core import probes
from zero.manage.core.config import ConfigError, ConfigService, ZeroConfig


@dataclass
class StepResult:
    ok: bool
    errors: list[str]
    warnings: list[str] | None = None


STEP_ORDER = [
    "welcome",
    "environment",
    "version",
    "telegram_mode",
    "telegram_credentials",
    "provider_add",
    "provider_test",
    "model_assign",
    "access_mode",
    "groups",
    "agents",
    "memory_storage",
    "websearch",
    "privacy",
    "updates",
    "backup_policy",
    "final_validation",
    "test_message",
]


class SetupService:
    """Durable setup: answers land in a draft; commit writes config atomically.

    Engine-facing side effects (secret storage, project bootstrap) go through
    ``engine`` — the composed Services bundle — so the wizard never touches
    files/DBs outside its adapters.
    """

    def __init__(
        self,
        cfgsvc: ConfigService,
        engine_provider: Callable[[], Any],
        *,
        secret_store: Callable[[str, str, str], str] | None = None,
    ) -> None:
        """``secret_store(name, secret_type, value) -> ref_id`` persists the
        value in the engine's encrypted store and returns its ``sec_…``
        reference. When None, the wizard runs in dry mode (refs stay None)
        and commit refuses to finish with un-stored secrets."""
        self.cfg = cfgsvc
        self.engine_factory = engine_provider
        self._store_secret = secret_store

    # -- draft ------------------------------------------------------------
    def resume(self) -> dict[str, Any]:
        return self.cfg.load_draft()

    def reset(self) -> None:
        self.cfg.clear_draft()

    def _draft(self) -> dict[str, Any]:
        d = self.cfg.load_draft()
        d.setdefault("data", {})
        return d

    def _save(self, draft: dict[str, Any], step: str | None) -> None:
        if step is not None:
            draft["current_step"] = step
        self.cfg.save_draft(draft)

    # -- validation per step ----------------------------------------------
    def validate(self, step: str, value: dict[str, Any]) -> StepResult:
        errors: list[str] = []
        warnings: list[str] = []
        if step == "environment":
            free_gb = _disk_free_gb(os.getcwd())
            if free_gb is not None and free_gb < 1.0:
                errors.append(f"low disk space ({free_gb:.2f} GB free)")
        elif step == "version":
            if value.get("channel") not in {"stable", "beta"}:
                errors.append("channel must be stable|beta")
        elif step == "telegram_mode":
            # Normalize forgivingly: "Bot-API", "bot-api", "botapi" … all
            # mean bot_api. Audit follow-up: the interactive wizard used to
            # discard the raw answer before it ever reached this check, so
            # even the exact string "bot_api" was rejected (deadlock).
            mode = str(value.get("mode") or "").strip().lower().replace("-", "_").replace(" ", "_")
            if mode == "botapi":
                mode = "bot_api"
            value["mode"] = mode
            if mode != "bot_api":
                errors.append(
                    "invalid mode "
                    + (f"{mode!r} " if mode else "(empty) ")
                    + "— available options: bot_api "
                    "(user-session mode intentionally not offered)"
                )
        elif step == "telegram_credentials":
            token = (value.get("token") or "").strip()
            if not token:
                errors.append("bot token required")
            elif _bad_secret(token):
                errors.append(_bad_secret(token))
            else:
                probe = probes.telegram_get_me(token)
                if not probe.get("ok"):
                    errors.append(f"bot token rejected: {probe.get('error')}")
                else:
                    value["bot_username"] = probe.get("username")
                    value["telegram_bot_id"] = probe.get("id")
        elif step == "provider_add":
            proto = value.get("protocol")
            base = (value.get("base_url") or "").rstrip("/")
            key = (value.get("api_key") or "").strip()
            pid = (value.get("id") or "").strip()
            if proto not in {"openai_compatible", "anthropic"}:
                errors.append("protocol must be openai_compatible|anthropic")
            if not base.startswith(("http://", "https://")):
                errors.append("base_url must be http(s)")
            if not pid:
                errors.append("provider id required")
            if not key:
                errors.append("api_key required")
            elif _bad_secret(key):
                errors.append(_bad_secret(key))
            if not errors and value.get("probe", True):
                if proto == "openai_compatible":
                    res = probes.openai_list_models(base, key)
                    if res.get("ok"):
                        discovered = res.get("models", [])[:200]
                        value["discovered_models"] = discovered
                        # Previously only discovered_models was recorded,
                        # so the committed provider ended up with an empty
                        # models list and routing could never match it.
                        if not value.get("models"):
                            value["models"] = discovered[:25]
                    else:
                        errors.append(f"auth/list failed: {res.get('error')}")
                else:
                    model = value.get("model") or "claude-sonnet-4"
                    res = probes.anthropic_ping(base, key, model)
                    if not res.get("ok"):
                        errors.append(f"ping failed: {res.get('error')}")
                    value.setdefault("models", [model])
        elif step == "provider_test":
            # Optional explicit completion probe using saved draft values.
            # Bug fix: the draft stores secrets MASKED (``sk-a…xyz`` — see
            # _mask) with the raw value under ``_raw``; this probe used to
            # read the masked value and crash httpx with UnicodeEncodeError
            # on the ellipsis. Read the raw secret, never the mask.
            draft = self._draft()["data"]
            pa = draft.get("provider_add", {})
            proto = pa.get("protocol")
            base = pa.get("base_url", "")
            # Prefer the raw secret; fall back to the stored (masked)
            # value only so OLD drafts (no _raw yet) get the probe's
            # clean "invalid characters" rejection instead of a crash.
            key = (pa.get("_raw") or {}).get("api_key") or pa.get("api_key", "")
            model = (value.get("model") or (pa.get("models") or [""])[0]) if pa else ""
            if not (proto and base and key and model):
                errors.append("provider/model not configured yet")
            elif proto == "anthropic":
                r = probes.anthropic_ping(base, key, model)
                if not r.get("ok"):
                    errors.append(f"completion probe failed: {r.get('error')}")
            else:
                r = probes.openai_completion_probe(base, key, model)
                if not r.get("ok"):
                    errors.append(f"completion probe failed: {r.get('error')}")
        elif step == "model_assign":
            primary = (value.get("primary_model") or "").strip()
            if not primary:
                errors.append("primary_model required")
            # Bug fix (silent footgun): the wizard accepted a fallback
            # identical to the primary (e.g. both "claude-opus-5") with no
            # complaint — such a "fallback" retries the exact same capacity
            # and adds no resilience. Surface a non-blocking warning.
            raw_csv = value.get("fallback_models_csv")
            if raw_csv is None and isinstance(value.get("fallback_models"), (list, tuple)):
                fallbacks = [str(f).strip() for f in value["fallback_models"] if str(f).strip()]
            else:
                fallbacks = [f.strip() for f in str(raw_csv or "").split(",") if f.strip()]
            if primary and primary in fallbacks:
                warnings.append(
                    f"fallback model '{primary}' equals the primary model — "
                    "a fallback adds no resilience; consider a different model"
                )
        elif step == "access_mode":
            mode = value.get("mode")
            if mode not in {"owner_only", "users", "groups", "users_and_groups", "public"}:
                errors.append("invalid access mode")
            if mode == "public" and not value.get("confirm_public"):
                errors.append("public mode requires confirm_public=true")
        elif step == "groups":
            gid = str(value.get("chat_id") or "").strip()
            if not gid:
                errors.append("chat_id required (use discover to find it)")
            if value.get("discover") and value.get("token"):
                r = probes.telegram_recent_chats(value["token"])
                value["candidates"] = r.get("chats", []) if r.get("ok") else []
                if not r.get("ok"):
                    warnings.append(f"discovery unavailable: {r.get('error')}")
        elif step == "websearch":
            if value.get("enabled"):
                if not value.get("provider_id"):
                    errors.append("websearch.provider_id required when enabled")
                else:
                    # Commit-trap fix: ZeroConfig rejects a websearch
                    # provider_id that references no provider — that used
                    # to surface only at commit(), AFTER all 18 steps were
                    # answered (rc 2, whole session wasted). Validate here,
                    # with the ids that will exist in the committed config.
                    known = self._known_provider_ids()
                    pid = str(value["provider_id"]).strip()
                    if not known:
                        errors.append(
                            "no provider is configured yet — websearch reuses a "
                            "configured provider; answer the Provider step first "
                            "(or skip websearch)"
                        )
                    elif pid not in known:
                        errors.append(
                            f"websearch.provider_id {pid!r} does not match a configured "
                            f"provider (available: {', '.join(sorted(known))})"
                        )
                key = (value.get("api_key") or "").strip()
                if not key:
                    errors.append("websearch api_key required when enabled")
                elif _bad_secret(key):
                    errors.append(_bad_secret(key))
        elif step == "backup_policy":
            sched = value.get("schedule", "daily")
            if sched not in {"off", "daily", "hourly"}:
                errors.append("schedule invalid")
        elif step == "final_validation":
            # Real final validation: build the full config from the draft
            # so schema/cross-field problems surface HERE, with a named
            # step, instead of as a traceback from commit().
            try:
                self.build_preview()
            except Exception as exc:  # noqa: BLE001 - any config problem
                errors.append(f"configuration invalid: {exc}")
        elif step in {
            "privacy",
            "updates",
            "memory_storage",
            "agents",
            "welcome",
        }:
            pass
        elif step == "test_message":
            # Bug fix: this step only COLLECTED a chat id — the message was
            # never sent, so "Send test message" verified nothing (the CLI
            # even printed the self-referencing transition "ok ->
            # test_message"). An empty chat id keeps the old skip
            # semantics (optional step); a provided chat id now performs
            # the real sendMessage round-trip.
            chat = str(value.get("chat_id") or "").strip()
            if chat:
                token = self._resolve_bot_token()
                if not token:
                    warnings.append(
                        "bot token not available in this session (resumed draft) — "
                        "test message not sent; verify delivery via the running bot"
                    )
                else:
                    r = probes.telegram_send_message(
                        token, chat, "Zero setup complete — this is a test message."
                    )
                    if r.get("ok"):
                        value["sent_message_id"] = r.get("message_id")
                        # Visible confirmation — the CLI prints warnings but
                        # stays silent on a plain ok, and this step's whole
                        # point is proof of delivery.
                        warnings.append(f"test message delivered (message_id {r.get('message_id')})")
                    else:
                        errors.append(f"test message failed: {r.get('error')}")
        else:
            errors.append(f"unknown step {step}")
        return StepResult(not errors, errors, warnings or None)

    def _resolve_bot_token(self) -> str | None:
        """Best-effort bot token for the test-message step.

        Prefers the raw token captured earlier in THIS wizard session
        (the draft stores it under ``telegram_credentials._raw``); for a
        resumed draft only the masked value remains, so fall back to the
        engine's encrypted store when an engine is wired. Any failure
        degrades to None — the step then soft-passes with a warning.
        """
        data = self._draft()["data"]
        tc = data.get("telegram_credentials", {}) or {}
        raw = probes.clean_secret((tc.get("_raw") or {}).get("token") or "")
        if raw:
            return raw
        try:
            ref = self.cfg.load().telegram.bot_token_ref
            if not ref:
                return None
            engine = self.engine_factory() if self.engine_factory else None
            services = getattr(engine, "services", engine)
            resolver = getattr(getattr(services, "secrets", None), "resolve_value", None)
            if resolver is None:
                return None
            project = getattr(services, "management_project", None)
            if project is None:
                return None
            return probes.clean_secret(
                resolver(
                    project_id=project.id,
                    secret_id=ref,
                    actor_id=project.owner_user_id,
                )
            )
        except Exception:  # noqa: BLE001 - best-effort only, never crash
            return None

    def _known_provider_ids(self) -> set[str]:
        """Provider ids that will exist in the committed config: the draft's
        provider_add (this session) plus any already-configured providers."""
        ids: set[str] = set()
        data = self._draft()["data"]
        pa_id = (data.get("provider_add", {}) or {}).get("id")
        if pa_id:
            ids.add(str(pa_id).strip())
        try:
            for p in self.cfg.load().providers:
                ids.add(p.id)
        except ConfigError:
            # No config yet (fresh wizard run) — the draft's provider_add
            # is the only source at this point.
            return ids
        return ids

    # -- answer + navigation ----------------------------------------------
    def answer(self, step: str, value: dict[str, Any]) -> StepResult:
        probe_value = dict(value)
        result = self.validate(step, probe_value)
        if result.ok and self._store_secret is not None:
            # Persist secrets immediately (real operation), replacing raw
            # values with durable references before anything is drafted.
            try:
                if step == "telegram_credentials" and probe_value.get("token"):
                    ref = self._store_secret("telegram-bot-token", "token", probe_value["token"])
                    probe_value["token_ref"] = ref
                elif step == "provider_add" and probe_value.get("api_key"):
                    ref = self._store_secret(
                        f"{probe_value.get('id', 'provider')}-api-key",
                        "api_key",
                        probe_value["api_key"],
                    )
                    probe_value["api_key_ref"] = ref
                elif (
                    step == "websearch"
                    and probe_value.get("enabled")
                    and probe_value.get("api_key")
                ):
                    ref = self._store_secret("websearch-api-key", "api_key", probe_value["api_key"])
                    probe_value["api_key_ref"] = ref
            except Exception as exc:  # noqa: BLE001 - storage failure blocks
                return StepResult(False, [f"secret store failed: {type(exc).__name__}"], result.warnings)
            result = StepResult(True, [], result.warnings)
        draft = self._draft()
        if result.ok:
            secret_keys = {"token", "api_key"}
            safe_value = {k: (_mask(v) if k in secret_keys else v) for k, v in probe_value.items()}
            # keep raw secrets ONLY for later steps needing live calls
            raw_subset = {k: probe_value[k] for k in secret_keys if k in probe_value}
            # validation may enrich the value (bot_username,
            # discovered_models); persist those too.
            for k, v in probe_value.items():
                if k not in value and k not in secret_keys:
                    safe_value[k] = v
            draft["data"][step] = {**safe_value, "_raw": raw_subset}
            idx = STEP_ORDER.index(step) if step in STEP_ORDER else len(STEP_ORDER) - 1
            nxt = STEP_ORDER[min(idx + 1, len(STEP_ORDER) - 1)]
            self._save(draft, nxt)
        return result

    def back(self, step: str) -> None:
        draft = self._draft()
        idx = STEP_ORDER.index(step) if step in STEP_ORDER else 0
        self._save(draft, STEP_ORDER[max(0, idx - 1)])

    def skip(self, step: str) -> str:
        """Advance past ``step`` without recording an answer (optional steps).

        The draft keeps no data for the step, so commit-time defaults
        apply. Returns the next step id so UIs can echo the progress.
        """
        draft = self._draft()
        idx = STEP_ORDER.index(step) if step in STEP_ORDER else 0
        nxt = STEP_ORDER[min(idx + 1, len(STEP_ORDER) - 1)]
        self._save(draft, nxt)
        return nxt

    def current(self) -> str:
        d = self.cfg.load_draft()
        return d.get("current_step") or STEP_ORDER[0]

    # -- commit -------------------------------------------------------------
    def commit(self) -> ZeroConfig:
        """Validate everything then write config.yaml atomically."""
        data = self._draft()["data"]
        cfg = self._build_config(data)
        # Dry-mode guard: never write a config pointing at secrets that
        # were never stored.
        dangling = []
        if cfg.telegram.bot_token_ref is None and data.get("telegram_credentials", {}).get("token"):
            dangling.append("telegram bot token")
        for p in cfg.providers:
            if p.api_key_ref is None:
                dangling.append(f"provider {p.id} key")
        if cfg.websearch.enabled and cfg.websearch.api_key_ref is None:
            dangling.append("websearch key")
        if dangling:
            raise ConfigError(
                "secrets not stored (no secret backend wired): " + ", ".join(sorted(set(dangling)))
            )
        self.cfg.save(cfg)
        self.cfg.clear_draft()
        return cfg

    def build_preview(self) -> ZeroConfig:
        return self._build_config(self._draft()["data"])

    def _build_config(self, data: dict[str, Any]) -> ZeroConfig:
        try:
            existing = self.cfg.load()
        except ConfigError:
            existing = ZeroConfig()
        env = data.get("environment", {})
        tc = data.get("telegram_credentials", {})
        pa = data.get("provider_add", {})
        ma = data.get("model_assign", {})
        am = data.get("access_mode", {})
        # Audit D5 follow-up: every UI used to answer "groups" with a flat
        # {chat_id, title} payload while only the synthetic "confirmed" list
        # was read here — so wizard-configured groups silently vanished from
        # the committed config. Accept both shapes.
        raw_groups = data.get("groups", {}) or {}
        groups = list(raw_groups.get("confirmed") or [])
        if not groups and str(raw_groups.get("chat_id") or "").strip():
            groups = [
                {
                    "chat_id": raw_groups["chat_id"],
                    "title": raw_groups.get("title") or "",
                    "kind": raw_groups.get("kind") or "supergroup",
                }
            ]
        ws = data.get("websearch", {})
        bk = data.get("backup_policy", {})
        up = data.get("updates", {"channel": "stable"})
        pv = data.get("privacy", {"telemetry_enabled": False})

        providers = list(existing.providers)
        if pa.get("id"):
            providers = [p for p in providers if p.id != pa["id"]]
            providers.append(
                __import__("zero.manage.core.config", fromlist=["ProviderCfg"]).ProviderCfg(
                    id=pa["id"],
                    protocol=pa.get("protocol", "openai_compatible"),
                    display_name=pa.get("display_name") or pa["id"],
                    base_url=pa.get("base_url", ""),
                    api_key_ref=pa.get("api_key_ref"),
                    models=pa.get("models") or ma.get("models") or [],
                )
            )
        from datetime import UTC, datetime

        access = existing.access.model_copy(deep=True)
        if am:
            access.mode = am.get("mode", access.mode)
            if access.mode == "public" and not access.public_confirmed_at:
                access.public_confirmed_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        # Audit D5: agents.default_agent applies to groups that do not
        # declare their own agent.
        default_agent = (data.get("agents", {}) or {}).get("default_agent", "main_worker")
        from zero.manage.core.config import GroupPolicy

        gpol = [
            GroupPolicy(
                chat_id=str(g.get("chat_id")),
                title=g.get("title", ""),
                kind=g.get("kind", "supergroup"),
                enabled=True,
                default_agent=(data.get("agents", {}).get(g.get("chat_id")) or default_agent),
                added_by="setup",
            )
            for g in groups
        ]
        access.groups = gpol or access.groups

        new = existing.model_copy(deep=True)
        new.server.environment = env.get("environment", new.server.environment)
        new.telegram.bot_token_ref = tc.get("token_ref") or new.telegram.bot_token_ref
        new.telegram.bot_username = tc.get("bot_username") or new.telegram.bot_username
        new.providers = providers
        new.routing.primary_model = (
            ma["primary_model"] if ma.get("primary_model") else new.routing.primary_model
        )
        # Audit D5: accept both the structured key and the wizard's CSV
        # field; a comma string is parsed instead of silently dropped.
        raw_fallback = ma.get("fallback_models")
        if raw_fallback is None and ma.get("fallback_models_csv"):
            raw_fallback = [
                item.strip() for item in str(ma["fallback_models_csv"]).split(",") if item.strip()
            ]
        if raw_fallback:
            new.routing.fallback_models = list(raw_fallback)
        new.access = access
        new.websearch.enabled = bool(ws.get("enabled", False))
        new.websearch.provider_id = ws.get("provider_id")
        if ws.get("api_key_ref"):
            new.websearch.api_key_ref = ws["api_key_ref"]
        new.backups.schedule = bk.get("schedule", new.backups.schedule)
        new.updates.channel = up.get("channel", new.updates.channel)
        # Audit D5: persist the collected auto_apply flag.
        if "auto_apply" in up:
            new.updates.auto_apply = bool(up["auto_apply"])
        new.privacy.telemetry_enabled = bool(pv.get("telemetry_enabled", False))
        return new

    # -- convenience probes used by UIs ------------------------------------
    def group_candidates(self, bot_token: str) -> dict[str, object]:
        return probes.telegram_recent_chats(bot_token)


def _mask(value: str | None) -> str:
    if not value:
        return ""
    return value[:4] + "…" + value[-4:] if len(value) > 8 else "…"


def _bad_secret(value: str) -> str | None:
    """Friendly validation message for keys/tokens that can never work.

    HTTP headers are ASCII; a value containing anything else (most
    commonly the literal ellipsis of a truncated copy like ``sk-a…z``,
    or invisible paste artifacts) must be rejected with guidance BEFORE
    any network call — previously it crashed httpx with
    UnicodeEncodeError and took the wizard down with a traceback.
    """
    cleaned = value.strip()
    if not cleaned:
        return None  # empty is handled by required-checks
    if "…" in cleaned:
        return "value looks like a truncated copy (contains '…') — paste the full token/key"
    if not cleaned.isascii() or not cleaned.isprintable():
        return "value contains invalid (non-ASCII or invisible) characters — re-copy the full token/key"
    return None


def _disk_free_gb(path: str) -> float | None:
    try:
        usage = os.statvfs(path) if hasattr(os, "statvfs") else None
        if usage is None:
            import shutil

            _total, _used, free = shutil.disk_usage(path)
            return free / 1024**3
        return (usage.f_bavail * usage.f_frsize) / 1024**3
    except OSError:
        return None
