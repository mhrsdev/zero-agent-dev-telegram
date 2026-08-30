"""Round-5 regression tests — Hermes-agent parity audit.

Reference: /hermes-agent (NousResearch) messaging architecture; the gap
analysis found 9 missing capabilities in Zero's Telegram surface. This
file pins the fixes:

1. markdown → Telegram HTML rendering (Hermes format_message parity);
2. UTF-16 chunking with code-fence preservation (Hermes
   truncate_message parity) — the old code silently truncated at 4096;
3. media metadata on the canonical envelope + getFile/download;
4. typing indicator + build_telegram_adapter credential seam;
5. multimodal content parts through the OpenAI-compatible adapter
   (verified live against the operator's gateway: claude-opus-5
   answered a color probe) with a loud Anthropic-protocol rejection;
6. durable per-scope chat history (migration 0032) + ChatService
   history/image plumbing;
7. the Telegram plan card with REAL approve/reject buttons (the
   callback pipeline existed since M13 but no card was ever sent);
8. conversational fallback for non-actionable messages;
9. keyless web_search tool (the WebSearchCfg stub became real).

Polling heartbeat/stall watchdogs live in test_polling_heartbeat.py.
"""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest

from zero.adapters.messaging import RetryPolicy
from zero.adapters.telegram import TelegramAdapter, _extract_media
from zero.adapters.telegram_render import (
    TELEGRAM_MESSAGE_LIMIT,
    chunk_telegram_text,
    render_telegram_html,
    utf16_len,
)
from zero.config import Settings
from zero.domain.interfaces import MediaAttachment, NormalizedEvent
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations


# ----------------------------------------------------------------------
# 1. markdown → Telegram HTML
# ----------------------------------------------------------------------
def test_render_bold_italic_code() -> None:
    assert render_telegram_html("**bold** *it* `code`") == (
        "<b>bold</b> <i>it</i> <code>code</code>"
    )


def test_render_header_quote_strike() -> None:
    out = render_telegram_html("# Title\n> quoted\n~~gone~~")
    assert "<b>Title</b>" in out
    assert "<blockquote>quoted</blockquote>" in out
    assert "<s>gone</s>" in out


def test_render_escapes_html_and_scripts() -> None:
    out = render_telegram_html("<script>alert(1)</script>")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_render_links_and_scheme_guard() -> None:
    out = render_telegram_html("[ok](https://example.com/x) [bad](javascript:alert(1))")
    assert '<a href="https://example.com/x">ok</a>' in out
    # javascript: must degrade to text, never an anchor; the renderer is
    # deterministic and lossless, so the raw marker stays visible text.
    assert "<a href=\"javascript:" not in out
    assert "[bad](javascript:alert(1))" in out


def test_render_fenced_code_keeps_content_verbatim() -> None:
    out = render_telegram_html("```python\nprint('<b>hi</b>')\n```")
    assert "<pre><code" in out
    assert "&lt;b&gt;hi&lt;/b&gt;" in out  # escaped, not interpreted
    assert "language-python" in out


def test_render_unmatched_markers_stay_literal() -> None:
    out = render_telegram_html("a * lone star and _ an unclosed ```fence")
    assert "*" in out and "_" in out  # never dropped
    assert "<b>" not in out


# ----------------------------------------------------------------------
# 2. UTF-16 chunking with fence preservation
# ----------------------------------------------------------------------
def test_utf16_len_counts_astral_pairs() -> None:
    assert utf16_len("😀") == 2
    assert utf16_len("ab") == 2


def test_chunk_long_text_splits_naturally() -> None:
    text = "para one\n\n" + "x" * 5000 + "\n\npara two"
    chunks = chunk_telegram_text(text)
    assert len(chunks) >= 2
    assert all(utf16_len(c) <= TELEGRAM_MESSAGE_LIMIT for c in chunks)
    assert "".join(chunks).count("x") == 5000  # nothing lost
    assert "para two" in chunks[-1]


def test_chunk_preserves_code_fence_across_boundary() -> None:
    text = "intro\n```py\n" + "y" * 5000 + "\n```\ntail"
    chunks = chunk_telegram_text(text)
    assert len(chunks) >= 2
    for chunk in chunks:
        # Every chunk has balanced fence markers (even count of ```).
        assert chunk.count("```") % 2 == 0
        assert utf16_len(chunk) <= TELEGRAM_MESSAGE_LIMIT
    assert chunks[0].startswith("intro")
    assert "tail" in chunks[-1]


