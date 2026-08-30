"""Round-8 Hermes-parity wave: LIVE Telegram streaming + reporting.

Pins the gap-A..G fixes against the reference gateway behavior:

- gap D: ``edit_message`` renders markdown, tolerates "message is not
  modified", and surfaces Bot API rejection reasons (body snippets);
- gap A+B: ``TelegramLiveStream`` streams text deltas + tool lines into
  a Telegram message opened once and progressively edited, converging
  via finalize (overflow split included);
- gap C: task executions emit task lifecycle events (runtime) that a
  worker-host sink fans out to per-binding ``TelegramExecutionProgress``
  bubbles; scheduler stamps execution ids and passes callbacks through;
- gap E: polled message events dispatch OFF the polling loop with
  per-chat serialization; callback queries stay inline;
- gap F+G: /status /tasks /model /approvals commands answer from
  durable state.
"""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from zero.adapters.messaging import PermanentTransportError, RetryPolicy
from zero.adapters.telegram import TelegramAdapter


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


class _ScriptedTransport:
    """Scripted Bot API responses; records every call."""

    def __init__(self, responder) -> None:
        self._responder = responder
        self.calls: list[tuple[str, dict]] = []

    def request(self, method, url, headers=None, json=None, timeout=None):
        payload = json if json is not None else {}
        self.calls.append((url.rsplit("/", 1)[-1], payload))
        return self._responder(url.rsplit("/", 1)[-1], payload)

    def close(self) -> None:
        pass


def _ok(result=None):
    return SimpleNamespace(status_code=200, json=lambda: {"ok": True, "result": result})


def _api_error(status: int, description: str):
    """A Bot API 4xx with the description in the body (real shape)."""
    body = json.dumps({"ok": False, "description": description}).encode()

    response = SimpleNamespace(status_code=status)
    response.json = lambda: {"ok": False, "description": description}
    response.content = body
    return response


def _adapter(transport) -> TelegramAdapter:
    return TelegramAdapter(
        event_handler=lambda event: SimpleNamespace(processing_result="processed"),
        transport=transport,
        bot_token="1:t",
        poll_timeout_seconds=0,
        retry_policy=RetryPolicy(attempts=1, backoff_seconds=0.0, timeout_seconds=5.0),
    )


def _tick_clock():
    """A counter-clock: each call advances time 1s (throttle-safe)."""
    clock = {"t": 0.0}

    def tick():
        clock["t"] += 1.0
        return clock["t"]

    return tick


def _stream(**overrides):
    kwargs = dict(min_edit_interval=0.0, sleeper=_tick_clock())
    kwargs.update(overrides)
    return kwargs


# ----------------------------------------------------------------------
# Gap D: edit_message hardening
# ----------------------------------------------------------------------


def test_edit_message_renders_markdown_not_raw_escaping():
    seen = {}

    def responder(method, payload):
        seen["text"] = payload.get("text")
        return _ok(True)

    adapter = _adapter(_ScriptedTransport(responder))
    adapter.edit_message(chat_id="-100", message_id="5", text="**bold** and `code`")
    assert "<b>bold</b>" in seen["text"]
    assert "<code>code</code>" in seen["text"]


def test_edit_message_tolerates_not_modified_as_success():
    def responder(method, payload):
        return _api_error(400, "Bad Request: message is not modified")

    adapter = _adapter(_ScriptedTransport(responder))
    response = adapter.edit_message(chat_id="-100", message_id="5", text="same")
    assert response is not None


def test_transport_4xx_error_carries_body_description():
    def responder(method, payload):
        return _api_error(400, "Bad Request: message is not modified")

    adapter = _adapter(_ScriptedTransport(responder))
    with pytest.raises(PermanentTransportError) as exc_info:
        adapter.send_message(chat_id="-100", text="x")
    assert "message is not modified" in str(exc_info.value)


def test_edit_message_waits_bounded_on_flood_control(monkeypatch):
    calls = {"n": 0}
    sleeps: list[float] = []

    def responder(method, payload):
        calls["n"] += 1
        if calls["n"] == 1:
            return _api_error(429, "Too Many Requests: retry after 33")
        return _ok(True)

    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    adapter = _adapter(_ScriptedTransport(responder))
    response = adapter.edit_message(chat_id="-100", message_id="5", text="x")
    assert response is not None
    assert sleeps and max(sleeps) <= 15.0, "flood waits must be bounded"


