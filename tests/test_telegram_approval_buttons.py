"""Round-7 FULLY verification — Telegram inline keyboard + approval UX.

Pins the three "FULLY" guarantees at the adapter and service boundary.

INLINE KEYBOARD (button UX)
- a button press is answered on the Bot API ONCE, AFTER processing,
  with an outcome toast (``answerCallbackQuery`` text) — Hermes
  ``query.answer(text=...)`` parity — on BOTH intake paths (webhook
  and polling);
- the answer is best-effort: a Telegram outage must not destroy the
  durable dispatch result;
- plain messages never trigger ``answerCallbackQuery``.

APPROVAL (durable boundary matrix)
- the reject button runs the SAME durable pipeline as approve
  (plan → rejected, one-shot token consumption);
- unknown callback tokens are rejected loudly (error entry, no plan
  state change);
- a linked-but-NOT-a-member actor is denied and the token survives
  unused (UI controls carry references, not authority);
- an expired token is rejected;
- a replayed token is idempotent (already pinned in
  test_interfaces.py; asserted here against the service too).

Reference: /home/z/my-project/hermes-agent messaging layer (query
acknowledgement semantics).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from zero.adapters.messaging import AdapterError, RetryPolicy
from zero.adapters.telegram import (
    TelegramAdapter,
    _callback_outcome_text,
)
from zero.app.services import build_services
from zero.config import Settings
from zero.domain.interfaces import NormalizedEvent
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations


# ----------------------------------------------------------------------
# Fixtures (self-contained: same shape as test_interfaces.py)
# ----------------------------------------------------------------------


@pytest.fixture
def services(test_settings: Settings):
    database = Database(test_settings)
    apply_migrations(database)
    return build_services(test_settings, database)


@pytest.fixture
def project_with_owner_and_binding(services):
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(owner_id=owner.id, name="Project A")
    services.identity.link_external_identity(
        user_id=owner.id,
        platform="telegram",
        external_id="7086634092",
        external_username="owner",
        verified=True,
    )
    binding = services.interfaces.create_binding(
        project_id=project.id,
        actor_id=owner.id,
        platform="telegram",
        chat_id="100",
        topic_id="7",
        is_enabled=True,
    )
    return owner, project, binding


def _proposed_plan(services, owner, project):
    """A plan with one proposed revision, reached the durable way."""
    import uuid

    from zero.domain.plans import PlanRevisionContent

    conv = services.plans.ingest_conversation_event(
        project_id=project.id,
        actor_id=owner.id,
        source="telegram",
        origin_kind="authenticated_human",
        content="Add a login page",
        external_event_id=f"ev-{uuid.uuid4().hex}",
    )
    plan = services.plans.create_plan(project_id=project.id, actor_id=owner.id)
    content = PlanRevisionContent(
        objective="Add a login page",
        scope=(),
        constraints=(),
        acceptance_criteria=("Login form renders",),
        risks=(),
        unresolved_questions=(),
        source_event_ids=(conv.id,),
    )
    services.plans.propose_revision(
        plan_id=plan.id,
        project_id=project.id,
        actor_id=owner.id,
        content=content,
    )
    return plan


# ----------------------------------------------------------------------
# 1. Outcome toast mapping
# ----------------------------------------------------------------------


def test_callback_outcome_text_mapping() -> None:
    """The toast must say what HAPPENED (Hermes parity): approve and
    reject get distinct, human-visible outcomes; denials and errors
    degrade honestly; unknown shapes stay neutral."""
    assert (
        _callback_outcome_text(
            SimpleNamespace(
                processing_result="processed",
                processing_detail="callback approve executed for revision 1",
            )
        )
        == "✅ Plan approved"
    )
    assert (
        _callback_outcome_text(
            SimpleNamespace(
                processing_result="processed",
                processing_detail="callback reject executed for revision 2",
            )
        )
        == "✖️ Plan rejected"
    )
    assert (
        _callback_outcome_text(
            SimpleNamespace(
                processing_result="denied",
                processing_detail="callback actor is not authorized for this action",
            )
        )
        == "⛔ Not allowed"
    )
    assert (
        _callback_outcome_text(
            SimpleNamespace(
                processing_result="error",
                processing_detail="callback token expired",
            )
        )
        == "⚠️ Failed — see logs"
    )
    # Unknown / None results degrade to a neutral acknowledgment —
    # the spinner always stops with SOME feedback.
    assert _callback_outcome_text(None) == "✅ Done"
    assert _callback_outcome_text(SimpleNamespace()) == "✅ Done"


# ----------------------------------------------------------------------
# 2. Webhook path answers the press with the outcome
# ----------------------------------------------------------------------


class _RecordingTransport:
    """Records Bot API method calls; fails selectively on demand."""

    def __init__(self, fail_methods: tuple[str, ...] = ()) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._fail = fail_methods

    def request(self, method, url, headers=None, json=None, timeout=None):
        name = url.rsplit("/", 1)[-1]
        self.calls.append((name, json if json is not None else {}))
        if name in self._fail:
            raise AdapterError(f"{name} unavailable (simulated Telegram outage)")
        return SimpleNamespace(status_code=200, json=lambda: {"ok": True, "result": []})


def _adapter(transport, handler) -> TelegramAdapter:
    return TelegramAdapter(
        event_handler=handler,
        transport=transport,
        bot_token="123:TESTTOKENVALUE",
        webhook_secret="test-secret",
        poll_timeout_seconds=0,
        retry_policy=RetryPolicy(attempts=1, backoff_seconds=0.0, timeout_seconds=5.0),
    )


def _callback_update(query_id: str, data: str) -> dict:
    return {
        "update_id": 4242,
        "callback_query": {
            "id": query_id,
            "from": {"id": 7086634092, "username": "owner"},
            "message": {
                "message_id": 900,
                "from": {"id": 123, "is_bot": True},
                "chat": {"id": -100, "type": "supergroup"},
                "date": 1730000000,
            },
            "data": data,
        },
    }


def test_webhook_button_press_answered_once_with_outcome() -> None:
    """handle_webhook answers the callback ONCE, AFTER dispatch, with
    the outcome text — the Telegram client toast on the pressed button."""
    transport = _RecordingTransport()
    dispatch_result = SimpleNamespace(
        processing_result="processed",
        processing_detail="callback approve executed for revision 1",
    )
    adapter = _adapter(transport, handler=lambda e: dispatch_result)
    result = adapter.handle_webhook(
        _callback_update("cbq-1", "ct_approve_token"),
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-secret"},
    )
    assert result is dispatch_result  # durable result survives
    answers = [p for name, p in transport.calls if name == "answerCallbackQuery"]
    assert len(answers) == 1
    assert answers[0]["callback_query_id"] == "cbq-1"
    assert answers[0]["text"] == "✅ Plan approved"


def test_webhook_reject_press_gets_rejected_toast() -> None:
    transport = _RecordingTransport()
    dispatch_result = SimpleNamespace(
        processing_result="processed",
        processing_detail="callback reject executed for revision 1",
    )
    adapter = _adapter(transport, handler=lambda e: dispatch_result)
    adapter.handle_webhook(
        _callback_update("cbq-2", "ct_reject_token"),
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-secret"},
    )
    answers = [p for name, p in transport.calls if name == "answerCallbackQuery"]
    assert answers[0]["text"] == "✖️ Plan rejected"


def test_webhook_denied_press_gets_denied_toast() -> None:
    transport = _RecordingTransport()
    dispatch_result = SimpleNamespace(
        processing_result="denied",
        processing_detail="callback actor is not authorized for this action",
    )
    adapter = _adapter(transport, handler=lambda e: dispatch_result)
    adapter.handle_webhook(
        _callback_update("cbq-3", "ct_stolen_token"),
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-secret"},
    )
    answers = [p for name, p in transport.calls if name == "answerCallbackQuery"]
    assert answers[0]["text"] == "⛔ Not allowed"


# ----------------------------------------------------------------------
# 3. Polling path answers the press too
# ----------------------------------------------------------------------


def test_polling_button_press_answered_with_outcome() -> None:
    """A button press that arrives via getUpdates gets the SAME outcome
    feedback as one arriving via webhook (no spinning clock until the
    ~10s Telegram timeout)."""
    transport = _RecordingTransport()

    def request(method, url, payload=None, **_):
        name = url.rsplit("/", 1)[-1]
        if name == "getUpdates":
            return SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "ok": True,
                    "result": [_callback_update("cbq-9", "ct_token_via_polling")],
                },
            )
        transport.calls.append((name, payload if payload is not None else {}))
        return SimpleNamespace(status_code=200, json=lambda: {"ok": True, "result": []})

    dispatch_result = SimpleNamespace(
        processing_result="processed",
        processing_detail="callback approve executed for revision 3",
    )
    adapter = _adapter(transport, handler=lambda e: dispatch_result)
    adapter._request = request  # serve the batch from the fake Bot API
    results = adapter.poll_once()
    assert results == [dispatch_result]
    answers = [p for name, p in transport.calls if name == "answerCallbackQuery"]
    assert len(answers) == 1
    assert answers[0]["callback_query_id"] == "cbq-9"
    assert answers[0]["text"] == "✅ Plan approved"


def test_stale_query_id_400_never_kills_the_polling_worker() -> None:
    """Round-7 LIVE finding: Telegram answers ``answerCallbackQuery``
    with HTTP 400 ``QUERY_ID_INVALID`` for a stale/already-answered
    query. That surfaces as a plain RuntimeError (ok=false), NOT an
    AdapterError — and it used to propagate out of ``poll_once`` and
    kill the whole polling worker: ONE expired button press took the
    bot offline. The batch must now complete and the offset advance."""

    class _Query400Transport:
        def __init__(self):
            self.calls: list[tuple[str, dict]] = []

        def request(self, method, url, payload=None, **_):
            name = url.rsplit("/", 1)[-1]
            self.calls.append((name, payload if payload is not None else {}))
            if name == "getUpdates":
                return SimpleNamespace(
                    status_code=200,
                    json=lambda: {
                        "ok": True,
                        "result": [
                            _callback_update("cbq-expired", "ct_old_token"),
                            # A second update behind the poisoned answer:
                            # the batch must still reach it.
                            {
                                "update_id": 4243,
                                "message": {
                                    "message_id": 901,
                                    "from": {"id": 7086634092},
                                    "chat": {"id": -100, "type": "supergroup"},
                                    "date": 1730000000,
                                    "text": "hello after the stale press",
                                },
                            },
                        ],
                    },
                )
            if name == "answerCallbackQuery":
                return SimpleNamespace(
                    status_code=400,
                    json=lambda: {
                        "ok": False,
                        "description": "Bad Request: query is too old and "
                        "response timeout expired or query ID is invalid",
                    },
                )
            return SimpleNamespace(
                status_code=200, json=lambda: {"ok": True, "result": []}
            )

    transport = _Query400Transport()
    seen: list = []
    adapter = _adapter(transport, handler=lambda e: seen.append(e) or SimpleNamespace(
        processing_result="processed", processing_detail="x"
    ))
    adapter._request = transport.request
    results = adapter.poll_once()  # must NOT raise
    # Both updates processed; the offset advanced past the poisoned one.
    assert len(results) == 2
    assert len(seen) == 2
    assert [p for name, p in transport.calls if name == "answerCallbackQuery"]


# ----------------------------------------------------------------------
# 4. Best-effort answer + message suppression
# ----------------------------------------------------------------------


def test_answer_outage_never_destroyes_the_dispatch_result() -> None:
    """Telegram being down must not fail the intake: the durable event
    log remains authoritative (Hermes best-effort acknowledgement)."""
    transport = _RecordingTransport(fail_methods=("answerCallbackQuery",))
    dispatch_result = SimpleNamespace(
        processing_result="processed",
        processing_detail="callback approve executed for revision 1",
    )
    adapter = _adapter(transport, handler=lambda e: dispatch_result)
    result = adapter.handle_webhook(
        _callback_update("cbq-out", "ct_token"),
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-secret"},
    )
    assert result is dispatch_result


def test_dispatch_crash_still_answers_the_press() -> None:
    """The spinner must stop on EVERY path: a dispatch that RAISES still
    answers the press with an honest failure toast before the exception
    propagates (webhook path)."""
    transport = _RecordingTransport()

    def crashing_handler(event):
        raise RuntimeError("domain dispatch exploded")

    adapter = _adapter(transport, handler=crashing_handler)
    with pytest.raises(RuntimeError):
        adapter.handle_webhook(
            _callback_update("cbq-crash", "ct_token"),
            headers={"X-Telegram-Bot-Api-Secret-Token": "test-secret"},
        )
    answers = [p for name, p in transport.calls if name == "answerCallbackQuery"]
    assert len(answers) == 1
    assert answers[0]["callback_query_id"] == "cbq-crash"
    assert answers[0]["text"] == "⚠️ Failed — see logs"


def test_polling_dispatch_crash_still_answers_the_press() -> None:
    """Same invariant on the polling path."""
    transport = _RecordingTransport()

    def request(method, url, payload=None, **_):
        name = url.rsplit("/", 1)[-1]
        if name == "getUpdates":
            return SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "ok": True,
                    "result": [_callback_update("cbq-crash-poll", "ct_token")],
                },
            )
        transport.calls.append((name, payload if payload is not None else {}))
        return SimpleNamespace(status_code=200, json=lambda: {"ok": True, "result": []})

    def crashing_handler(event):
        raise RuntimeError("domain dispatch exploded")

    adapter = _adapter(transport, handler=crashing_handler)
    adapter._request = request
    with pytest.raises(RuntimeError):
        adapter.poll_once()
    answers = [p for name, p in transport.calls if name == "answerCallbackQuery"]
    assert len(answers) == 1
    assert answers[0]["text"] == "⚠️ Failed — see logs"


# ----------------------------------------------------------------------
# 4b. Webhook composition: the transport service owns the answer
# ----------------------------------------------------------------------
#
# Live-round-7 bug: the engine's WEBHOOK adapter holds no bot token
# (tokens are per-binding secrets resolved at action time), so the
# adapter's own answer attempt died silently at _api_url with
# WebhookAuthError — every webhook-delivered button press spun until
# Telegram's ~10s query timeout. The transport service now answers the
# press AFTER dispatch using the binding's resolved credential.


def _secret_keyed_services():
    """A services stack with a secret key (needed to store the binding's
    bot-token secret the engine's composition resolves at answer time)."""
    settings = Settings.load_for_test(secret_key="e" * 64)
    database = Database(settings)
    apply_migrations(database)
    return settings, build_services(settings, database)


def test_webhook_press_answered_via_binding_credential(test_settings) -> None:
    """The REAL engine composition: a token-less webhook adapter + the
    transport service must still produce an answerCallbackQuery on the
    Bot API with the outcome text."""
    from zero.app.interface_transport_service import InterfaceTransportService
    from zero.persistence.repositories.interface_repository import (
        InterfaceRepository,
    )

    test_settings, services = _secret_keyed_services()
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(owner_id=owner.id, name="P")
    services.identity.link_external_identity(
        user_id=owner.id,
        platform="telegram",
        external_id="7086634092",
        verified=True,
    )
    bot_ref = services.secrets.store(
        project_id=project.id,
        name="telegram-bot-token",
        secret_type="token",
        value="123:TESTTOKENVALUE",
        actor_id=owner.id,
    )
    binding = services.interfaces.create_binding(
        project_id=project.id,
        actor_id=owner.id,
        platform="telegram",
        chat_id="-100",
        bot_token_ref=bot_ref.id.value,
        is_enabled=True,
    )
    plan = _proposed_plan(services, owner, project)
    token = services.interfaces.create_callback_token(
        project_id=project.id,
        plan_id=plan.id,
        revision_number=1,
        action="approve",
        created_by=owner.id,
    )

    api_transport = _RecordingTransport()
    svc = InterfaceTransportService(
        services.interfaces,
        InterfaceRepository(services.database),
        test_settings,
        secret_service=services.secrets,
        transport=api_transport,
    )
    # The engine's webhook adapter shape: NO bot token.
    svc._adapters["telegram"] = TelegramAdapter(
        event_handler=services.interfaces.process_inbound_event,
        transport=api_transport,
        webhook_secret="engine-webhook-secret",
    )

    update = _callback_update("cbq-engine-1", token.id.value)
    result = svc.process_webhook(
        platform="telegram",
        project_id=project.id,
        binding_id=binding.id,
        body=json.dumps(update).encode(),
        headers={"X-Telegram-Bot-Api-Secret-Token": "engine-webhook-secret"},
    )

    # The durable pipeline ran.
    assert result.processing_result == "processed"
    plan = services.plans.get_plan(plan.id, project_id=project.id, actor_id=owner.id)
    assert plan.current_state == "approved"
    # AND the press was answered on the Bot API with the outcome toast.
    answers = [
        p for name, p in api_transport.calls if name == "answerCallbackQuery"
    ]
    assert len(answers) == 1
    assert answers[0]["callback_query_id"] == "cbq-engine-1"
    assert answers[0]["text"] == "✅ Plan approved"


def test_webhook_press_by_stranger_gets_no_credential_resolution(test_settings) -> None:
    """A stranger's denied press must not attempt any Bot API call (no
    resolved actor → nothing to authenticate the secret read with), and
    the durable denial stands."""
    from zero.app.interface_transport_service import InterfaceTransportService
    from zero.persistence.repositories.interface_repository import (
        InterfaceRepository,
    )

    test_settings, services = _secret_keyed_services()
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(owner_id=owner.id, name="P")
    services.identity.link_external_identity(
        user_id=owner.id,
        platform="telegram",
        external_id="7086634092",
        verified=True,
    )
    bot_ref = services.secrets.store(
        project_id=project.id,
        name="telegram-bot-token",
        secret_type="token",
        value="123:TESTTOKENVALUE",
        actor_id=owner.id,
    )
    binding = services.interfaces.create_binding(
        project_id=project.id,
        actor_id=owner.id,
        platform="telegram",
        chat_id="-100",
        bot_token_ref=bot_ref.id.value,
        is_enabled=True,
    )
    plan = _proposed_plan(services, owner, project)
    token = services.interfaces.create_callback_token(
        project_id=project.id,
        plan_id=plan.id,
        revision_number=1,
        action="approve",
        created_by=owner.id,
    )

    api_transport = _RecordingTransport()
    svc = InterfaceTransportService(
        services.interfaces,
        InterfaceRepository(services.database),
        test_settings,
        secret_service=services.secrets,
        transport=api_transport,
    )
    svc._adapters["telegram"] = TelegramAdapter(
        event_handler=services.interfaces.process_inbound_event,
        transport=api_transport,
        webhook_secret="engine-webhook-secret",
    )

    update = _callback_update("cbq-stranger-1", token.id.value)
    update["callback_query"]["from"] = {"id": 666000666, "username": "stranger"}
    result = svc.process_webhook(
        platform="telegram",
        project_id=project.id,
        binding_id=binding.id,
        body=json.dumps(update).encode(),
        headers={"X-Telegram-Bot-Api-Secret-Token": "engine-webhook-secret"},
    )

    assert result.processing_result == "ignored_unlinked"
    plan = services.plans.get_plan(plan.id, project_id=project.id, actor_id=owner.id)
    assert plan.current_state == "proposed"
    stored = services.interfaces.get_callback_token(token.id)
    assert not stored.is_used
    # No Bot API traffic at all: no sendMessage, no answerCallbackQuery.
    assert api_transport.calls == []


def test_plain_message_never_answers_a_callback() -> None:
    transport = _RecordingTransport()
    adapter = _adapter(transport, handler=lambda e: None)
    adapter.handle_webhook(
        {
            "update_id": 1,
            "message": {
                "message_id": 5,
                "from": {"id": 7086634092},
                "chat": {"id": -100, "type": "supergroup"},
                "date": 1730000000,
                "text": "hello",
            },
        },
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-secret"},
    )
    assert transport.calls == []  # no answerCallbackQuery for messages


# ----------------------------------------------------------------------
# 5. Durable approval boundary matrix (service level)
# ----------------------------------------------------------------------


def _callback_event(external_event_id: str, token_value: str, actor: str = "7086634092"):
    return NormalizedEvent(
        platform="telegram",
        external_event_id=external_event_id,
        external_actor_id=actor,
        chat_id="100",
        topic_id="7",
        event_kind="callback_query",
        content=token_value,
        callback_token=token_value,
    )


def test_reject_button_rejects_plan_through_durable_pipeline(
    services, project_with_owner_and_binding
) -> None:
    """The ✖️ Reject button is a first-class citizen: same pipeline,
    same one-shot token consumption, plan lands in 'rejected'."""
    owner, project, _binding = project_with_owner_and_binding
    plan = _proposed_plan(services, owner, project)
    token = services.interfaces.create_callback_token(
        project_id=project.id,
        plan_id=plan.id,
        revision_number=1,
        action="reject",
        created_by=owner.id,
    )
    result = services.interfaces.process_inbound_event(
        _callback_event("cb_reject_1", token.id.value)
    )
    assert result.processing_result == "processed"
    assert "reject" in (result.processing_detail or "")
    plan = services.plans.get_plan(plan.id, project_id=project.id, actor_id=owner.id)
    assert plan.current_state == "rejected"
    # One-shot consumption.
    stored = services.interfaces.get_callback_token(token.id)
    assert stored.is_used


def test_unknown_callback_token_is_a_loud_error(
    services, project_with_owner_and_binding
) -> None:
    """A forged callback_data value must never touch plan state."""
    owner, project, _binding = project_with_owner_and_binding
    plan = _proposed_plan(services, owner, project)
    result = services.interfaces.process_inbound_event(
        _callback_event("cb_forged_1", "ct_totally_made_up")
    )
    assert result.processing_result == "error"
    assert "not found" in (result.processing_detail or "")
    plan = services.plans.get_plan(plan.id, project_id=project.id, actor_id=owner.id)
    assert plan.current_state == "proposed"


def test_non_member_actor_cannot_press_buttons(
    services, project_with_owner_and_binding
) -> None:
    """UI controls carry references, not authority: a linked+verified
    Telegram user who is NOT a project member is denied at the
    membership gate (defense in depth — the press never even reaches
    the callback handler), and the token survives unused for the
    legitimate approver."""
    owner, project, _binding = project_with_owner_and_binding
    plan = _proposed_plan(services, owner, project)
    token = services.interfaces.create_callback_token(
        project_id=project.id,
        plan_id=plan.id,
        revision_number=1,
        action="approve",
        created_by=owner.id,
    )
    # A second Telegram user, linked+verified, but NOT a member.
    outsider = services.identity.create_user(display_name="Outsider")
    services.identity.link_external_identity(
        user_id=outsider.id,
        platform="telegram",
        external_id="666000",
        verified=True,
    )
    result = services.interfaces.process_inbound_event(
        _callback_event("cb_outsider_1", token.id.value, actor="666000")
    )
    assert result.processing_result == "denied"
    assert "not a member" in (result.processing_detail or "")
    plan = services.plans.get_plan(plan.id, project_id=project.id, actor_id=owner.id)
    assert plan.current_state == "proposed"
    stored = services.interfaces.get_callback_token(token.id)
    assert not stored.is_used  # token NOT burned by the denied press
    # The legitimate owner can still approve with the SAME token.
    ok = services.interfaces.process_inbound_event(
        _callback_event("cb_owner_after_1", token.id.value)
    )
    assert ok.processing_result == "processed"
    plan = services.plans.get_plan(plan.id, project_id=project.id, actor_id=owner.id)
    assert plan.current_state == "approved"


def test_viewer_role_cannot_press_approve_button(
    services, project_with_owner_and_binding
) -> None:
    """The DEEP authorization layer: a project member with the read-only
    'viewer' role passes the membership gate but is denied at the
    per-action permission check inside the callback handler
    (plan.approve), and the token survives unused."""
    owner, project, _binding = project_with_owner_and_binding
    plan = _proposed_plan(services, owner, project)
    token = services.interfaces.create_callback_token(
        project_id=project.id,
        plan_id=plan.id,
        revision_number=1,
        action="approve",
        created_by=owner.id,
    )
    viewer_user = services.identity.create_user(display_name="Viewer")
    services.identity.add_member(
        project_id=project.id,
        actor_id=owner.id,
        member_id=viewer_user.id,
        role="viewer",
    )
    services.identity.link_external_identity(
        user_id=viewer_user.id,
        platform="telegram",
        external_id="777000",
        verified=True,
    )
    result = services.interfaces.process_inbound_event(
        _callback_event("cb_viewer_1", token.id.value, actor="777000")
    )
    assert result.processing_result == "denied"
    assert "not authorized" in (result.processing_detail or "")
    plan = services.plans.get_plan(plan.id, project_id=project.id, actor_id=owner.id)
    assert plan.current_state == "proposed"
    stored = services.interfaces.get_callback_token(token.id)
    assert not stored.is_used


def test_expired_callback_token_is_rejected(
    services, project_with_owner_and_binding
) -> None:
    """An expired button must not act — and must say so honestly."""
    from zero.domain.ids import generate_callback_token_id
    from zero.domain.interfaces import CallbackToken, CallbackTokenId

    owner, project, _binding = project_with_owner_and_binding
    plan = _proposed_plan(services, owner, project)
    expired = CallbackToken(
        id=CallbackTokenId(generate_callback_token_id()),
        project_id=project.id,
        plan_id=plan.id,
        revision_number=1,
        action="approve",
        expires_at="2000-01-01T00:00:00.000000Z",
        used_at=None,
        created_by=owner.id,
        created_at="1999-12-31T23:59:59.000000Z",
    )
    services.interfaces._repo.insert_callback_token(expired)
    result = services.interfaces.process_inbound_event(
        _callback_event("cb_expired_1", expired.id.value)
    )
    assert result.processing_result == "error"
    assert "expired" in (result.processing_detail or "")
    plan = services.plans.get_plan(plan.id, project_id=project.id, actor_id=owner.id)
    assert plan.current_state == "proposed"


def test_approve_then_replay_is_idempotent(
    services, project_with_owner_and_binding
) -> None:
    """Double-press protection lives at the token: a replayed approve
    token reports 'already used' and never approves twice."""
    owner, project, _binding = project_with_owner_and_binding
    plan = _proposed_plan(services, owner, project)
    token = services.interfaces.create_callback_token(
        project_id=project.id,
        plan_id=plan.id,
        revision_number=1,
        action="approve",
        created_by=owner.id,
    )
    first = services.interfaces.process_inbound_event(
        _callback_event("cb_replay_1", token.id.value)
    )
    assert first.processing_result == "processed"
    second = services.interfaces.process_inbound_event(
        _callback_event("cb_replay_2", token.id.value)
    )
    assert second.processing_result == "processed"
    assert "already used" in (second.processing_detail or "")
    handoffs = services.plans.list_handoffs_for_project(project.id, actor_id=owner.id)
    assert len(handoffs) == 1
