"""Regression: raised ToolErrors must be recoverable, not task-fatal.

Real run (2026-08-28, execution exec_hpybca4epxnxhjw0yvov302j): the model
called run_command with `bash -c "ls -la > /tmp/out.txt …"`. `bash` is
not allowlisted, the handler raised ToolError("server-owned handler
failed"), and agent_runtime let it propagate — the whole task failed on
its first inspection step even though every other defect class (invalid
arguments, undeclared tool, approval denial) is recovered via synthetic
tool-error results (Hermes parity).

Pinned behavior:
1. a raised ToolError becomes a structured tool message; the loop
   continues (the model can change approach) — no exception escapes;
2. repeated identical failures still trip the identical-failure breaker
   and end the loop through the standard nudge path;
3. ToolInvocationDeniedError likewise feeds back without aborting;
4. worktree run_command's advertised description names the exact
   allowlist (so models can comply before calling);
5. WorktreeService.allowed_commands exposes the enforced policy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from zero.domain.tools import ToolError, ToolInvocationDeniedError

# ----------------------------------------------------------------------
# Harness: drive AgentRuntime._run_tool_rounds with stub collaborators
# ----------------------------------------------------------------------


@dataclass
class _FakeRequest:
    """Just enough for dataclasses.replace() inside the loop."""

    messages: tuple = ()
    provider: str = "openai-compatible"
    model_name: str = "claude-opus-5"
    max_tokens: int = 1024
    tools: tuple = ()
    stream: bool = False


def _call(tool_name="run_command", call_id="call_1", args='{"command": "bash"}'):
    return SimpleNamespace(
        tool_name=tool_name,
        tool_call_id=call_id,
        arguments=args,
    )


def _response(tool_calls):
    return SimpleNamespace(content="", tool_calls=tool_calls, finish_reason="tool_calls")


@pytest.fixture()
def harness():
    from zero.app.agent_runtime import AgentRuntime

    state = {
        "provider_calls": 0,
        "invoke_calls": 0,
        "queued_responses": [],
    }
    provider_request = SimpleNamespace(id=SimpleNamespace(value="preq_1"))

    def send(**kwargs):
        state["provider_calls"] += 1
        return provider_request, state["queued_responses"].pop(0)

    def renew(**kwargs):
        return None

    runtime = AgentRuntime.__new__(AgentRuntime)
    object.__setattr__(runtime, "_worker", SimpleNamespace(renew_task_lease=renew))
    object.__setattr__(runtime, "_providers", SimpleNamespace(send_request_with_fallback=send))
    object.__setattr__(runtime, "_tools", SimpleNamespace())
    object.__setattr__(runtime, "_enable_delegation", False)
    object.__setattr__(runtime, "_approval_gate", None)
    object.__setattr__(runtime, "_metrics", None)

    h = SimpleNamespace(
        runtime=runtime,
        state=state,
        provider_request=provider_request,
        execution_id=SimpleNamespace(value="exec_1"),
        project_id=SimpleNamespace(value="p_1"),
        actor_id=SimpleNamespace(value="zu_1"),
        request=_FakeRequest(),
        invoke=None,  # set per-test
    )

    def set_invoke(fn):
        h.invoke = fn
        object.__setattr__(
            runtime, "_tools", SimpleNamespace()
        )

        def invoke(**kwargs):
            state["invoke_calls"] += 1
            return fn(**kwargs)

        object.__setattr__(runtime, "_tools", SimpleNamespace(invoke=invoke))

    h.set_invoke = set_invoke

    def run(first_response, *, max_tool_rounds=4):
        task = SimpleNamespace(
            id=SimpleNamespace(value="task_1"), project_id=h.project_id
        )
        attempt = SimpleNamespace(id=SimpleNamespace(value="att_1"))
        return runtime._run_tool_rounds(
            task=task,
            attempt=attempt,
            actor_id=h.actor_id,
            execution_id=h.execution_id,
            project_id=h.project_id,
            request=h.request,
            response=first_response,
            provider_request_id=provider_request.id,
            agent_scope="main_worker",
            tool_names=("run_command", "read_file"),
            max_tool_rounds=max_tool_rounds,
            cancel_event=None,
            lease_owner="harness",
            lease_duration_seconds=600,
            source="system",
        )

    h.run = run
    return h


def _tool_messages(messages):
    return [m for m in messages if getattr(m, "role", "") == "tool"]


# ----------------------------------------------------------------------
# 1: raised ToolError -> structured feedback, loop continues
# ----------------------------------------------------------------------


def test_raised_tool_error_feeds_back_and_continues(harness):
    """The exact real-run scenario: `bash` not allowlisted -> ToolError."""
    def invoke(**kwargs):
        raise ToolError("server-owned handler failed")

    harness.set_invoke(invoke)
    harness.state["queued_responses"] = [_response([])]  # next round: final answer

    final, _request_id, messages = harness.run(_response([_call()]))

    assert final.tool_calls == []
    tool_msgs = _tool_messages(messages)
    assert len(tool_msgs) == 1
    payload = json.loads(tool_msgs[0].content)
    assert payload["error"] == "tool_execution_failed"
    assert payload["tool"] == "run_command"
    assert "handler failed" in payload["detail"]
    assert "hint" in payload
    assert harness.state["invoke_calls"] == 1
    assert harness.state["provider_calls"] == 1  # exactly one follow-up round


# ----------------------------------------------------------------------
# 2: identical failures trip the breaker through the standard nudge path
# ----------------------------------------------------------------------


def test_identical_tool_errors_trip_breaker(harness, monkeypatch):
    from zero.app import agent_runtime as ar

    monkeypatch.setattr(ar, "_FAILURE_ABORT_THRESHOLD", 2, raising=False)

    def invoke(**kwargs):
        raise ToolError("server-owned handler failed")

    harness.set_invoke(invoke)
    final_response = _response([])
    harness.state["queued_responses"] = [final_response]  # consumed by the nudge

    calls = [_call(call_id="c1"), _call(call_id="c2")]  # two identical failures
    result, _request_id, messages = harness.run(_response(calls), max_tool_rounds=4)

    assert result is final_response  # the nudge (toolless) reply closed the loop
    assert harness.state["invoke_calls"] == 2  # both calls attempted, then abort
    assert len(_tool_messages(messages)) == 2


# ----------------------------------------------------------------------
# 3: denial feeds back, never aborts
# ----------------------------------------------------------------------


def test_denied_tool_feeds_back_without_aborting(harness):
    ok_result = SimpleNamespace(model_facing="done", status="success", error=None)
    flips = {"n": 0}

    def invoke(**kwargs):
        flips["n"] += 1
        if flips["n"] == 1:
            raise ToolInvocationDeniedError("not allowed for this scope")
        return ok_result

    harness.set_invoke(invoke)
    harness.state["queued_responses"] = [_response([])]

    harness.run(_response([_call()]))
    # No assertion on final content needed: reaching here means no
    # exception escaped and the loop returned a clean final response.
    assert flips["n"] == 1


# ----------------------------------------------------------------------
# 4+5: the enforced policy must be advertised where the model can see it
# ----------------------------------------------------------------------


def test_worktree_service_exposes_allowed_commands(test_settings):
    from zero.app.services import build_services
    from zero.persistence.connection import Database
    from zero.persistence.migrations import apply_migrations

    settings = test_settings.model_copy(
        update={"worktree_allowed_commands": ("python3", "ls", "cat")}
    )
    database = Database(settings)
    apply_migrations(database)
    services = build_services(settings, database)
    ws = services.worktree
    allowed = ws.allowed_commands
    assert isinstance(allowed, tuple)
    assert set(allowed) == {"cat", "ls", "python3"}
    assert all(isinstance(c, str) and "/" not in c for c in allowed)


def test_run_command_description_advertises_allowlist(test_settings):
    """The tool description must enumerate the permitted binaries."""
    from zero.app.services import build_services
    from zero.persistence.connection import Database
    from zero.persistence.migrations import apply_migrations

    settings = test_settings.model_copy(
        update={"worktree_allowed_commands": ("python3", "ls", "cat")}
    )
    database = Database(settings)
    apply_migrations(database)
    services = build_services(settings, database)
    ws = services.worktree
    tools = services.tools
    tools.register_worktree_tools(ws)
    tool = tools.get_tool_by_name("run_command")
    assert tool.description, "description must not be empty"
    for command in ws.allowed_commands:
        assert command in tool.description, (
            f"allowlisted command {command!r} missing from description: "
            f"{tool.description!r}"
        )
    assert "NO shell" in tool.description or "no shell" in tool.description


def test_persistent_tool_declaration_refresh_on_rebind(test_settings):
    """A stale persisted schema must be refreshed when the server-owned
    handler re-binds (run_command gained stdout/stderr result fields and
    old rows failed output validation against the stale contract)."""
    from zero.app.services import build_services
    from zero.persistence.connection import Database
    from zero.persistence.migrations import apply_migrations

    settings = test_settings.model_copy(
        update={"worktree_allowed_commands": ("python3", "ls", "cat")}
    )
    database = Database(settings)
    apply_migrations(database)
    services = build_services(settings, database)
    ws = services.worktree
    tools = services.tools
    tools.register_worktree_tools(ws)

    # Simulate an OLD persisted row: rewrite run_command's output schema
    # to the historical contract (no stdout/stderr), then re-bind.
    stale = tools.get_tool_by_name("run_command")
    old_schema = {
        "type": "object",
        "properties": {
            "run_id": {"type": "string"},
            "state": {"type": "string"},
            "exit_code": {"type": ["integer", "null"]},
            "artifact_ids": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["run_id", "state", "exit_code", "artifact_ids"],
        "additionalProperties": False,
    }
    tools._tool_repo.update_tool_declaration(
        stale.id,
        description="old description",
        input_schema=stale.input_schema,
        output_schema=old_schema,
    )
    assert "stdout" not in tools.get_tool_by_name("run_command").output_schema["properties"]

    # Re-bind: declaration must refresh in lockstep with the handler.
    tools.register_worktree_tools(ws)
    refreshed = tools.get_tool_by_name("run_command")
    assert "stdout" in refreshed.output_schema["properties"]
    assert "stderr" in refreshed.output_schema["properties"]
    assert "NO shell" in refreshed.description or "no shell" in refreshed.description
