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
SandboxExecutor = Literal["none", "docker", "firejail"]
TelegramMode = Literal["bot_api", "user_session"]

#: The default database backend. PostgreSQL URLs are accepted when the
#: ``[pg]`` extra is installed (GAP 2); anything else is refused at
#: configuration load time so that accepted configuration always
#: matches runtime capability.
SUPPORTED_DATABASE_SCHEME = "sqlite"
POSTGRESQL_DATABASE_SCHEMES: Final[tuple[str, ...]] = ("postgresql", "postgres")

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


#: Proxy URL schemes accepted for ZERO_TELEGRAM_PROXY_URL. socks5h
#: (hostname resolved by the proxy) matters on filtered networks where
#: local DNS itself poisons api.telegram.org.
_TELEGRAM_PROXY_SCHEMES: Final[tuple[str, ...]] = ("http", "https", "socks5", "socks5h")


def mask_proxy_credentials(proxy_url: str | None) -> str | None:
    """Mask userinfo credentials in a proxy URL for log-safe rendering.

    ``http://user:secret@host:port`` renders as ``http://user:***@host:port``
    (and the username as well when absent). ``None`` passes through.
    """
    if not proxy_url:
        return proxy_url
    try:
        parts = urlsplit(proxy_url)
    except ValueError:
        return "[UNPARSEABLE_PROXY]"
    if not parts.netloc or "@" not in parts.netloc:
        return proxy_url
    userinfo, _, hostport = parts.netloc.rpartition("@")
    user = userinfo.split(":", 1)[0]
    return parts._replace(netloc=f"{user or 'user'}:***@{hostport}").geturl()


def _validate_telegram_proxy(raw_value: str | None) -> str | None:
    """Validate and normalize ZERO_TELEGRAM_PROXY_URL (fail-closed)."""
    value = (raw_value or "").strip()
    if not value:
        return None
    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise ConfigError(
            "ZERO_TELEGRAM_PROXY_URL is not a parseable URL — expected e.g. "
            "socks5://127.0.0.1:1080 or http://127.0.0.1:8080."
        ) from exc
    scheme = (parts.scheme or "").lower()
    if scheme not in _TELEGRAM_PROXY_SCHEMES:
        raise ConfigError(
            f"ZERO_TELEGRAM_PROXY_URL scheme must be one of "
            f"{', '.join(_TELEGRAM_PROXY_SCHEMES)}; got {scheme!r}."
        )
    if not parts.hostname:
        raise ConfigError(
            "ZERO_TELEGRAM_PROXY_URL must include a host — expected e.g. "
            "socks5://127.0.0.1:1080."
        )
    if scheme in ("socks5", "socks5h"):
        try:
            import socksio  # noqa: F401 - availability probe only
        except ImportError as exc:
            raise ConfigError(
                "ZERO_TELEGRAM_PROXY_URL uses socks5 but the socks extra is "
                "not installed. Next action: pip install \"httpx[socks]\" "
                "(or use an http:// proxy)."
            ) from exc
    return value