def test_chunk_hard_splits_giant_fence_without_loss() -> None:
    text = "```\n" + "z" * 9000 + "\n```"
    chunks = chunk_telegram_text(text, 2000)
    assert all(utf16_len(c) <= 2000 for c in chunks)
    assert all(c.count("```") % 2 == 0 for c in chunks)
    assert "".join(chunks).count("z") == 9000


def test_chunk_indicators_appended_when_requested() -> None:
    chunks = chunk_telegram_text("a" * 5000, with_indicators=True)
    assert len(chunks) == 2
    assert chunks[0].endswith("(1/2)")
    assert chunks[1].endswith("(2/2)")


def test_chunk_empty_and_tiny() -> None:
    assert chunk_telegram_text("") == []
    assert chunk_telegram_text("short") == ["short"]


# ----------------------------------------------------------------------
# 3. Media metadata on the canonical envelope
# ----------------------------------------------------------------------
def _adapter(**overrides) -> TelegramAdapter:
    return TelegramAdapter(
        event_handler=lambda e: None,
        transport=overrides.pop("transport"),
        bot_token="123:TESTTOKENVALUE",
        poll_timeout_seconds=0,
        retry_policy=RetryPolicy(attempts=1, backoff_seconds=0.0, timeout_seconds=5.0),
        **overrides,
    )


def test_extract_media_prefers_largest_photo() -> None:
    message = {
        "photo": [
            {"file_id": "small", "width": 90, "height": 90},
            {"file_id": "big", "width": 800, "height": 600, "file_size": 12345},
        ],
        "document": {
            "file_id": "d1",
            "file_name": "notes.txt",
            "mime_type": "text/plain",
            "file_size": 99,
        },
        "voice": {"file_id": "v1", "mime_type": "audio/ogg"},
    }
    kinds = [(a.kind, a.file_id) for a in _extract_media(message)]
    assert ("photo", "big") in kinds  # largest wins
    assert ("document", "d1") in kinds
    assert ("voice", "v1") in kinds


def test_normalize_event_carries_media_message_id_and_reply_target() -> None:
    class _Resp:
        status_code = 200

        def json(self):
            return {"ok": True, "result": []}

    class _Transport:
        def request(self, *a, **k):
            return _Resp()

    adapter = _adapter(transport=_Transport())
    event = adapter.normalize_update(
        {
            "update_id": 5,
            "message": {
                "message_id": 10,
                "from": {"id": 777},
                "chat": {"id": -100},
                "text": "hi",
                "photo": [{"file_id": "big", "width": 10, "height": 10}],
                "reply_to_message": {"message_id": 42},
            },
        }
    )
    assert [a.file_id for a in event.media] == ["big"]
    assert event.message_id == "10"
    assert event.reply_to_message_id == "42"


class _ApiTransport:
    """Records Bot API JSON calls; serves getFile/download endpoints."""

    def __init__(self, file_bytes: bytes = b"payload") -> None:
        self.calls: list[tuple[str, dict]] = []
        self.file_bytes = file_bytes

    def request(self, method, url, headers=None, json=None, timeout=None):
        self.calls.append((url.rsplit("/", 1)[-1], json if json is not None else {}))
        if url.endswith("/sendMessage"):
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"ok": True, "result": {"message_id": 101}},
            )
        if url.endswith("/sendChatAction"):
            return SimpleNamespace(status_code=200, json=lambda: {"ok": True})
        if url.endswith("/getFile"):
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"ok": True, "result": {"file_id": "f", "file_path": "docs/x.txt"}},
            )
        if "/file/bot" in url:
            response = SimpleNamespace(status_code=200, json=lambda: {"ok": True})
            response.content = self.file_bytes
            return response
        return SimpleNamespace(status_code=200, json=lambda: {"ok": True, "result": []})


def test_send_message_renders_and_keeps_buttons_on_last_chunk() -> None:
    transport = _ApiTransport()
    adapter = _adapter(transport=transport)
    markup = {"inline_keyboard": [[{"text": "ok", "callback_data": "ct_x"}]]}
    adapter.send_message(
        chat_id="-100", text="**hello** world", reply_markup=markup
    )
    sends = [payload for name, payload in transport.calls if name == "sendMessage"]
    assert len(sends) == 1
    assert "<b>hello</b>" in sends[0]["text"]
    assert sends[0]["parse_mode"] == "HTML"
    assert sends[0]["reply_markup"] == markup