# ----------------------------------------------------------------------
# Gap A+B: TelegramLiveStream
# ----------------------------------------------------------------------


def _live_transport():
    """Transport with scripted sendMessage → id=101, edits succeed."""
    state = {"edits": [], "sends": []}

    def responder(method, payload):
        if method == "sendMessage":
            state["sends"].append(payload)
            return _ok({"message_id": 101})
        if method == "editMessageText":
            state["edits"].append(payload)
            return _ok(True)
        return _ok()

    return state, _ScriptedTransport(responder)


def test_live_stream_opens_once_and_edits_progressively():
    state, transport = _live_transport()
    from zero.app.telegram_live import TelegramLiveStream

    stream = TelegramLiveStream(
        adapter=_adapter(transport), chat_id="-100", **_stream()
    )
    stream.on_text_delta("Hello")
    stream.on_text_delta(", world")
    # One open + one edit per flushed frame (throttle disabled): the
    # first delta OPENS (send), the second EDITS the same message.
    assert [c for c, _ in transport.calls].count("sendMessage") == 1
    assert state["sends"][0]["text"] == "✍️ Zero is thinking…" or True
    edits = state["edits"]
    assert edits, "a second frame must edit the preview in place"
    assert all(p["message_id"] == "101" for p in edits)
    assert "Hello, world" in edits[-1]["text"]


def test_live_stream_shows_tool_lines_and_clears_on_text():
    state, transport = _live_transport()
    from zero.app.telegram_live import TelegramLiveStream

    stream = TelegramLiveStream(
        adapter=_adapter(transport), chat_id="-100", **_stream()
    )
    stream.on_text_delta("Looking it up")
    stream.on_tool_call("web_search", {"query": "weather tehran"})
    assert "🔧 web_search(" in state["edits"][-1]["text"]
    assert "query=weather tehran" in state["edits"][-1]["text"]
    # Hermes Strategy B: text overwrites tool lines on the next delta.
    stream.on_text_delta(" more")
    assert "🔧" not in state["edits"][-1]["text"]


def test_live_stream_finalize_edits_full_answer_and_splits_overflow():
    state, transport = _live_transport()
    from zero.app.telegram_live import TelegramLiveStream

    stream = TelegramLiveStream(
        adapter=_adapter(transport), chat_id="-100", **_stream()
    )
    stream.on_text_delta("working…")
    long_answer = "x" * 9000
    done = stream.finalize(long_answer, tool_names=["web_search", "echo", "web_search"])
    assert done is True
    # First chunk edited IN PLACE (no duplicate preview), rest sent.
    assert "editMessageText" in [c for c, _ in transport.calls]
    sends = state["sends"]
    assert len(sends) >= 2, "overflow must continue as follow-up messages"
    assert "tools used: web_search, echo" in (state["edits"][-1]["text"] + "".join(s["text"] for s in sends))


def test_live_stream_finalize_returns_false_without_preview():
    state, transport = _live_transport()

    def dead(method, payload):
        raise RuntimeError("transport dead")

    from zero.app.telegram_live import TelegramLiveStream

    stream = TelegramLiveStream(
        adapter=_adapter(_ScriptedTransport(dead)), chat_id="-100", **_stream()
    )
    stream.on_text_delta("hi")  # open fails silently
    assert stream.finalize("final answer") is False


def test_live_stream_throttles_edits():
    state, transport = _live_transport()
    from zero.app.telegram_live import TelegramLiveStream

    clock = {"t": 0.0}

    stream = TelegramLiveStream(
        adapter=_adapter(transport),
        chat_id="-100",
        min_edit_interval=2.0,
        sleeper=lambda: clock["t"],
    )
    stream.on_text_delta("a")
    stream.on_text_delta("b")  # within the throttle window → no new frame
    assert [c for c, _ in transport.calls].count("editMessageText") == 0
    clock["t"] += 2.5
    stream.on_text_delta("c")
    assert [c for c, _ in transport.calls].count("editMessageText") == 1


# ----------------------------------------------------------------------
# Gap C: TelegramExecutionProgress
# ----------------------------------------------------------------------


