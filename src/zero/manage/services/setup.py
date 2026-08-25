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
            if value.get("mode") != "bot_api":
                errors.append(
                    "only bot_api is available in this release "
                    "(user-session mode intentionally not offered)"
                )
        elif step == "telegram_credentials":
            token = (value.get("token") or "").strip()
            if not token:
                errors.append("bot token required")
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
            if not errors and value.get("probe", True):
                if proto == "openai_compatible":
                    res = probes.openai_list_models(base, key)
                    if res.get("ok"):
                        value["discovered_models"] = res.get("models", [])[:200]
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
            draft = self._draft()["data"]
            pa = draft.get("provider_add", {})
            proto = pa.get("protocol")
            base = pa.get("base_url", "")
            key = pa.get("api_key", "")
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
                if not (value.get("api_key") or "").strip():
                    errors.append("websearch api_key required when enabled")
        elif step == "backup_policy":
            sched = value.get("schedule", "daily")
            if sched not in {"off", "daily", "hourly"}:
                errors.append("schedule invalid")
        elif step in {
            "privacy",
            "updates",
            "memory_storage",
            "agents",
            "final_validation",
            "test_message",
            "welcome",
        }:
            pass
        else:
            errors.append(f"unknown step {step}")
        return StepResult(not errors, errors, warnings or None)

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
                return StepResult(False, [f"secret store failed: {type(exc).__name__}"])
            result = StepResult(True, [])
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
        groups = data.get("groups", {}).get("confirmed", [])
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