def test_send_message_chunks_long_text_with_reply_threading() -> None:
    transport = _ApiTransport()
    adapter = _adapter(transport=transport)
    adapter.send_message(
        chat_id="-100",
        text="line\n\n" + "word " * 3000,
        reply_to_message_id="77",
    )
    sends = [payload for name, payload in transport.calls if name == "sendMessage"]
    assert len(sends) >= 2
    assert all(utf16_len(p["text"]) <= TELEGRAM_MESSAGE_LIMIT for p in sends)
    # Reply threading rides chunk 1 only; indicators appended.
    assert sends[0]["reply_to_message_id"] == "77"
    assert "reply_to_message_id" not in sends[1]
    assert "(1/" in sends[0]["text"]
    assert f"({len(sends)}/" in sends[-1]["text"]


def test_get_file_and_download_roundtrip() -> None:
    transport = _ApiTransport(file_bytes=b"file-bytes")
    adapter = _adapter(transport=transport)
    info = adapter.get_file(file_id="abc")
    assert info["file_path"] == "docs/x.txt"
    blob = adapter.download_file_bytes(file_path=info["file_path"])
    assert blob == b"file-bytes"


def test_send_chat_action_typing() -> None:
    transport = _ApiTransport()
    adapter = _adapter(transport=transport)
    adapter.send_chat_action(chat_id="-100", action="typing")
    actions = [payload for name, payload in transport.calls if name == "sendChatAction"]
    assert actions and actions[0]["action"] == "typing"


def test_send_message_drops_dead_reply_anchor_on_400() -> None:
    """Live-e2e regression: replying to a fabricated/nonexistent message
    id makes Telegram 400 ('message to be replied not found'); the chunk
    must still go out WITHOUT the anchor (Hermes thread-not-found parity),
    and the buttons must survive."""
    from zero.adapters.messaging import PermanentTransportError

    class _Reply400Transport:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        def request(self, method, url, headers=None, json=None, timeout=None):
            self.calls.append((url.rsplit("/", 1)[-1], json or {}))
            if url.endswith("/sendMessage"):
                if json.get("reply_to_message_id") is not None:
                    raise PermanentTransportError("provider returned HTTP status 400")
                return SimpleNamespace(
                    status_code=200,
                    json=lambda: {"ok": True, "result": {"message_id": 555}},
                )
            return SimpleNamespace(status_code=200, json=lambda: {"ok": True, "result": []})

    transport = _Reply400Transport()
    adapter = _adapter(transport=transport)
    markup = {"inline_keyboard": [[{"text": "ok", "callback_data": "ct_x"}]]}
    adapter.send_message(
        chat_id="-100",
        text="card body",
        reply_markup=markup,
        reply_to_message_id="7002",
    )
    sends = [payload for name, payload in transport.calls if name == "sendMessage"]
    assert len(sends) == 2  # 400 with anchor → retry without anchor
    assert sends[0]["reply_to_message_id"] == "7002"
    assert "reply_to_message_id" not in sends[1]
    assert sends[1]["reply_markup"] == markup  # buttons survive the fallback