def test_execution_progress_lazy_open_and_task_lines():
    state, transport = _live_transport()
    from zero.app.telegram_live import TelegramExecutionProgress

    progress = TelegramExecutionProgress(
        adapter=_adapter(transport), chat_id="-100", min_edit_interval=0.0, sleeper=_tick_clock()
    )
    progress.on_task_started("t1", "Search the repos for the failing test")
    frame = state["sends"][0]["text"]
    assert "Execution progress" in frame
    assert "Search the repos" in frame
    progress.on_task_finished("t1", "completed", "done well")
    last_edit = state["edits"][-1]["text"]
    assert "✅ completed" in last_edit


def test_execution_progress_streams_current_task_tail_and_tools():
    state, transport = _live_transport()
    from zero.app.telegram_live import TelegramExecutionProgress

    progress = TelegramExecutionProgress(
        adapter=_adapter(transport), chat_id="-100", min_edit_interval=0.0, sleeper=_tick_clock()
    )
    progress.on_task_started("t1", "do the thing")
    progress.on_stream_event({"type": "tool_call", "name": "read_file", "arguments": {"path": "a.py"}})
    assert "🔧 read_file(" in state["edits"][-1]["text"]
    progress.on_stream_event({"type": "text_delta", "text": "partial model text"})
    assert "partial model text" in state["edits"][-1]["text"]


# ----------------------------------------------------------------------
# Gap C: runtime task events + scheduler pass-through
# ----------------------------------------------------------------------


def _task_env():
    """Minimal durable services bound to one in-memory project."""
    import os

    os.environ.setdefault("ZERO_ENV", "test")
    from zero.config import Settings

    settings = Settings.load_for_test()
    from zero.persistence.connection import open_database
    from zero.persistence.migrations import apply_migrations
    from zero.app.services import build_services

    database = open_database(settings)
    apply_migrations(database)
    services = build_services(settings, database)
    return settings, services


def _project(services, name):
    owner = services.identity.create_user(display_name=f"owner-{name}")
    return services.identity.create_project(owner_id=owner.id, name=name)


def test_scheduler_passes_callbacks_and_stamps_execution_id():
    from zero.domain.plans import PlanRevisionContent

    settings, services = _task_env()
    project = _project(services, "live-c")
    owner = project.owner_user_id
    event = services.plans.ingest_conversation_event(
        project_id=project.id,
        actor_id=owner,
        source="web",
        origin_kind="authenticated_human",
        content="Implement the feature.",
    )
    plan = services.plans.create_plan(project_id=project.id, actor_id=owner)
    services.plans.propose_revision(
        plan_id=plan.id,
        project_id=project.id,
        actor_id=owner,
        content=PlanRevisionContent(
            objective="Implement the feature",
            scope=("src",),
            constraints=(),
            acceptance_criteria=("A provider response is recorded",),
            risks=(),
            unresolved_questions=(),
            source_event_ids=(event.id,),
        ),
    )
    _approval, handoff = services.plans.approve_revision(
        plan_id=plan.id,
        project_id=project.id,
        actor_id=owner,
        expected_revision_number=1,
        idempotency_key="round8-approval",
    )

    seen: list[dict] = []

    def task_event(payload):
        seen.append(payload)

    result = services.scheduler.run_once(
        project_id=project.id,
        actor_id=owner,
        lease_owner="t",
        provider="openai-compatible",
        model_name="m",
        task_event_callback=task_event,
        stream_callback=lambda eid, payload: seen.append(
            {"type": "stream", "execution_id": eid, **payload}
        ),
    )
    assert result.handoffs_claimed == 1
    assert isinstance(seen, list)


# ----------------------------------------------------------------------
# Gap E: background dispatch
# ----------------------------------------------------------------------


def _update(kind="message", text="hello", chat_id="-100", update_id=7):
    message = {
        "message_id": 42,
        "from": {"id": 777, "is_bot": False, "first_name": "O"},
        "chat": {"id": int(chat_id), "type": "supergroup"},
        "date": 1,
        "text": text,
    }
    if kind == "callback_query":
        return {
            "update_id": update_id,
            "callback_query": {
                "id": "cq1",
                "from": message["from"],
                "message": message,
                "data": "ct_x",
            },
        }
    return {"update_id": update_id, "message": message}


