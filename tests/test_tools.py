"""Tool service tests — registry, capability grants, invocation lifecycle.

Per ``zero-tool-capability-runtime`` SKILL.md:

- A Zero tool is a server-owned capability with a name, bounded input,
  authorization policy, execution limits, result policy, and audit
  identity.
- Tool schemas are trust boundaries: model output is untrusted input.
- Tool choice and tool permission are separate.
- A denied tool does not become available through a different
  interface or child agent.

Per PLAN.md M3 validation:
- 'Malformed tool inputs fail before invocation.'
- 'Tool timeout/failure is typed and audited.'
- 'Agents receive capability handles or sanitized results, never raw
  credentials.'
"""

from __future__ import annotations

import pytest

from zero.app.services import build_services
from zero.config import Settings
from zero.domain.tools import (
    ToolAlreadyExistsError,
    ToolInputValidationError,
    ToolInvocationDeniedError,
    ToolNotFoundError,
)
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations


@pytest.fixture
def services(test_settings: Settings):
    database = Database(test_settings)
    apply_migrations(database)
    return build_services(test_settings, database)


@pytest.fixture
def project_with_owner(services):
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(
        owner_id=owner.id, name="Tool Test Project"
    )
    return owner, project


# ----------------------------------------------------------------------
# Registration
# ----------------------------------------------------------------------


def test_register_echo_tool(services) -> None:
    tool = services.tools.register_echo_tool()
    assert tool.id.value.startswith("tool_")
    assert tool.name == "echo"
    assert "echo" in tool.description.lower()
    assert tool.input_schema["properties"]["message"]["type"] == "string"
    assert tool.output_schema["properties"]["echoed"]["type"] == "string"


def test_register_duplicate_tool_name_rejected(services) -> None:
    services.tools.register_echo_tool()
    with pytest.raises(ToolAlreadyExistsError):
        services.tools.register_echo_tool()


def test_list_tools_returns_all_registered(services) -> None:
    services.tools.register_echo_tool()
    tools = services.tools.list_tools()
    assert len(tools) == 1
    assert tools[0].name == "echo"


def test_get_tool_by_name(services) -> None:
    services.tools.register_echo_tool()
    tool = services.tools.get_tool_by_name("echo")
    assert tool.name == "echo"


def test_get_tool_by_name_raises_for_nonexistent(services) -> None:
    with pytest.raises(ToolNotFoundError):
        services.tools.get_tool_by_name("nonexistent")


# ----------------------------------------------------------------------
# Capability grants
# ----------------------------------------------------------------------


def test_grant_tool_creates_grant(services, project_with_owner) -> None:
    _owner, project = project_with_owner
    tool = services.tools.register_echo_tool()
    grant = services.tools.grant_tool(
        project_id=project.id, actor_id=project.owner_user_id,
        tool_id=tool.id,
        agent_scope="main_worker",
    )
    assert grant.id.value.startswith("tg_")
    assert grant.project_id == project.id
    assert grant.tool_id == tool.id
    assert grant.agent_scope == "main_worker"


def test_tool_invocation_cap_is_enforced(services, project_with_owner) -> None:
    owner, project = project_with_owner
    tool = services.tools.register_echo_tool()
    services.tools.grant_tool(
        project_id=project.id, actor_id=project.owner_user_id,
        tool_id=tool.id,
        agent_scope="main_worker",
        max_invocations=1,
    )
    assert services.tools.invoke(
        project_id=project.id,
        actor_id=owner.id,
        agent_scope="main_worker",
        tool_name="echo",
        input_data={"message": "once"},
    ).status == "success"
    with pytest.raises(ToolInvocationDeniedError, match="limit reached"):
        services.tools.invoke(
            project_id=project.id,
            actor_id=owner.id,
            agent_scope="main_worker",
            tool_name="echo",
            input_data={"message": "twice"},
        )
    count = services.database.connect().execute(
        "SELECT invocation_count FROM tool_grants WHERE id = ?",
        (services.tools.list_grants_for_project(project.id, actor_id=project.owner_user_id)[0].id.value,),
    ).fetchone()[0]
    assert count == 1


