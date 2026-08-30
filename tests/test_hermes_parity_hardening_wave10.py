"""Hermes-parity hardening regressions (2026-08-31 audit).

Covers the gaps found by diffing Zero against the reference gateway:

- G2: bot-sender filter on the Telegram transport (``ZERO_TELEGRAM_ALLOW_BOTS``)
- G3: group mention gating (require-mention, exempt chats, per-group override)
- G5: in-batch text burst coalescing (Hermes text batching parity)
- G7: polling requests channel posts (allowed_updates widened)
- G6: live-stream flood-strike circuit breaker (Hermes _MAX_FLOOD_STRIKES)
- G9: env-only deployment bootstrap (config.yaml synthesized from env)
- G11: tool-approval inline buttons (token mint/resolve/consume loop)
- G13: /new clears the durable chat scope, /id reports scope ids
- G1: scheduler integration stage honors resolver-resolved repositories
- G12: MCP requests are bounded (a hung server cannot stall boot)
- delegation audit import fix (now_utc_iso)
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from zero.adapters.telegram import TelegramAdapter
from zero.domain.interfaces import NormalizedEvent


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _adapter(**kwargs) -> TelegramAdapter:
    defaults = dict(
        event_handler=lambda event: None,
        bot_token="123:fake",
        bot_username="ZeroGuardBot",
        bot_id="999",
        require_mention=True,
    )
    defaults.update(kwargs)
    return TelegramAdapter(**defaults)


def _group_message(
    *,
    text: str,
    user_id: int = 42,
    update_id: int = 100,
    is_bot: bool = False,
    entities=None,
    reply_to=None,
    chat_id: int = -100123,
) -> dict:
    message = {
        "message_id": update_id,
        "from": {"id": user_id, "is_bot": is_bot, "first_name": "T"},
        "chat": {"id": chat_id, "type": "supergroup", "title": "Team"},
        "date": 1,
        "text": text,
    }
    if entities:
        message["entities"] = entities
    if reply_to:
        message["reply_to_message"] = reply_to
    return {"update_id": update_id, "message": message}


class _Recorder:
    def __init__(self) -> None:
        self.events: list[NormalizedEvent] = []

    def __call__(self, event) -> None:
        self.events.append(event)


class _FakeResponse:
    status_code = 200
    headers: dict[str, str] = {}

    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.content = json.dumps(payload).encode()

    def json(self):
        return self._payload


class _FakeTransport:
    """Minimal HttpTransport double returning canned Telegram payloads."""

    def __init__(self, updates: list[dict]) -> None:
        self._updates = updates
        self.calls: list[tuple[str, dict]] = []

    def request(self, method, url, *, headers=None, json=None, timeout=None, payload=None):
        import httpx as _httpx

        self.calls.append((method, json if json is not None else (payload or {})))
        if "getUpdates" in url:
            return _FakeResponse({"ok": True, "result": self._updates})
        return _FakeResponse({"ok": True, "result": {"message_id": 1, "ok": True}})


# ----------------------------------------------------------------------
# G2: bot-sender filter
# ----------------------------------------------------------------------


def test_bot_sender_messages_are_skipped_by_default():
    recorder = _Recorder()
    adapter = _adapter(event_handler=recorder)
    transport = _FakeTransport(
        [_group_message(text="hello from another bot", is_bot=True, user_id=777)]
    )
    adapter._transport = transport  # type: ignore[attr-defined]
    results = adapter.poll_once(scope_key="s")
    assert recorder.events == []
    # The skipped update still owned a durable offset: it was consumed.
    assert transport.calls and transport.calls[0][0] == "POST"
    assert results == []


def test_bot_sender_messages_processed_when_allow_bots_all(monkeypatch):
    monkeypatch.setenv("ZERO_TELEGRAM_ALLOW_BOTS", "all")
    recorder = _Recorder()
    adapter = _adapter(event_handler=recorder, allow_bots="all", require_mention=False)
    transport = _FakeTransport(
        [_group_message(text="hello from another bot", is_bot=True, user_id=777)]
    )
    adapter._transport = transport  # type: ignore[attr-defined]
    adapter.poll_once(scope_key="s")
    assert len(recorder.events) == 1


# ----------------------------------------------------------------------
# G3: group mention gating
# ----------------------------------------------------------------------


def test_unaddressed_group_message_is_skipped():
    recorder = _Recorder()
    adapter = _adapter(event_handler=recorder)
    transport = _FakeTransport([_group_message(text="casual chatter")])
    adapter._transport = transport  # type: ignore[attr-defined]
    adapter.poll_once(scope_key="s")
    assert recorder.events == []


def test_mentioned_group_message_is_processed():
    recorder = _Recorder()
    adapter = _adapter(event_handler=recorder)
    text = "@ZeroGuardBot please run the build"
    transport = _FakeTransport(
        [
            _group_message(
                text=text,
                entities=[{"type": "mention", "offset": 0, "length": 13}],
            )
        ]
    )
    adapter._transport = transport  # type: ignore[attr-defined]
    adapter.poll_once(scope_key="s")
    assert len(recorder.events) == 1


def test_reply_to_bot_message_is_processed():
    recorder = _Recorder()
    adapter = _adapter(event_handler=recorder)
    transport = _FakeTransport(
        [
            _group_message(
                text="what did you mean here?",
                reply_to={
                    "message_id": 5,
                    "from": {"id": 999, "is_bot": True, "username": "ZeroGuardBot"},
                },
            )
        ]
    )
    adapter._transport = transport  # type: ignore[attr-defined]
    adapter.poll_once(scope_key="s")
    assert len(recorder.events) == 1


def test_command_in_group_is_processed_without_mention():
    recorder = _Recorder()
    adapter = _adapter(event_handler=recorder)
    transport = _FakeTransport([_group_message(text="/status")])
    adapter._transport = transport  # type: ignore[attr-defined]
    adapter.poll_once(scope_key="s")
    assert len(recorder.events) == 1


def test_private_chat_is_never_mention_gated():
    recorder = _Recorder()
    adapter = _adapter(event_handler=recorder)
    update = {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "from": {"id": 42, "is_bot": False, "first_name": "T"},
            "chat": {"id": 42, "type": "private", "first_name": "T"},
            "date": 1,
            "text": "just talk to me",
        },
    }
    transport = _FakeTransport([update])
    adapter._transport = transport  # type: ignore[attr-defined]
    adapter.poll_once(scope_key="s")
    assert len(recorder.events) == 1


def test_mention_exempt_chat_processes_everything(monkeypatch):
    monkeypatch.setenv("ZERO_TELEGRAM_MENTION_EXEMPT_CHATS", "-100123")
    recorder = _Recorder()
    adapter = _adapter(event_handler=recorder, mention_exempt_chats={"-100123"})
    transport = _FakeTransport([_group_message(text="no mention here")])
    adapter._transport = transport  # type: ignore[attr-defined]
    adapter.poll_once(scope_key="s")
    assert len(recorder.events) == 1


def test_require_mention_false_processes_everything():
    recorder = _Recorder()
    adapter = _adapter(event_handler=recorder, require_mention=False)
    transport = _FakeTransport([_group_message(text="no mention here")])
    adapter._transport = transport  # type: ignore[attr-defined]
    adapter.poll_once(scope_key="s")
    assert len(recorder.events) == 1


def test_unknown_bot_identity_fails_open():
    recorder = _Recorder()
    adapter = _adapter(event_handler=recorder, bot_username=None, bot_id=None)
    transport = _FakeTransport([_group_message(text="no mention here")])
    adapter._transport = transport  # type: ignore[attr-defined]
    adapter.poll_once(scope_key="s")
    assert len(recorder.events) == 1


# ----------------------------------------------------------------------
# G5: in-batch burst coalescing
# ----------------------------------------------------------------------


def test_consecutive_text_messages_merge_into_one_turn():
    recorder = _Recorder()
    adapter = _adapter(event_handler=recorder, require_mention=False)
    transport = _FakeTransport(
        [
            _group_message(text="part one", update_id=100),
            _group_message(text="part two", update_id=101),
            _group_message(text="part three", update_id=102),
        ]
    )
    adapter._transport = transport  # type: ignore[attr-defined]
    adapter.poll_once(scope_key="s")
    assert len(recorder.events) == 1
    merged = recorder.events[0]
    assert merged.content == "part one\npart two\npart three"
    # The merged turn keeps the FIRST update's id for durable dedup.
    assert merged.external_event_id == "100"


def test_commands_never_merge_with_text():
    recorder = _Recorder()
    adapter = _adapter(event_handler=recorder, require_mention=False)
    transport = _FakeTransport(
        [
            _group_message(text="a", update_id=100),
            _group_message(text="/status", update_id=101),
        ]
    )
    adapter._transport = transport  # type: ignore[attr-defined]
    adapter.poll_once(scope_key="s")
    kinds = [(e.event_kind, e.content) for e in recorder.events]
    assert kinds == [("message", "a"), ("command", "/status")]


def test_merge_scope_is_per_chat_and_actor():
    recorder = _Recorder()
    adapter = _adapter(event_handler=recorder, require_mention=False)
    transport = _FakeTransport(
        [
            _group_message(text="alice 1", user_id=1, update_id=100),
            _group_message(text="bob 1", user_id=2, update_id=101),
            _group_message(text="alice 2", user_id=1, update_id=102),
        ]
    )
    adapter._transport = transport  # type: ignore[attr-defined]
    adapter.poll_once(scope_key="s")
    by_actor = {e.external_actor_id: e.content for e in recorder.events}
    assert by_actor == {"1": "alice 1\nalice 2", "2": "bob 1"}


def test_messages_without_dates_are_never_merged():
    """Legacy payloads without ``date`` keep the separate-dispatch contract
    (test_audit_real_http pins this shape)."""
    recorder = _Recorder()
    adapter = _adapter(event_handler=recorder, require_mention=False)
    transport = _FakeTransport(
        [
            _group_message(text="a", update_id=100),
            _group_message(text="b", update_id=101),
        ]
    )
    for update in transport._updates:
        update["message"].pop("date", None)
    adapter._transport = transport  # type: ignore[attr-defined]
    adapter.poll_once(scope_key="s")
    assert [e.content for e in recorder.events] == ["a", "b"]


def test_stale_date_gap_dispatches_separately():
    """A real time gap (>120s) is a new thought, not a split message."""
    recorder = _Recorder()
    adapter = _adapter(event_handler=recorder, require_mention=False)
    transport = _FakeTransport(
        [
            _group_message(text="first thought", update_id=100),
        ]
    )
    transport._updates[0]["message"]["date"] = 1_000
    late = _group_message(text="ten minutes later", update_id=101)
    late["message"]["date"] = 1_700
    transport._updates.append(late)
    adapter._transport = transport  # type: ignore[attr-defined]
    adapter.poll_once(scope_key="s")
    assert [e.content for e in recorder.events] == ["first thought", "ten minutes later"]


# ----------------------------------------------------------------------
# G7: allowed_updates widened
# ----------------------------------------------------------------------


def test_poll_requests_channel_posts():
    adapter = _adapter(require_mention=False)
    transport = _FakeTransport([])
    adapter._transport = transport  # type: ignore[attr-defined]
    adapter.poll_once(scope_key="s")
    method, payload = transport.calls[0]
    assert "channel_post" in payload["allowed_updates"]
    assert "edited_channel_post" in payload["allowed_updates"]


# ----------------------------------------------------------------------
# G6: live-stream flood breaker
# ----------------------------------------------------------------------


class _FloodAdapter:
    """Fails the first three edits with flood control, then recovers."""

    def __init__(self) -> None:
        self.edits = 0

    def send_message(self, **kwargs):
        return _FakeResponse({"ok": True, "result": {"message_id": 11}})

    def edit_message(self, **kwargs):
        self.edits += 1
        if self.edits <= 3:
            raise RuntimeError("Too Many Requests: retry after 44")
        return _FakeResponse({"ok": True, "result": {"message_id": 11}})


def test_live_stream_disables_edits_after_three_flood_strikes():
    from zero.app.telegram_live import TelegramLiveStream

    flood = _FloodAdapter()
    stream = TelegramLiveStream(adapter=flood, chat_id="1", min_edit_interval=0.0)
    for _ in range(5):
        stream.on_text_delta("more text " * 3)
    assert flood.edits == 3  # 3 strikes, then progressive edits stop
    ok = stream.finalize("final answer")
    assert ok is True
    assert flood.edits == 4  # finalize still attempts its single edit


# ----------------------------------------------------------------------
# G1: scheduler integration gate
# ----------------------------------------------------------------------


def test_scheduler_integration_stage_uses_effective_repository(tmp_path):
    import inspect

    from zero.app import scheduler_service

    source = inspect.getsource(scheduler_service.SchedulerService.run_once)
    assert "effective_repository_id is not None" in source
    # The raw caller argument must no longer gate the stage.
    assert "and repository_id is not None" not in source


# ----------------------------------------------------------------------
# G13: /new and /id commands
# ----------------------------------------------------------------------


def test_new_clears_chat_scope_history(tmp_path, monkeypatch):
    from tests.test_dead_bot_regressions import zero_home  # noqa: F401

    home = tmp_path / "zero-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ZERO_HOME", str(home))
    from zero.persistence.connection import open_database
    from zero.persistence.migrations import apply_migrations
    from zero.config import Settings
    from zero.app.services import build_services
    from zero.app.telegram_commands import TelegramCommandBook

    settings = Settings.load(zero_env_fallback="development")
    database = open_database(settings)
    apply_migrations(database)
    services = build_services(settings, database)
    for _ in range(3):
        services.chat_history.append(
            project_id="p_x",
            platform="telegram",
            chat_id="-1001",
            topic_id=None,
            role="user",
            content="hi",
            created_at="2026-08-31T00:00:00Z",
        )
    book = TelegramCommandBook(services)
    reply = book.reply_for(
        "/new",
        project_id=SimpleNamespace(value="p_x"),
        actor_id=None,
        chat_id="-1001",
        topic_id=None,
    )
    assert "cleared 3" in reply
    assert services.chat_history.recent(
        platform="telegram", chat_id="-1001", topic_id=None
    ) == []


def test_id_reply_reports_scope():
    from zero.app.telegram_commands import TelegramCommandBook

    book = TelegramCommandBook(SimpleNamespace())
    reply = book.reply_for(
        "/id",
        project_id=SimpleNamespace(value="p_1"),
        actor_id=None,
        chat_id="-1002",
        topic_id="7",
    )
    assert "-1002" in reply and "p_1" in reply and "7" in reply


# ----------------------------------------------------------------------
# G11: tool approval buttons
# ----------------------------------------------------------------------


def _engine_with_project(tmp_path, monkeypatch):
    """Real DB + real project so FK constraints hold; fully isolated cwd."""
    from zero.persistence.connection import open_database
    from zero.persistence.migrations import apply_migrations
    from zero.config import Settings
    from zero.app.services import build_services
    from zero.manage.cli import _ensure_management_scope

    cwd = tmp_path / "engine-cwd"
    cwd.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(cwd)
    monkeypatch.setenv(
        "ZERO_DATABASE_URL", f"sqlite:///{(tmp_path / 'engine.db').as_posix()}"
    )
    settings = Settings.load(zero_env_fallback="development")
    database = open_database(settings)
    apply_migrations(database)
    services = build_services(settings, database)
    project = _ensure_management_scope(services)
    return services, project


def test_tool_approval_token_roundtrip(tmp_path, monkeypatch):
    from zero.domain.ids import generate_tool_approval_token_id
    from zero.domain.interfaces import ToolApprovalToken, ToolApprovalTokenId
    from zero.persistence.repositories.interface_repository import InterfaceRepository
    from tests.test_dead_bot_regressions import zero_home  # noqa: F401 — fixture

    home = tmp_path / "zero-home-tat"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ZERO_HOME", str(home))
    services, project = _engine_with_project(tmp_path, monkeypatch)
    repo = InterfaceRepository(services.database)
    token = ToolApprovalToken(
        id=ToolApprovalTokenId(generate_tool_approval_token_id()),
        project_id=project.id,
        approval_id="ta_abc",
        action="allow_once",
        expires_at="2027-01-01T00:00:00Z",
        used_at=None,
        created_by=project.owner_user_id,
        created_at="2026-08-31T00:00:00Z",
    )
    repo.insert_tool_approval_token(token)
    fetched = repo.get_tool_approval_token(token.id)
    assert fetched.approval_id == "ta_abc"
    assert fetched.is_used is False
    assert repo.mark_tool_approval_token_used(token.id, "2026-08-31T01:00:00Z")
    assert repo.get_tool_approval_token(token.id).is_used is True
    # One-shot: the second consume is a no-op.
    assert not repo.mark_tool_approval_token_used(token.id, "2026-08-31T02:00:00Z")


def test_gate_notifier_fires_on_new_pending(tmp_path, monkeypatch):
    from zero.app.approval_gate import ToolApprovalGate
    from tests.test_dead_bot_regressions import zero_home  # noqa: F401 — fixture

    home = tmp_path / "zero-home-gate"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ZERO_HOME", str(home))
    services, project = _engine_with_project(tmp_path, monkeypatch)
    gate = ToolApprovalGate(services.database, mode="manual")
    seen: list = []
    gate.attach_notifier(seen.append)
    verdict = gate.evaluate(
        project_id=project.id.value,
        execution_id="exec_1",
        tool_name="run_command",
        input_data={"command": "make test"},
    )
    assert verdict.state == "pending"
    assert len(seen) == 1
    assert seen[0].tool_name == "run_command"
    # A duplicate evaluation within the pending window does NOT re-notify.
    verdict2 = gate.evaluate(
        project_id=project.id.value,
        execution_id="exec_1",
        tool_name="run_command",
        input_data={"command": "make test"},
    )
    assert verdict2.state == "pending"
    assert len(seen) == 1


# ----------------------------------------------------------------------
# G12: MCP bounded requests
# ----------------------------------------------------------------------


def test_mcp_hung_server_times_out(tmp_path):
    """A server that never answers costs ONE bounded timeout, not boot."""
    import sys
    import time as _time

    from zero.manage.core.mcp_client import MCPServerProcess

    hung = tmp_path / "hung.py"
    hung.write_text("import time\nwhile True:\n    time.sleep(0.1)\n")
    server = MCPServerProcess(name="hung", command=[sys.executable, str(hung)])
    start = _time.monotonic()
    ok = server.connect()
    elapsed = _time.monotonic() - start
    assert ok is False
    assert elapsed < 30.0, f"connect() blocked for {elapsed:.1f}s"
    assert server._proc is None  # shut down after the timeout


def test_mcp_echo_server_completes_handshake(tmp_path):
    import sys

    from zero.manage.core.mcp_client import MCPServerProcess

    script = tmp_path / "echo_server.py"
    script.write_text(
        "\n".join(
            [
                "import json, sys",
                "for line in sys.stdin:",
                "    line = line.strip()",
                "    if not line:",
                "        continue",
                "    req = json.loads(line)",
                "    if req.get('method') == 'initialize':",
                "        print(json.dumps({'jsonrpc':'2.0','id':req['id'],'result':{'protocolVersion':'2024-11-05','capabilities':{},'serverInfo':{'name':'echo','version':'1'}}}), flush=True)",
                "    elif req.get('method') == 'tools/list':",
                "        print(json.dumps({'jsonrpc':'2.0','id':req['id'],'result':{'tools':[{'name':'echo','description':'echo','inputSchema':{}}]}}), flush=True)",
                "    elif req.get('method') == 'tools/call':",
                "        print(json.dumps({'jsonrpc':'2.0','id':req['id'],'result':{'content':[{'type':'text','text':'ECHO-OK'}]}}), flush=True)",
            ]
        )
    )
    server = MCPServerProcess(name="echo", command=[sys.executable, str(script)])
    assert server.connect() is True
    assert [t["name"] for t in server.tools] == ["echo"]
    assert server.call_tool("echo", {}) == "ECHO-OK"
    server.shutdown()


# ----------------------------------------------------------------------
# delegation audit import fix
# ----------------------------------------------------------------------


def test_delegate_audit_import_is_repairable():
    """The delegation audit writer must import a REAL clock symbol."""
    import inspect

    from zero.app import agent_runtime

    source = inspect.getsource(agent_runtime.AgentRuntime._audit_delegation)
    assert "from zero.app.clock import now_utc_iso" in source
    assert "_now_utc_iso" not in source


# ----------------------------------------------------------------------
# Live-run regression: config sync must rebind server handlers on boot
# ----------------------------------------------------------------------


def test_config_sync_rebinds_internet_search_handler(tmp_path, monkeypatch):
    """A persisted internet_search row must get its process-local handler
    re-attached on every boot (live 500: 'No handler registered')."""
    from zero.app.tools_websearch import make_web_search_handler
    from zero.manage.core.config import ConfigService
    from tests.test_dead_bot_regressions import zero_home  # noqa: F401 — fixture

    home = tmp_path / "zero-home-rebind"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ZERO_HOME", str(home))
    services, project = _engine_with_project(tmp_path, monkeypatch)

    # First boot: config sync registers the tool + handler.
    from zero.app.config_sync import _ensure_web_search_tool

    _ensure_web_search_tool(services, project, project.owner_user_id)
    tool = services.tools.get_tool_by_name("internet_search")
    assert services.tools._handlers.get(tool.handler_key) is not None

    # Simulate a restart: a fresh ToolService has an EMPTY handler registry
    # while the tool row persists.
    from zero.app.tool_service import ToolService

    fresh = ToolService(
        services.tools._tool_repo,
        services.tools._audit_repo if hasattr(services.tools, "_audit_repo") else None,
        services.authorization,
    )
    assert fresh._handlers.get(tool.handler_key) is None
    fresh.rebind_server_handler(tool, handler=make_web_search_handler(), inline=True)
    assert fresh._handlers.get(tool.handler_key) is not None
    assert tool.handler_key in fresh._inline_handler_keys


# ----------------------------------------------------------------------
# Live-run regression: websearch retries transient backend failures
# ----------------------------------------------------------------------


def test_websearch_retries_transient_failures(monkeypatch):
    """A flaky DDG backend must not fail the first attempt (live run:
    real results and ConnectTimeout flapped within minutes)."""
    import zero.app.tools_websearch as ws

    calls = {"n": 0}

    def flaky_fetcher(query: str) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise type("ConnectTimeout", (Exception,), {})("boom")
        return (
            '<a href="https://example.com/x" class="result-link">Result</a>'
            '<td class="result-snippet">snippet text</td>'
        )

    monkeypatch.setattr(ws.time, "sleep", lambda _s: None)
    handler = ws.make_web_search_handler(fetcher=flaky_fetcher, fetch_attempts=2)
    out = handler({"query": "zero live"}, context=None)
    assert calls["n"] == 2
    assert out["results"] and out["results"][0]["url"] == "https://example.com/x"


def test_websearch_non_transient_fails_fast():
    import zero.app.tools_websearch as ws

    calls = {"n": 0}

    def hard_failer(query: str) -> str:
        calls["n"] += 1
        raise RuntimeError("http 403 blocked")

    handler = ws.make_web_search_handler(fetcher=hard_failer, fetch_attempts=3)
    out = handler({"query": "zero live"}, context=None)
    assert calls["n"] == 1  # non-transient: no retry
    assert "unreachable" in out["error"]