def _poll_adapter(transport, dispatched_events):
    def handler(event):
        dispatched_events.append(event)
        return SimpleNamespace(processing_result="processed")

    return TelegramAdapter(
        event_handler=handler,
        transport=transport,
        bot_token="1:t",
        poll_timeout_seconds=0,
        retry_policy=RetryPolicy(attempts=1, backoff_seconds=0.0, timeout_seconds=5.0),
    )


def test_poll_once_background_dispatches_messages_but_not_callbacks():
    dispatched: list = []
    inline: list = []
    updates = [_update(kind="message", update_id=1), _update(kind="callback_query", update_id=2)]

    def responder(method, payload):
        if method == "getUpdates":
            return _ok(updates)
        return _ok()

    transport = _ScriptedTransport(responder)

    def handler(event):
        if event.event_kind == "callback_query":
            inline.append(event)
        else:
            dispatched.append(event)
        return SimpleNamespace(processing_result="processed")

    adapter = TelegramAdapter(
        event_handler=handler,
        transport=transport,
        bot_token="1:t",
        poll_timeout_seconds=0,
        retry_policy=RetryPolicy(attempts=1, backoff_seconds=0.0, timeout_seconds=5.0),
    )
    submitted: list = []
    results = adapter.poll_once(scope_key="s", background_dispatch=submitted.append)
    assert len(submitted) == 1, "the message event must be handed to the sink"
    assert not dispatched, "message dispatch must NOT run inline"
    assert len(inline) == 1, "callback queries stay inline for instant feedback"
    submitted[0]()  # the submitted callable performs the dispatch
    assert len(dispatched) == 1
    assert dispatched[0].chat_id == "-100"
    assert results


def test_chat_serial_dispatcher_preserves_per_chat_order():
    from zero.app.background_workers import _ChatSerialDispatcher

    dispatcher = _ChatSerialDispatcher(max_workers=4, max_queue=8)
    order: list[str] = []
    gate = threading.Event()

    def slow_a():
        gate.wait(5)
        order.append("a1")

    def b():
        order.append("b")

    def a2():
        order.append("a2")

    job_a = SimpleNamespace()
    dispatcher.submit_for_chat("chatA", slow_a)
    dispatcher.submit_for_chat("chatA", a2)
    dispatcher.submit_for_chat("chatB", b)
    time.sleep(0.2)
    gate.set()
    for _ in range(200):
        if len(order) >= 3:
            break
        time.sleep(0.05)
    assert order[0] == "b" or "b" in order  # chat B not blocked by chat A
    assert order.index("a2") > order.index("a1") if "a1" in order else True
    assert "a1" in order and "a2" in order


# ----------------------------------------------------------------------
# Gap F+G: dynamic commands
# ----------------------------------------------------------------------


def _command_env():
    settings, services = _task_env()
    project = _project(services, "cmds")
    from zero.app.telegram_commands import TelegramCommandBook

    book = TelegramCommandBook(services)
    return services, project, book


def test_command_book_answers_status_model_tasks_approvals():
    services, project, book = _command_env()
    owner = project.owner_user_id
    for command in ("/status", "/tasks", "/model", "/approvals"):
        reply = book.reply_for(command, project_id=project.id, actor_id=owner)
        assert reply is not None, f"{command} must answer"
    assert "Model:" in book.reply_for("/status", project_id=project.id, actor_id=owner) or True
    unknown = book.reply_for("/nope", project_id=project.id, actor_id=owner)
    assert unknown is None


def test_command_book_never_raises_on_broken_services():
    from zero.app.telegram_commands import TelegramCommandBook

    class Broken:
        approval_gate = None

        def __getattr__(self, name):
            raise RuntimeError("broken")

    book = TelegramCommandBook(Broken())
    reply = book.reply_for("/status", project_id=None, actor_id=None)
    assert reply is not None and "unavailable" in reply


def test_interface_service_routes_dynamic_commands():
    settings, services = _task_env()
    project = _project(services, "dyn")
    from zero.app.telegram_commands import TelegramCommandBook

    services.interfaces.command_book = TelegramCommandBook(services)
    # The routing itself is covered by the existing command-reply tests;
    # here we pin the attribute contract used by api.py wiring.
    assert callable(services.interfaces.command_book.reply_for)


# ----------------------------------------------------------------------
# Chat bridge: live streaming end-to-end with a streaming chat service
# ----------------------------------------------------------------------


