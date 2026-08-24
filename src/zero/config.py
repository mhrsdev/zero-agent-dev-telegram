"""Configuration as a trust boundary.

See ADR 0004 (``docs/decisions/0004-configuration-as-trust-boundary.md``)
for the full rationale. Summary:

- One typed, validated, immutable :class:`Settings` instance.
- Loaded from environment variables (with optional ``.env`` for local
  development).
- Fail-closed: missing security-critical values raise :class:`ConfigError`
  rather than selecting unsafe defaults.
- Test/production overlap is refused at load time.
- Secrets are referenced, never embedded in logs or audit records.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Final, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, SecretStr

Environment = Literal["development", "test", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
WorktreeIsolationMode = Literal["disabled", "host_bounded"]

#: The only database backend this release implements and tests.
#: PostgreSQL/MySQL/etc. URLs are refused at configuration load time so
#: that accepted configuration always matches runtime capability (the
#: application factory must never fail with a late ``Unsupported
#: database URL`` error after settings validation passed).
SUPPORTED_DATABASE_SCHEME = "sqlite"

# A path that contains any of these substrings is refused when
# ZERO_ENV=test, to prevent a test config from pointing at a production
# database by accident.
_PRODUCTION_PATH_HINTS: Final[tuple[str, ...]] = ("prod", "production")

# A path that looks like the development default or in-memory db is
# refused when ZERO_ENV=production, to prevent production from
# accidentally running on a throwaway database.
_DEVELOPMENT_PATH_HINTS: Final[tuple[str, ...]] = (
    ":memory:",
    "sqlite://./",
    "sqlite:///./",
)

_MIN_SECRET_KEY_BYTES: Final[int] = 32

#: Cross-platform default worktree root. ``/tmp`` is not absolute on
#: every platform (for example Windows), so the default resolves to the
#: platform temp directory at import time.
DEFAULT_WORKTREE_ROOT: Final[str] = str(Path(tempfile.gettempdir()) / "zero-worktrees")


class ConfigError(RuntimeError):
    """Raised when configuration is missing, ambiguous, or unsafe.

    This is a typed domain failure (per ``zero-control-plane-trust``
    §"Failure shapes teach the boundary"). The application must not start
    if a :class:`ConfigError` is raised at startup.
    """


class Settings(BaseModel):
    """Validated runtime configuration.

    Construct via :meth:`Settings.load` (from env vars) or
    :meth:`Settings.load_for_test` (explicit kwargs, forced to test mode).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    zero_env: Environment
    database_url: str
    log_level: LogLevel = "INFO"
    secret_key: SecretStr | None = None
    auth_required: bool = False
    bootstrap_token: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"
    openai_timeout_seconds: float = 60.0
    anthropic_api_key: SecretStr | None = None
    anthropic_base_url: str = "https://api.anthropic.com"
    anthropic_model: str = "claude-sonnet-4"
    anthropic_timeout_seconds: float = 60.0
    #: Automatic requeue budget for failed tasks (0 disables auto-retry).
    task_max_attempts: int = 0
    #: Total dispatch attempts per provider request (first call +
    #: in-process retries of transient/rate-limit failures). Reference
    #: parity: Hermes defaults to retrying before failing over.
    provider_max_attempts: int = 2
    telegram_webhook_secret: SecretStr | None = None
    discord_application_public_key: SecretStr | None = None
    # Host-bounded execution is deliberately test/development-only. Production
    # remains disabled until a genuine sandbox backend is configured.
    worktree_isolation_mode: WorktreeIsolationMode = "disabled"
    worktree_allowed_commands: tuple[str, ...] = ()
    worktree_root: str = DEFAULT_WORKTREE_ROOT

    # Managed background workers. The scheduler worker drains approved
    # handoffs and ready tasks, the delivery worker drains result
    # deliveries, and the polling worker hosts Telegram long-polling.
    # Tests default to disabled so the ASGI app never runs autonomous
    # work unless a test explicitly opts in.
    workers_enabled: bool = True
    scheduler_interval_seconds: float = 5.0
    delivery_interval_seconds: float = 2.0
    polling_interval_seconds: float = 1.0
    combined_test_command: tuple[str, ...] = ()
    combined_test_timeout_seconds: int = 300

    # ------------------------------------------------------------------
    # Derived properties
    # ------------------------------------------------------------------

    @property
    def is_test(self) -> bool:
        return self.zero_env == "test"

    @property
    def is_production(self) -> bool:
        return self.zero_env == "production"

    @property
    def is_development(self) -> bool:
        return self.zero_env == "development"

    @property
    def is_in_memory_db(self) -> bool:
        return ":memory:" in self.database_url

    # ------------------------------------------------------------------
    # Redaction
    # ------------------------------------------------------------------

    def safe_repr(self) -> str:
        """Return a log-safe representation with secrets redacted."""
        secret_repr = "[REDACTED]" if self.secret_key else "None"
        bootstrap_repr = "[REDACTED]" if self.bootstrap_token else "None"
        provider_repr = "[REDACTED]" if self.openai_api_key else "None"
        return (
            f"Settings(zero_env={self.zero_env!r}, "
            f"database_url={self.database_url!r}, "
            f"log_level={self.log_level!r}, "
            f"secret_key={secret_repr}, "
            f"auth_required={self.auth_required!r}, "
            f"bootstrap_token={bootstrap_repr}, "
            f"openai_api_key={provider_repr}, "
            f"openai_base_url={self.openai_base_url!r}, "
            f"openai_model={self.openai_model!r}, "
            f"openai_timeout_seconds={self.openai_timeout_seconds!r}, "
            f"telegram_webhook_secret={'[REDACTED]' if self.telegram_webhook_secret else 'None'}, "
            f"discord_application_public_key={'[REDACTED]' if self.discord_application_public_key else 'None'}, "
            f"worktree_isolation_mode={self.worktree_isolation_mode!r}, "
            f"worktree_allowed_commands={self.worktree_allowed_commands!r}, "
            f"worktree_root={self.worktree_root!r}, "
            f"workers_enabled={self.workers_enabled!r}, "
            f"scheduler_interval_seconds={self.scheduler_interval_seconds!r}, "
            f"delivery_interval_seconds={self.delivery_interval_seconds!r}, "
            f"polling_interval_seconds={self.polling_interval_seconds!r})"
        )

    def __repr__(self) -> str:  # pragma: no cover - defensive
        # Pydantic's default __repr__ would print SecretStr as
        # SecretStr('**********') which is fine, but we want to be
        # explicit that we never leak the raw value.
        return self.safe_repr()

    # ------------------------------------------------------------------
    # Loaders
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, *, env_file: Path | str | None = None) -> Settings:
        """Load settings from environment variables.

        Args:
            env_file: Optional path to a ``.env`` file for local
                development. Ignored if the file does not exist.

        Raises:
            ConfigError: if any fail-closed rule is violated.
        """
        raw = _read_env(env_file)
        zero_env = raw.get("ZERO_ENV")
        if zero_env is None:
            raise ConfigError("ZERO_ENV is required (one of: development, test, production).")
        if zero_env not in ("development", "test", "production"):
            raise ConfigError(
                f"ZERO_ENV must be one of development, test, production; got {zero_env!r}."
            )

        database_url = raw.get("ZERO_DATABASE_URL")
        if database_url is None:
            if zero_env == "test":
                database_url = "sqlite::memory:"
            elif zero_env == "development":
                database_url = "sqlite:///./zero_develop.db"
            else:
                raise ConfigError("ZERO_DATABASE_URL is required in production.")

        # Normalize sqlite:// prefixes to a canonical form.
        database_url = _normalize_sqlite_url(database_url)
        _require_supported_database_scheme(database_url)

        log_level = (raw.get("ZERO_LOG_LEVEL") or "INFO").upper()
        if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            raise ConfigError(
                f"ZERO_LOG_LEVEL must be one of DEBUG, INFO, WARNING, ERROR; got {log_level!r}."
            )

        secret_key_raw = raw.get("ZERO_SECRET_KEY")
        secret_key: SecretStr | None = None
        if secret_key_raw:
            secret_key = SecretStr(secret_key_raw)

        auth_raw = raw.get("ZERO_AUTH_REQUIRED")
        if auth_raw is None:
            auth_required = zero_env == "production"
        elif auth_raw.strip().lower() in {"1", "true", "yes", "on"}:
            auth_required = True
        elif auth_raw.strip().lower() in {"0", "false", "no", "off"}:
            auth_required = False
        else:
            raise ConfigError("ZERO_AUTH_REQUIRED must be true or false.")

        bootstrap_raw = raw.get("ZERO_BOOTSTRAP_TOKEN")
        bootstrap_token = SecretStr(bootstrap_raw) if bootstrap_raw else None
        allowed_commands = tuple(
            sorted(
                {
                    item.strip()
                    for item in (raw.get("ZERO_WORKTREE_ALLOWED_COMMANDS") or "").split(",")
                    if item.strip()
                }
            )
        )

        isolation_mode = raw.get("ZERO_WORKTREE_ISOLATION_MODE", "disabled").strip().lower()
        if isolation_mode not in {"disabled", "host_bounded"}:
            raise ConfigError("ZERO_WORKTREE_ISOLATION_MODE must be 'disabled' or 'host_bounded'.")

        openai_key_raw = raw.get("ZERO_OPENAI_API_KEY")
        openai_api_key = SecretStr(openai_key_raw) if openai_key_raw else None
        openai_base_url = raw.get("ZERO_OPENAI_BASE_URL", "https://api.openai.com/v1")
        openai_model = raw.get("ZERO_OPENAI_MODEL", "gpt-4o-mini")
        if not openai_model.strip():
            raise ConfigError("ZERO_OPENAI_MODEL must not be empty.")
        timeout_raw = raw.get("ZERO_OPENAI_TIMEOUT_SECONDS", "60")
        try:
            openai_timeout_seconds = float(timeout_raw)
        except ValueError as exc:
            raise ConfigError("ZERO_OPENAI_TIMEOUT_SECONDS must be a positive number.") from exc
        if openai_timeout_seconds <= 0:
            raise ConfigError("ZERO_OPENAI_TIMEOUT_SECONDS must be a positive number.")

        anthropic_key_raw = raw.get("ZERO_ANTHROPIC_API_KEY")
        anthropic_api_key = SecretStr(anthropic_key_raw) if anthropic_key_raw else None
        anthropic_base_url = raw.get("ZERO_ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip(
            "/"
        )
        parsed_anthropic = urlsplit(anthropic_base_url)
        if parsed_anthropic.scheme not in {"http", "https"} or not parsed_anthropic.netloc:
            raise ConfigError("ZERO_ANTHROPIC_BASE_URL must be an absolute HTTP(S) URL.")
        anthropic_model = raw.get("ZERO_ANTHROPIC_MODEL", "claude-sonnet-4")
        if not anthropic_model.strip():
            raise ConfigError("ZERO_ANTHROPIC_MODEL must not be empty.")
        try:
            anthropic_timeout_seconds = float(raw.get("ZERO_ANTHROPIC_TIMEOUT_SECONDS", "60"))
        except ValueError as exc:
            raise ConfigError("ZERO_ANTHROPIC_TIMEOUT_SECONDS must be a positive number.") from exc
        if anthropic_timeout_seconds <= 0:
            raise ConfigError("ZERO_ANTHROPIC_TIMEOUT_SECONDS must be a positive number.")

        attempts_raw = raw.get("ZERO_TASK_MAX_ATTEMPTS", "0")
        try:
            task_max_attempts = int(attempts_raw)
        except ValueError as exc:
            raise ConfigError("ZERO_TASK_MAX_ATTEMPTS must be an integer.") from exc
        if task_max_attempts < 0 or task_max_attempts > 16:
            raise ConfigError("ZERO_TASK_MAX_ATTEMPTS must be between 0 and 16.")

        provider_attempts_raw = raw.get("ZERO_PROVIDER_MAX_ATTEMPTS", "2")
        try:
            provider_max_attempts = int(provider_attempts_raw)
        except ValueError as exc:
            raise ConfigError("ZERO_PROVIDER_MAX_ATTEMPTS must be an integer.") from exc
        if provider_max_attempts < 1 or provider_max_attempts > 8:
            raise ConfigError("ZERO_PROVIDER_MAX_ATTEMPTS must be between 1 and 8.")

        telegram_webhook_secret_raw = raw.get("ZERO_TELEGRAM_WEBHOOK_SECRET")
        telegram_webhook_secret = (
            SecretStr(telegram_webhook_secret_raw) if telegram_webhook_secret_raw else None
        )
        discord_public_key_raw = raw.get("ZERO_DISCORD_APPLICATION_PUBLIC_KEY")
        discord_application_public_key = (
            SecretStr(discord_public_key_raw) if discord_public_key_raw else None
        )

        workers_raw = raw.get("ZERO_WORKERS_ENABLED")
        if workers_raw is None or workers_raw.strip().lower() in {"1", "true", "yes", "on"}:
            workers_enabled = True
        elif workers_raw.strip().lower() in {"0", "false", "no", "off"}:
            workers_enabled = False
        else:
            raise ConfigError("ZERO_WORKERS_ENABLED must be true or false.")
        scheduler_interval_seconds = _read_positive_float(
            raw, "ZERO_SCHEDULER_INTERVAL_SECONDS", 5.0
        )
        delivery_interval_seconds = _read_positive_float(raw, "ZERO_DELIVERY_INTERVAL_SECONDS", 2.0)
        polling_interval_seconds = _read_positive_float(raw, "ZERO_POLLING_INTERVAL_SECONDS", 1.0)
        combined_test_command = tuple(
            item.strip()
            for item in (raw.get("ZERO_COMBINED_TEST_COMMAND") or "").split()
            if item.strip()
        )
        combined_test_timeout_seconds = int(
            _read_positive_float(raw, "ZERO_COMBINED_TEST_TIMEOUT_SECONDS", 300.0)
        )

        settings = cls(
            zero_env=zero_env,  # type: ignore[arg-type]
            database_url=database_url,
            log_level=log_level,  # type: ignore[arg-type]
            secret_key=secret_key,
            auth_required=auth_required,
            bootstrap_token=bootstrap_token,
            openai_api_key=openai_api_key,
            openai_base_url=openai_base_url,
            openai_model=openai_model,
            openai_timeout_seconds=openai_timeout_seconds,
            anthropic_api_key=anthropic_api_key,
            anthropic_base_url=anthropic_base_url,
            anthropic_model=anthropic_model,
            anthropic_timeout_seconds=anthropic_timeout_seconds,
            task_max_attempts=task_max_attempts,
            provider_max_attempts=provider_max_attempts,
            telegram_webhook_secret=telegram_webhook_secret,
            discord_application_public_key=discord_application_public_key,
            worktree_isolation_mode=isolation_mode,  # type: ignore[arg-type]
            worktree_allowed_commands=allowed_commands,
            worktree_root=raw.get("ZERO_WORKTREE_ROOT", DEFAULT_WORKTREE_ROOT),
            workers_enabled=workers_enabled,
            scheduler_interval_seconds=scheduler_interval_seconds,
            delivery_interval_seconds=delivery_interval_seconds,
            polling_interval_seconds=polling_interval_seconds,
            combined_test_command=combined_test_command,
            combined_test_timeout_seconds=combined_test_timeout_seconds,
        )
        settings._enforce_fail_closed_rules()
        return settings

    @classmethod
    def load_for_test(cls, **overrides: Any) -> Settings:
        """Construct settings for tests with explicit kwargs.

        Forces ``zero_env='test'``. Accepts the same fields as
        :class:`Settings`. Any ``zero_env`` passed in ``overrides`` is
        ignored (tests cannot escape the test environment through this
        constructor).
        """
        overrides["zero_env"] = "test"
        if "database_url" not in overrides:
            overrides["database_url"] = "sqlite::memory:"
        if "log_level" not in overrides:
            overrides["log_level"] = "WARNING"
        if "worktree_isolation_mode" not in overrides:
            overrides["worktree_isolation_mode"] = "host_bounded"
        # Tests may deliberately omit secret_key; we only require it in
        # production.
        overrides.setdefault("workers_enabled", False)
        settings = cls(**overrides)
        settings._enforce_fail_closed_rules()
        return settings

    # ------------------------------------------------------------------
    # Fail-closed rules
    # ------------------------------------------------------------------

    def _enforce_fail_closed_rules(self) -> None:
        if self.secret_key is not None and not self.secret_key.get_secret_value().strip():
            raise ConfigError("ZERO_SECRET_KEY must not be blank.")
        _require_supported_database_scheme(self.database_url)
        if self.auth_required:
            if not self.secret_key:
                raise ConfigError("ZERO_SECRET_KEY is required when auth is enabled.")
            if self.bootstrap_token:
                bootstrap = self.bootstrap_token.get_secret_value()
                if len(bootstrap.encode("utf-8")) < _MIN_SECRET_KEY_BYTES:
                    raise ConfigError(
                        f"ZERO_BOOTSTRAP_TOKEN must be at least {_MIN_SECRET_KEY_BYTES} bytes."
                    )
        if self.is_production:
            self._enforce_production_rules()
        if self.is_test:
            self._enforce_test_rules()

    def _enforce_production_rules(self) -> None:
        if not self.auth_required:
            raise ConfigError("ZERO_AUTH_REQUIRED cannot be disabled in production.")
        if not self.secret_key:
            raise ConfigError("ZERO_SECRET_KEY is required in production.")
        # SecretStr.get_secret_value() is the only way to read the raw
        # value; we do this only to check length, never to log.
        raw = self.secret_key.get_secret_value() if self.secret_key else ""
        if len(raw.encode("utf-8")) < _MIN_SECRET_KEY_BYTES:
            raise ConfigError(
                f"ZERO_SECRET_KEY must be at least {_MIN_SECRET_KEY_BYTES} bytes in production."
            )
        # Fail-closed bootstrap: with authentication enabled and no
        # access tokens yet, a deployment without a bootstrap token has
        # no usable initial authentication path at all.
        if not self.database_url:
            raise ConfigError("ZERO_DATABASE_URL is required in production.")
        lowered = self.database_url.lower()
        for hint in _DEVELOPMENT_PATH_HINTS:
            if hint in lowered:
                raise ConfigError(
                    f"ZERO_DATABASE_URL looks like a development or in-memory "
                    f"database ({self.database_url!r}); production refused."
                )
        if not self.bootstrap_token and not allow_manual_provisioning():
            raise ConfigError(
                "ZERO_BOOTSTRAP_TOKEN is required in production so the first "
                "user can be provisioned; set ZERO_ALLOW_MANUAL_PROVISIONING=1 "
                "only when an external provisioning workflow exists."
            )
        if self.bootstrap_token:
            bootstrap = self.bootstrap_token.get_secret_value()
            if len(bootstrap.encode("utf-8")) < _MIN_SECRET_KEY_BYTES:
                raise ConfigError(
                    f"ZERO_BOOTSTRAP_TOKEN must be at least {_MIN_SECRET_KEY_BYTES} bytes."
                )
        if self.worktree_isolation_mode == "host_bounded":
            raise ConfigError(
                "host-bounded worktree execution is not permitted in production; "
                "configure a genuine isolation backend before enabling commands."
            )

    def _enforce_test_rules(self) -> None:
        lowered = self.database_url.lower()
        for hint in _PRODUCTION_PATH_HINTS:
            if hint in lowered:
                raise ConfigError(
                    f"ZERO_DATABASE_URL in test mode contains {hint!r}; "
                    f"refusing to point a test at a production-shaped path."
                )


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

