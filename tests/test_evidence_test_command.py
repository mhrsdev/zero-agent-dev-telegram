"""Regression: the runtime evidence test command must be explicit config.

Real run (2026-08-28): build_services() never passed ``test_command`` to
AgentRuntime, so the constructor default ``("pytest", "-q")`` applied.
When the LLM decomposer gave a task test-report evidence requirements,
the evidence collector ran ``pytest`` — a binary the worktree command
policy does not allowlist — and every such task failed with "command
'pytest' is not permitted by the configured policy".

Pinned behavior:
1. Settings parses ZERO_EVIDENCE_TEST_COMMAND;
2. build_services wires it into the runtime (and unset means None);
3. AgentRuntime no longer carries a hidden pytest default.
"""

from __future__ import annotations

from zero.app.agent_runtime import AgentRuntime
from zero.config import Settings


def test_settings_parse_evidence_test_command():
    settings = Settings.load_for_test(
        evidence_test_command=("python3", "-m", "unittest", "discover"),
    )
    assert settings.evidence_test_command == ("python3", "-m", "unittest", "discover")


def test_settings_parse_evidence_test_command_from_env(monkeypatch):
    monkeypatch.setenv("ZERO_EVIDENCE_TEST_COMMAND", "python3 -m unittest discover -s tests")
    settings = Settings.load(env_file=None, zero_env_fallback="development")
    assert settings.evidence_test_command == (
        "python3",
        "-m",
        "unittest",
        "discover",
        "-s",
        "tests",
    )


def test_settings_default_is_empty():
    settings = Settings.load_for_test()
    assert settings.evidence_test_command == ()


def test_build_services_wires_evidence_command(test_settings):
    from zero.app.services import build_services
    from zero.persistence.connection import Database
    from zero.persistence.migrations import apply_migrations

    settings = test_settings.model_copy(
        update={
            "evidence_test_command": ("python3", "-m", "unittest", "discover"),
            "worktree_allowed_commands": ("python3", "ls", "cat"),
        }
    )
    database = Database(settings)
    apply_migrations(database)
    services = build_services(settings, database)
    assert services.runtime._test_command == ("python3", "-m", "unittest", "discover")


def test_build_services_unset_means_none(test_settings):
    from zero.app.services import build_services
    from zero.persistence.connection import Database
    from zero.persistence.migrations import apply_migrations

    database = Database(test_settings)
    apply_migrations(database)
    services = build_services(test_settings, database)
    assert services.runtime._test_command is None


def test_agent_runtime_constructor_has_no_hidden_pytest_default():
    import inspect

    sig = inspect.signature(AgentRuntime.__init__)
    assert sig.parameters["test_command"].default is None
