"""Shared pytest fixtures."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# Set test env vars BEFORE any zero import.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1234567890:TEST_TOKEN_FOR_PYTEST_ONLY_XXXXXXXXXX")
os.environ.setdefault("ZERO_ROUTER_API_KEY", "zr_test_pytest_only_token_XXXXXXXXXXXXXXXXXX")


@pytest.fixture
def temp_dir() -> Path:
    """Provide a temporary directory that's cleaned up after the test."""
    with tempfile.TemporaryDirectory(prefix="zero-test-") as d:
        yield Path(d)


@pytest.fixture
def env_with_test_secrets(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Set test secrets in env vars."""
    secrets = {
        "TELEGRAM_BOT_TOKEN": "1234567890:TEST_TOKEN_FOR_PYTEST_ONLY_XXXXXXXXXX",
        "ZERO_ROUTER_API_KEY": "zr_test_pytest_only_token_XXXXXXXXXXXXXXXXXX",
        "GITHUB_TOKEN": "ghp_TEST_TOKEN_FOR_PYTEST_ONLY_XXXXXXXXXXXXXXXXXX",
    }
    for k, v in secrets.items():
        monkeypatch.setenv(k, v)
    return secrets