_ENV_VAR_PREFIX = "ZERO_"


def allow_manual_provisioning() -> bool:
    """Return True when the operator explicitly accepted an external
    first-user provisioning workflow instead of a bootstrap token."""
    return (os.environ.get("ZERO_ALLOW_MANUAL_PROVISIONING", "").strip().lower()) in {
        "1",
        "true",
        "yes",
        "on",
    }


def _read_positive_float(raw: dict[str, str], key: str, default: float) -> float:
    """Read a positive float environment value with a default."""
    value_raw = raw.get(key)
    if value_raw is None or not value_raw.strip():
        return default
    try:
        value = float(value_raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a positive number.") from exc
    if value <= 0:
        raise ConfigError(f"{key} must be a positive number.")
    return value


def _require_supported_database_scheme(database_url: str) -> None:
    """Refuse database URLs whose backend this release does not ship.

    Configuration acceptance and runtime capability must agree: a URL
    that would fail later inside :func:`create_app` is rejected here,
    at the trust boundary, with an actionable message.
    """
    scheme = database_url.split(":", 1)[0].strip().lower()
    if scheme != SUPPORTED_DATABASE_SCHEME:
        raise ConfigError(
            f"Unsupported database URL scheme {scheme!r}: this release implements "
            f"{SUPPORTED_DATABASE_SCHEME!r} only. Configure a SQLite database URL; "
            f"production backends require an explicit persistence-layer port."
        )


def _read_env(env_file: Path | str | None) -> dict[str, str]:
    """Read environment variables, optionally layered with a .env file.

    The .env file is for local development convenience only. Real
    process environment variables take precedence over .env values.
    """
    result: dict[str, str] = {}
    # First, .env file (if any) — lowest precedence.
    if env_file is not None:
        path = Path(env_file)
        if path.is_file():
            for key, value in _parse_dotenv(path):
                result[key] = value
    # Then, real env vars — highest precedence.
    for key, value in os.environ.items():
        if key.startswith(_ENV_VAR_PREFIX) or key in {
            "ZERO_ENV",
            "ZERO_DATABASE_URL",
            "ZERO_LOG_LEVEL",
            "ZERO_SECRET_KEY",
        }:
            result[key] = value
    return result


def _parse_dotenv(path: Path) -> list[tuple[str, str]]:
    """A tiny, safe .env parser. Does not support shell expansion.

    We deliberately do not use python-dotenv to avoid pulling in another
    dependency for a Phase 1 convenience feature. The format we support:
    - KEY=VALUE
    - lines starting with # are comments
    - surrounding quotes are stripped
    """
    pairs: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        if key:
            pairs.append((key, value))
    return pairs


def _normalize_sqlite_url(url: str) -> str:
    """Normalize SQLite URLs to a canonical form.

    Accepts:
    - ``sqlite::memory:`` -> ``sqlite::memory:``
    - ``sqlite://:memory:`` -> ``sqlite::memory:``
    - ``sqlite:///:memory:`` -> ``sqlite::memory:``
    - ``sqlite:///./foo.db`` -> ``sqlite:///./foo.db`` (file path kept)
    - ``sqlite:///foo.db`` -> ``sqlite:///foo.db``
    - ``sqlite://foo.db`` -> ``sqlite:///foo.db``
    """
    if not url.startswith("sqlite"):
        return url
    if ":memory:" in url:
        return "sqlite::memory:"
    # Preserve four-slash URLs: after the ``sqlite:///`` scheme prefix the
    # leading slash is the absolute filesystem path.  Collapsing it turns a
    # runner-temp path such as ``sqlite:////tmp/zero.db`` into a checkout-
    # relative ``tmp/zero.db`` path.
    if url.startswith("sqlite:////"):
        path = url[len("sqlite:////") :]
        if not path:
            raise ConfigError(f"Invalid SQLite URL: {url!r}")
        return f"sqlite:////{path}"
    if url.startswith("sqlite:///"):
        path = url[len("sqlite:///") :]
        if not path:
            raise ConfigError(f"Invalid SQLite URL: {url!r}")
        return f"sqlite:///{path}"
    if url.startswith("sqlite://"):
        path = url[len("sqlite://") :].lstrip("/")
        if not path:
            raise ConfigError(f"Invalid SQLite URL: {url!r}")
        return f"sqlite:///{path}"
    if url.startswith("sqlite:"):
        path = url[len("sqlite:") :].lstrip("/")
        if not path:
            raise ConfigError(f"Invalid SQLite URL: {url!r}")
        return f"sqlite:///{path}"
    return url
