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
import re
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, SecretStr

Environment = Literal["development", "test", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]

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
        return (
            f"Settings(zero_env={self.zero_env!r}, "
            f"database_url={self.database_url!r}, "
            f"log_level={self.log_level!r}, "
            f"secret_key={secret_repr}, "
            f"auth_required={self.auth_required!r}, "
            f"bootstrap_token={bootstrap_repr})"
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
            raise ConfigError(
                "ZERO_ENV is required (one of: development, test, production)."
            )
        if zero_env not in ("development", "test", "production"):
            raise ConfigError(
                f"ZERO_ENV must be one of development, test, production; "
                f"got {zero_env!r}."
            )

        database_url = raw.get("ZERO_DATABASE_URL")
        if database_url is None:
            if zero_env == "test":
                database_url = "sqlite::memory:"
            elif zero_env == "development":
                database_url = "sqlite:///./zero_develop.db"
            else:
                raise ConfigError(
                    "ZERO_DATABASE_URL is required in production."
                )

        # Normalize sqlite:// prefixes to a canonical form.
        database_url = _normalize_sqlite_url(database_url)

        log_level = (raw.get("ZERO_LOG_LEVEL") or "INFO").upper()
        if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            raise ConfigError(
                f"ZERO_LOG_LEVEL must be one of DEBUG, INFO, WARNING, ERROR; "
                f"got {log_level!r}."
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

        settings = cls(
            zero_env=zero_env,  # type: ignore[arg-type]
            database_url=database_url,
            log_level=log_level,  # type: ignore[arg-type]
            secret_key=secret_key,
            auth_required=auth_required,
            bootstrap_token=bootstrap_token,
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
        # Tests may deliberately omit secret_key; we only require it in
        # production.
        settings = cls(**overrides)
        settings._enforce_fail_closed_rules()
        return settings

    # ------------------------------------------------------------------
    # Fail-closed rules
    # ------------------------------------------------------------------

    def _enforce_fail_closed_rules(self) -> None:
        if self.auth_required:
            if not self.secret_key:
                raise ConfigError("ZERO_SECRET_KEY is required when auth is enabled.")
            if self.bootstrap_token:
                bootstrap = self.bootstrap_token.get_secret_value()
                if len(bootstrap.encode("utf-8")) < _MIN_SECRET_KEY_BYTES:
                    raise ConfigError(
                        f"ZERO_BOOTSTRAP_TOKEN must be at least "
                        f"{_MIN_SECRET_KEY_BYTES} bytes."
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
                f"ZERO_SECRET_KEY must be at least "
                f"{_MIN_SECRET_KEY_BYTES} bytes in production."
            )
        if not self.database_url:
            raise ConfigError("ZERO_DATABASE_URL is required in production.")
        lowered = self.database_url.lower()
        for hint in _DEVELOPMENT_PATH_HINTS:
            if hint in lowered:
                raise ConfigError(
                    f"ZERO_DATABASE_URL looks like a development or in-memory "
                    f"database ({self.database_url!r}); production refused."
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


_SQLITE_PREFIX_RE = re.compile(r"^sqlite:?/*")


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
    # Strip the sqlite:// prefix and rebuild canonically.
    rest = _SQLITE_PREFIX_RE.sub("", url)
    if not rest:
        raise ConfigError(f"Invalid SQLite URL: {url!r}")
    return f"sqlite:///{rest}"
