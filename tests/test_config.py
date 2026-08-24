"""Configuration validation tests.

Per ADR 0004: configuration is a trust boundary. Missing
security-critical values must fail closed rather than select unsafe
defaults. Test/production overlap is refused at load time.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from zero.config import (
    ConfigError,
    Settings,
    _normalize_sqlite_url,
    _parse_dotenv,
)

# ----------------------------------------------------------------------
# Required ZERO_ENV
# ----------------------------------------------------------------------


def test_missing_zero_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # Remove ZERO_ENV if it happens to be set in the environment.
    monkeypatch.delenv("ZERO_ENV", raising=False)
    monkeypatch.delenv("ZERO_DATABASE_URL", raising=False)
    monkeypatch.delenv("ZERO_SECRET_KEY", raising=False)
    monkeypatch.delenv("ZERO_LOG_LEVEL", raising=False)
    with pytest.raises(ConfigError, match="ZERO_ENV is required"):
        Settings.load()


def test_invalid_zero_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZERO_ENV", "staging")
    with pytest.raises(ConfigError, match="must be one of"):
        Settings.load()


# ----------------------------------------------------------------------
# Fail-closed: production
# ----------------------------------------------------------------------


def test_production_requires_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZERO_ENV", "production")
    monkeypatch.setenv("ZERO_SECRET_KEY", "x" * 64)
    monkeypatch.delenv("ZERO_DATABASE_URL", raising=False)
    with pytest.raises(ConfigError, match="ZERO_DATABASE_URL is required"):
        Settings.load()


def test_production_requires_secret_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZERO_ENV", "production")
    monkeypatch.setenv("ZERO_DATABASE_URL", "sqlite:///var/lib/zero/production.db")
    monkeypatch.delenv("ZERO_SECRET_KEY", raising=False)
    with pytest.raises(ConfigError, match="ZERO_SECRET_KEY is required"):
        Settings.load()


def test_production_refuses_short_secret_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZERO_ENV", "production")
    monkeypatch.setenv("ZERO_DATABASE_URL", "sqlite:///var/lib/zero/production.db")
    monkeypatch.setenv("ZERO_SECRET_KEY", "short")  # < 32 bytes
    with pytest.raises(ConfigError, match="at least 32 bytes"):
        Settings.load()


def test_production_refuses_whitespace_secret_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZERO_ENV", "production")
    monkeypatch.setenv("ZERO_DATABASE_URL", "sqlite:///var/lib/zero/production.db")
    monkeypatch.setenv("ZERO_SECRET_KEY", "   ")
    with pytest.raises(ConfigError, match="must not be blank"):
        Settings.load()


def test_production_refuses_in_memory_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZERO_ENV", "production")
    monkeypatch.setenv("ZERO_SECRET_KEY", "x" * 64)
    monkeypatch.setenv("ZERO_DATABASE_URL", "sqlite::memory:")
    with pytest.raises(ConfigError, match="development or in-memory"):
        Settings.load()


def test_production_refuses_development_default_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZERO_ENV", "production")
    monkeypatch.setenv("ZERO_SECRET_KEY", "x" * 64)
    monkeypatch.setenv("ZERO_DATABASE_URL", "sqlite:///./zero_develop.db")
    with pytest.raises(ConfigError, match="development or in-memory"):
        Settings.load()


# ----------------------------------------------------------------------
# Fail-closed: test refuses production-shaped paths
# ----------------------------------------------------------------------


def test_test_refuses_production_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZERO_ENV", "test")
    monkeypatch.setenv("ZERO_DATABASE_URL", "sqlite:///var/lib/zero/production.db")
    with pytest.raises(ConfigError, match="contains 'prod'"):
        Settings.load()


# ----------------------------------------------------------------------
# Safe defaults
# ----------------------------------------------------------------------


def test_test_mode_auto_selects_in_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZERO_ENV", "test")
    monkeypatch.delenv("ZERO_DATABASE_URL", raising=False)
    settings = Settings.load()
    assert settings.is_test
    assert settings.is_in_memory_db
    assert settings.database_url == "sqlite::memory:"


def test_development_mode_auto_selects_local_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZERO_ENV", "development")
    monkeypatch.delenv("ZERO_DATABASE_URL", raising=False)
    monkeypatch.delenv("ZERO_SECRET_KEY", raising=False)
    settings = Settings.load()
    assert settings.is_development
    assert settings.database_url == "sqlite:///./zero_develop.db"
    # secret_key is optional in development
    assert settings.secret_key is None


def test_load_reads_optional_openai_provider_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZERO_ENV", "development")
    monkeypatch.delenv("ZERO_DATABASE_URL", raising=False)
    monkeypatch.setenv("ZERO_OPENAI_API_KEY", "synthetic-provider-key")
    monkeypatch.setenv("ZERO_OPENAI_BASE_URL", "https://provider.invalid/v1")
    monkeypatch.setenv("ZERO_OPENAI_MODEL", "test-model")
    monkeypatch.setenv("ZERO_OPENAI_TIMEOUT_SECONDS", "12.5")

    settings = Settings.load()

    assert settings.openai_api_key is not None
    assert settings.openai_api_key.get_secret_value() == "synthetic-provider-key"
    assert settings.openai_base_url == "https://provider.invalid/v1"
    assert settings.openai_model == "test-model"
    assert settings.openai_timeout_seconds == 12.5
    assert "synthetic-provider-key" not in settings.safe_repr()


def test_production_loads_with_all_required_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZERO_ENV", "production")
    monkeypatch.setenv("ZERO_SECRET_KEY", "x" * 64)
    monkeypatch.setenv("ZERO_BOOTSTRAP_TOKEN", "b" * 64)
    monkeypatch.setenv("ZERO_DATABASE_URL", "sqlite:///var/lib/zero/production.db")
    settings = Settings.load()
    assert settings.is_production
    assert settings.secret_key is not None


def test_production_without_bootstrap_token_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-closed bootstrap: authentication without a provisioning path
    must not pass settings validation."""
    monkeypatch.setenv("ZERO_ENV", "production")
    monkeypatch.setenv("ZERO_SECRET_KEY", "x" * 64)
    monkeypatch.delenv("ZERO_BOOTSTRAP_TOKEN", raising=False)
    monkeypatch.delenv("ZERO_ALLOW_MANUAL_PROVISIONING", raising=False)
    monkeypatch.setenv("ZERO_DATABASE_URL", "sqlite:///var/lib/zero/production.db")
    with pytest.raises(ConfigError, match="ZERO_BOOTSTRAP_TOKEN is required"):
        Settings.load()


