"""RED tests for Gate D messaging adapters and callback hardening."""

from __future__ import annotations

import importlib
import json
from dataclasses import dataclass

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from zero.adapters.messaging import WebhookAuthError
from zero.app.api import create_app
from zero.app.services import build_services
from zero.config import Settings
from zero.domain.authorization import AuthorizationError
from zero.domain.interfaces import NormalizedEvent
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations
from zero.persistence.repositories.interface_repository import InterfaceRepository


@dataclass
class FakeResponse:
    status_code: int
    payload: dict

    def json(self):
        return self.payload


class FakeTransport:
    def __init__(self, responses=None, failures=0):
        self.responses = list(responses or [])
        self.failures = failures
        self.calls: list[dict] = []

    def request(self, method, url, *, headers=None, json=None, timeout=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers or {},
                "json": json,
                "timeout": timeout,
            }
        )
        if self.failures:
            self.failures -= 1
            raise TimeoutError("temporary transport failure")
        if self.responses:
            return self.responses.pop(0)
        return FakeResponse(200, {"ok": True, "result": {}})


def _load_adapter(module_name: str, class_name: str):
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:  # meaningful RED, not a collection error
        pytest.fail(f"missing adapter module {module_name}: {exc}")
    try:
        return getattr(module, class_name)
    except AttributeError as exc:
        pytest.fail(f"missing adapter class {class_name}: {exc}")


@pytest.fixture
def services(test_settings: Settings):
    database = Database(test_settings)
    apply_migrations(database)
    return build_services(test_settings, database)


@pytest.fixture
def owner_binding(services):
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(owner_id=owner.id, name="Project")
    services.identity.link_external_identity(
        user_id=owner.id,
        platform="telegram",
        external_id="1001",
        verified=True,
    )
    binding = services.interfaces.create_binding(
        project_id=project.id,
        actor_id=owner.id,
        platform="telegram",
        chat_id="-100",
        topic_id="42",
        is_enabled=True,
    )
    return owner, project, binding


def test_telegram_update_normalization_preserves_scope_and_wide_ids():
    TelegramAdapter = _load_adapter("zero.adapters.telegram", "TelegramAdapter")
    adapter = TelegramAdapter(event_handler=lambda event: event)
    update = {
        "update_id": 9876543210123,
        "message": {
            "message_id": 9,
            "from": {"id": 9223372036854775807, "username": "alice"},
            "chat": {"id": -1009876543210, "type": "supergroup"},
            "message_thread_id": 42,
            "text": "<hello> & plan",
        },
    }

    event = adapter.normalize_update(update)

    assert isinstance(event, NormalizedEvent)
    assert event.external_event_id == "9876543210123"
    assert event.external_actor_id == "9223372036854775807"
    assert event.chat_id == "-1009876543210"
    assert event.topic_id == "42"
    assert event.event_kind == "message"
    assert event.content == "<hello> & plan"


