"""Tests for the Anthropic Messages adapter and Hermes-parity behaviors.

Grounded in the audited reference wire spec (hermes-agent
``agent/anthropic_adapter.py``):
- system travels as a top-level block list with a cache breakpoint;
- assistant tool_calls render as tool_use blocks; tool results as
  adjacent-user tool_result blocks with orphan stripping;
- stop reasons map to the canonical finish vocabulary; usage classes
  are read directly (cache tokens are disjoint, no subtraction);
- empty content without end_turn/refusal is invalid;
- streaming accumulates input_json_delta per index and merges usage.
"""

from __future__ import annotations

import json
from threading import Event
from types import SimpleNamespace

import httpx
import pytest

from zero.app.agent_runtime import (
    MAX_TOOL_ROUNDS_NUDGE_REQUEST,
    AgentRuntime,
    RuntimeToolError,
)
from zero.app.compaction_service import COMPACTION_SUMMARIZER_SYSTEM, REQUIRED_SUMMARY_SECTIONS
from zero.app.provider_adapter import AnthropicMessagesProviderAdapter
from zero.config import Settings
from zero.domain.execution import ExecutionId, TaskId
from zero.domain.identity import ProjectId, UserId
from zero.domain.providers import (
    CanonicalMessage,
    CanonicalRequest,
    CanonicalResponse,
    ProviderError,
    ProviderRequestId,
    ToolCallResult,
    ToolDeclaration,
)
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations


@pytest.fixture
def services(test_settings: Settings):
    database = Database(test_settings)
    apply_migrations(database)
    from zero.app.services import build_services

    return build_services(test_settings, database)


def _adapter(handler) -> AnthropicMessagesProviderAdapter:
    return AnthropicMessagesProviderAdapter(
        api_key="test-key",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _request(**overrides) -> CanonicalRequest:
    fields: dict = {
        "provider": "anthropic",
        "model_name": "claude-sonnet-4",
        "messages": (CanonicalMessage(role="user", content="hello"),),
        "max_tokens": 1024,
    }
    fields.update(overrides)
    return CanonicalRequest(**fields)


# ----------------------------------------------------------------------
# Request rendering
# ----------------------------------------------------------------------


def test_request_maps_system_tools_and_tool_history() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "hi"}],
                "usage": {"input_tokens": 5, "output_tokens": 2},
            },
        )

    adapter = _adapter(handler)
    request = _request(
        system_message="You are Zero.",
        tools=(
            ToolDeclaration(
                name="write_file",
                description="Write it.",
                parameters={"type": "object", "properties": {"path": {"type": "string"}}},
            ),
        ),
        messages=(
            CanonicalMessage(role="user", content="do it"),
            CanonicalMessage(
                role="assistant",
                content="",
                tool_calls=(("write_file", "call_1", '{"path": "a.txt"}'),),
            ),
            CanonicalMessage(role="tool", content="written", tool_call_id="call_1"),
        ),
    )
    response = adapter.send_request(request)
    payload = captured["payload"]
    # Mandatory sampling field and top-level system blocks with caching.
    assert payload["max_tokens"] == 1024
    assert payload["system"][0]["text"] == "You are Zero."
    assert payload["system"][0]["cache_control"] == {"type": "ephemeral"}
    # Tools carry real input_schema plus a trailing cache breakpoint.
    assert payload["tools"][0]["input_schema"]["properties"] == {"path": {"type": "string"}}
    assert payload["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    # History: assistant tool_use then adjacent user tool_result.
    messages = payload["messages"]
    assert messages[0]["content"][0]["text"] == "do it"
    assistant_blocks = messages[1]["content"]
    assert assistant_blocks[0]["type"] == "tool_use"
    assert assistant_blocks[0]["id"] == "call_1"
    assert assistant_blocks[0]["input"] == {"path": "a.txt"}
    result_block = messages[2]["content"][0]
    assert result_block["type"] == "tool_result"
    assert result_block["tool_use_id"] == "call_1"
    assert result_block["content"] == "written"
    # Response parsing: text joined, usage direct (no subtraction).
    assert response.content == "hi"
    assert response.finish_reason == "stop"
    assert response.usage.input_tokens == 5
    assert response.usage.output_tokens == 2
    assert response.provider_message_id == "msg_1"


def test_orphan_tool_pairs_are_stripped() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "id": "m",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "ok"}],
                "usage": {},
            },
        )

    adapter = _adapter(handler)
    request = _request(
        messages=(
            CanonicalMessage(role="user", content="go"),
            CanonicalMessage(
                role="assistant",
                content="",
                tool_calls=(
                    ("echo", "keep_1", "{}"),
                    ("echo", "drop_1", "{}"),
                ),
            ),
            CanonicalMessage(role="tool", content="done", tool_call_id="keep_1"),
        )
    )
    adapter.send_request(request)
    messages = captured["payload"]["messages"]
    kept = [
        block
        for block in messages[1]["content"]
        if block.get("type") == "tool_use" or block.get("text") != "(tool call removed)"
    ]
    tool_uses = [block for block in messages[1]["content"] if block.get("type") == "tool_use"]
    assert [block["id"] for block in tool_uses] == ["keep_1"]
    assert any(block.get("text") == "(tool call removed)" for block in messages[1]["content"])
    assert kept  # placeholder present alongside surviving call


