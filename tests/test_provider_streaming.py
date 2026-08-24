from __future__ import annotations

from threading import Event, Thread

import httpx
import pytest

from zero.app.provider_adapter import OpenAICompatibleProviderAdapter
from zero.app.services import build_services
from zero.config import Settings
from zero.domain.providers import (
    CanonicalMessage,
    CanonicalRequest,
    ProviderCancelledError,
    ProviderModel,
    ProviderModelId,
    ProviderUnknownOutcomeError,
)
from zero.persistence import migrations
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations


def _services():
    settings = Settings.load_for_test()
    database = Database(settings)
    apply_migrations(database)
    return build_services(settings, database)


def test_legacy_streaming_request_is_reconciled_by_lease_migration(tmp_path, monkeypatch) -> None:
    settings = Settings.load_for_test(database_url=f"sqlite:///{tmp_path / 'legacy.db'}")
    database = Database(settings)
    original_files = migrations._migration_files()
    # Simulate a database from before the legacy-provider-recovery
    # migration specifically (not merely "the last file", which can
    # change as new migrations are added).
    legacy_files = [path for path in original_files if not path.stem.startswith("0026_")]
    monkeypatch.setattr(migrations, "_migration_files", lambda: legacy_files)
    assert apply_migrations(database) == len(legacy_files)

    services = build_services(settings, database)
    owner = services.identity.create_user(display_name="Legacy owner")
    project = services.identity.create_project(owner_id=owner.id, name="Legacy project")
    conn = database.connect()
    conn.execute(
        "INSERT INTO provider_requests "
        "(id, project_id, provider, model_name, request_hash, state, attempt_count, "
        "claim_owner, claim_token, lease_expires_at, heartbeat_at) "
        "VALUES (?, ?, ?, ?, ?, 'streaming', 1, NULL, NULL, ?, ?)",
        (
            "legacy-provider-request",
            project.id.value,
            "fake",
            "fake-standard",
            "legacy-request-hash",
            "2099-01-01T00:00:00.000Z",
            "2026-01-01T00:00:00.000Z",
        ),
    )
    conn.commit()

    monkeypatch.setattr(migrations, "_migration_files", lambda: original_files)
    assert apply_migrations(database) == 1
    conn = database.connect()
    row = conn.execute(
        "SELECT state, error_class, claim_token, lease_expires_at "
        "FROM provider_requests WHERE id = ?",
        ("legacy-provider-request",),
    ).fetchone()
    assert tuple(row) == ("unknown", "unknown_outcome", None, None)


def test_fake_stream_is_aggregated_and_persisted() -> None:
    services = _services()
    owner = services.identity.create_user(display_name="Stream owner")
    project = services.identity.create_project(owner_id=owner.id, name="Stream project")
    request = CanonicalRequest(
        provider="fake",
        model_name="fake-standard",
        messages=(CanonicalMessage(role="user", content="stream me"),),
        stream=True,
    )
    provider_request, response = services.providers.send_request(
        project_id=project.id,
        actor_id=owner.id,
        request=request,
        idempotency_key="stream-test",
    )
    assert response.content == "Fake response to: stream me"
    assert provider_request.state == "completed"


def test_provider_finalization_failure_is_durable_unknown(monkeypatch) -> None:
    services = _services()
    owner = services.identity.create_user(display_name="Finalization owner")
    project = services.identity.create_project(owner_id=owner.id, name="Finalization project")

    def fail_store(*args, **kwargs):
        raise RuntimeError("synthetic finalization failure")

    monkeypatch.setattr(services.artifacts, "store_artifact", fail_store)
    with pytest.raises(RuntimeError, match="finalization failure"):
        services.providers.send_request(
            project_id=project.id,
            actor_id=owner.id,
            request=CanonicalRequest(
                provider="fake",
                model_name="fake-standard",
                messages=(CanonicalMessage(role="user", content="finalize"),),
            ),
            idempotency_key="finalization-failure",
        )

    stored = services.providers._repo.get_provider_request_by_idempotency_key(
        project.id, "finalization-failure"
    )
    assert stored is not None
    assert stored.state == "unknown"
    assert stored.error_class == "unknown_outcome"


def test_provider_cancellation_is_durable() -> None:
    services = _services()
    owner = services.identity.create_user(display_name="Cancel owner")
    project = services.identity.create_project(owner_id=owner.id, name="Cancel project")
    cancelled = Event()
    cancelled.set()
    request = CanonicalRequest(
        provider="fake",
        model_name="fake-standard",
        messages=(CanonicalMessage(role="user", content="cancel me"),),
        stream=True,
    )
    with pytest.raises(ProviderCancelledError):
        services.providers.send_request(
            project_id=project.id,
            actor_id=owner.id,
            request=request,
            idempotency_key="cancel-test",
            cancel_event=cancelled,
        )
    stored = services.providers._repo.get_provider_request_by_idempotency_key(
        project.id, "cancel-test"
    )
    assert stored is not None
    assert stored.state == "cancelled"