def test_telegram_webhook_secret_is_constant_time_validated():
    TelegramAdapter = _load_adapter("zero.adapters.telegram", "TelegramAdapter")
    adapter = TelegramAdapter(event_handler=lambda event: event, webhook_secret="secret")
    update = {
        "update_id": 1,
        "message": {
            "from": {"id": 1},
            "chat": {"id": 2},
            "text": "hello",
        },
    }

    with pytest.raises(WebhookAuthError):
        adapter.handle_webhook(update, headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"})
    event = adapter.handle_webhook(
        update,
        headers={"X-Telegram-Bot-Api-Secret-Token": "secret"},
    )
    assert event.external_event_id == "1"


def test_telegram_webhook_without_secret_fails_closed():
    TelegramAdapter = _load_adapter("zero.adapters.telegram", "TelegramAdapter")
    adapter = TelegramAdapter(event_handler=lambda event: event)
    with pytest.raises(Exception, match="verification is not configured"):
        adapter.verify_webhook({})


def test_telegram_outbound_rendering_escapes_markup_and_retries_bounded():
    TelegramAdapter = _load_adapter("zero.adapters.telegram", "TelegramAdapter")
    transport = FakeTransport(failures=2)
    adapter = TelegramAdapter(
        event_handler=lambda event: event,
        transport=transport,
        bot_token="test-token",
        retry_attempts=3,
        retry_backoff_seconds=0,
    )

    response = adapter.send_message(chat_id="-100", text="<unsafe> & secret")

    assert response.status_code == 200
    assert len(transport.calls) == 3
    payload = transport.calls[-1]["json"]
    assert payload["chat_id"] == "-100"
    assert "&lt;unsafe&gt; &amp; secret" == payload["text"]
    assert "test-token" not in json.dumps(transport.calls[-1]["headers"])


def test_telegram_polling_cursor_is_durable_and_advances_after_processing(
    test_settings: Settings,
):
    TelegramAdapter = _load_adapter("zero.adapters.telegram", "TelegramAdapter")
    database = Database(test_settings)
    apply_migrations(database)
    cursor_store = InterfaceRepository(database)
    seen: list[NormalizedEvent] = []
    transport = FakeTransport(
        responses=[
            FakeResponse(
                200,
                {
                    "ok": True,
                    "result": [
                        {
                            "update_id": 41,
                            "message": {
                                "from": {"id": 1},
                                "chat": {"id": 2},
                                "text": "hello",
                            },
                        }
                    ],
                },
            ),
            FakeResponse(200, {"ok": True, "result": []}),
        ]
    )
    adapter = TelegramAdapter(
        event_handler=seen.append,
        transport=transport,
        bot_token="test-token",
        cursor_store=cursor_store,
        retry_attempts=1,
        retry_backoff_seconds=0,
    )

    first = adapter.poll_once(scope_key="bot")
    second = adapter.poll_once(scope_key="bot")

    assert len(first) == 1
    assert len(seen) == 1
    assert second == []
    assert transport.calls[0]["json"].get("offset") is None
    assert transport.calls[1]["json"]["offset"] == 42
    assert cursor_store.get_cursor("telegram", "bot") == "42"


def test_telegram_poll_records_errored_event_and_advances_cursor():
    """An errored durable event no longer aborts the batch or kills the
    polling loop: the result is recorded, the error is logged, and the
    cursor advances past the poison update (audit fix)."""
    TelegramAdapter = _load_adapter("zero.adapters.telegram", "TelegramAdapter")
    cursor_store = type(
        "CursorStore",
        (),
        {
            "values": {},
            "get_cursor": lambda self, platform, scope: self.values.get((platform, scope)),
            "set_cursor": lambda self, platform, scope, value: self.values.__setitem__(
                (platform, scope), value
            ),
        },
    )()
    transport = FakeTransport(
        responses=[
            FakeResponse(
                200,
                {
                    "ok": True,
                    "result": [
                        {
                            "update_id": 10,
                            "message": {
                                "from": {"id": "user-1"},
                                "chat": {"id": "chat-1"},
                                "text": "hello",
                            },
                        }
                    ],
                },
            )
        ]
    )
    adapter = TelegramAdapter(
        event_handler=lambda _event: type("Result", (), {"processing_result": "error"})(),
        transport=transport,
        bot_token="bot-token",
        cursor_store=cursor_store,
        poll_timeout_seconds=0,
        retry_attempts=1,
    )
    results = adapter.poll_once(scope_key="scope")
    assert len(results) == 1
    assert results[0].processing_result == "error"
    assert cursor_store.get_cursor("telegram", "scope") == "11"


def test_telegram_poll_advances_cursor_past_malformed_addressable_update():
    TelegramAdapter = _load_adapter("zero.adapters.telegram", "TelegramAdapter")
    cursor_store = type(
        "CursorStore",
        (),
        {
            "values": {},
            "get_cursor": lambda self, platform, scope: self.values.get((platform, scope)),
            "set_cursor": lambda self, platform, scope, value: self.values.__setitem__(
                (platform, scope), value
            ),
        },
    )()
    transport = FakeTransport(
        responses=[
            FakeResponse(
                200,
                {
                    "ok": True,
                    "result": [
                        {
                            "update_id": 11,
                            "callback_query": "malformed",
                        }
                    ],
                },
            )
        ]
    )
    adapter = TelegramAdapter(
        event_handler=lambda event: event,
        transport=transport,
        bot_token="bot-token",
        cursor_store=cursor_store,
        poll_timeout_seconds=0,
        retry_attempts=1,
    )

    assert adapter.poll_once(scope_key="scope") == []
    assert cursor_store.values == {("telegram", "scope"): "12"}


def test_telegram_callback_acknowledges_before_domain_dispatch():
    TelegramAdapter = _load_adapter("zero.adapters.telegram", "TelegramAdapter")
    order = []

    class OrderedTransport(FakeTransport):
        def request(self, method, url, *, headers=None, json=None, timeout=None):
            order.append("ack")
            return super().request(method, url, headers=headers, json=json, timeout=timeout)

    def domain_handler(event):
        order.append("dispatch")
        return event

    adapter = TelegramAdapter(
        event_handler=domain_handler,
        transport=OrderedTransport(),
        bot_token="bot-token",
        webhook_secret="webhook-secret",
        retry_attempts=1,
    )
    adapter.handle_webhook(
        {
            "update_id": 12,
            "callback_query": {
                "id": "callback-1",
                "from": {"id": "user-1"},
                "message": {"chat": {"id": "chat-1"}},
                "data": "approve",
            },
        },
        headers={"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"},
    )

    assert order == ["ack", "dispatch"]


def test_discord_interaction_signature_and_callback_normalization():
    DiscordAdapter = _load_adapter("zero.adapters.discord", "DiscordAdapter")
    cryptography = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
    serialization = importlib.import_module("cryptography.hazmat.primitives.serialization")
    private_key = cryptography.Ed25519PrivateKey.generate()
    public_key = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        .hex()
    )
    body = {
        "id": "discord-event-1",
        "type": 3,
        "channel_id": "9223372036854775807",
        "member": {"user": {"id": "18446744073709551615"}},
        "message": {"id": "message-7", "channel_id": "9223372036854775807"},
        "data": {"custom_id": "ct_callback"},
    }
    raw = json.dumps(body, separators=(",", ":")).encode()
    timestamp = "1700000000"
    signature = private_key.sign(timestamp.encode() + raw).hex()
    adapter = DiscordAdapter(
        event_handler=lambda event: event,
        application_public_key=public_key,
    )

    event = adapter.handle_webhook(
        raw,
        headers={
            "X-Signature-Timestamp": timestamp,
            "X-Signature-Ed25519": signature,
        },
    )

    assert event.platform == "discord"
    assert event.external_event_id == "discord-event-1"
    assert event.external_actor_id == "18446744073709551615"
    assert event.chat_id == "9223372036854775807"
    assert event.callback_token == "ct_callback"
    assert event.event_kind == "callback_query"


