"""Canonical typed configuration for Zero Dev Telegram (schema v1).

One truth file (`config.yaml`) edited by every management UI through
ConfigService. Secrets are NEVER inline — only `sec_…` references into the
engine's Fernet-backed store. Environment variables (ZERO_*) remain
supported and override file values at load time; overrides are reported so
UIs never fight the environment.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = 1
REF_RE = re.compile(r"^sec_[a-z0-9_]+$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class ServerCfg(_Strict):
    host: str = "127.0.0.1"
    port: int = Field(8000, ge=1, le=65535)
    environment: Literal["development", "test", "production"] = "development"


class WebhookCfg(_Strict):
    enabled: bool = False
    secret_ref: str | None = None


class TelegramCfg(_Strict):
    mode: Literal["bot_api"] = "bot_api"
    bot_token_ref: str | None = None
    bot_username: str | None = None
    webhook: WebhookCfg = WebhookCfg()
    polling_interval_seconds: float = Field(1.0, gt=0)


class GroupPolicy(_Strict):
    chat_id: str = Field(..., min_length=1)
    title: str = ""
    kind: Literal["private", "group", "supergroup", "forum", "channel"] = "supergroup"
    topic_id: str | None = None
    enabled: bool = True
    default_agent: str = "main_worker"
    allowed_features: list[str] = Field(default_factory=lambda: ["chat"])
    rate_limit_per_min: int = Field(10, ge=1)
    daily_token_budget: int = Field(200_000, ge=0)
    added_by: str | None = None
    added_at: float | None = None


class AccessCfg(_Strict):
    mode: Literal["owner_only", "users", "groups", "users_and_groups", "public"] = "owner_only"
    public_confirmed_at: str | None = None
    allow_users: list[str] = Field(default_factory=list)
    auto_verify_linked_members: bool = True
    groups: list[GroupPolicy] = Field(default_factory=list)

    @model_validator(mode="after")
    def _public_guard(self) -> AccessCfg:
        if self.mode == "public" and not self.public_confirmed_at:
            raise ValueError(
                "access.mode=public requires public_confirmed_at (explicit confirmation)"
            )
        return self


class ProviderCfg(_Strict):
    id: str = Field(..., min_length=2, max_length=64)
    protocol: Literal["openai_compatible", "anthropic"] = "openai_compatible"
    display_name: str = ""
    base_url: str = Field(..., min_length=8)
    api_key_ref: str | None = None
    enabled: bool = True
    fallback_priority: int = Field(10, ge=1)
    models: list[str] = Field(default_factory=list)


class BreakerCfg(_Strict):
    failure_threshold: int = Field(5, ge=1)
    cooldown_seconds: int = Field(60, ge=5)


class RoutingCfg(_Strict):
    primary_model: str | None = None
    fallback_models: list[str] = Field(default_factory=list)
    request_timeout_seconds: int = Field(120, ge=5)
    max_attempts_per_provider: int = Field(2, ge=1, le=8)
    breaker: BreakerCfg = BreakerCfg()


class UsageLimits(_Strict):
    soft_daily_tokens: int = Field(500_000, ge=0)
    hard_daily_tokens: int = Field(1_000_000, ge=0)
    per_group_daily_tokens: dict[str, int] = Field(default_factory=dict)


class WebSearchCfg(_Strict):
    enabled: bool = False
    provider_id: str | None = None
    api_key_ref: str | None = None


class BackupsCfg(_Strict):
    schedule: Literal["off", "daily", "hourly"] = "daily"
    retention: int = Field(7, ge=1)
    include_secrets: bool = False


class UpdatesCfg(_Strict):
    channel: Literal["stable", "beta"] = "stable"
    auto_check: bool = True
    auto_apply: bool = False


class PrivacyCfg(_Strict):
    telemetry_enabled: bool = False


class ZeroConfig(_Strict):
    schema_version: int = SCHEMA_VERSION
    owner_project_id: str | None = None  # engine project backing this bot
    server: ServerCfg = ServerCfg()
    telegram: TelegramCfg = TelegramCfg()
    access: AccessCfg = AccessCfg()
    providers: list[ProviderCfg] = Field(default_factory=list)
    routing: RoutingCfg = RoutingCfg()
    usage: UsageLimits = UsageLimits()
    websearch: WebSearchCfg = WebSearchCfg()
    backups: BackupsCfg = BackupsCfg()
    updates: UpdatesCfg = UpdatesCfg()
    privacy: PrivacyCfg = PrivacyCfg()

    @field_validator("providers")
    @classmethod
    def _unique_provider_ids(cls, v: list[ProviderCfg]) -> list[ProviderCfg]:
        ids = [p.id for p in v]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate provider id")
        return v

    @model_validator(mode="after")
    def _cross(self) -> ZeroConfig:
        model_pool = {m for p in self.providers for m in p.models}
        for m in [self.routing.primary_model, *self.routing.fallback_models]:
            if m and model_pool and m not in model_pool:
                raise ValueError(f"routing model {m!r} not offered by any provider")
        if self.websearch.enabled and (
            not self.websearch.provider_id
            or not any(p.id == self.websearch.provider_id for p in self.providers)
        ):
            raise ValueError("websearch.provider_id must reference a provider")
        return self

    # -- helpers ---------------------------------------------------------
    def redacted_dict(self) -> dict[str, Any]:
        data = json.loads(self.model_dump_json())

        def scrub(node: Any) -> Any:
            if isinstance(node, dict):
                return {
                    k: ("__REDACTED__" if isinstance(v, str) and REF_RE.match(v) else scrub(v))
                    for k, v in node.items()
                }
            if isinstance(node, list):
                return [scrub(x) for x in node]
            return node

        return scrub(data)


class ConfigError(ValueError):
    pass


class ConfigService:
    """Load/validate/save the canonical config with atomicity + history."""

    def __init__(self, home: Path) -> None:
        self.home = Path(home)
        self.path = self.home / "config.yaml"
        self.lock = self.home / ".config.lock"
        self.last_good = self.home / "config.last-good.yaml"
        self.backups = self.home / "backups"
        self.draft_path = self.home / "state" / "setup-draft.json"
        self.home.mkdir(parents=True, exist_ok=True)
        self.backups.mkdir(parents=True, exist_ok=True)
        self.draft_path.parent.mkdir(parents=True, exist_ok=True)

    # -- load ------------------------------------------------------------
    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> ZeroConfig:
        if not self.exists():
            return ZeroConfig()
        raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        version = int(raw.get("schema_version", 1))
        if version > SCHEMA_VERSION:
            raise ConfigError("config was written by a newer Zero; upgrade first")
        try:
            return ZeroConfig.model_validate(raw)
        except Exception as exc:  # pragma: no cover - message shaping
            raise ConfigError(f"invalid configuration: {exc}") from exc

    def env_overrides(self) -> dict[str, str]:
        """Report which canonical keys are currently overridden by ZERO_* env."""
        mapping = {
            "server.environment": "ZERO_ENV",
            "telegram.polling_interval_seconds": "ZERO_POLLING_INTERVAL_SECONDS",
            "routing.max_attempts_per_provider": "ZERO_PROVIDER_MAX_ATTEMPTS",
            "websearch": "ZERO_OPENAI_API_KEY",
        }
        out = {}
        for key, var in mapping.items():
            if os.environ.get(var):
                out[key] = var
        return out

    # -- save ------------------------------------------------------------
    def save(self, cfg: ZeroConfig, *, rotate_last_good: bool = True) -> None:
        payload = yaml.safe_dump(
            json.loads(cfg.model_dump_json()), sort_keys=False, allow_unicode=True
        )
        self._locked_write(self.path, payload)
        os.chmod(self.path, 0o600)
        if rotate_last_good and self.path.exists():
            self.last_good.write_text(payload, encoding="utf-8")

    def rollback_to_last_good(self) -> bool:
        if not self.last_good.exists():
            return False
        self._locked_write(self.path, self.last_good.read_text(encoding="utf-8"))
        return True

    def diff_last_good(self) -> dict[str, Any]:
        if not self.last_good.exists():
            return {"changed": []}
        old = self.last_good.read_text(encoding="utf-8").splitlines()
        new = self.path.read_text(encoding="utf-8").splitlines() if self.exists() else []
        changed = [
            {"line": i + 1, "before": a, "after": b}
            for i, (a, b) in enumerate(zip(old, new))
            if a != b
        ]
        max_len = max(len(old), len(new))
        for i in range(min(len(old), len(new)), max_len):
            changed.append(
                {
                    "line": i + 1,
                    "before": old[i] if i < len(old) else "",
                    "after": new[i] if i < len(new) else "",
                }
            )
        return {"changed": changed}

    # -- draft (setup state machine persistence) --------------------------
    def load_draft(self) -> dict[str, Any]:
        if not self.draft_path.exists():
            return {"version": 1, "current_step": None, "data": {}}
        return json.loads(self.draft_path.read_text(encoding="utf-8"))

    def save_draft(self, draft: dict[str, Any]) -> None:
        self._locked_write(self.draft_path, json.dumps(draft, indent=2))

    def clear_draft(self) -> None:
        try:
            self.draft_path.unlink()
        except FileNotFoundError:
            pass

    # -- internals --------------------------------------------------------
    def _locked_write(self, target: Path, text: str) -> None:
        lock = self.lock.open("a+")
        try:
            for _ in range(50):  # ~5s bounded contention
                try:
                    os.remove(str(lock.name) + ".hold")
                    break
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
                try:
                    Path(str(lock.name) + ".hold").open("x").close()
                    break
                except FileExistsError:
                    time.sleep(0.1)
            tmp = target.with_suffix(target.suffix + f".tmp{os.getpid()}")
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        finally:
            lock.close()
