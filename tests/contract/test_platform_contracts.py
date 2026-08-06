"""Contract tests for Platform contracts — Phase P T-P.11.

Validates that our internal types conform to the JSON Schemas in
zero/contracts/v1/.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import validate
from zero.core.scope import Scope
from zero.platform import (
    Capability,
    CapabilityState,
    ConfigFieldMeta,
    EventEnvelope,
    HealthReport,
    HealthStatus,
    RemoteCommand,
    compute_capability_hash,
)

CONTRACTS_DIR = Path(__file__).resolve().parents[2] / "zero" / "contracts" / "v1"


def load_schema(name: str) -> dict:
    path = CONTRACTS_DIR / f"{name}.schema.json"
    return json.loads(path.read_text())


# ---------------------------------------------------------------------- capability

class TestCapabilityContract:
    def test_valid_capability_matches_schema(self) -> None:
        schema = load_schema("capability")
        cap = Capability(
            namespace="telegram",
            name="send_message",
            state=CapabilityState.AVAILABLE,
        )
        validate(instance=cap.to_dict(), schema=schema)

    def test_invalid_state_rejected(self) -> None:
        schema = load_schema("capability")
        bad = {"namespace": "x", "name": "y", "state": "invalid_state"}
        with pytest.raises(Exception):
            validate(instance=bad, schema=schema)

    def test_capability_hash_stable(self) -> None:
        """Same capabilities → same hash."""
        caps = [
            Capability(namespace="a", name="b", state=CapabilityState.AVAILABLE),
            Capability(namespace="c", name="d", state=CapabilityState.DEGRADED),
        ]
        h1 = compute_capability_hash(caps)
        h2 = compute_capability_hash(list(reversed(caps)))  # order shouldn't matter
        assert h1 == h2


# ---------------------------------------------------------------------- event envelope

class TestEventEnvelopeContract:
    def test_valid_event_matches_schema(self) -> None:
        schema = load_schema("event-envelope")
        scope = Scope.development(
            org_id="org_01HABC", workspace_id="ws_01HABC",
            project_id="prj_01HABC", group_id="grp_01HABC", topic_id=100,
        ).with_default_memory_scope()
        evt = EventEnvelope(
            name="agent.run.completed",
            scope=scope,
            data={"run_id": "run_01HABC", "cost_usd": 0.05},
        )
        validate(instance=evt.to_dict(), schema=schema)

    def test_forbidden_data_keys_rejected_at_construction(self) -> None:
        """EventEnvelope constructor rejects forbidden keys."""
        scope = Scope.personal(user_id="usr_01HALICE").with_default_memory_scope()
        with pytest.raises(ValueError, match="forbidden keys"):
            EventEnvelope(
                name="x",
                scope=scope,
                data={"raw_user_message": "sensitive content"},
            )


# ---------------------------------------------------------------------- remote command

class TestRemoteCommandContract:
    def test_valid_command_matches_schema(self) -> None:
        schema = load_schema("remote-command")
        cmd = RemoteCommand(
            command_type="config.set",
            params={"key": "logging.level", "value": "debug"},
            signature="a" * 64,  # fake HMAC signature
        )
        # The schema requires signature; check our type enforces it.
        validate(instance=cmd.to_dict(), schema=schema)

    def test_shell_exec_command_type_not_in_allowlist(self) -> None:
        """T-P.4: no shell.exec command type."""
        schema = load_schema("remote-command")
        bad = {
            "id": "cmd_test",
            "command_type": "shell.exec",  # NOT in enum
            "params": {"command": "rm -rf /"},
            "received_at": "2026-01-01T00:00:00Z",
            "signature": "a" * 64,
        }
        with pytest.raises(Exception):
            validate(instance=bad, schema=schema)


# ---------------------------------------------------------------------- health

class TestHealthContract:
    def test_valid_health_matches_schema(self) -> None:
        schema = load_schema("health")
        report = HealthReport(
            status=HealthStatus.HEALTHY,
            capabilities_hash="a" * 64,
            version="0.1.0",
        )
        validate(instance=report.to_dict(), schema=schema)

    def test_offline_status_not_allowed(self) -> None:
        """T-9.4: Zero never reports 'offline' (that's Platform-side)."""
        schema = load_schema("health")
        bad = {
            "status": "offline",
            "capabilities_hash": "a" * 64,
            "version": "0.1.0",
            "checked_at": "2026-01-01T00:00:00Z",
        }
        with pytest.raises(Exception):
            validate(instance=bad, schema=schema)


# ---------------------------------------------------------------------- config field

class TestConfigFieldContract:
    def test_secret_field_no_value_exposed(self) -> None:
        """T-P.5: secret fields NEVER have values (even masked)."""
        schema = load_schema("config-field")
        field = ConfigFieldMeta(
            name="telegram.bot_token",
            type="secret_ref",
            required=True,
            secret=True,
            configured=True,
            last_rotated_at="2026-01-01T00:00:00Z",
        )
        d = field.to_dict()
        validate(instance=d, schema=schema)
        # The 'default' field must be null for secret fields.
        assert d["default"] is None
        # No value-bearing field present.
        for k in ("value", "masked_value", "preview"):
            assert k not in d

    def test_non_secret_field_can_have_default(self) -> None:
        schema = load_schema("config-field")
        field = ConfigFieldMeta(
            name="logging.level",
            type="string",
            required=False,
            default="info",
            secret=False,
        )
        d = field.to_dict()
        validate(instance=d, schema=schema)
        assert d["default"] == "info"
