"""GAP 8b/G2 — per-call tool approval gate (Hermes parity).

Covers: hardline floor, deny-rule precedence over standing allows,
session grants scoped per execution, pending TTL expiry re-issue,
idempotent resolution + input validation, and the disabled-mode API
contract (/projects/{id}/tool-approvals -> 409 when mode=off).
Runtime integration lives in tests/test_agent_runtime.py beside the
vertical fixtures.
"""

from __future__ import annotations

import pytest

from zero.app.approval_gate import (
    ApprovalError,
    ToolApprovalGate,
    ToolNotFoundDuringApproval,
)
from zero.config import Settings
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations

PROJECT = "p_testapprovalproject000000"


def _gate(mode: str = "manual", **kwargs) -> ToolApprovalGate:
    settings = Settings.load_for_test()
    database = Database(settings)
    apply_migrations(database)
    # A real user row keeps projects.owner_user_id FK integrity honest.
    conn = database.connect()
    conn.execute(
        "INSERT OR IGNORE INTO users (id, display_name, status, created_at) "
        "VALUES (?, ?, 'active', ?)",
        ("zu_ownerplaceholder0000000000", "Gate Owner", "2026-08-27T00:00:00"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO projects (id, name, created_at) VALUES (?, ?, ?)",
        (PROJECT, "approvals-it", "2026-08-27T00:00:00"),
    )
    conn.execute(
        "UPDATE projects SET owner_user_id = ? WHERE id = ? AND owner_user_id IS NULL",
        ("zu_ownerplaceholder0000000000", PROJECT),
    )
    database.commit()
    return ToolApprovalGate(database, mode=mode, **kwargs)


def _evaluate(gate: ToolApprovalGate, tool: str = "echo", **input_data):
    return gate.evaluate(
        project_id=PROJECT,
        execution_id=None,
        tool_name=tool,
        input_data=input_data or {"message": "hi"},
    )


# ----------------------------------------------------------------------
# unit semantics
# ----------------------------------------------------------------------


def test_mode_off_allows_everything() -> None:
    gate = _gate("off")
    verdict = gate.evaluate(
        project_id=PROJECT,
        execution_id="exec_x",
        tool_name="terminal",
        input_data={"command": "rm -rf /"},
    )
    assert verdict.state == "allowed"
    assert verdict.cause == "mode_off"


def test_hardline_denied_even_in_manual_mode() -> None:
    gate = _gate("manual")
    catastrophic = [
        {"command": "rm -rf / --no-preserve-root"},
        {"command": "mkfs.ext4 /dev/sda1"},
        {"script": ":(){ :|:& };:"},
        {"cmd": "shutdown now"},
    ]
    for args in catastrophic:
        verdict = gate.evaluate(
            project_id=PROJECT,
            execution_id=None,
            tool_name="shell",
            input_data=args,
        )
        assert verdict.state == "denied", args
        assert verdict.cause is not None and verdict.cause.startswith("hardline:")


def test_deny_rule_outranks_standing_allow() -> None:
    gate = _gate("manual")
    first = _evaluate(gate, "write_file", path="/tmp/a.txt")
    assert first.state == "pending" and first.request is not None
    resolved = gate.resolve(
        first.request.id, decision="allow", decided_by_user_id="u1", grain="always"
    )
    assert resolved.decision == "allow"

    wildcard = _evaluate(gate, "write_file", path="/tmp/DIFFERENT.txt")
    assert wildcard.state == "pending" and wildcard.request is not None
    gate.resolve(wildcard.request.id, decision="deny", decided_by_user_id="u1", grain="always")

    blocked = _evaluate(gate, "write_file", path="/tmp/a.txt")
    assert blocked.state == "denied"
    assert blocked.cause == "rule"


def test_pending_ttl_expiry_reissues_request() -> None:
    import time as _time

    fake_now = [_time.time()]  # epoch-realistic so parsed rows compare sanely
    gate = _gate("manual", pending_ttl_seconds=10.0, clock=lambda: fake_now[0])
    first = _evaluate(gate, "search_web", query="zero")
    assert first.state == "pending"
    fake_now[0] += 3600.0
    second = _evaluate(gate, "search_web", query="zero")
    assert second.state == "pending"
    assert second.request is not None and first.request is not None
    assert second.request.id != first.request.id


def test_session_grant_scoped_to_execution() -> None:
    gate = _gate("manual")
    req = gate.evaluate(
        project_id=PROJECT,
        execution_id="exec_s",
        tool_name="echo",
        input_data={"message": "hi"},
    )
    assert req.state == "pending" and req.request is not None
    gate.resolve(req.request.id, decision="allow", decided_by_user_id="u1", grain="session")

    again = gate.evaluate(
        project_id=PROJECT,
        execution_id="exec_s",
        tool_name="echo",
        input_data={"message": "hi"},
    )
    assert again.state == "allowed" and again.cause == "session_grant"

    other_exec = gate.evaluate(
        project_id=PROJECT,
        execution_id="exec_other",
        tool_name="echo",
        input_data={"message": "hi"},
    )
    assert other_exec.state == "pending"


def test_double_resolve_is_idempotent_and_inputs_validated() -> None:
    gate = _gate("manual")
    req = _evaluate(gate, "echo", a=1)
    assert req.request is not None
    rid = req.request.id

    once = gate.resolve(rid, decision="deny", decided_by_user_id="u1")
    twice = gate.resolve(rid, decision="allow", decided_by_user_id="u1")
    assert once.decision == twice.decision == "deny"

    with pytest.raises(ApprovalError):
        gate.resolve(rid, decision="maybe", decided_by_user_id="u1")
    with pytest.raises(ApprovalError):
        gate.resolve(rid, decision="allow", grain="forever", decided_by_user_id="u1")
    with pytest.raises(ToolNotFoundDuringApproval):
        gate.resolve("ta_missing", decision="allow", decided_by_user_id="u1")


def test_args_hash_is_canonical_across_key_order() -> None:
    assert ToolApprovalGate.canonical_args_hash({"a": 1, "b": 2}) == (
        ToolApprovalGate.canonical_args_hash({"b": 2, "a": 1})
    )


# ----------------------------------------------------------------------
# REST contract while the feature flag is off (default deployments)
# ----------------------------------------------------------------------


def test_tool_approvals_endpoint_returns_409_when_disabled() -> None:
    import asyncio

    from httpx import ASGITransport, AsyncClient

    from zero.app.api import create_app

    settings = Settings.load_for_test()
    database = Database(settings)
    apply_migrations(database)
    app = create_app(settings)

    async def call():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            return await ac.get("/projects/p_any/tool-approvals", params={"actor_id": "zu_x"})

    response = asyncio.run(call())
    assert response.status_code == 409
    body = response.json()
    assert body["detail"]["error"] == "tool_approval_disabled"