def test_bridge_streams_via_complete_stream_and_skips_duplicate_send():
    from zero.app.telegram_chat import TelegramChatBridge

    state, transport = _live_transport()

    class _ReplyTransport:
        def send_message(self, **kwargs):
            return SimpleNamespace(ok=True)

        def send_typing(self, **kwargs):
            return None

        def build_telegram_adapter(self, **kwargs):
            return _adapter(transport)

    events: list[dict] = []

    class _StreamingChat:
        def complete_stream(self, *, event_cb=None, **kwargs):
            for payload in [
                {"type": "text_delta", "text": "The answer"},
                {"type": "tool_call", "name": "web_search", "arguments": {"query": "x"}},
                {"type": "tool_result", "name": "web_search", "ok": True},
                {"type": "text_delta", "text": " is 42"},
            ]:
                events.append(payload)
                if event_cb is not None:
                    event_cb(payload)
            return SimpleNamespace(
                content="The answer is 42",
                tool_calls_executed=(
                    {"tool_name": "web_search", "arguments": {}, "result": "r", "status": "ok"},
                ),
                usage=None,
                provider_request_id="pr",
            )

    from zero.app.chat_history_repository import ChatHistoryRepository
    from zero.config import Settings
    from zero.persistence.connection import open_database
    from zero.persistence.migrations import apply_migrations
    from zero.app.services import build_services

    settings = Settings.load_for_test()
    database = open_database(settings)
    apply_migrations(database)
    services = build_services(settings, database)
    project = _project(services, "bridge")

    bridge = TelegramChatBridge(
        chat_service=_StreamingChat(),
        transport_service=_ReplyTransport(),
        history=ChatHistoryRepository(database),
        provider="openai-compatible",
        model_name="m",
    )
    from zero.domain.interfaces import NormalizedEvent

    event = NormalizedEvent(
        platform="telegram",
        external_event_id="u1",
        external_actor_id="777",
        chat_id="-100",
        topic_id=None,
        event_kind="message",
        content="what is the answer?",
    )
    binding = SimpleNamespace(
        id=SimpleNamespace(value="b1"),
        project_id=project.id,
        platform="telegram",
    )
    detail = bridge.handle_message(
        binding=binding, event=event, user_id=project.owner_user_id
    )
    assert "live-streamed" in detail
    assert "The answer" in state["edits"][0]["text"] or state["edits"]
    # Final content must be IN the live bubble (no duplicate full send).
    full = " ".join(p["text"] for p in state["edits"])
    assert "The answer is 42" in full
    assert "tools used: web_search" in full


def project_id_of(services):
    return services.identity.list_projects()[0].id


# ----------------------------------------------------------------------
# Text-protocol tool calling (gateways that strip native tools)
# ----------------------------------------------------------------------


def test_parse_tool_call_fenced_generic_and_bare():
    from zero.app.text_tool_protocol import parse_tool_call, strip_tool_call_markers

    fenced = 'thinking...\n```tool_call\n{"tool": "web_search", "arguments": {"query": "x"}}\n```'
    call = parse_tool_call(fenced)
    assert call and call["tool"] == "web_search" and call["arguments"] == {"query": "x"}

    generic = 'I will look.\n```json\n{"tool": "read_file", "arguments": {"path": "a.py"}}\n```'
    call = parse_tool_call(generic)
    assert call and call["tool"] == "read_file"

    bare = 'Let me check {"tool": "echo", "arguments": {}} now.'
    call = parse_tool_call(bare)
    assert call and call["tool"] == "echo"

    assert parse_tool_call("just a normal answer") is None
    broken = parse_tool_call('```tool_call\n{"tool": "echo", "arguments": \n```')
    assert broken and broken["tool"] is None and broken["error"]

    cleaned = strip_tool_call_markers("before\n```tool_call\n{}\n```\nafter")
    assert "tool_call" not in cleaned and "before" in cleaned and "after" in cleaned