def test_stop_reason_and_usage_mapping() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "m",
                "stop_reason": "tool_use",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "echo",
                        "input": {"message": "x"},
                    }
                ],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 4,
                    "cache_read_input_tokens": 6,
                    "cache_creation_input_tokens": 3,
                },
            },
        )

    response = _adapter(handler).send_request(_request())
    assert response.finish_reason == "tool_calls"
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].arguments == '{"message": "x"}'
    # Disjoint classes preserved verbatim (no subtraction).
    assert response.usage.input_tokens == 10
    assert response.usage.cache_read_input_tokens == 6
    assert response.usage.cache_creation_input_tokens == 3


def test_empty_content_without_terminal_stop_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"id": "m", "stop_reason": None, "content": [], "usage": {}},
        )

    with pytest.raises(ProviderError):
        _adapter(handler).send_request(_request())


def test_http_status_error_classification() -> None:
    def make_handler(status: int):
        return lambda request: httpx.Response(status, json={})

    adapter = _adapter(make_handler(401))
    with pytest.raises(ProviderError) as exc_info:
        adapter.send_request(_request())
    assert "auth failed" in str(exc_info.value)

    rate_limited = _adapter(make_handler(429))
    with pytest.raises(ProviderError) as exc_info:
        rate_limited.send_request(_request())
    assert "rate limit" in str(exc_info.value)


# ----------------------------------------------------------------------
# Streaming
# ----------------------------------------------------------------------


def _sse_body() -> bytes:
    events = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_sse",
                "usage": {"input_tokens": 7},
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "tu_9", "name": "echo"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"mes'},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": 'sage": "hi"}'},
        },
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 3},
        },
        {"type": "message_stop"},
    ]
    lines = "".join(f"event: x\ndata: {json.dumps(e)}\n\n" for e in events)
    return lines.encode("utf-8")


def test_streaming_accumulates_tool_input_and_usage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse_body())

    adapter = _adapter(handler)
    events = list(adapter.send_request_stream(_request()))
    tool_events = [e for e in events if e.kind == "tool_call_delta"]
    assert len(tool_events) == 1
    call = tool_events[0].tool_call
    assert call.tool_name == "echo"
    assert call.tool_call_id == "tu_9"
    assert json.loads(call.arguments) == {"message": "hi"}
    usage_events = [e for e in events if e.kind == "usage"]
    assert usage_events[0].usage.input_tokens == 7
    assert usage_events[0].usage.output_tokens == 3
    end_event = events[-1]
    assert end_event.kind == "message_end"
    assert end_event.finish_reason == "tool_calls"