def test_unenforceable_tool_timeout_is_rejected(
    services, project_with_owner
) -> None:
    _, project = project_with_owner
    tool = services.tools.register_echo_tool()
    with pytest.raises(ValueError, match="isolated tool runner"):
        services.tools.grant_tool(
            project_id=project.id, actor_id=project.owner_user_id,
            tool_id=tool.id,
            agent_scope="main_worker",
            timeout_seconds=1,
        )


def test_grant_is_idempotent(services, project_with_owner) -> None:
    """Granting the same (project, tool, scope) twice is idempotent."""
    _owner, project = project_with_owner
    tool = services.tools.register_echo_tool()
    services.tools.grant_tool(
        project_id=project.id, actor_id=project.owner_user_id,
        tool_id=tool.id,
        agent_scope="main_worker",
    )
    # Second grant should succeed silently (idempotent).
    services.tools.grant_tool(
        project_id=project.id, actor_id=project.owner_user_id,
        tool_id=tool.id,
        agent_scope="main_worker",
    )
    grants = services.tools.list_grants_for_project(project.id, actor_id=project.owner_user_id)
    assert len(grants) == 1


def test_revoke_tool_grant_takes_effect_immediately(
    services, project_with_owner
) -> None:
    """Per PLAN.md M3: 'Revocation takes effect through all implemented
    interfaces.'"""
    owner, project = project_with_owner
    tool = services.tools.register_echo_tool()
    services.tools.grant_tool(
        project_id=project.id, actor_id=project.owner_user_id,
        tool_id=tool.id,
        agent_scope="main_worker",
    )
    # Invocation works before revocation.
    services.tools.invoke(
        project_id=project.id,
        actor_id=owner.id,
        agent_scope="main_worker",
        tool_name="echo",
        input_data={"message": "before"},
    )
    # Revoke.
    services.tools.revoke_tool_grant(
        project_id=project.id, actor_id=project.owner_user_id,
        tool_id=tool.id,
        agent_scope="main_worker",
    )
    # Invocation now denied.
    with pytest.raises(ToolInvocationDeniedError):
        services.tools.invoke(
            project_id=project.id,
            actor_id=owner.id,
            agent_scope="main_worker",
            tool_name="echo",
            input_data={"message": "after"},
        )


# ----------------------------------------------------------------------
# Invocation lifecycle
# ----------------------------------------------------------------------


def test_invoke_echo_succeeds(services, project_with_owner) -> None:
    owner, project = project_with_owner
    tool = services.tools.register_echo_tool()
    services.tools.grant_tool(
        project_id=project.id, actor_id=project.owner_user_id,
        tool_id=tool.id,
        agent_scope="main_worker",
    )
    result = services.tools.invoke(
        project_id=project.id,
        actor_id=owner.id,
        agent_scope="main_worker",
        tool_name="echo",
        input_data={"message": "hello world"},
    )
    assert result.status == "success"
    assert result.output == {"echoed": "hello world", "length": 11}
    assert "hello world" in result.model_facing
    assert result.duration_ms >= 0


def test_invoke_without_grant_is_denied(services, project_with_owner) -> None:
    """Per zero-tool-capability-runtime §"Tool choice and tool
    permission are separate": a model may reason that a tool is
    relevant, but the control plane still decides whether the
    project and agent type may invoke it."""
    owner, project = project_with_owner
    services.tools.register_echo_tool()
    with pytest.raises(ToolInvocationDeniedError):
        services.tools.invoke(
            project_id=project.id,
            actor_id=owner.id,
            agent_scope="main_worker",
            tool_name="echo",
            input_data={"message": "denied"},
        )


def test_invoke_with_unknown_tool_raises(services, project_with_owner) -> None:
    owner, project = project_with_owner
    with pytest.raises(ToolNotFoundError):
        services.tools.invoke(
            project_id=project.id,
            actor_id=owner.id,
            agent_scope="main_worker",
            tool_name="nonexistent",
            input_data={},
        )


