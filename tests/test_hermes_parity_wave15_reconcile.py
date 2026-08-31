"""Hermes-parity wave 15 — fixes 17/19/21/22 (multi-team drill discoveries).

Each test pins a defect found during the 2026-09-01 live multi-team
decomposition drill. No simulation: routes are exercised against the
real app composition, the adapter tests drive the real retry engine.
"""
from __future__ import annotations

import inspect

import pytest
from httpx import ASGITransport, AsyncClient

from zero.adapters.messaging import (
    BaseMessagingAdapter,
    PermanentTransportError,
    RetryPolicy,
    TransportError,
    TransportRejectedError,
)
from zero.app.api import create_app
from zero.config import Settings


def _retry_policy() -> RetryPolicy:
    return RetryPolicy(attempts=3, backoff_seconds=0.0, timeout_seconds=1.0)


# ---------------------------------------------------------------------------
# Fix 17 — the real LLM planner must be reachable over the HTTP API surface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_planner_propose_route_exists_with_honest_guards() -> None:
    """The fix-17 surface must exist. An unknown event id resolves through
    real authz/lookup paths — an honest 4xx, never a fabricated revision
    and never a 500."""
    settings = Settings.load_for_test()
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        user = (await ac.post("/users", json={"display_name": "Owner"})).json()
        project = (
            await ac.post("/projects", json={"owner_id": user["id"], "name": "P17"})
        ).json()
        r = await ac.post(
            f"/projects/{project['id']}/planner/propose",
            json={"actor_id": user["id"], "event_id": "evt_does_not_exist"},
        )
        assert r.status_code in (400, 404, 409, 422), r.text


def test_planner_propose_endpoint_registered_in_openapi() -> None:
    settings = Settings.load_for_test()
    app = create_app(settings)
    path = app.openapi()["paths"].get("/projects/{project_id}/planner/propose")
    assert path is not None and "post" in path


# ---------------------------------------------------------------------------
# Fix 19 — long-generation internal LLM calls must stream
# ---------------------------------------------------------------------------


def test_planner_request_streams() -> None:
    from zero.app.planner_service import PlannerService

    src = inspect.getsource(PlannerService.propose_from_event)
    assert "stream=True" in src, "planner must stream (gateway edge kills silent bodies)"


def test_decomposition_requests_stream() -> None:
    from zero.app import task_decomposition

    src = inspect.getsource(task_decomposition)
    assert src.count("stream=True") >= 2, "both decomposition call sites must stream"


def test_compaction_summarizer_streams() -> None:
    from zero.app import services as services_mod

    src = inspect.getsource(services_mod)
    assert "stream=True" in src, "compaction summarizer must stream"


def test_send_request_dispatches_stream_to_stream_collector() -> None:
    """send_request must route stream=True through the adapter's
    send_request_stream + _collect_stream (with lease heartbeats), and
    only fall back to the plain adapter.send_request otherwise."""
    from zero.app.provider_service import ProviderService

    src = inspect.getsource(ProviderService.send_request)
    assert "if request.stream:" in src
    stream_branch = src.split("if request.stream:", 1)[1].split("else:", 1)[0]
    assert "send_request_stream" in stream_branch
    assert "_collect_stream" in stream_branch
    assert "heartbeat" in stream_branch


# ---------------------------------------------------------------------------
# Fix 21 — blocked-task reconciliation must be reachable over HTTP
# ---------------------------------------------------------------------------


def test_reconcile_request_model_is_module_level() -> None:
    """The body model must be importable (module level), not a closure
    local — this module uses ``from __future__ import annotations``, so a
    nested class degrades to an unresolvable annotation and FastAPI turns
    the body into a required QUERY parameter (observed live as 422
    ``query req required``)."""
    from zero.app.routers.execution import ReconcileTaskRequest

    assert ReconcileTaskRequest.model_fields["actor_id"].is_required()


def test_reconcile_endpoint_registered_in_openapi() -> None:
    settings = Settings.load_for_test()
    app = create_app(settings)
    path = app.openapi()["paths"].get(
        "/projects/{project_id}/executions/{execution_id}/tasks/{task_id}/reconcile"
    )
    assert path is not None and "post" in path


@pytest.mark.asyncio
async def test_reconcile_route_rejects_unknown_task_honestly() -> None:
    settings = Settings.load_for_test()
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        user = (await ac.post("/users", json={"display_name": "Owner"})).json()
        project = (
            await ac.post("/projects", json={"owner_id": user["id"], "name": "P21"})
        ).json()
        r = await ac.post(
            f"/projects/{project['id']}/executions/exec_none/tasks/task_none/reconcile",
            json={"actor_id": user["id"]},
        )
        assert r.status_code in (400, 404, 409, 422), r.text


# ---------------------------------------------------------------------------
# Fix 22 — HTTP rejections are retryable, network failures are ambiguous
# ---------------------------------------------------------------------------


def _make_adapter(transport, attempts: int = 3) -> BaseMessagingAdapter:
    class _A(BaseMessagingAdapter):
        platform = "telegram"

    return _A(
        event_handler=lambda event: None,
        transport=transport,
        retry_policy=_retry_policy(),
        sleeper=lambda _s: None,
    )


def _rejecting_transport():
    class _R:
        status_code = 429
        text = '{"ok":false,"error_code":429,"description":"Too Many Requests: retry after 17"}'

        def json(self):  # HttpResponse protocol
            return {
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests: retry after 17",
            }

    class _T:
        def request(self, method, url, headers=None, json=None, timeout=None):
            return _R()

    return _T()