def test_discord_acknowledges_only_after_durable_dispatch():
    DiscordAdapter = _load_adapter("zero.adapters.discord", "DiscordAdapter")
    seen = []
    transport = FakeTransport()
    adapter = DiscordAdapter(
        event_handler=lambda event: seen.append(event) or event,
        transport=transport,
        bot_token="bot-token",
        webhook_secret="webhook-secret",
    )
    result = adapter.handle_webhook(
        {
            "id": "discord-ack-1",
            "type": 2,
            "token": "interaction-token",
            "channel_id": "channel-1",
            "user": {"id": "user-1"},
            "data": {"name": "status"},
        },
        headers={"X-Discord-Bot-Secret": "webhook-secret"},
    )

    assert result.external_event_id == "discord-ack-1"
    assert seen and seen[0].transport_interaction_token == "interaction-token"
    assert transport.calls[-1]["url"].endswith(
        "/interactions/discord-ack-1/interaction-token/callback"
    )


def test_discord_acknowledges_before_slow_domain_dispatch():
    DiscordAdapter = _load_adapter("zero.adapters.discord", "DiscordAdapter")
    order = []

    class OrderedTransport(FakeTransport):
        def request(self, method, url, *, headers=None, json=None, timeout=None):
            order.append("ack")
            return super().request(method, url, headers=headers, json=json, timeout=timeout)

    def slow_domain_handler(event):
        order.append("dispatch")
        return event

    adapter = DiscordAdapter(
        event_handler=slow_domain_handler,
        transport=OrderedTransport(),
        webhook_secret="webhook-secret",
    )
    adapter.handle_webhook(
        {
            "id": "discord-order-1",
            "type": 2,
            "token": "interaction-token",
            "channel_id": "channel-1",
            "user": {"id": "user-1"},
            "data": {"name": "status"},
        },
        headers={"X-Discord-Bot-Secret": "webhook-secret"},
    )

    assert order == ["ack", "dispatch"]