def test_provider_cancellation_reaches_inflight_adapter() -> None:
    services = _services()
    owner = services.identity.create_user(display_name="Inflight cancel owner")
    project = services.identity.create_project(owner_id=owner.id, name="Inflight cancel project")
    started = Event()
    cancelled = Event()
    failures: list[BaseException] = []
    adapter = services.providers._adapters["fake"]
    original_send = adapter.send_request

    def blocked_send(request, *, cancel_event=None):
        started.set()
        if cancel_event is None:
            raise AssertionError("provider cancellation event was not propagated")
        cancel_event.wait(2)
        raise ProviderCancelledError("provider cancelled while in flight")

    adapter.send_request = blocked_send

    def invoke() -> None:
        try:
            services.providers.send_request(
                project_id=project.id,
                actor_id=owner.id,
                request=CanonicalRequest(
                    provider="fake",
                    model_name="fake-standard",
                    messages=(CanonicalMessage(role="user", content="blocked"),),
                ),
                idempotency_key="inflight-cancel",
                cancel_event=cancelled,
            )
        except ProviderCancelledError as exc:  # thread boundary for the expected provider error
            failures.append(exc)

    thread = Thread(target=invoke)
    thread.start()
    assert started.wait(2)
    cancelled.set()
    thread.join(3)
    adapter.send_request = original_send

    assert not thread.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], ProviderCancelledError)
    stored = services.providers._repo.get_provider_request_by_idempotency_key(
        project.id, "inflight-cancel"
    )
    assert stored is not None
    assert stored.state == "cancelled"


def test_provider_model_capabilities_gate_stream_tools_and_output_limit() -> None:
    services = _services()
    owner = services.identity.create_user(display_name="Capability owner")
    project = services.identity.create_project(owner_id=owner.id, name="Capability project")
    services.providers._repo.insert_provider_model(
        ProviderModel(
            id=ProviderModelId("pm_limited"),
            provider="fake",
            model_name="limited-model",
            context_window=128,
            max_output_tokens=8,
            capabilities=(),
        )
    )

    with pytest.raises(ValueError, match="streaming"):
        services.providers.send_request(
            project_id=project.id,
            actor_id=owner.id,
            request=CanonicalRequest(
                provider="fake",
                model_name="limited-model",
                messages=(CanonicalMessage(role="user", content="stream"),),
                stream=True,
            ),
        )
    with pytest.raises(ValueError, match="native tools"):
        services.providers.send_request(
            project_id=project.id,
            actor_id=owner.id,
            request=CanonicalRequest(
                provider="fake",
                model_name="limited-model",
                messages=(CanonicalMessage(role="user", content="tools"),),
                tools=("read_file",),
            ),
        )
    with pytest.raises(ValueError, match="max_output_tokens"):
        services.providers.send_request(
            project_id=project.id,
            actor_id=owner.id,
            request=CanonicalRequest(
                provider="fake",
                model_name="limited-model",
                messages=(CanonicalMessage(role="user", content="long"),),
                max_tokens=9,
            ),
        )


def test_openai_stream_preserves_continuation_ids_finish_reason_and_message_id() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        body = 'data: {"id":"chatcmpl-test","choices":[{"delta":{"tool_calls":[{"id":"call-1","index":0,"function":{"name":"echo","arguments":"{\\"message\\":"}}]},"finish_reason":null}]}\ndata: {"id":"chatcmpl-test","choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"ok\\"}"}}]},"finish_reason":null}]}\ndata: {"id":"chatcmpl-test","choices":[{"delta":{},"finish_reason":"tool_calls"}]}\ndata: [DONE]\n'
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body.encode(),
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = OpenAICompatibleProviderAdapter(
        api_key="synthetic-token",
        base_url="https://provider.test/v1",
        client=client,
    )
    events = list(
        adapter.send_request_stream(
            CanonicalRequest(
                provider="openai-compatible",
                model_name="synthetic-model",
                messages=(CanonicalMessage(role="user", content="call echo"),),
                stream=True,
            )
        )
    )

    calls = [event.tool_call for event in events if event.tool_call is not None]
    assert [call.tool_call_id for call in calls] == ["call-1", "call-1"]
    assert "".join(call.arguments for call in calls) == '{"message":"ok"}'
    assert events[0].provider_message_id == "chatcmpl-test"
    assert any(event.finish_reason == "tool_calls" for event in events)


def test_openai_stream_without_terminal_marker_is_unknown_outcome() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        body = (
            'data: {"id":"chatcmpl-truncated","choices":[{"delta":'
            '{"content":"partial"},"finish_reason":null}]}\n'
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body.encode(),
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = OpenAICompatibleProviderAdapter(
        api_key="synthetic-token",
        base_url="https://provider.test/v1",
        client=client,
    )

    with pytest.raises(ProviderUnknownOutcomeError, match="terminal message marker"):
        list(
            adapter.send_request_stream(
                CanonicalRequest(
                    provider="openai-compatible",
                    model_name="synthetic-model",
                    messages=(CanonicalMessage(role="user", content="truncate"),),
                    stream=True,
                )
            )
        )