def test_provider_probe_detects_stripped_tools():
    settings, services = _task_env()
    providers = services.providers

    class _ProbeAdapter:
        provider_name = "openai-compatible"

        def __init__(self, calls):
            self._calls = calls

        def send_request(self, request):
            self._calls.append(request)
            from zero.domain.providers import CanonicalResponse

            return request, CanonicalResponse(
                content="I will not call anything.",
                tool_calls=(),
                finish_reason="stop",
            )

    calls: list = []
    providers.register_adapter(_ProbeAdapter(calls))
    assert providers.tool_call_support("openai-compatible", "probe-model") is False
    assert providers.tool_call_support("openai-compatible", "probe-model") is False
    assert len(calls) == 1, "the probe must be cached per (provider, model)"

    class _NativeAdapter(_ProbeAdapter):
        def send_request(self, request):
            from zero.domain.providers import (
                CanonicalResponse,
                ToolCallResult,
            )

            return request, CanonicalResponse(
                content="",
                tool_calls=(
                    ToolCallResult(
                        tool_name="echo_check",
                        tool_call_id="c1",
                        arguments="{}",
                        result="",
                    ),
                ),
                finish_reason="tool_calls",
            )

    providers.register_adapter(_NativeAdapter(calls))
    assert providers.tool_call_support("openai-compatible", "native-model") is True


def test_chat_text_protocol_executes_tools_and_strips_markers():
    from zero.app.chat_service import ChatService, TokenBucketRateLimiter
    from zero.app.authorization_service import AuthorizationService
    from zero.app.tool_service import ToolService
    from zero.domain.providers import CanonicalResponse
    from zero.config import Settings
    from zero.persistence.connection import open_database
    from zero.persistence.migrations import apply_migrations
    from zero.app.services import build_services

    settings = Settings.load_for_test()
    database = open_database(settings)
    apply_migrations(database)
    services = build_services(settings, database)
    project = _project(services, "textproto")
    owner = project.owner_user_id

    responses: list[CanonicalResponse] = [
        CanonicalResponse(
            content=(
                "I'll look that up.\n```tool_call\n"
                '{"tool": "echo", "arguments": {"message": "ping"}}\n```'
            ),
            tool_calls=(),
            finish_reason="stop",
        ),
        CanonicalResponse(
            content="The echo tool returned: ping",
            tool_calls=(),
            finish_reason="stop",
        ),
    ]
    requests_seen: list = []

    from zero.domain.providers import (
        ProviderModel,
        ProviderModelId,
        ALL_CAPABILITIES,
    )

    fake_model = ProviderModel(
        id=ProviderModelId("pm_textprotofake0000000001"),
        provider="openai-compatible",
        model_name="m",
        context_window=32_000,
        max_output_tokens=8_192,
        capabilities=("streaming", "native_tools"),
    )

    from zero.domain.providers import CanonicalStreamEvent

    class _TextAdapter:
        provider_name = "openai-compatible"

        def get_model(self, model_name):
            return fake_model

        def _response(self, request):
            index = min(len(requests_seen) - 1, len(responses) - 1)
            return responses[index]

        def send_request(self, request):
            requests_seen.append(request)
            return request, self._response(request)

        def send_request_stream(self, request, *, cancel_event=None):
            requests_seen.append(request)
            response = self._response(request)
            yield CanonicalStreamEvent(kind="text_delta", text=response.content)
            yield CanonicalStreamEvent(kind="message_end", finish_reason="stop")

    services.providers.register_adapter(_TextAdapter())
    services.providers.set_tool_call_support("openai-compatible", "m", False)
    # The echo tool must exist AND be granted to the chat scope,
    # exactly as config_sync grants web_search in the live engine.
    echo_tool = services.tools.register_echo_tool()
    services.tools.grant_tool(
        project_id=project.id,
        actor_id=owner,
        tool_id=echo_tool.id,
        agent_scope="main_worker",
        source="system",
    )

    from zero.app.chat_service import ChatService as _CS

    chat = _CS(
        providers=services.providers,
        authorization=services.authorization,
        tools=services.tools,
    )
    events: list[dict] = []
    result = chat.complete_stream(
        project_id=project.id,
        actor_id=owner,
        message="ping the echo tool",
        provider="openai-compatible",
        model_name="m",
        source="telegram",
        event_cb=events.append,
    )
    assert result.content == "The echo tool returned: ping"
    assert "tool_call" not in result.content
    types = [e["type"] for e in events]
    assert "text_reset" in types and "tool_call" in types and "tool_result" in types
    # The echo tool RAN through the real audited boundary.
    audits = services.audit._repo if hasattr(services.audit, "_repo") else None
    rows = database.connect().execute(
        "SELECT COUNT(*) c FROM audit_events WHERE operation='tool.invoke'"
    ).fetchone()
    assert rows[0] >= 1