def test_discord_interaction_callback_does_not_require_bot_token():
    DiscordAdapter = _load_adapter("zero.adapters.discord", "DiscordAdapter")
    transport = FakeTransport()
    adapter = DiscordAdapter(
        event_handler=lambda event: event,
        transport=transport,
        webhook_secret="webhook-secret",
    )

    adapter.handle_webhook(
        {
            "id": "discord-callback-no-bot-token",
            "type": 2,
            "token": "interaction-token",
            "channel_id": "channel-1",
            "user": {"id": "user-1"},
            "data": {"name": "status"},
        },
        headers={"X-Discord-Bot-Secret": "webhook-secret"},
    )

    assert "Authorization" not in transport.calls[-1]["headers"]


def test_discord_thread_delivery_targets_thread_channel():
    DiscordAdapter = _load_adapter("zero.adapters.discord", "DiscordAdapter")
    transport = FakeTransport(responses=[FakeResponse(200, {"id": "message-1"})])
    adapter = DiscordAdapter(
        event_handler=lambda event: event,
        transport=transport,
        bot_token="bot-token",
    )

    adapter.send_message(channel_id="parent-channel", thread_id="thread-channel", text="result")

    call = transport.calls[-1]
    assert call["url"].endswith("/channels/thread-channel/messages")
    assert "message_reference" not in call["json"]


def test_callback_edit_is_denied_without_consuming_token(services, owner_binding):
    owner, project, _binding = owner_binding
    event = services.plans.ingest_conversation_event(
        project_id=project.id,
        actor_id=owner.id,
        source="web",
        origin_kind="authenticated_human",
        content="Make a plan",
    )
    plan = services.plans.create_plan(project_id=project.id, actor_id=owner.id)
    from zero.domain.plans import PlanRevisionContent

    services.plans.propose_revision(
        plan_id=plan.id,
        project_id=project.id,
        actor_id=owner.id,
        content=PlanRevisionContent(
            objective="Make a plan",
            scope=(),
            constraints=(),
            acceptance_criteria=("done",),
            risks=(),
            unresolved_questions=(),
            source_event_ids=(event.id,),
        ),
    )
    token = services.interfaces.create_callback_token(
        project_id=project.id,
        plan_id=plan.id,
        revision_number=1,
        action="edit",
        created_by=owner.id,
    )

    result = services.interfaces.process_inbound_event(
        NormalizedEvent(
            platform="telegram",
            external_event_id="edit-1",
            external_actor_id="1001",
            chat_id="-100",
            topic_id="42",
            event_kind="callback_query",
            content="edit",
            callback_token=token.id.value,
        )
    )

    assert result.processing_result == "denied"
    assert "edit" in (result.processing_detail or "").lower()
    assert services.interfaces.get_callback_token(token.id).is_used is False


def test_interface_binding_rejects_plaintext_bot_token(services, owner_binding):
    owner, project, _binding = owner_binding

    with pytest.raises(ValueError, match="secret reference"):
        services.interfaces.create_binding(
            project_id=project.id,
            actor_id=owner.id,
            platform="telegram",
            chat_id="-101",
            bot_token_ref="plaintext-bot-token",
        )


def test_interface_binding_does_not_return_foreign_existing_binding(services, owner_binding):
    _owner_a, project_a, _binding_a = owner_binding
    owner_b = services.identity.create_user(display_name="Second owner")
    project_b = services.identity.create_project(owner_id=owner_b.id, name="Second project")
    services.interfaces.create_binding(
        project_id=project_b.id,
        actor_id=owner_b.id,
        platform="telegram",
        chat_id="-202",
        topic_id="7",
        is_enabled=True,
    )

    with pytest.raises(ValueError, match="another project"):
        services.interfaces.create_binding(
            project_id=project_a.id,
            actor_id=_owner_a.id,
            platform="telegram",
            chat_id="-202",
            topic_id="7",
            is_enabled=True,
        )