def test_production_manual_provisioning_opt_in_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZERO_ENV", "production")
    monkeypatch.setenv("ZERO_SECRET_KEY", "x" * 64)
    monkeypatch.delenv("ZERO_BOOTSTRAP_TOKEN", raising=False)
    monkeypatch.setenv("ZERO_ALLOW_MANUAL_PROVISIONING", "1")
    monkeypatch.setenv("ZERO_DATABASE_URL", "sqlite:///var/lib/zero/production.db")
    settings = Settings.load()
    assert settings.bootstrap_token is None


def test_unsupported_database_scheme_fails_at_config_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PostgreSQL-style URL must be rejected by configuration loading,
    not later inside the application factory."""
    monkeypatch.setenv("ZERO_ENV", "production")
    monkeypatch.setenv("ZERO_SECRET_KEY", "x" * 64)
    monkeypatch.setenv("ZERO_BOOTSTRAP_TOKEN", "b" * 64)
    monkeypatch.setenv("ZERO_DATABASE_URL", "postgresql://user:pw@db.example.invalid/zero")
    with pytest.raises(ConfigError, match="Unsupported database URL scheme"):
        Settings.load()


def test_development_rejects_unsupported_database_scheme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZERO_ENV", "development")
    monkeypatch.setenv("ZERO_DATABASE_URL", "mysql://localhost/zero")
    with pytest.raises(ConfigError, match="Unsupported database URL scheme"):
        Settings.load()


# ----------------------------------------------------------------------
# Redaction
# ----------------------------------------------------------------------


def test_safe_repr_redacts_secret_key() -> None:
    settings = Settings.load_for_test(secret_key="super-secret-value")
    repr_str = settings.safe_repr()
    assert "super-secret-value" not in repr_str
    assert "[REDACTED]" in repr_str


def test_repr_does_not_leak_secret_key() -> None:
    settings = Settings.load_for_test(secret_key="super-secret-value")
    assert "super-secret-value" not in repr(settings)


# ----------------------------------------------------------------------
# load_for_test forces test mode
# ----------------------------------------------------------------------


def test_load_for_test_forces_test_environment() -> None:
    # Even if someone passes zero_env='production', it must be ignored.
    settings = Settings.load_for_test(zero_env="production")  # type: ignore[arg-type]
    assert settings.is_test


def test_load_for_test_accepts_custom_database_url() -> None:
    settings = Settings.load_for_test(database_url="sqlite:///tmp/test.db")
    assert settings.database_url == "sqlite:///tmp/test.db"


# ----------------------------------------------------------------------
# .env file parsing
# ----------------------------------------------------------------------


def test_parse_dotenv_strips_quotes_and_comments(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n"
        'ZERO_ENV="test"\n'
        "ZERO_LOG_LEVEL='DEBUG'\n"
        "ZERO_DATABASE_URL=sqlite::memory:\n"
        "\n"
        "INVALID_LINE_WITHOUT_EQUALS\n"
    )
    pairs = dict(_parse_dotenv(env_file))
    assert pairs["ZERO_ENV"] == "test"
    assert pairs["ZERO_LOG_LEVEL"] == "DEBUG"
    assert pairs["ZERO_DATABASE_URL"] == "sqlite::memory:"
    assert "INVALID_LINE_WITHOUT_EQUALS" not in pairs


# ----------------------------------------------------------------------
# SQLite URL normalization
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("sqlite::memory:", "sqlite::memory:"),
        ("sqlite://:memory:", "sqlite::memory:"),
        ("sqlite:///:memory:", "sqlite::memory:"),
        ("sqlite:///./foo.db", "sqlite:///./foo.db"),
        ("sqlite:///foo.db", "sqlite:///foo.db"),
        ("sqlite:////tmp/zero-release.db", "sqlite:////tmp/zero-release.db"),
        ("sqlite://foo.db", "sqlite:///foo.db"),
        ("postgres:///foo", "postgres:///foo"),
    ],
)
def test_normalize_sqlite_url(raw: str, expected: str) -> None:
    assert _normalize_sqlite_url(raw) == expected


# ----------------------------------------------------------------------
# Settings is immutable
# ----------------------------------------------------------------------


def test_settings_is_immutable() -> None:
    settings = Settings.load_for_test()
    with pytest.raises(ValidationError):
        settings.zero_env = "production"  # type: ignore[misc]


def test_settings_forbids_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        Settings.load_for_test(unknown_field="value")  # type: ignore[arg-type]