class Settings(BaseModel):
    """Validated runtime configuration.

    Construct via :meth:`Settings.load` (from env vars) or
    :meth:`Settings.load_for_test` (explicit kwargs, forced to test mode).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    zero_env: Environment
    database_url: str
    #: GAP 2: PostgreSQL connection pool bounds (``ZERO_PG_POOL_MIN/MAX``).
    pg_pool_min: int = 2
    pg_pool_max: int = 20
    #: GAP 4: Bot API (default) or explicit user-session opt-in. The
    #: user-session path additionally requires the [session] extra;
    #: composition refuses the combination otherwise.
    telegram_mode: TelegramMode = "bot_api"
    log_level: LogLevel = "INFO"
    secret_key: SecretStr | None = None
    auth_required: bool = False
    bootstrap_token: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"
    #: Ordered same-provider fallback models for ``send_request_with_fallback``
    #: (Hermes parity, audit 2026-08-28). Comma-separated via
    #: ``ZERO_OPENAI_FALLBACK_MODELS``; empty keeps the historical
    #: single-model behavior. Matches the routing contract the setup
    #: wizard writes to config.yaml (``routing.fallback_models``).
    openai_fallback_models: tuple[str, ...] = ()
    openai_timeout_seconds: float = 60.0
    anthropic_api_key: SecretStr | None = None
    anthropic_base_url: str = "https://api.anthropic.com"
    anthropic_model: str = "claude-sonnet-4"
    anthropic_timeout_seconds: float = 60.0
    #: Automatic requeue budget for failed tasks (0 disables auto-retry).
    task_max_attempts: int = 0
    #: Optional JSONL evidence sink for S7 decomposition recovery
    #: analytics (per-model typo rates). Empty disables the sink.
    decomposition_analytics_path: str = ""
    #: GAP 8b/G2 Hermes-parity per-call tool approval gate.
    #: ``off`` (default) keeps plan-level-only authorization;
    #: ``manual`` consults ToolApprovalGate before every declared
    #: tool call inside task executions (hardline floor always on).
    tool_approval_mode: str = "off"
    #: GAP 8b/G3 scheduler fan-out: how many independent executions a
    #: single tick drains concurrently (1 = historical serial ticks).
    #: Within one execution, dependency order stays strictly serial.
    tick_parallel_executions: int = 1
    #: Total dispatch attempts per provider request (first call +
    #: in-process retries of transient/rate-limit failures). Reference
    #: parity: Hermes defaults to retrying before failing over.
    provider_max_attempts: int = 2
    telegram_webhook_secret: SecretStr | None = None
    #: Optional outbound proxy for Telegram Bot API traffic only
    #: (polling + sendMessage). Accepts http://, https://, socks5:// and
    #: socks5h:// URLs (socks requires the httpx[socks] extra). Standard
    #: HTTPS_PROXY/ALL_PROXY environment variables are honored by httpx
    #: independently of this setting. Credentials in the URL are masked
    #: in every log/repr path.
    telegram_proxy_url: str | None = None
    discord_application_public_key: SecretStr | None = None
    # Host-bounded execution is deliberately test/development-only. Production
    # remains disabled until a genuine sandbox backend is configured.
    worktree_isolation_mode: WorktreeIsolationMode = "disabled"
    #: GAP 3: the genuine isolation backend used for worktree commands.
    #: ``none`` keeps production refusal; ``docker``/``firejail`` enable
    #: sandboxed execution (probed at composition, fail closed).
    sandbox_executor: SandboxExecutor = "none"
    sandbox_image: str = "python:3.12-slim"
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
    # Per-task evidence verification command (run by the agent runtime's
    # evidence collector when a task's expected_evidence requires a test
    # report/exit status). Empty = test evidence cannot be proven at the
    # runtime level and requesting it fails closed with an explicit
    # configuration hint. Must name a binary the worktree command policy
    # allowlists, e.g. "python3 -m unittest discover -s tests -v".
    evidence_test_command: tuple[str, ...] = ()

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
            f"telegram_proxy_url={mask_proxy_credentials(self.telegram_proxy_url)!r}, "
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
    def load(
        cls,
        *,
        env_file: Path | str | None = None,
        zero_env_fallback: str | None = None,
    ) -> Settings:
        """Load settings from environment variables.

        Args:
            env_file: Optional path to a ``.env`` file for local
                development. Ignored if the file does not exist. When
                ``None``, ``$ZERO_HOME/.env`` is used automatically —
                bug fix (2026-08-29): the engine entry points
                (``zero.main``, spawned by ``zero start``) never passed
                a path, so the file ``zero setup``/``_ensure_*_key``
                persist values into was silently NEVER read. Process
                environment variables still take precedence over file
                values.
            zero_env_fallback: Optional environment to assume when
                ``ZERO_ENV`` is not set anywhere (process env or env
                file). Only ``"development"`` is accepted — production
                must always be configured explicitly so the fail-closed
                rules stay in charge. Developer-facing entry points
                (``zero-develop serve``, ``zero.main``) pass this so a
                bare start works; validation commands such as
                ``check-config`` keep loading strictly.

        Raises:
            ConfigError: if any fail-closed rule is violated.
        """
        # Bug fix (2026-08-29): when the caller passes no explicit .env
        # path, default to $ZERO_HOME/.env — the one file the setup
        # wizard and the key bootstraps persist engine-critical values
        # (ZERO_SECRET_KEY, pinned ZERO_DATABASE_URL) into. Without this
        # default those values were invisible to `zero start`/uvicorn,
        # which let the database drift per-CWD until every secret
        # reference in config.yaml failed to resolve.
        if env_file is None:
            import os
            from pathlib import Path as _Path

            _home = _Path(os.environ.get("ZERO_HOME", str(_Path.home() / ".zero")))
            _default_env = _home / ".env"
            if _default_env.is_file():
                env_file = _default_env
        raw = _read_env(env_file)
        zero_env = raw.get("ZERO_ENV")
        if zero_env is None:
            if zero_env_fallback == "development":
                raw["ZERO_ENV"] = zero_env = zero_env_fallback
            elif zero_env_fallback is not None:
                raise ConfigError(
                    f"Invalid ZERO_ENV fallback {zero_env_fallback!r}: only 'development' "
                    "may be assumed automatically. Configure ZERO_ENV explicitly."
                )
            else:
                raise ConfigError(
                    "ZERO_ENV is required (one of: development, test, "
                    "production). Next action: run 'export ZERO_ENV=development' "
                    "for a local server, or run 'zero setup' to write $ZERO_HOME/.env."
                )
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

        # Structural validation that needs no optional dependency runs
        # before capability gates: a pool-bound mistake must surface as
        # a POOL_MIN/MAX error rather than an instruction to install
        # the [pg] extra, which would mask the real misconfiguration on
        # hosts where psycopg is absent.
        pg_pool_min = _read_pool_size(raw, "ZERO_PG_POOL_MIN", 2)
        pg_pool_max = _read_pool_size(raw, "ZERO_PG_POOL_MAX", 20)
        if pg_pool_min > pg_pool_max:
            raise ConfigError("ZERO_PG_POOL_MIN must not exceed ZERO_PG_POOL_MAX.")

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

        sandbox_executor = raw.get("ZERO_SANDBOX_EXECUTOR", "none").strip().lower()
        if sandbox_executor not in {"none", "docker", "firejail"}:
            raise ConfigError("ZERO_SANDBOX_EXECUTOR must be one of none, docker, firejail.")
        sandbox_image = raw.get("ZERO_SANDBOX_IMAGE", "python:3.12-slim").strip()
        if not sandbox_image:
            raise ConfigError("ZERO_SANDBOX_IMAGE must not be empty.")

        openai_key_raw = raw.get("ZERO_OPENAI_API_KEY")
        openai_api_key = SecretStr(openai_key_raw) if openai_key_raw else None
        openai_base_url = raw.get("ZERO_OPENAI_BASE_URL", "https://api.openai.com/v1")
        openai_model = raw.get("ZERO_OPENAI_MODEL", "gpt-4o-mini")
        if not openai_model.strip():
            raise ConfigError("ZERO_OPENAI_MODEL must not be empty.")
        # Ordered same-provider fallback models (Hermes parity). Entries
        # are trimmed and de-duplicated; the primary model itself is
        # dropped from the list at composition time (it is always tried
        # first) rather than being an error.
        fallback_models_raw = str(raw.get("ZERO_OPENAI_FALLBACK_MODELS", "") or "")
        openai_fallback_models = tuple(
            dict.fromkeys(  # de-duplicate, preserve order
                part.strip() for part in fallback_models_raw.split(",") if part.strip()
            )
        )
        openai_fallback_models = tuple(
            name for name in openai_fallback_models if name != openai_model.strip()
        )
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

        decomposition_analytics_path = str(
            raw.get("ZERO_DECOMPOSITION_ANALYTICS_PATH", "") or ""
        ).strip()

        tool_approval_mode = str(raw.get("ZERO_TOOL_APPROVAL_MODE", "off") or "off")
        tool_approval_mode = tool_approval_mode.strip().lower()
        if tool_approval_mode not in ("off", "manual"):
            raise ConfigError("ZERO_TOOL_APPROVAL_MODE must be 'off' or 'manual'.")

        tick_parallel_raw = str(raw.get("ZERO_TICK_PARALLEL_EXECUTIONS", "1"))
        try:
            tick_parallel_executions = int(tick_parallel_raw)
        except ValueError as exc:
            raise ConfigError("ZERO_TICK_PARALLEL_EXECUTIONS must be an integer.") from exc
        if tick_parallel_executions < 1 or tick_parallel_executions > 8:
            raise ConfigError("ZERO_TICK_PARALLEL_EXECUTIONS must be between 1 and 8.")

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
        # Optional Telegram-only egress proxy (filtered networks). Validated
        # here so a bad scheme/missing socks extra fails at boot with an
        # actionable message instead of a mystery transport error later.
        telegram_proxy_url = _validate_telegram_proxy(raw.get("ZERO_TELEGRAM_PROXY_URL"))

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
        evidence_test_command = tuple(
            item.strip()
            for item in (raw.get("ZERO_EVIDENCE_TEST_COMMAND") or "").split()
            if item.strip()
        )

        telegram_mode = raw.get("ZERO_TELEGRAM_MODE", "bot_api").strip().lower()
        if telegram_mode not in {"bot_api", "user_session"}:
            raise ConfigError("ZERO_TELEGRAM_MODE must be 'bot_api' or 'user_session'.")

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
            openai_fallback_models=openai_fallback_models,
            openai_timeout_seconds=openai_timeout_seconds,
            anthropic_api_key=anthropic_api_key,
            anthropic_base_url=anthropic_base_url,
            anthropic_model=anthropic_model,
            anthropic_timeout_seconds=anthropic_timeout_seconds,
            task_max_attempts=task_max_attempts,
            provider_max_attempts=provider_max_attempts,
            decomposition_analytics_path=decomposition_analytics_path,
            tool_approval_mode=tool_approval_mode,
            tick_parallel_executions=tick_parallel_executions,
            telegram_webhook_secret=telegram_webhook_secret,
            telegram_proxy_url=telegram_proxy_url,
            discord_application_public_key=discord_application_public_key,
            worktree_isolation_mode=isolation_mode,  # type: ignore[arg-type]
            sandbox_executor=sandbox_executor,  # type: ignore[arg-type]
            sandbox_image=sandbox_image,
            worktree_allowed_commands=allowed_commands,
            worktree_root=raw.get("ZERO_WORKTREE_ROOT", DEFAULT_WORKTREE_ROOT),
            workers_enabled=workers_enabled,
            scheduler_interval_seconds=scheduler_interval_seconds,
            delivery_interval_seconds=delivery_interval_seconds,
            polling_interval_seconds=polling_interval_seconds,
            combined_test_command=combined_test_command,
            combined_test_timeout_seconds=combined_test_timeout_seconds,
            evidence_test_command=evidence_test_command,
            pg_pool_min=pg_pool_min,
            pg_pool_max=pg_pool_max,
            telegram_mode=telegram_mode,  # type: ignore[arg-type]
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
        # GAP 3: production permits host_bounded *mode* only when a
        # genuine sandbox backend is selected; the backend itself is
        # probed (fail closed) at composition time.
        if self.worktree_isolation_mode == "host_bounded" and self.sandbox_executor == "none":
            raise ConfigError(
                "host-bounded worktree execution is not permitted in production; "
                "configure a genuine isolation backend "
                "(ZERO_SANDBOX_EXECUTOR=docker|firejail) before enabling commands."
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


def _read_pool_size(raw: dict[str, str], key: str, default: int) -> int:
    """Read a bounded pool-size integer (GAP 2)."""
    value_raw = raw.get(key)
    if value_raw is None or not value_raw.strip():
        return default
    try:
        value = int(value_raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer.") from exc
    if not 1 <= value <= 100:
        raise ConfigError(f"{key} must be between 1 and 100.")
    return value


def _require_supported_database_scheme(database_url: str) -> None:
    """Refuse database URLs whose backend this release does not ship.

    Configuration acceptance and runtime capability must agree. SQLite
    is always available; PostgreSQL URLs are accepted only when the
    ``[pg]`` extra (psycopg) is importable — otherwise the load fails
    closed with an actionable error instead of a late runtime failure.
    """
    scheme = database_url.split(":", 1)[0].strip().lower()
    if scheme == SUPPORTED_DATABASE_SCHEME:
        return
    if scheme in POSTGRESQL_DATABASE_SCHEMES:
        if _postgres_driver_available():
            return
        raise ConfigError(
            "ZERO_DATABASE_URL uses PostgreSQL but the [pg] extra is not "
            "installed; install it with: pip install 'zero-develop[pg]'"
        )
    supported = f"{SUPPORTED_DATABASE_SCHEME!r}"
    if _postgres_driver_available():
        supported += " and 'postgresql'"
    raise ConfigError(
        f"Unsupported database URL scheme {scheme!r}: this release implements "
        f"{supported}. Configure a supported database URL."
    )


def _postgres_driver_available() -> bool:
    """Probe psycopg importability once per process."""
    global _PG_DRIVER_AVAILABLE
    if _PG_DRIVER_AVAILABLE is None:
        try:
            import psycopg  # noqa: F401

            _PG_DRIVER_AVAILABLE = True
        except ImportError:
            _PG_DRIVER_AVAILABLE = False
    return _PG_DRIVER_AVAILABLE


_PG_DRIVER_AVAILABLE: bool | None = None


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