def test_invoke_with_invalid_input_raises_validation_error(
    services, project_with_owner
) -> None:
    """Per zero-tool-capability-runtime §"Tool schemas are trust
    boundaries": model output is untrusted input. Validation covers
    type, shape, length, allowed values, project ownership, path
    normalization, and domain preconditions before side effects
    begin."""
    owner, project = project_with_owner
    tool = services.tools.register_echo_tool()
    services.tools.grant_tool(
        project_id=project.id, actor_id=project.owner_user_id,
        tool_id=tool.id,
        agent_scope="main_worker",
    )
    # Missing required field.
    with pytest.raises(ToolInputValidationError):
        services.tools.invoke(
            project_id=project.id,
            actor_id=owner.id,
            agent_scope="main_worker",
            tool_name="echo",
            input_data={},
        )
    # Wrong type.
    with pytest.raises(ToolInputValidationError):
        services.tools.invoke(
            project_id=project.id,
            actor_id=owner.id,
            agent_scope="main_worker",
            tool_name="echo",
            input_data={"message": 123},
        )
    # Empty string violates minLength=1.
    with pytest.raises(ToolInputValidationError):
        services.tools.invoke(
            project_id=project.id,
            actor_id=owner.id,
            agent_scope="main_worker",
            tool_name="echo",
            input_data={"message": ""},
        )
    # Additional property violates additionalProperties=False.
    with pytest.raises(ToolInputValidationError):
        services.tools.invoke(
            project_id=project.id,
            actor_id=owner.id,
            agent_scope="main_worker",
            tool_name="echo",
            input_data={"message": "ok", "extra": "no"},
        )


def test_invoke_audits_success(services, project_with_owner) -> None:
    owner, project = project_with_owner
    tool = services.tools.register_echo_tool()
    services.tools.grant_tool(
        project_id=project.id, actor_id=project.owner_user_id,
        tool_id=tool.id,
        agent_scope="main_worker",
    )
    services.tools.invoke(
        project_id=project.id,
        actor_id=owner.id,
        agent_scope="main_worker",
        tool_name="echo",
        input_data={"message": "audit me"},
    )
    events = services.audit.list_for_project(project_id=project.id, actor_id=project.owner_user_id, limit=50)
    invoke_events = [
        e for e in events if e.operation == "tool.invoke"
    ]
    assert len(invoke_events) >= 1
    event = invoke_events[0]
    assert event.target_id == "echo"
    assert event.result == "success"
    # The audit event MUST NOT contain the raw input or output.
    assert "audit me" not in (event.redacted_summary or "")


def test_invoke_audits_denial(services, project_with_owner) -> None:
    """Per zero-tool-capability-runtime §"Audit describes the operation
    without copying payloads": denials are audited with the operation
    and target, not the input."""
    owner, project = project_with_owner
    services.tools.register_echo_tool()
    with pytest.raises(ToolInvocationDeniedError):
        services.tools.invoke(
            project_id=project.id,
            actor_id=owner.id,
            agent_scope="main_worker",
            tool_name="echo",
            input_data={"message": "should be denied"},
        )
    events = services.audit.list_for_project(project_id=project.id, actor_id=project.owner_user_id, limit=50)
    denial_events = [
        e
        for e in events
        if e.operation == "tool.invoke" and e.result == "denied"
    ]
    assert len(denial_events) >= 1
    assert denial_events[0].target_id == "echo"
    # The audit event MUST NOT contain the raw input.
    assert "should be denied" not in (denial_events[0].redacted_summary or "")