# ----------------------------------------------------------------------
# 5. Multimodal content parts (provider layer)
# ----------------------------------------------------------------------
def test_openai_adapter_renders_user_content_parts() -> None:
    from zero.app.provider_adapter import OpenAICompatibleProviderAdapter
    from zero.domain.providers import CanonicalMessage, CanonicalRequest

    request = CanonicalRequest(
        provider="openai-compatible",
        model_name="claude-opus-5",
        messages=(
            CanonicalMessage(
                role="user",
                content="what color?",
                content_parts=(
                    {"type": "text", "text": "what color?"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
                ),
            ),
        ),
        max_tokens=10,
    )
    rendered = OpenAICompatibleProviderAdapter._render_messages(request, [
        {"role": "user", "content": "what color?",
         "content_parts": (
             {"type": "text", "text": "what color?"},
             {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
         )},
    ])
    assert isinstance(rendered[0]["content"], list)
    assert rendered[0]["content"][1]["type"] == "image_url"


def test_request_hash_distinguishes_parts_but_stays_stable_without() -> None:
    from zero.domain.providers import CanonicalMessage, CanonicalRequest
    from zero.app.provider_adapter import compute_request_hash

    base = dict(
        provider="openai-compatible",
        model_name="m",
        max_tokens=8,
    )
    plain = CanonicalRequest(
        messages=(CanonicalMessage(role="user", content="hi"),), **base
    )
    parts = CanonicalRequest(
        messages=(
            CanonicalMessage(
                role="user",
                content="hi",
                content_parts=({"type": "text", "text": "hi"},),
            ),
        ),
        **base,
    )
    assert compute_request_hash(plain) == compute_request_hash(plain)  # stable
    assert compute_request_hash(plain) != compute_request_hash(parts)  # distinguishes


# ----------------------------------------------------------------------
# 6. Durable chat history + ChatService plumbing
# ----------------------------------------------------------------------
@pytest.fixture
def services(test_settings: Settings):
    database = Database(test_settings)
    apply_migrations(database)
    from zero.app.services import build_services

    return build_services(test_settings, database)


def test_chat_history_repository_roundtrip(services) -> None:
    from zero.app.chat_history_repository import ChatHistoryRepository

    repo = ChatHistoryRepository(services.database)
    for index in range(14):
        repo.append(
            project_id="p1",
            platform="telegram",
            chat_id="-100",
            topic_id=None,
            role="user" if index % 2 == 0 else "assistant",
            content=f"turn {index}",
            created_at=f"2026-01-01T00:00:{index:02d}.000000Z",
        )
    window = repo.recent(platform="telegram", chat_id="-100", topic_id=None, limit=10)
    assert len(window) == 10
    assert window[0]["content"] == "turn 4"  # oldest-first bounded window
    assert window[-1]["content"] == "turn 13"
    # Topic scopes are isolated.
    assert repo.recent(platform="telegram", chat_id="-100", topic_id="5") == []
    assert repo.clear(platform="telegram", chat_id="-100", topic_id=None) == 14


def test_chat_service_sanitizes_history_and_attaches_parts(services) -> None:
    """Through the REAL authorization path (owner actor): history is
    sanitized (tool/system rows dropped) and image parts attach to the
    user message as OpenAI-compatible content parts."""
    from zero.app.chat_service import ChatService
    from zero.domain.providers import CanonicalMessage

    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(owner_id=owner.id, name="P")

    captured: dict = {}

    def _fake_send(*, request, **kwargs):
        captured["request"] = request
        from zero.domain.providers import CanonicalResponse

        return SimpleNamespace(id=SimpleNamespace(value="pr1")), CanonicalResponse(
            content="ok",
        )

    services.providers.send_request_with_fallback = _fake_send
    chat = ChatService(
        providers=services.providers,
        authorization=services.authorization,
        tools=None,
    )
    history = (
        CanonicalMessage(role="user", content="q1"),
        CanonicalMessage(role="assistant", content="a1"),
        CanonicalMessage(role="tool", content="injected", tool_call_id="t"),
    )
    chat.complete(
        project_id=project.id,
        actor_id=owner.id,
        message="q2",
        provider="openai-compatible",
        model_name="m",
        history=history,
        image_data_urls=("data:image/png;base64,AAA",),
    )
    request = captured["request"]
    roles = [m.role for m in request.messages]
    assert roles == ["user", "assistant", "user"]  # tool row sanitized
    last = request.messages[-1]
    assert last.content_parts is not None
    part_types = [p["type"] for p in last.content_parts]
    assert part_types == ["text", "image_url"]


# ----------------------------------------------------------------------
# 7+8. Plan card with buttons + conversational fallback
# ----------------------------------------------------------------------
class _ReplyTransport:
    """Captures outbound sends at the InterfaceTransportService seam."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send_message(self, **kwargs) -> str:
        self.sent.append(kwargs)
        return "111"

    def build_telegram_adapter(self, **kwargs):
        return _adapter(transport=_ApiTransport())

    def send_typing(self, **kwargs) -> None:
        self.sent.append({"typing": kwargs})


def test_plan_card_sends_inline_buttons_and_callback_approves(services) -> None:
    from zero.domain.plans import PlanRevisionContent

    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(owner_id=owner.id, name="P")
    services.identity.link_external_identity(
        user_id=owner.id,
        platform="telegram",
        external_id="777",
        external_username="owner",
        verified=True,
    )
    binding = services.interfaces.create_binding(
        project_id=project.id,
        actor_id=owner.id,
        platform="telegram",
        chat_id="-100",
        is_enabled=True,
    )
    plan = services.plans.create_plan(
        project_id=project.id, actor_id=owner.id, source="telegram"
    )
    conv = services.plans.ingest_conversation_event(
        project_id=project.id,
        actor_id=owner.id,
        source="telegram",
        origin_kind="authenticated_human",
        content="do the thing",
        external_event_id="ev-1",
    )
    revision = services.plans.propose_revision(
        plan_id=plan.id,
        project_id=project.id,
        actor_id=owner.id,
        content=PlanRevisionContent(
            objective="Add a <script>alert</script> feature",
            scope=("step one",),
            constraints=(),
            acceptance_criteria=("tests pass",),
            risks=(),
            unresolved_questions=(),
            source_event_ids=(conv.id,),
        ),
        source="telegram",
    )

    transport = _ReplyTransport()
    services.interfaces.direct_reply_transport = transport
    event = NormalizedEvent(
        platform="telegram",
        external_event_id="1",
        external_actor_id="777",
        chat_id="-100",
        topic_id=None,
        event_kind="message",
        content="do the thing",
        message_id="55",
    )
    detail = services.interfaces._send_plan_card(
        binding=binding, event=event, user_id=owner.id, revision=revision
    )
    assert detail == "plan card sent with approval buttons"
    assert len(transport.sent) == 1
    sent = transport.sent[0]
    assert sent["chat_id"] == "-100"
    assert sent["reply_to_message_id"] == "55"
    markup = sent["reply_markup"]
    buttons = markup["inline_keyboard"][0]
    assert len(buttons) == 2
    # Buttons must carry the two freshly created callback token ids.
    tokens = services.interfaces._repo.list_callback_tokens_for_plan(
        project.id, plan.id
    )
    actions = {t.action for t in tokens}
    assert actions == {"approve", "reject"}
    data_ids = {b["callback_data"] for b in buttons}
    assert data_ids == {t.id.value for t in tokens}
    # Model content is escaped even inside the trusted card.
    assert "<script>" not in sent["text"]
    assert "&lt;script&gt;" in sent["text"]

    # Duplicate delivery must NOT create a second pair of buttons.
    detail2 = services.interfaces._send_plan_card(
        binding=binding, event=event, user_id=owner.id, revision=revision
    )
    assert "already exist" in detail2
    assert len(transport.sent) == 1

    # The approve button is live: a callback_query carrying the approve
    # token id approves the revision through the SAME durable pipeline.
    approve_token = next(t for t in tokens if t.action == "approve")
    callback = NormalizedEvent(
        platform="telegram",
        external_event_id="2",
        external_actor_id="777",
        chat_id="-100",
        topic_id=None,
        event_kind="callback_query",
        content=approve_token.id.value,
        callback_token=approve_token.id.value,
    )
    entry = services.interfaces.process_inbound_event(callback)
    assert entry.processing_result == "processed"
    approved_plan = services.plans.get_plan(
        plan.id, project_id=project.id, actor_id=owner.id, source="telegram"
    )
    assert approved_plan.current_state == "approved"


def test_conversational_fallback_replies_and_persists_history(services) -> None:
    from zero.app.chat_history_repository import ChatHistoryRepository
    from zero.app.telegram_chat import TelegramChatBridge

    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(owner_id=owner.id, name="P")
    binding = services.interfaces.create_binding(
        project_id=project.id,
        actor_id=owner.id,
        platform="telegram",
        chat_id="-100",
        is_enabled=True,
    )

    class _FakeChat:
        def complete(self, **kwargs):
            assert kwargs["history"], "prior turns must ride the request"
            return SimpleNamespace(
                content="here is the answer", tool_calls_executed=(), usage=None,
                provider_request_id="pr",
            )

    transport = _ReplyTransport()
    bridge = TelegramChatBridge(
        chat_service=_FakeChat(),
        transport_service=transport,
        history=ChatHistoryRepository(services.database),
        provider="openai-compatible",
        model_name="m",
    )
    # Seed prior context (as a previous turn would have).
    bridge._history.append(
        project_id=project.id.value,
        platform="telegram",
        chat_id="-100",
        topic_id=None,
        role="user",
        content="what is zero?",
        created_at="2026-01-01T00:00:00.000000Z",
    )
    event = NormalizedEvent(
        platform="telegram",
        external_event_id="9",
        external_actor_id="777",
        chat_id="-100",
        topic_id=None,
        event_kind="message",
        content="and its memory feature?",
        message_id="31",
    )
    detail = bridge.handle_message(binding=binding, event=event, user_id=owner.id)
    assert "conversational reply sent" in detail
    assert len(transport.sent) == 2  # one typing indicator + one reply
    assert "typing" in transport.sent[0]
    reply = transport.sent[-1]
    assert reply["text"] == "here is the answer"
    assert reply["reply_to_message_id"] == "31"
    turns = bridge._history.recent(
        platform="telegram", chat_id="-100", topic_id=None, limit=5
    )
    assert turns[-1] == {"role": "assistant", "content": "here is the answer"}


def test_bridge_downloads_photo_and_document_inline(services) -> None:
    from zero.app.chat_history_repository import ChatHistoryRepository
    from zero.app.telegram_chat import TelegramChatBridge

    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(owner_id=owner.id, name="P")
    binding = services.interfaces.create_binding(
        project_id=project.id,
        actor_id=owner.id,
        platform="telegram",
        chat_id="-100",
        is_enabled=True,
    )

    captured: dict = {}

    class _VisionChat:
        def complete(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                content="it is red", tool_calls_executed=(), usage=None,
                provider_request_id="pr",
            )

    png = base64.b64encode(b"\x89PNG-fake-bytes").decode()
    transport = _ApiTransport(file_bytes=b"\x89PNG-fake-bytes")

    class _TransportService(_ReplyTransport):
        def build_telegram_adapter(self, **kwargs):
            return _adapter(transport=transport)

    reply_service = _TransportService()
    bridge = TelegramChatBridge(
        chat_service=_VisionChat(),
        transport_service=reply_service,
        history=ChatHistoryRepository(services.database),
        provider="openai-compatible",
        model_name="m",
    )
    event = NormalizedEvent(
        platform="telegram",
        external_event_id="11",
        external_actor_id="777",
        chat_id="-100",
        topic_id=None,
        event_kind="message",
        content="what color is this?",
        media=(
            MediaAttachment(kind="photo", file_id="p1", mime_type="image/png"),
            MediaAttachment(
                kind="document",
                file_id="d1",
                file_name="notes.txt",
                mime_type="text/plain",
            ),
        ),
        message_id="40",
    )
    detail = bridge.handle_message(binding=binding, event=event, user_id=owner.id)
    assert "conversational reply sent" in detail
    # The photo rode the request as an image_url data part...
    parts = captured["image_data_urls"]
    assert parts and parts[0].startswith("data:image/png;base64,")
    assert base64.b64decode(parts[0].split(",", 1)[1]) == b"\x89PNG-fake-bytes"
    # ...and the text document was decoded inline into the message.
    assert "[document: notes.txt]" in captured["message"]
    # A getFile + a file download happened; typing ran on its own thread.
    assert any(name == "getFile" for name, _ in transport.calls)
    assert any("typing" in item for item in reply_service.sent)


# ----------------------------------------------------------------------
# 9. Keyless web_search tool
# ----------------------------------------------------------------------
def test_websearch_parser_handles_real_ddg_lite_layout() -> None:
    from zero.app.tools_websearch import parse_ddg_lite

    page = """
    <td><a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fcore.telegram.org%2Fbots%2Fapi&amp;rut=abc"
        class='result-link'>Telegram Bot <b>API</b></a></td>
    <td class='result-snippet'>The <b>Bot</b> API is an HTTP interface.</td>
    """
    results = parse_ddg_lite(page)
    assert len(results) == 1
    assert results[0]["url"] == "https://core.telegram.org/bots/api"
    assert results[0]["title"] == "Telegram Bot API"
    assert "HTTP interface" in results[0]["snippet"]


def test_websearch_handler_shapes_results_and_errors() -> None:
    from zero.app.tools_websearch import make_web_search_handler

    page = (
        "<a href='//duckduckgo.com/l/?uddg=https%3A%2F%2Fx.example%2Fa' "
        "class='result-link'>A</a>"
        "<td class='result-snippet'>first</td>"
    )
    handler = make_web_search_handler(fetcher=lambda q: page)
    out = handler({"query": "test"}, None)
    assert out["results"][0]["url"] == "https://x.example/a"

    def _boom(q):
        raise ConnectionError("no network")

    failing = make_web_search_handler(fetcher=_boom)
    out = failing({"query": "test"}, None)
    assert out["results"] == []
    assert "unreachable" in out["error"]
    assert "ConnectionError" in out["error"]


def test_websearch_live_smoke() -> None:
    """Real network: the keyless backend must answer from this host.

    Live-network smoke: when the backend is unreachable from this host
    (the operator's filtered network flaps), SKIP with the reason rather
    than fail — the handler's offline behavior is pinned by the
    unreachable-path tests above, and a network outage is an
    environmental fact, not a regression.
    """
    from zero.app.tools_websearch import make_web_search_handler

    handler = make_web_search_handler()
    out = handler({"query": "telegram bot api"}, None)
    if out.get("error"):
        pytest.skip(f"websearch backend unreachable from this host: {out['error']}")
    assert len(out["results"]) >= 3
    assert all(r["url"].startswith("http") for r in out["results"])


# ----------------------------------------------------------------------
# Transport seam: typing + adapter composition
# ----------------------------------------------------------------------
def test_transport_service_typing_is_best_effort(services) -> None:
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(owner_id=owner.id, name="P")
    binding = services.interfaces.create_binding(
        project_id=project.id,
        actor_id=owner.id,
        platform="telegram",
        chat_id="-100",
        is_enabled=True,
    )
    # No secret service / transport configured for outbound: typing must
    # swallow the failure instead of raising.
    services.interface_transports.send_typing(
        project_id=project.id,
        binding_id=binding.id,
        actor_id=owner.id,
        chat_id="-100",
    )


# ----------------------------------------------------------------------
# Live-e2e: Cloudflare edge 403 must be TRANSIENT, not auth_failure
# ----------------------------------------------------------------------
def test_cloudflare_edge_403_classifies_transient_not_auth() -> None:
    """Observed live (round-5 e2e): the operator's gateway behind
    Cloudflare intermittently answers 403 with an EMPTY body + CF
    headers (server: cloudflare, cf-ray). Identical payloads succeed
    before and after the blip — it is an edge block, not a key failure.

    Contract: the OpenAI adapter raises the 'gateway edge protection'
    message for that shape, and the provider service classifier maps it
    to ``transient`` (bounded same-provider retry) — while a real auth
    failure keeps ``auth_failure`` (fail fast)."""
    from types import SimpleNamespace

    from zero.app.provider_adapter import OpenAICompatibleProviderAdapter
    from zero.app.provider_service import ProviderService
    from zero.domain.providers import CanonicalMessage, CanonicalRequest

    request = CanonicalRequest(
        provider="openai-compatible",
        model_name="claude-opus-5",
        messages=(CanonicalMessage(role="user", content="hi"),),
        max_tokens=10,
    )

    def _response(status_code: int, text: str, headers: dict) -> SimpleNamespace:
        return SimpleNamespace(
            status_code=status_code,
            text=text,
            headers=SimpleNamespace(get=lambda k, default=None: headers.get(k, default)),
            json=lambda: ({"error": {"message": "bad key"}} if text else {}),
        )

    adapter = OpenAICompatibleProviderAdapter.__new__(OpenAICompatibleProviderAdapter)

    def _fake_post(url, headers=None, json=None):
        return _response(403, "", {"server": "cloudflare", "cf-ray": "abc"})

    adapter._client = SimpleNamespace(post=_fake_post)
    adapter._base_url = "https://gateway.example/v1"
    adapter._api_key = "sk-test"

    with pytest.raises(Exception) as exc_info:
        adapter.send_request(request)
    message = str(exc_info.value)
    assert "gateway edge protection" in message
    assert ProviderService._classify_error(None, exc_info.value) == "transient"

    # A JSON-bodied 403 (real key failure, no CF block) stays auth_failure.
    adapter._client = SimpleNamespace(
        post=lambda url, headers=None, json=None: _response(
            403, '{"error":{"message":"invalid key"}}', {"server": "nginx"}
        )
    )
    with pytest.raises(Exception) as exc_info2:
        adapter.send_request(request)
    assert ProviderService._classify_error(None, exc_info2.value) == "auth_failure"


# ----------------------------------------------------------------------
# 10. SSE-only gateway tolerance (live round-5 finding)
# ----------------------------------------------------------------------
def test_openai_adapter_aggregates_forced_sse_body() -> None:
    """Live finding (round 5): api.justwoker.icu answers every
    tool-declaring chat/completions request with text/event-stream
    chunks EVEN when the request did not ask for streaming. The planner
    (toolless) got JSON; conversational chat turns with granted tools
    died with 'provider returned invalid JSON' on every message.

    Contract: the OpenAI adapter aggregates the delta stream into one
    CanonicalResponse — text concatenated, tool-call fragments merged
    by index, last usage/finish_reason win (Hermes SSE-only-gateway
    parity, anthropic_adapter.create_anthropic_message)."""
    from zero.app.provider_adapter import OpenAICompatibleProviderAdapter
    from zero.domain.providers import CanonicalMessage, CanonicalRequest

    request = CanonicalRequest(
        provider="openai-compatible",
        model_name="claude-opus-5",
        messages=(CanonicalMessage(role="user", content="hi"),),
        max_tokens=10,
    )

    sse_body = (
        'data: {"id":"msg_1","object":"chat.completion.chunk","choices":[{"index":0,'
        '"delta":{"role":"assistant","content":""},"finish_reason":null}],"usage":null}\n\n'
        'data: {"id":"msg_1","choices":[{"index":0,"delta":{"content":"Hello"},'
        '"finish_reason":null}],"usage":null}\n\n'
        'data: {"id":"msg_1","choices":[{"index":0,"delta":{"content":" world"},'
        '"finish_reason":null}],"usage":null}\n\n'
        'data: {"id":"msg_1","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,'
        '"id":"call_1","type":"function","function":{"name":"web_search",'
        '"arguments":"{\\"query\\":"}}]},"finish_reason":null}],"usage":null}\n\n'
        'data: {"id":"msg_1","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,'
        '"function":{"arguments":"\\"zero\\"}"}}]},"finish_reason":null}],'
        '"usage":null}\n\n'
        'data: {"id":"msg_1","choices":[{"index":0,"delta":{},'
        '"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":11,'
        '"completion_tokens":7}}\n\n'
        "data: [DONE]\n\n"
    )

    class _SSEResponse:
        status_code = 200
        text = sse_body
        headers = SimpleNamespace(
            get=lambda k, default=None: "text/event-stream" if k == "content-type" else default
        )

        def json(self):  # pragma: no cover - proves the JSON path is NOT used
            raise ValueError("not JSON")

    class _SSEClient:
        def post(self, url, headers=None, json=None):
            return _SSEResponse()

    adapter = OpenAICompatibleProviderAdapter.__new__(OpenAICompatibleProviderAdapter)
    adapter._client = _SSEClient()
    adapter._base_url = "https://gateway.example/v1"
    adapter._api_key = "sk-test"

    response = adapter.send_request(request)
    assert response.content == "Hello world"
    assert len(response.tool_calls) == 1
    call = response.tool_calls[0]
    assert call.tool_name == "web_search"
    assert call.tool_call_id == "call_1"
    assert json.loads(call.arguments) == {"query": "zero"}
    assert response.finish_reason == "tool_calls"
    assert response.usage is not None and response.usage.input_tokens == 11
    assert response.provider_message_id == "msg_1"


def test_openai_adapter_still_rejects_garbage_body() -> None:
    """A non-SSE, non-JSON 200 body must keep failing loudly."""
    from zero.app.provider_adapter import OpenAICompatibleProviderAdapter
    from zero.domain.providers import CanonicalMessage, CanonicalRequest

    request = CanonicalRequest(
        provider="openai-compatible",
        model_name="m",
        messages=(CanonicalMessage(role="user", content="hi"),),
        max_tokens=10,
    )

    class _GarbageResponse:
        status_code = 200
        text = "<html>gateway error page</html>"
        headers = SimpleNamespace(get=lambda k, default=None: "text/html" if k == "content-type" else default)

        def json(self):
            raise ValueError("not JSON")

    class _Client:
        def post(self, url, headers=None, json=None):
            return _GarbageResponse()

    adapter = OpenAICompatibleProviderAdapter.__new__(OpenAICompatibleProviderAdapter)
    adapter._client = _Client()
    adapter._base_url = "https://gateway.example/v1"
    adapter._api_key = "sk-test"

    with pytest.raises(Exception) as exc_info:
        adapter.send_request(request)
    assert "invalid JSON" in str(exc_info.value)