def test_http_rejection_raises_typed_retryable_error() -> None:
    adapter = _make_adapter(_rejecting_transport())
    with pytest.raises(TransportRejectedError) as excinfo:
        adapter._request("POST", "http://x/sendMessage", payload={"chat_id": 1, "text": "hi"})
    assert "429" in str(excinfo.value)


def test_rejections_arent_permanent_and_failures_arent_rejected() -> None:
    """The class split must be real: PermanentTransportError is NOT a
    TransportRejectedError and vice versa — they route to different
    durable-delivery behaviors."""
    assert not issubclass(PermanentTransportError, TransportRejectedError)
    assert issubclass(TransportRejectedError, TransportError)


def test_rejected_send_maps_to_retryable_interface_error(monkeypatch) -> None:
    """A 429 (response received, message provably not landed) must surface
    as a plain InterfaceTransportError — the RETRYABLE branch — never as
    InterfaceTransportUnknownOutcome."""
    from types import SimpleNamespace

    import zero.app.interface_transport_service as its
    from zero.app.interface_transport_service import InterfaceTransportError

    def _raising_adapter(**kwargs):
        return SimpleNamespace(
            send_message=lambda **kw: (_ for _ in ()).throw(
                TransportRejectedError("provider returned retryable HTTP status 429")
            )
        )

    monkeypatch.setattr(its, "TelegramAdapter", _raising_adapter)
    monkeypatch.setattr(its, "DiscordAdapter", _raising_adapter)

    svc = its.InterfaceTransportService.__new__(its.InterfaceTransportService)
    svc._secret_service = SimpleNamespace(
        resolve_value=lambda **kw: "123:fake-token"
    )
    svc._transport = SimpleNamespace()
    binding = SimpleNamespace(
        platform="telegram", chat_id="1", topic_id=None, bot_token_ref="sec_x",
        is_enabled=True,
    )
    svc._interface_repo = SimpleNamespace(
        get_binding_by_id=lambda project_id, binding_id: binding
    )
    with pytest.raises(InterfaceTransportError) as excinfo:
        svc.send_message(
            project_id=SimpleNamespace(value="p"),
            binding_id=SimpleNamespace(value="b"),
            actor_id=SimpleNamespace(value="u"),
            text="hello",
        )
    assert "retryable" in str(excinfo.value)
    assert type(excinfo.value) is not its.InterfaceTransportUnknownOutcome


# ---------------------------------------------------------------------------
# Fix 23 (live re-run) — duplicate secret name is a 409 conflict, not a 500
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_secret_name_conflicts_without_500() -> None:
    """Storing a secret name that already exists in the project must
    return 409 (idempotency conflict) — never a 500 that falsely blames
    missing ZERO_SECRET_KEY material (observed live on the 2026-09-01
    re-run of the multi-team drill driver)."""
    from pydantic import SecretStr

    settings = Settings.load_for_test(
        secret_key=SecretStr("wave15-test-key-material"),
        auth_required=False,
    )
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        user = (await ac.post("/users", json={"display_name": "Owner"})).json()
        project = (
            await ac.post("/projects", json={"owner_id": user["id"], "name": "P23"})
        ).json()
        payload = {"name": "drill-token", "secret_type": "token", "value": "v-1"}
        first = await ac.post(
            f"/projects/{project['id']}/secrets", json=payload
        )
        assert first.status_code == 201, first.text
        again = await ac.post(
            f"/projects/{project['id']}/secrets", json=payload
        )
        assert again.status_code == 409, again.text
        # A different name still stores fine after the conflict.
        other = await ac.post(
            f"/projects/{project['id']}/secrets",
            json={**payload, "name": "drill-token-2"},
        )
        assert other.status_code == 201, other.text


def test_true_transport_failure_stays_unknown(monkeypatch) -> None:
    """A network-level failure (no HTTP response) must STAY ambiguous —
    the provider may or may not have queued the message; the durable
    delivery row must not silently re-send on that basis."""
    from types import SimpleNamespace

    import zero.app.interface_transport_service as its
    from zero.app.interface_transport_service import InterfaceTransportUnknownOutcome

    def _raising_adapter(**kwargs):
        return SimpleNamespace(
            send_message=lambda **kw: (_ for _ in ()).throw(
                TransportError("provider transport failed after retries — connection reset")
            )
        )

    monkeypatch.setattr(its, "TelegramAdapter", _raising_adapter)
    monkeypatch.setattr(its, "DiscordAdapter", _raising_adapter)

    svc = its.InterfaceTransportService.__new__(its.InterfaceTransportService)
    svc._secret_service = SimpleNamespace(resolve_value=lambda **kw: "123:fake-token")
    svc._transport = SimpleNamespace()
    binding = SimpleNamespace(
        platform="telegram", chat_id="1", topic_id=None, bot_token_ref="sec_x",
        is_enabled=True,
    )
    svc._interface_repo = SimpleNamespace(
        get_binding_by_id=lambda project_id, binding_id: binding
    )
    with pytest.raises(InterfaceTransportUnknownOutcome):
        svc.send_message(
            project_id=SimpleNamespace(value="p"),
            binding_id=SimpleNamespace(value="b"),
            actor_id=SimpleNamespace(value="u"),
            text="hello",
        )