def test_invoke_audits_validation_failure(services, project_with_owner) -> None:
    owner, project = project_with_owner
    tool = services.tools.register_echo_tool()
    services.tools.grant_tool(
        project_id=project.id, actor_id=project.owner_user_id,
        tool_id=tool.id,
        agent_scope="main_worker",
    )
    with pytest.raises(ToolInputValidationError):
        services.tools.invoke(
            project_id=project.id,
            actor_id=owner.id,
            agent_scope="main_worker",
            tool_name="echo",
            input_data={},
        )
    events = services.audit.list_for_project(project_id=project.id, actor_id=project.owner_user_id, limit=50)
    failure_events = [
        e
        for e in events
        if e.operation == "tool.invoke" and e.result == "failure"
    ]
    assert len(failure_events) >= 1


# ----------------------------------------------------------------------
# Cross-project grant isolation
# ----------------------------------------------------------------------


def test_grant_in_project_a_not_usable_in_project_b(services) -> None:
    """Per zero-tool-capability-runtime §"A denied tool does not become
    available through a different interface or child agent": a grant
    in project A is not usable from project B."""
    owner_a = services.identity.create_user(display_name="Owner A")
    project_a = services.identity.create_project(
        owner_id=owner_a.id, name="Project A"
    )
    owner_b = services.identity.create_user(display_name="Owner B")
    project_b = services.identity.create_project(
        owner_id=owner_b.id, name="Project B"
    )
    tool = services.tools.register_echo_tool()
    # Grant only in project A.
    services.tools.grant_tool(
        project_id=project_a.id, actor_id=project_a.owner_user_id,
        tool_id=tool.id,
        agent_scope="main_worker",
    )
    # Project A can invoke.
    services.tools.invoke(
        project_id=project_a.id,
        actor_id=owner_a.id,
        agent_scope="main_worker",
        tool_name="echo",
        input_data={"message": "from A"},
    )
    # Project B cannot.
    with pytest.raises(ToolInvocationDeniedError):
        services.tools.invoke(
            project_id=project_b.id,
            actor_id=owner_b.id,
            agent_scope="main_worker",
            tool_name="echo",
            input_data={"message": "from B"},
        )


# ----------------------------------------------------------------------
# Different agent scopes are independent
# ----------------------------------------------------------------------


def test_grant_for_one_scope_does_not_apply_to_other_scopes(
    services, project_with_owner
) -> None:
    """Per zero-tool-capability-runtime §"Registry metadata and runtime
    capability differ": a grant describes who may invoke one bounded
    part of a tool in one context. Granting to main_worker does NOT
    grant to main_planner."""
    owner, project = project_with_owner
    tool = services.tools.register_echo_tool()
    services.tools.grant_tool(
        project_id=project.id, actor_id=project.owner_user_id,
        tool_id=tool.id,
        agent_scope="main_worker",
    )
    # main_worker can invoke.
    services.tools.invoke(
        project_id=project.id,
        actor_id=owner.id,
        agent_scope="main_worker",
        tool_name="echo",
        input_data={"message": "ok"},
    )
    # main_planner cannot (no grant for that scope).
    with pytest.raises(ToolInvocationDeniedError):
        services.tools.invoke(
            project_id=project.id,
            actor_id=owner.id,
            agent_scope="main_planner",
            tool_name="echo",
            input_data={"message": "no"},
        )


# ----------------------------------------------------------------------
# Model-facing rendering is bounded
# ----------------------------------------------------------------------


def test_model_facing_rendering_is_bounded(services, project_with_owner) -> None:
    """Per zero-tool-capability-runtime §"Output policy is part of the
    tool": the model-facing rendering is bounded and redacted. For
    large outputs, the rendering is truncated."""
    owner, project = project_with_owner
    tool = services.tools.register_echo_tool()
    services.tools.grant_tool(
        project_id=project.id, actor_id=project.owner_user_id,
        tool_id=tool.id,
        agent_scope="main_worker",
    )
    # Echo a 1000-character message.
    big_message = "x" * 1000
    result = services.tools.invoke(
        project_id=project.id,
        actor_id=owner.id,
        agent_scope="main_worker",
        tool_name="echo",
        input_data={"message": big_message},
    )
    # The full output is preserved.
    assert result.output["echoed"] == big_message
    # The model-facing rendering is bounded.
    assert len(result.model_facing) <= 500
