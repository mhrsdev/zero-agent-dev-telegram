"""Zero v2 configuration — ADR 0007 implementation.

Config sources and priority (ADR 0007 §1):

    1. CLI args                       (highest)
    2. env var ``ZERO_<SECTION>__<KEY>``  (``__`` separates nesting)
    3. ``~/.zero/config.yaml``        (user)
    4. ``/etc/zero/config.yaml``      (system)
    5. code default                   (lowest)

Pydantic validates the entire tree at startup. Invalid config = startup failure
with a clear message, never a silent default.

Any field marked ``secret=True`` MUST be a ``secret://`` reference — raw values
are rejected at parse time.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from zero.core.secret import (
    CompositeSecretResolver,
    SecretError,
    SecretResolver,
    SecretValue,
    is_secret_ref,
)

__all__ = [
    "DEFAULT_CONFIG_PATHS",
    "ENV_PREFIX",
    "AgentConfig",
    "ConfigError",
    "DatabaseConfig",
    "LoggingConfig",
    "MemoryConfig",
    "PlatformConfig",
    "RouterConfig",
    "SecurityConfig",
    "TelegramConfig",
    "TelemetryConfig",
    "ZeroConfig",
    "get_config",
    "load_config",
    "reset_config_cache",
]


# ---------------------------------------------------------------------- helpers

class ConfigError(RuntimeError):
    """Raised when config cannot be loaded or validated."""


ENV_PREFIX = "ZERO_"
ENV_NESTING_SEP = "__"
DEFAULT_CONFIG_PATHS: tuple[Path, ...] = (
    Path.home() / ".zero" / "config.yaml",
    Path("/etc/zero/config.yaml"),
)


# ---------------------------------------------------------------------- sub-configs

class DatabaseConfig(BaseModel):
    """Database connection config (ADR 0003 — three schemas / three roles)."""

    model_config = ConfigDict(extra="forbid")

    backend: Literal["sqlite", "postgres"] = "sqlite"

    # SQLite: a single directory containing 3 DB files (personal.db, normal.db, dev.db).
    # This is the SQLite equivalent of three PostgreSQL schemas (ADR 0003 §6, open decision).
    sqlite_dir: Path | None = Field(default=None, description="Directory for 3 SQLite DB files")

    # PostgreSQL: three separate connection strings (one per schema/role).
    postgres_personal_dsn: str | None = None
    postgres_normal_dsn: str | None = None
    postgres_dev_dsn: str | None = None

    @model_validator(mode="after")
    def _validate_backend(self) -> DatabaseConfig:
        if self.backend == "sqlite" and self.sqlite_dir is None:
            # Default: ~/.zero/db/
            self.sqlite_dir = Path.home() / ".zero" / "db"
        if self.backend == "postgres":
            missing = [
                n
                for n in ("postgres_personal_dsn", "postgres_normal_dsn", "postgres_dev_dsn")
                if not getattr(self, n)
            ]
            if missing:
                raise ValueError(
                    f"postgres backend requires all three DSNs; missing: {missing}"
                )
        return self


class TelegramConfig(BaseModel):
    """Telegram bot config. Token is ALWAYS a secret reference."""

    model_config = ConfigDict(extra="forbid")

    bot_token: Annotated[str, Field(description="secret://env/TELEGRAM_BOT_TOKEN")]
    bot_username: str | None = None
    webhook_url: str | None = None  # if None → long-polling
    webhook_secret: str | None = None  # also a secret ref if set
    allowed_updates: list[str] = Field(
        default_factory=lambda: ["message", "edited_message", "callback_query"]
    )
    drop_pending_updates: bool = False

    @field_validator("bot_token")
    @classmethod
    def _bot_token_must_be_ref(cls, v: str) -> str:
        if not is_secret_ref(v):
            raise ValueError(
                "telegram.bot_token must be a secret:// reference, got raw value. "
                "Use: secret://env/TELEGRAM_BOT_TOKEN"
            )
        return v

    @field_validator("webhook_secret")
    @classmethod
    def _webhook_secret_must_be_ref(cls, v: str | None) -> str | None:
        if v is not None and not is_secret_ref(v):
            raise ValueError("telegram.webhook_secret must be a secret:// reference")
        return v


class RouterConfig(BaseModel):
    """Router (LLM gateway) config — ADR 0004, Phase R.

    Zero is a pure HTTP consumer of Router via OpenAI protocol. Zero NEVER
    picks models — structural test enforces this (no model_selection function).

    Provider modes:
        - ``provider="custom"``    → talk to an external Router service at
          ``base_url`` (default; production multi-tenant setup).
        - ``provider="gemini"``    → start a local RouterShim that proxies to
          Google Gemini via the OpenAI-compatible endpoint. ``api_key`` is
          the Gemini API key (``secret://env/GEMINI_API_KEY``).
        - ``provider="openai"``    → local shim → OpenAI direct.
        - ``provider="openrouter"``→ local shim → OpenRouter.
        - ``provider="shim"``      → start the local shim with the configured
          ``base_url`` (for testing / development).
    """

    model_config = ConfigDict(extra="forbid")

    base_url: str = "http://127.0.0.1:8080/v1"
    api_key: Annotated[str, Field(description="secret://env/ZERO_ROUTER_API_KEY")]
    timeout_seconds: float = 60.0
    max_retries: int = 3
    # Optional default model hint (router may still override based on capability)
    default_model: str | None = None
    # Provider selection (default "custom" — talks to external Router service).
    provider: Literal["custom", "gemini", "openai", "openrouter", "shim"] = "custom"
    # When provider != "custom", the shim listens on this port (0 = pick free).
    shim_port: int = 0
    shim_host: str = "127.0.0.1"

    @field_validator("api_key")
    @classmethod
    def _api_key_must_be_ref(cls, v: str) -> str:
        if not is_secret_ref(v):
            raise ValueError("router.api_key must be a secret:// reference")
        return v


class SecurityConfig(BaseModel):
    """Security-related config (Phase 8)."""

    model_config = ConfigDict(extra="forbid")

    approval_timeout_seconds: int = 300  # 5 min default
    approval_mode: Literal["manual", "auto_low_risk"] = "manual"
    session_ttl_seconds: int = 86400  # 24h
    session_max_concurrent_per_user: int = 5
    net_guard_enabled: bool = True
    sbom_required: bool = True  # CI gate
    audit_log_path: Path = Path.home() / ".zero" / "audit.log"


class AgentConfig(BaseModel):
    """Agent runtime config (Phase 7)."""

    model_config = ConfigDict(extra="forbid")

    max_turns: int = 100
    max_concurrent_runs: int = 3
    budget_default_usd: float = 5.0
    budget_warning_threshold: float = 0.8  # 80%
    sandbox_default_memory_mb: int = 512
    sandbox_default_cpu_seconds: int = 600
    sandbox_default_timeout_seconds: int = 1800


class MemoryConfig(BaseModel):
    """Memory system config (Phase 6)."""

    model_config = ConfigDict(extra="forbid")

    scratch_retention_days: int = 30
    max_retrieval_tokens: int = 4000
    semantic_search_enabled: bool = False  # extension
    fact_promotion_requires_role: str = "maintainer"


class LoggingConfig(BaseModel):
    """Structured logging config."""

    model_config = ConfigDict(extra="forbid")

    level: Literal["debug", "info", "warning", "error", "critical"] = "info"
    format: Literal["json", "console"] = "json"
    redact_secrets: bool = True
    log_user_message_content: bool = False  # privacy: never log user content at INFO+
    log_dir: Path = Path.home() / ".zero" / "logs"


class PlatformConfig(BaseModel):
    """Platform (future website) integration config — Phase P contracts only."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    base_url: str | None = None
    # Outbound auth (HMAC signature) — extension
    outbound_key_ref: str | None = None
    # Long-poll endpoint for remote commands
    long_poll_url: str | None = None
    long_poll_timeout_seconds: int = 30


class TelemetryConfig(BaseModel):
    """Telemetry config (Phase 10). Default OFF."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    installation_id_file: Path = Path.home() / ".zero" / "installation_id"
    endpoint_url: str | None = None


# ---------------------------------------------------------------------- top-level config

class ZeroConfig(BaseModel):
    """Top-level Zero v2 config, validated at startup."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    installation_id: str | None = None  # set on first run, persisted
    instance_id: str | None = None  # zi_<ulid>, set on first run

    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    telegram: TelegramConfig | None = None
    router: RouterConfig | None = None
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    platform: PlatformConfig = Field(default_factory=PlatformConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)


# ---------------------------------------------------------------------- loaders

def _load_yaml_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML in {path}: {e}") from e
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"top-level config in {path} must be a mapping, got {type(data).__name__}")
    return data