def test_verified_external_user_outside_project_cannot_ingest_message(services, owner_binding):
    owner, project, _binding = owner_binding
    outsider = services.identity.create_user(display_name="Outsider")
    services.identity.link_external_identity(
        user_id=outsider.id,
        platform="telegram",
        external_id="9002",
        verified=True,
    )
    before = services.plans.list_conversation_events(project_id=project.id, actor_id=owner.id)

    result = services.interfaces.process_inbound_event(
        NormalizedEvent(
            platform="telegram",
            external_event_id="outsider-message-1",
            external_actor_id="9002",
            chat_id="-100",
            topic_id="42",
            event_kind="message",
            content="foreign project message",
        )
    )

    after = services.plans.list_conversation_events(project_id=project.id, actor_id=owner.id)
    assert result.processing_result == "denied"
    assert len(after) == len(before)


def test_event_idempotency_is_scoped_to_interface_binding(services, owner_binding):
    _owner_a, project_a, _binding_a = owner_binding
    owner_b = services.identity.create_user(display_name="Second owner")
    project_b = services.identity.create_project(owner_id=owner_b.id, name="Second project")
    services.identity.link_external_identity(
        user_id=owner_b.id,
        platform="telegram",
        external_id="2002",
        verified=True,
    )
    services.interfaces.create_binding(
        project_id=project_b.id,
        actor_id=owner_b.id,
        platform="telegram",
        chat_id="-200",
        topic_id="43",
        is_enabled=True,
    )

    first = services.interfaces.process_inbound_event(
        NormalizedEvent(
            platform="telegram",
            external_event_id="same-update-id",
            external_actor_id="1001",
            chat_id="-100",
            topic_id="42",
            event_kind="other",
            content="first scope",
        )
    )
    second = services.interfaces.process_inbound_event(
        NormalizedEvent(
            platform="telegram",
            external_event_id="same-update-id",
            external_actor_id="2002",
            chat_id="-200",
            topic_id="43",
            event_kind="other",
            content="second scope",
        )
    )

    assert first.processing_result == "processed"
    assert second.processing_result == "processed"
    assert len(services.interfaces.list_event_log(project_a.id)) == 1
    assert len(services.interfaces.list_event_log(project_b.id)) == 1


def test_expired_interface_claim_can_be_reclaimed(services, owner_binding):
    _owner, _project, binding = owner_binding
    repository = InterfaceRepository(services.database)
    assert (
        repository.claim_event(
            "telegram",
            "crashed-event",
            binding_scope=binding.id.value,
            binding_id=binding.id.value,
            lease_seconds=300,
        )
        is True
    )
    services.database.connect().execute(
        "UPDATE interface_event_claims SET lease_expires_at = ? "
        "WHERE platform = ? AND binding_scope = ? AND external_event_id = ?",
        ("2000-01-01T00:00:00.000Z", "telegram", binding.id.value, "crashed-event"),
    )
    services.database.connect().commit()

    assert (
        repository.claim_event(
            "telegram",
            "crashed-event",
            binding_scope=binding.id.value,
            binding_id=binding.id.value,
            lease_seconds=300,
        )
        is True
    )
    row = (
        services.database.connect()
        .execute(
            "SELECT state, attempt_count FROM interface_event_claims "
            "WHERE platform = ? AND binding_scope = ? AND external_event_id = ?",
            ("telegram", binding.id.value, "crashed-event"),
        )
        .fetchone()
    )
    assert tuple(row) == ("processing", 2)


