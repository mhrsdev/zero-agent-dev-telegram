"""Round-9 regression: delegation calls must leave a durable audit trail.

GAP L (found live in round 9): the ``delegate`` tool is runtime-owned
and deliberately bypasses the static tool registry — which also meant it
bypassed the ``tool.invoke`` audit every registry tool writes. A
powerful action (spawning sub-agents with provider calls) therefore left
ZERO durable trace. The runtime now writes the same ``tool.invoke``
audit event for every delegate invocation (success and error paths).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tests.test_dead_bot_regressions import zero_home  # noqa: F401 — fixture


def _runtime_with_audit(home_path):
    from zero.persistence.connection import open_database
    from zero.persistence.migrations import apply_migrations
    from zero.config import Settings
    from zero.app.services import build_services
    from zero.app.agent_runtime import AgentRuntime

    settings = Settings.load(
        env_file=str(home_path / ".env"), zero_env_fallback="development"
    )
    database = open_database(settings)
    apply_migrations(database)
    services = build_services(settings, database)
    runtime = AgentRuntime(
        worker=services.worker,
        providers=services.providers,
        artifacts=services.artifacts,
        authorization=services.authorization,
        tools=services.tools,
        enable_delegation=True,
        audit_repo=getattr(services.audit, "_audit_repo", None),
    )
    return services, runtime


def test_delegate_error_path_writes_tool_invoke_audit(
    zero_home, tmp_path, monkeypatch
) -> None:
    """A delegate call that fails argument validation must still be
    durably audited (operation='tool.invoke', target_id='delegate')."""
    dir_engine = tmp_path / "engine-cwd"
    dir_engine.mkdir()
    monkeypatch.chdir(dir_engine)
    services, runtime = _runtime_with_audit(zero_home)
    if runtime._audit_repo is None:
        pytest.fail("audit repo must be wired for this test")

    from zero.manage.cli import _ensure_management_scope

    project = _ensure_management_scope(services)
    owner = services.identity.get_user(project.owner_user_id)
    from zero.domain.execution import ExecutionId

    payload = runtime._execute_delegation(
        call_arguments="this is not json",
        parent_allowed_tools=("read_file",),
        execution_id=ExecutionId("exec_audit_probe"),
        project_id=project.id,
        actor_id=owner.id,
        provider="fake",
        model_name="fake-standard",
    )
    assert payload["status"] == "error"

    rows = services.audit._audit_repo.list_for_project(project.id, limit=50)
    delegate_rows = [
        r for r in rows if r.operation == "tool.invoke" and r.target_id == "delegate"
    ]
    assert delegate_rows, "delegate error path must write a tool.invoke audit row"
    assert delegate_rows[0].result == "error"
    assert delegate_rows[0].correlation_id == "exec_audit_probe"


def test_delegate_success_path_writes_tool_invoke_audit(
    zero_home, tmp_path, monkeypatch
) -> None:
    """A delegate call whose sub-agent answers must be audited as a
    successful tool.invoke with a redaction-safe summary."""
    dir_engine = tmp_path / "engine-cwd"
    dir_engine.mkdir()
    monkeypatch.chdir(dir_engine)
    services, runtime = _runtime_with_audit(zero_home)

    from zero.manage.cli import _ensure_management_scope

    project = _ensure_management_scope(services)
    owner = services.identity.get_user(project.owner_user_id)
    from zero.domain.execution import ExecutionId
    from zero.domain.providers import CanonicalResponse

    def fake_send_request_with_fallback(**_kwargs):
        return (
            SimpleNamespace(id=SimpleNamespace(value="preq_audit_probe")),
            CanonicalResponse(content="SUBAGENT-OK", finish_reason="stop"),
        )

    monkeypatch.setattr(
        services.providers,
        "send_request_with_fallback",
        fake_send_request_with_fallback,
        raising=True,
    )
    payload = runtime._execute_delegation(
        call_arguments=json.dumps({"objective": "Reply with exactly: SUBAGENT-OK"}),
        parent_allowed_tools=(),
        execution_id=ExecutionId("exec_audit_probe_ok"),
        project_id=project.id,
        actor_id=owner.id,
        provider="fake",
        model_name="fake-standard",
    )
    assert payload["status"] == "completed"
    assert "SUBAGENT-OK" in payload["result"]

    rows = services.audit._audit_repo.list_for_project(project.id, limit=50)
    delegate_rows = [
        r for r in rows if r.operation == "tool.invoke" and r.target_id == "delegate"
    ]
    assert delegate_rows, "delegate success path must write a tool.invoke audit row"
    assert delegate_rows[0].result == "success"
    summary = delegate_rows[0].redacted_summary or ""
    # The bounded, redacted objective rides the summary for auditability.
    assert "objective=Reply with exactly" in summary
    assert "depth=1" in summary