def test_streaming_mid_tool_drop_is_unknown_outcome() -> None:
    truncated = (
        b"data: "
        + json.dumps(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "t", "name": "n"},
            }
        ).encode()
        + b"\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=truncated)

    from zero.domain.providers import ProviderUnknownOutcomeError

    adapter = _adapter(handler)
    with pytest.raises(ProviderUnknownOutcomeError):
        list(adapter.send_request_stream(_request()))


# ----------------------------------------------------------------------
# Model catalog
# ----------------------------------------------------------------------


def test_anthropic_model_catalog_defaults_conservative() -> None:
    adapter = _adapter(lambda request: httpx.Response(200, json={}))
    known = adapter.get_model("claude-sonnet-4")
    assert known.context_window == 200_000
    unknown = adapter.get_model("future-model-x")
    assert unknown.context_window < 200_000


# ----------------------------------------------------------------------
# Compaction summarizer wiring
# ----------------------------------------------------------------------


def _approved_execution(services, project_name: str):
    """Create a real approved-plan execution (compaction lineage)."""
    from zero.app.worker_service import TaskSpec
    from zero.domain.plans import PlanRevisionContent

    owner = services.identity.create_user(display_name=project_name)
    project = services.identity.create_project(owner_id=owner.id, name=project_name)
    event = services.plans.ingest_conversation_event(
        project_id=project.id,
        actor_id=owner.id,
        source="web",
        origin_kind="authenticated_human",
        content="Add a feature.",
    )
    plan = services.plans.create_plan(project_id=project.id, actor_id=owner.id)
    content = PlanRevisionContent(
        objective="Add a feature",
        scope=(),
        constraints=(),
        acceptance_criteria=("Works",),
        risks=(),
        unresolved_questions=(),
        source_event_ids=(event.id,),
    )
    services.plans.propose_revision(
        plan_id=plan.id, project_id=project.id, actor_id=owner.id, content=content
    )
    _, handoff = services.plans.approve_revision(
        plan_id=plan.id,
        project_id=project.id,
        actor_id=owner.id,
        expected_revision_number=1,
        idempotency_key=f"{project_name}-a1",
    )
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        project_id=project.id,
        actor_id=owner.id,
        task_specs=[TaskSpec(key="A", objective="Task A")],
    )
    return owner, project, execution


def test_compaction_prefers_llm_summary_and_falls_back(services) -> None:
    owner, project, execution = _approved_execution(services, "Compact Owner")
    execution_id = execution.id

    valid_llm_summary = "\n".join(
        f"{section}: details for {section}" for section in REQUIRED_SUMMARY_SECTIONS
    )

    # A working LLM summarizer wins over the template.
    services.compaction.summarizer = lambda **kwargs: valid_llm_summary
    record = services.compaction.compact(
        project_id=project.id,
        execution_id=execution_id,
        actor_id=owner.id,
        system_message="sys",
        user_prefix="proj",
        plan_contract="plan",
        execution_snapshot="{}",
        conversation_messages=[{"role": "user", "content": "work"}],
        context_window=100000,
    )
    assert record.summary == valid_llm_summary

    # A failing summarizer degrades to the deterministic template.
    def boom(**kwargs):
        raise RuntimeError("provider down")

    owner_two, project_two, execution_two = _approved_execution(services, "Compact Fallback")
    services.compaction.summarizer = boom
    record_two = services.compaction.compact(
        project_id=project_two.id,
        execution_id=execution_two.id,
        actor_id=owner_two.id,
        system_message="sys",
        user_prefix="proj",
        plan_contract="plan",
        execution_snapshot="{}",
        conversation_messages=[{"role": "user", "content": "work"}],
        context_window=100000,
    )
    assert record_two.summary.startswith("Compaction summary")
    # The composition default is the wired LLM path; restore for safety.
    services.compaction.summarizer = None


def test_composition_wires_real_summarizer_with_fake_provider(services) -> None:
    """The composed bundle wires an LLM summarizer that routes through
    the registered (fake) provider and still falls back cleanly."""
    owner, project, execution = _approved_execution(services, "CompactWired Owner")
    summarizer = services.compaction.summarizer
    assert summarizer is not None
    result = summarizer(
        project_id=project.id,
        execution_id=execution.id,
        actor_id=owner.id,
        messages=[{"role": "user", "content": "hello"}],
    )
    assert result is not None
    assert COMPACTION_SUMMARIZER_SYSTEM  # constant sanity