def test_reclaimed_interface_claim_fences_stale_completion(services, owner_binding):
    _owner, _project, binding = owner_binding
    repository = InterfaceRepository(services.database)
    first_token = repository.claim_event_with_token(
        "telegram",
        "fenced-event",
        binding_scope=binding.id.value,
        binding_id=binding.id.value,
        lease_seconds=300,
    )
    assert first_token
    services.database.connect().execute(
        "UPDATE interface_event_claims SET lease_expires_at = ? "
        "WHERE platform = ? AND binding_scope = ? AND external_event_id = ?",
        ("2000-01-01T00:00:00.000Z", "telegram", binding.id.value, "fenced-event"),
    )
    services.database.connect().commit()
    second_token = repository.claim_event_with_token(
        "telegram",
        "fenced-event",
        binding_scope=binding.id.value,
        binding_id=binding.id.value,
        lease_seconds=300,
    )
    assert second_token and second_token != first_token
    assert not repository.complete_event_claim(
        "telegram",
        "fenced-event",
        binding_scope=binding.id.value,
        claim_token=first_token,
    )
    row = (
        services.database.connect()
        .execute(
            "SELECT state, attempt_count FROM interface_event_claims "
            "WHERE platform = ? AND binding_scope = ? AND external_event_id = ?",
            ("telegram", binding.id.value, "fenced-event"),
        )
        .fetchone()
    )
    assert tuple(row) == ("processing", 2)

    owner, project, _binding = owner_binding
    result = services.interfaces.process_inbound_event(
        NormalizedEvent(
            platform="telegram",
            external_event_id="redaction-event-1",
            external_actor_id="1001",
            chat_id="-100",
            topic_id="42",
            event_kind="message",
            content="please store api_key=synthetic-secret and token=synthetic-token",
        )
    )

    assert "synthetic-secret" not in (result.event_content or "")
    assert "synthetic-token" not in (result.event_content or "")
    events = services.interfaces.list_event_log(project.id)
    assert "synthetic-secret" not in (events[0].event_content or "")
    conversation = services.plans.list_conversation_events(project_id=project.id, actor_id=owner.id)
    assert "synthetic-secret" not in conversation[-1].content


