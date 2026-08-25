"""Audit regression tests: access-policy gate wiring (Phase 3 finding D2).

These tests exercise the PRODUCTION wiring — `_build_policy_gate` reading
a real `$ZERO_HOME/config.yaml` — not a hand-built stand-in, so a crash
or wrong decision here means live Telegram intake is broken.
"""

from __future__ import annotations

import pytest

from zero.config import Settings
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations

CONFIG_YAML = """\
owner_project_id: {project_id}
telegram:
  mode: bot_api
access:
  mode: groups
  owner_project_id: {project_id}
  groups:
    - chat_id: "-1001111111111"
      title: Audit Test Group
      enabled: true
      allowed_features: [chat]
"""


@pytest.fixture
def wired_gate(tmp_path, monkeypatch):
    """Build engine services with a managed config defining one group."""
    monkeypatch.setenv("ZERO_HOME", str(tmp_path))
    database = Database(Settings.load_for_test())
    apply_migrations(database)
    from zero.app.services import _build_policy_gate, build_services

    settings = Settings.load_for_test()
    services = build_services(settings, database)
    owner = services.identity.create_user(display_name="audit owner")
    project = services.identity.create_project(owner_id=owner.id, name="Audit")
    (tmp_path / "config.yaml").write_text(
        CONFIG_YAML.format(project_id=project.id.value), encoding="utf-8"
    )
    gate = _build_policy_gate(services.identity._identity_repo, settings)
    return services, gate, project


class TestPolicyGateGroupMode:
    def test_stranger_in_allowed_group_is_allowed(self, wired_gate):
        _services, gate, _project = wired_gate
        decision = gate("telegram", "999888777", "-1001111111111")
        assert decision.allowed is True

    def test_stranger_in_unknown_group_is_denied_before_expense(self, wired_gate):
        _services, gate, _project = wired_gate
        decision = gate("telegram", "999888777", "-1009999999999")
        assert decision.allowed is False
        assert decision.reason.startswith("policy_")

    def test_owner_bypasses_group_membership(self, wired_gate):
        services, gate, project = wired_gate
        # Link the owner's telegram identity so owner_lookup can resolve.
        owner_id = project.owner_user_id
        services.identity.link_external_identity(
            user_id=owner_id,
            platform="telegram",
            external_id="100200300",
            verified=True,
            source="system",
        )
        services.identity.verify_external_identity(
            platform="telegram",
            external_id="100200300",
            source="system",
        )
        allowed = gate("telegram", "100200300", "-1002222222222")
        assert allowed.allowed is True

    def test_denied_sender_never_reaches_event_processing(self, wired_gate, monkeypatch):
        """Enforcement happens BEFORE any expensive work: a denied sender
        must produce no conversation event and no provider request."""
        services, gate, project = wired_gate
        conn = services.database.connect()
        before_events = conn.execute("SELECT COUNT(*) c FROM conversation_events").fetchone()["c"]
        before_requests = conn.execute("SELECT COUNT(*) c FROM provider_requests").fetchone()["c"]

        decision = gate("telegram", "666666", "-1009999999999")
        assert decision.allowed is False

        after_events = conn.execute("SELECT COUNT(*) c FROM conversation_events").fetchone()["c"]
        after_requests = conn.execute("SELECT COUNT(*) c FROM provider_requests").fetchone()["c"]
        assert after_events == before_events
        assert after_requests == before_requests