# ----------------------------------------------------------------------
# Tool-round nudge (Hermes parity)
# ----------------------------------------------------------------------


class _NudgeProvider:
    """Records requests; scripted responses per dispatch order."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def send_request_with_fallback(self, *, request, **_kwargs):
        self.requests.append(request)
        response = self.responses.pop(0)
        return SimpleNamespace(id=ProviderRequestId("preq_nudge")), response


def _runtime_with(provider_double):
    return AgentRuntime(
        worker=SimpleNamespace(renew_task_lease=lambda **kw: None),
        providers=provider_double,
        artifacts=SimpleNamespace(),
        authorization=SimpleNamespace(),
        tools=SimpleNamespace(invoke=lambda **kw: SimpleNamespace(model_facing="ok")),
    )


_TOOLING_RESPONSE = CanonicalResponse(
    content="", tool_calls=(ToolCallResult("echo", "c1", "{}", ""),), finish_reason="tool_calls"
)


def test_nudge_round_returns_final_answer_when_model_stops() -> None:
    # Round 1 keeps calling tools; the single nudge request then yields
    # the final answer.
    final = CanonicalResponse(content="All done.", tool_calls=(), finish_reason="stop")
    provider = _NudgeProvider([_TOOLING_RESPONSE, final])
    runtime = _runtime_with(provider)

    from zero.domain.execution import TaskAttemptId

    task = SimpleNamespace(id=TaskId("task_n"), project_id=ProjectId("p_nudge"))
    attempt = SimpleNamespace(id=TaskAttemptId("att_n"))
    base_request = CanonicalRequest(provider="fake", model_name="m", messages=())
    result_response, request_id, _final_messages = runtime._run_tool_rounds(
        task=task,
        attempt=attempt,
        actor_id=UserId("zu_nudge"),
        execution_id=ExecutionId("exec_n"),
        project_id=ProjectId("p_nudge"),
        request=base_request,
        response=_TOOLING_RESPONSE,
        provider_request_id=ProviderRequestId("preq_0"),
        agent_scope="main_worker",
        tool_names=("echo",),
        max_tool_rounds=1,
        cancel_event=Event(),
        lease_owner="w",
        lease_duration_seconds=300,
        source="system",
    )
    assert result_response.content == "All done."
    nudge_request = provider.requests[-1]
    assert nudge_request.tools == ()
    assert any(
        message.role == "user" and message.content == MAX_TOOL_ROUNDS_NUDGE_REQUEST
        for message in nudge_request.messages
    )
    assert isinstance(request_id, ProviderRequestId)


def test_nudge_round_failure_still_raises_after_stubborn_model() -> None:
    provider = _NudgeProvider([_TOOLING_RESPONSE, _TOOLING_RESPONSE, _TOOLING_RESPONSE])
    runtime = _runtime_with(provider)

    from zero.domain.execution import TaskAttemptId

    task = SimpleNamespace(id=TaskId("task_n2"), project_id=ProjectId("p_nudge"))
    attempt = SimpleNamespace(id=TaskAttemptId("att_n2"))
    base_request = CanonicalRequest(provider="fake", model_name="m", messages=())
    with pytest.raises(RuntimeToolError):
        runtime._run_tool_rounds(
            task=task,
            attempt=attempt,
            actor_id=UserId("zu_nudge"),
            execution_id=ExecutionId("exec_n2"),
            project_id=ProjectId("p_nudge"),
            request=base_request,
            response=_TOOLING_RESPONSE,
            provider_request_id=ProviderRequestId("preq_0"),
            agent_scope="main_worker",
            tool_names=("echo",),
            max_tool_rounds=1,
            cancel_event=Event(),
            lease_owner="w",
            lease_duration_seconds=300,
            source="system",
        )