def test_callback_action_failure_can_be_retried_without_poisoning_token(
    services, owner_binding, monkeypatch
):
    owner, project, _binding = owner_binding
    event = services.plans.ingest_conversation_event(
        project_id=project.id,
        actor_id=owner.id,
        source="web",
        origin_kind="authenticated_human",
        content="Retry me",
    )
    plan = services.plans.create_plan(project_id=project.id, actor_id=owner.id)
    from zero.domain.plans import PlanRevisionContent

    services.plans.propose_revision(
        plan_id=plan.id,
        project_id=project.id,
        actor_id=owner.id,
        content=PlanRevisionContent(
            objective="Retry me",
            scope=(),
            constraints=(),
            acceptance_criteria=("done",),
            risks=(),
            unresolved_questions=(),
            source_event_ids=(event.id,),
        ),
    )
    token = services.interfaces.create_callback_token(
        project_id=project.id,
        plan_id=plan.id,
        revision_number=1,
        action="approve",
        created_by=owner.id,
    )
    original = services.plans.approve_revision
    calls = {"count": 0}

    def fail_once(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("temporary failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(services.plans, "approve_revision", fail_once)
    first = services.interfaces.process_inbound_event(
        NormalizedEvent(
            platform="telegram",
            external_event_id="retry-1",
            external_actor_id="1001",
            chat_id="-100",
            topic_id="42",
            event_kind="callback_query",
            content="approve",
            callback_token=token.id.value,
        )
    )
    assert first.processing_result == "error"
    assert services.interfaces.get_callback_token(token.id).is_used is False

    second = services.interfaces.process_inbound_event(
        NormalizedEvent(
            platform="telegram",
            external_event_id="retry-2",
            external_actor_id="1001",
            chat_id="-100",
            topic_id="42",
            event_kind="callback_query",
            content="approve",
            callback_token=token.id.value,
        )
    )
    assert second.processing_result == "processed"
    assert services.interfaces.get_callback_token(token.id).is_used is True


def test_callback_token_creation_checks_plan_scope_and_permission(services):
    owner = services.identity.create_user(display_name="Owner")
    viewer = services.identity.create_user(display_name="Viewer")
    project = services.identity.create_project(owner_id=owner.id, name="Project")
    services.identity.add_member(
        project_id=project.id, actor_id=owner.id, member_id=viewer.id, role="viewer"
    )
    event = services.plans.ingest_conversation_event(
        project_id=project.id,
        actor_id=owner.id,
        source="web",
        origin_kind="authenticated_human",
        content="Plan",
    )
    plan = services.plans.create_plan(project_id=project.id, actor_id=owner.id)
    from zero.domain.plans import PlanRevisionContent

    services.plans.propose_revision(
        plan_id=plan.id,
        project_id=project.id,
        actor_id=owner.id,
        content=PlanRevisionContent(
            objective="Plan",
            scope=(),
            constraints=(),
            acceptance_criteria=("done",),
            risks=(),
            unresolved_questions=(),
            source_event_ids=(event.id,),
        ),
    )
    with pytest.raises(AuthorizationError):
        services.interfaces.create_callback_token(
            project_id=project.id,
            plan_id=plan.id,
            revision_number=1,
            action="approve",
            created_by=viewer.id,
        )


@pytest.mark.asyncio
async def test_composed_telegram_webhook_reaches_interface_service() -> None:
    settings = Settings.load_for_test(
        telegram_webhook_secret=SecretStr("telegram-test-webhook-secret")
    )
    app = create_app(settings)
    services = app.state.services
    owner = services.identity.create_user(display_name="Webhook Owner")
    project = services.identity.create_project(owner_id=owner.id, name="Webhook Project")
    services.identity.link_external_identity(
        user_id=owner.id,
        platform="telegram",
        external_id="9001",
        verified=True,
    )
    binding = services.interfaces.create_binding(
        project_id=project.id,
        actor_id=owner.id,
        platform="telegram",
        chat_id="7001",
        is_enabled=True,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/webhooks/telegram/{project.id.value}/{binding.id.value}",
            headers={"X-Telegram-Bot-Api-Secret-Token": "telegram-test-webhook-secret"},
            json={
                "update_id": 9001,
                "message": {
                    "from": {"id": 9001},
                    "chat": {"id": 7001},
                    "text": "hello from telegram",
                },
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["platform"] == "telegram"
    assert payload["processing_result"] == "processed"


@pytest.mark.asyncio
async def test_composed_discord_webhook_verifies_signature_and_processes_event() -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes_raw().hex()
    settings = Settings.load_for_test(discord_application_public_key=SecretStr(public_key))
    app = create_app(settings)
    services = app.state.services
    owner = services.identity.create_user(display_name="Discord Owner")
    project = services.identity.create_project(owner_id=owner.id, name="Discord Project")
    services.identity.link_external_identity(
        user_id=owner.id,
        platform="discord",
        external_id="9101",
        verified=True,
    )
    binding = services.interfaces.create_binding(
        project_id=project.id,
        actor_id=owner.id,
        platform="discord",
        chat_id="8101",
        is_enabled=True,
    )
    payload = {
        "id": "discord-event-1",
        "type": 2,
        "member": {"user": {"id": "9101"}},
        "channel_id": "8101",
        "data": {"name": "hello"},
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    timestamp = "1700000000"
    signature = private_key.sign(timestamp.encode() + raw).hex()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/webhooks/discord/{project.id.value}/{binding.id.value}",
            headers={
                "X-Signature-Timestamp": timestamp,
                "X-Signature-Ed25519": signature,
            },
            content=raw,
        )

    assert response.status_code == 200
    result = response.json()
    assert result["platform"] == "discord"
    assert result["processing_result"] == "processed"


@pytest.mark.asyncio
async def test_webhook_binding_scope_matches_payload_scope() -> None:
    settings = Settings.load_for_test(
        telegram_webhook_secret=SecretStr("telegram-test-webhook-secret")
    )
    app = create_app(settings)
    services = app.state.services
    owner_a = services.identity.create_user(display_name="Scope Owner A")
    owner_b = services.identity.create_user(display_name="Scope Owner B")
    project_a = services.identity.create_project(owner_id=owner_a.id, name="Scope A")
    project_b = services.identity.create_project(owner_id=owner_b.id, name="Scope B")
    services.identity.link_external_identity(
        user_id=owner_b.id,
        platform="telegram",
        external_id="9202",
        verified=True,
    )
    binding_a = services.interfaces.create_binding(
        project_id=project_a.id,
        actor_id=owner_a.id,
        platform="telegram",
        chat_id="8201",
        is_enabled=True,
    )
    binding_b = services.interfaces.create_binding(
        project_id=project_b.id,
        actor_id=owner_b.id,
        platform="telegram",
        chat_id="8202",
        is_enabled=True,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/webhooks/telegram/{project_a.id.value}/{binding_a.id.value}",
            headers={"X-Telegram-Bot-Api-Secret-Token": "telegram-test-webhook-secret"},
            json={
                "update_id": 9202,
                "message": {
                    "from": {"id": 9202},
                    "chat": {"id": int(binding_b.chat_id)},
                    "text": "must not cross binding scope",
                },
            },
        )

    assert response.status_code == 404
    assert services.interfaces.list_event_log(project_b.id) == []