def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    """Walk ZERO_<SECTION>__<KEY> env vars and merge into ``data``.

    Only env vars with the nesting separator (``__``) are applied as config
    overrides. Env vars like ``ZERO_ROUTER_API_KEY`` (single underscore) are
    NOT valid config overrides — they're either test fixtures or unrelated
    env vars that happen to start with ``ZERO_``.
    """
    for env_name, env_value in os.environ.items():
        if not env_name.startswith(ENV_PREFIX):
            continue
        # ZERO_DATABASE__BACKEND=postgres → data["database"]["backend"] = "postgres"
        path_str = env_name[len(ENV_PREFIX):].lower()
        # Require the nesting separator — single-underscore vars are NOT config.
        if ENV_NESTING_SEP not in path_str:
            continue
        parts = path_str.split(ENV_NESTING_SEP)
        if not parts:
            continue
        # Walk into data, creating dicts as needed.
        cursor: dict[str, Any] = data
        for part in parts[:-1]:
            existing = cursor.get(part)
            if not isinstance(existing, dict):
                existing = {}
                cursor[part] = existing
            cursor = existing
        cursor[parts[-1]] = env_value
    return data


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``overlay`` into ``base`` (overlay wins)."""
    result = dict(base)
    for k, v in overlay.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_config(
    config_paths: tuple[Path, ...] | None = None,
    *,
    overrides: dict[str, Any] | None = None,
    resolver: SecretResolver | None = None,
) -> ZeroConfig:
    """Load config from all sources in priority order and validate.

    Raises :class:`ConfigError` if validation fails.
    """
    paths = config_paths if config_paths is not None else DEFAULT_CONFIG_PATHS

    # Start from defaults (lowest priority).
    data: dict[str, Any] = {}

    # Apply files in order (system → user — last wins).
    for path in reversed(paths):
        file_data = _load_yaml_file(path)
        data = _deep_merge(data, file_data)

    # Apply env overrides (higher priority).
    data = _apply_env_overrides(data)

    # Apply programmatic overrides (highest priority).
    if overrides:
        data = _deep_merge(data, overrides)

    try:
        cfg = ZeroConfig.model_validate(data)
    except Exception as e:
        raise ConfigError(f"config validation failed: {e}") from e

    # If secrets need to be checked at load time (rare — usually deferred to use),
    # verify each secret_ref field has a resolver and exists. We do NOT resolve
    # values here (ADR 0007 §2: "value resolved in memory at moment of use").
    if resolver is not None:
        _verify_secret_refs(cfg, resolver)

    return cfg


def _verify_secret_refs(cfg: ZeroConfig, resolver: SecretResolver) -> None:
    """Verify that all secret:// references point to existing secrets.

    Does NOT load the values into memory — just confirms they exist.
    Raises ``ConfigError`` on any missing secret.
    """
    refs: list[str] = []
    if cfg.telegram is not None:
        refs.append(cfg.telegram.bot_token)
        if cfg.telegram.webhook_secret:
            refs.append(cfg.telegram.webhook_secret)
    if cfg.router is not None:
        refs.append(cfg.router.api_key)
    if cfg.platform.outbound_key_ref:
        refs.append(cfg.platform.outbound_key_ref)

    for ref in refs:
        if not resolver.exists(ref):
            raise ConfigError(
                f"secret reference {ref!r} does not resolve — "
                f"set the underlying secret before starting Zero"
            )


# ---------------------------------------------------------------------- global cache

@lru_cache(maxsize=1)
def _get_cached_config() -> ZeroConfig:
    return load_config(resolver=CompositeSecretResolver())


def get_config() -> ZeroConfig:
    """Return the process-wide cached config (loaded once).

    First call loads; subsequent calls return the cached instance.
    Use :func:`reset_config_cache` for tests.
    """
    return _get_cached_config()


def reset_config_cache() -> None:
    """Force the next :func:`get_config` call to re-load."""
    _get_cached_config.cache_clear()


# ---------------------------------------------------------------------- convenience

def resolve_config_secret(ref: str) -> SecretValue:
    """Convenience: resolve a secret reference using the default resolver.

    Raises :class:`SecretNotFoundError` if the secret is missing.
    """
    resolver = CompositeSecretResolver()
    try:
        return resolver.resolve(ref)
    except SecretError:
        raise
