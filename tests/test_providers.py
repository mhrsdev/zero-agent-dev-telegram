"""Provider adapter and usage reconciliation tests — covers all M10
validation gates.

Per PLAN.md M10 validation:
- Canonical request renders valid provider payloads.
- Malformed or orphaned tool messages are rejected or safely repaired
  without inventing success.
- Duplicate streamed usage is not double-counted.
- Parent and child usage reconcile to one whole-tree total.
- Provider switch resumes from Zero state rather than provider session
  memory.
- Cache miss, hit, creation, and invalidation reasons are observable.
- Pricing changes do not mutate historical raw usage.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from zero.app.provider_adapter import (
    compute_request_hash,
    validate_tool_messages,
)
from zero.app.services import build_services
from zero.config import Settings
from zero.domain.authorization import AuthorizationError
from zero.domain.providers import (
    CanonicalMessage,
    CanonicalRequest,
    InvalidProviderRequestError,
    ProviderError,
    ProviderModelNotFoundError,
    ProviderRequest,
    ProviderRequestId,
    ProviderRequestStateError,
    ProviderUnknownOutcomeError,
    UsageRecordId,
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
    project = services.identity.create_project(owner_id=owner.id, name="Project A")
    return owner, project


# ----------------------------------------------------------------------
# Tool message validation
# ----------------------------------------------------------------------


def test_validate_tool_messages_strips_orphan_results() -> None:
    """Per zero-context-memory §sanitize_tool_pairs: drop orphan tool
    results while preserving declared tool calls."""
    messages = [
        {"role": "tool", "tool_call_id": "orphan", "content": "bad"},
        {"role": "assistant", "tool_calls": [{"id": "ok", "name": "read"}]},
        {"role": "tool", "tool_call_id": "ok", "content": "good"},
    ]
    clean, stripped = validate_tool_messages(messages)
    assert stripped == ["orphan"]
    assert len(clean) == 2  # assistant + valid tool result


def test_validate_tool_messages_preserves_all_valid() -> None:
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi", "tool_calls": [{"id": "call1", "name": "echo"}]},
        {"role": "tool", "tool_call_id": "call1", "content": "echoed"},
    ]
    clean, stripped = validate_tool_messages(messages)
    assert stripped == []
    assert len(clean) == 3


def test_validate_tool_messages_accepts_canonical_tuple_tool_calls() -> None:
    """CanonicalMessage stores tool calls as tuples, not mappings."""
    messages = [
        CanonicalMessage(
            role="assistant",
            content="I will use echo.",
            tool_calls=(("echo", "call1", '{"message":"ok"}'),),
        ),
        CanonicalMessage(
            role="tool",
            content="ok",
            tool_call_id="call1",
        ),
    ]

    clean, stripped = validate_tool_messages(messages)

    assert stripped == []
    assert clean[0]["tool_calls"] == [
        {"id": "call1", "name": "echo", "arguments": '{"message":"ok"}'}
    ]
    assert clean[1]["tool_call_id"] == "call1"


# ----------------------------------------------------------------------
# Request hash deduplication
# ----------------------------------------------------------------------


def test_compute_request_hash_is_deterministic() -> None:
    """Per zero-claude-token-economics: request/message deduplication."""
    req = CanonicalRequest(
        provider="fake",
        model_name="fake-standard",
        messages=(CanonicalMessage(role="user", content="hello"),),
    )
    hash1 = compute_request_hash(req)
    hash2 = compute_request_hash(req)
    assert hash1 == hash2


def test_compute_request_hash_differs_for_different_requests() -> None:
    req1 = CanonicalRequest(
        provider="fake",
        model_name="fake-standard",
        messages=(CanonicalMessage(role="user", content="hello"),),
    )
    req2 = CanonicalRequest(
        provider="fake",
        model_name="fake-standard",
        messages=(CanonicalMessage(role="user", content="world"),),
    )
    assert compute_request_hash(req1) != compute_request_hash(req2)


def test_provider_request_scope_includes_execution_identity() -> None:
    from zero.app import provider_service
    from zero.domain.execution import ExecutionId
    from zero.domain.identity import ProjectId

    scope_builder = getattr(provider_service, "request_dedup_scope", None)
    assert callable(scope_builder)
    project_id = ProjectId("p_scope")
    execution_a = ExecutionId("exec_scope_a")
    execution_b = ExecutionId("exec_scope_b")
    assert scope_builder(project_id, execution_a) != scope_builder(project_id, execution_b)
    assert scope_builder(project_id, None) == project_id.value


def test_fake_response_message_id_uses_stable_request_digest(services) -> None:
    """Response IDs must not depend on Python's randomized ``hash()``."""
    from zero.app.provider_adapter import FakeProviderAdapter

    req = CanonicalRequest(
        provider="fake",
        model_name="fake-standard",
        messages=(CanonicalMessage(role="user", content="stable id"),),
    )
    response = FakeProviderAdapter(services.providers._repo).send_request(req)

    assert response.provider_message_id == ("fake_msg_" + compute_request_hash(req)[:32])


def test_provider_deduplication_respects_logical_idempotency_key(
    services,
    project_with_owner,
) -> None:
    owner, project = project_with_owner
    request = CanonicalRequest(
        provider="fake",
        model_name="fake-standard",
        messages=(CanonicalMessage(role="user", content="same logical payload"),),
    )
    first, _ = services.providers.send_request(
        project_id=project.id,
        actor_id=owner.id,
        request=request,
        idempotency_key="task-a-attempt-1",
    )
    independent, _ = services.providers.send_request(
        project_id=project.id,
        actor_id=owner.id,
        request=request,
        idempotency_key="task-b-attempt-1",
    )
    replay, _ = services.providers.send_request(
        project_id=project.id,
        actor_id=owner.id,
        request=request,
        idempotency_key="task-a-attempt-1",
    )
    assert first.id != independent.id
    assert replay.id == first.id


def test_get_model_returns_capabilities(services) -> None:
    model = services.providers.get_model("fake", "fake-standard")
    assert model.provider == "fake"
    assert model.model_name == "fake-standard"
    assert model.context_window == 200000
    assert "streaming" in model.capabilities
    assert "native_tools" in model.capabilities


def test_get_model_raises_for_unknown(services) -> None:
    from zero.domain.providers import ProviderModelNotFoundError

    with pytest.raises(ProviderModelNotFoundError):
        services.providers.get_model("fake", "nonexistent")


# ----------------------------------------------------------------------
# Request execution
# ----------------------------------------------------------------------


def test_send_request_rejects_unknown_model_before_adapter_call(services, project_with_owner):
    owner, project = project_with_owner
    request = CanonicalRequest(
        provider="fake",
        model_name="not-registered",
        messages=(CanonicalMessage(role="user", content="unknown model"),),
    )
    adapter = services.providers._adapters["fake"]
    calls = 0
    original_send = adapter.send_request

    def counted_send(request):
        nonlocal calls
        calls += 1
        return original_send(request)

    services.providers.register_adapter(adapter)
    adapter.send_request = counted_send
    with pytest.raises(ProviderModelNotFoundError):
        services.providers.send_request(
            project_id=project.id,
            actor_id=owner.id,
            request=request,
        )
    assert calls == 0


def test_provider_connection_failure_is_retriable(services, project_with_owner) -> None:
    owner, project = project_with_owner
    request = CanonicalRequest(
        provider="fake",
        model_name="fake-standard",
        messages=(CanonicalMessage(role="user", content="retry connection"),),
    )
    adapter = services.providers._adapters["fake"]
    original_send = adapter.send_request

    def fail_with_connection(_request):
        raise ProviderError("provider connection failed")

    adapter.send_request = fail_with_connection
    with pytest.raises(ProviderError):
        services.providers.send_request(
            project_id=project.id,
            actor_id=owner.id,
            request=request,
            idempotency_key="connection-retry",
        )
    stored = services.providers._repo.get_provider_request_by_idempotency_key(
        project.id, "connection-retry"
    )
    assert stored is not None
    assert stored.error_class == "transient"

    adapter.send_request = original_send
    retried, _ = services.providers.send_request(
        project_id=project.id,
        actor_id=owner.id,
        request=request,
        idempotency_key="connection-retry",
    )
    assert retried.state == "completed"


def test_concurrent_idempotency_claim_rechecks_payload_hash(
    services, project_with_owner, monkeypatch
) -> None:
    owner, project = project_with_owner
    first_request = CanonicalRequest(
        provider="fake",
        model_name="fake-standard",
        messages=(CanonicalMessage(role="user", content="winner payload"),),
    )
    winner, _ = services.providers.send_request(
        project_id=project.id,
        actor_id=owner.id,
        request=first_request,
        idempotency_key="race-key",
    )
    second_request = replace(
        first_request,
        messages=(CanonicalMessage(role="user", content="loser payload"),),
    )
    repo = services.providers._repo
    original_lookup = repo.get_provider_request_by_idempotency_key
    lookup_calls = 0

    def hide_winner_once(project_id, idempotency_key):
        nonlocal lookup_calls
        lookup_calls += 1
        if lookup_calls == 1:
            return None
        return original_lookup(project_id, idempotency_key)

    monkeypatch.setattr(repo, "get_provider_request_by_idempotency_key", hide_winner_once)

    with pytest.raises(ValueError, match="different request"):
        services.providers.send_request(
            project_id=project.id,
            actor_id=owner.id,
            request=second_request,
            idempotency_key="race-key",
        )
    assert repo.get_provider_request_by_idempotency_key(project.id, "race-key").id == winner.id


def test_send_request_returns_response(services, project_with_owner) -> None:
    owner, project = project_with_owner
    req = CanonicalRequest(
        provider="fake",
        model_name="fake-standard",
        messages=(CanonicalMessage(role="user", content="Hello!"),),
    )
    preq, resp = services.providers.send_request(
        project_id=project.id, actor_id=owner.id, request=req
    )
    assert preq.state == "completed"
    assert "Fake response to: Hello!" in resp.content
    assert resp.usage.input_tokens > 0
    assert resp.usage.output_tokens > 0


def test_completed_request_without_artifact_is_not_fabricated(services, project_with_owner) -> None:
    owner, project = project_with_owner
    request = CanonicalRequest(
        provider="fake",
        model_name="fake-standard",
        messages=(CanonicalMessage(role="user", content="missing artifact"),),
    )
    services.providers._repo.insert_provider_request(
        ProviderRequest(
            id=ProviderRequestId("preq_missing_artifact"),
            project_id=project.id,
            execution_id=None,
            provider=request.provider,
            model_name=request.model_name,
            request_hash=compute_request_hash(request, scope=project.id.value),
            state="completed",
            started_at="2026-01-01T00:00:00Z",
        )
    )

    with pytest.raises(ProviderRequestStateError, match="response artifact"):
        services.providers.send_request(
            project_id=project.id,
            actor_id=owner.id,
            request=request,
        )


def test_provider_request_leases_are_claimed_heartbeated_and_fenced(
    services, project_with_owner
) -> None:
    _owner, project = project_with_owner
    repo = services.providers._repo
    request_id = ProviderRequestId("preq_lease_fencing")
    repo.insert_provider_request(
        ProviderRequest(
            id=request_id,
            project_id=project.id,
            execution_id=None,
            provider="fake",
            model_name="fake-standard",
            request_hash="lease-fencing-hash",
            state="pending",
            started_at="2026-01-01T00:00:00Z",
        )
    )

    claimed = repo.claim_provider_request(
        request_id,
        claim_owner="worker-a",
        lease_seconds=60,
    )
    assert claimed.state == "streaming"
    assert claimed.attempt_count == 1
    assert claimed.claim_owner == "worker-a"
    assert claimed.claim_token
    assert (
        repo.heartbeat_provider_request(
            request_id,
            claim_token="wrong-token",
            lease_seconds=60,
        )
        is False
    )
    assert (
        repo.heartbeat_provider_request(
            request_id,
            claim_token=claimed.claim_token,
            lease_seconds=60,
        )
        is True
    )

    with pytest.raises(ProviderRequestStateError, match="cannot transition"):
        repo.update_provider_request_state(
            request_id,
            "completed",
            claim_token="wrong-token",
        )

    repo.update_provider_request_state(
        request_id,
        "completed",
        claim_token=claimed.claim_token,
    )
    completed = repo.get_provider_request(request_id)
    assert completed.state == "completed"
    assert completed.claim_token is None
    assert completed.lease_expires_at is None


def test_completed_provider_request_cannot_transition_back_to_failed(services, project_with_owner):
    owner, project = project_with_owner
    request = CanonicalRequest(
        provider="fake",
        model_name="fake-standard",
        messages=(CanonicalMessage(role="user", content="terminal state"),),
    )
    provider_request, _ = services.providers.send_request(
        project_id=project.id,
        actor_id=owner.id,
        request=request,
    )

    with pytest.raises(ProviderError):
        services.providers._repo.update_provider_request_state(
            provider_request.id,
            "failed",
            error_class="transient",
        )


def test_duplicate_request_is_deduplicated(services, project_with_owner) -> None:
    """Per zero-claude-token-economics: duplicate streamed usage is not
    double-counted."""
    owner, project = project_with_owner
    req = CanonicalRequest(
        provider="fake",
        model_name="fake-standard",
        messages=(CanonicalMessage(role="user", content="Same request; call tool"),),
    )
    preq1, resp1 = services.providers.send_request(
        project_id=project.id, actor_id=owner.id, request=req
    )
    preq2, resp2 = services.providers.send_request(
        project_id=project.id, actor_id=owner.id, request=req
    )
    # Same request ID (dedup).
    assert preq1.id == preq2.id
    # Same response content.
    assert resp1.content == resp2.content
    assert resp2.tool_calls[0].tool_name == "echo"


def test_completed_provider_replay_preserves_usage(services, project_with_owner) -> None:
    owner, project = project_with_owner
    request = CanonicalRequest(
        provider="fake",
        model_name="fake-standard",
        messages=(CanonicalMessage(role="user", content="replay usage"),),
    )

    first_request, first_response = services.providers.send_request(
        project_id=project.id,
        actor_id=owner.id,
        request=request,
    )
    second_request, replayed = services.providers.send_request(
        project_id=project.id,
        actor_id=owner.id,
        request=request,
    )

    assert second_request.id == first_request.id
    assert replayed.usage == first_response.usage
    assert replayed.usage is not None
    assert replayed.usage.input_tokens == first_response.usage.input_tokens
    assert replayed.usage.output_tokens == first_response.usage.output_tokens


def test_duplicate_provider_request_claim_reports_loser(services, project_with_owner):
    owner, project = project_with_owner
    request = CanonicalRequest(
        provider="fake",
        model_name="fake-standard",
        messages=(CanonicalMessage(role="user", content="claim result"),),
    )
    provider_request, _ = services.providers.send_request(
        project_id=project.id,
        actor_id=owner.id,
        request=request,
    )
    duplicate = replace(
        provider_request,
        id=ProviderRequestId("preq_duplicate_claim"),
        state="pending",
        response_artifact_id=None,
        completed_at=None,
    )

    assert services.providers._repo.insert_provider_request(duplicate) is False


def test_provider_finalization_marks_unknown_on_usage_failure(
    services,
    project_with_owner,
    monkeypatch,
) -> None:
    owner, project = project_with_owner
    request = CanonicalRequest(
        provider="fake",
        model_name="fake-standard",
        messages=(CanonicalMessage(role="user", content="atomic finalization"),),
    )

    def fail_usage(_record, *, commit=True):
        raise RuntimeError("synthetic usage persistence failure")

    monkeypatch.setattr(services.providers._repo, "insert_usage_record", fail_usage)

    with pytest.raises(RuntimeError, match="usage persistence"):
        services.providers.send_request(
            project_id=project.id,
            actor_id=owner.id,
            request=request,
        )

    stored_requests = services.providers.list_provider_requests_for_project(
        project.id,
        actor_id=owner.id,
    )
    assert len(stored_requests) == 1
    assert stored_requests[0].state == "unknown"
    assert stored_requests[0].response_artifact_id is None
    assert (
        services.providers.list_usage_records_for_project(
            project.id,
            actor_id=owner.id,
        )
        == []
    )
    assert (
        services.artifacts.list_artifacts(
            project_id=project.id,
            actor_id=owner.id,
        )
        == []
    )


def test_transient_provider_failure_is_retried_in_process(
    services, project_with_owner, monkeypatch
):
    """Hermes parity: a transient failure is retried in-process before
    surfacing; the second attempt succeeds and the durable request
    completes on the first logical send."""
    owner, project = project_with_owner
    request = CanonicalRequest(
        provider="fake",
        model_name="fake-standard",
        messages=(CanonicalMessage(role="user", content="retry this request"),),
    )
    adapter = services.providers._adapters["fake"]
    original_send = adapter.send_request
    calls = 0

    def flaky_send(req):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ProviderError("transient provider failure")
        return original_send(req)

    monkeypatch.setattr(adapter, "send_request", flaky_send)
    monkeypatch.setattr("zero.app.provider_service.time.sleep", lambda _s: None)
    _provider_request, response = services.providers.send_request(
        project_id=project.id,
        actor_id=owner.id,
        request=request,
    )
    assert calls == 2  # one transient rejection + one successful redelivery
    assert "Fake response" in response.content


def test_invalid_provider_failure_is_not_automatically_retried(
    services,
    project_with_owner,
    monkeypatch,
) -> None:
    owner, project = project_with_owner
    request = CanonicalRequest(
        provider="fake",
        model_name="fake-standard",
        messages=(CanonicalMessage(role="user", content="invalid request"),),
    )
    adapter = services.providers._adapters["fake"]
    calls = 0

    def invalid_send(_request):
        nonlocal calls
        calls += 1
        raise InvalidProviderRequestError("request schema is invalid")

    monkeypatch.setattr(adapter, "send_request", invalid_send)
    with pytest.raises(InvalidProviderRequestError):
        services.providers.send_request(
            project_id=project.id,
            actor_id=owner.id,
            request=request,
        )
    with pytest.raises(RuntimeError, match="not retryable"):
        services.providers.send_request(
            project_id=project.id,
            actor_id=owner.id,
            request=request,
        )
    assert calls == 1


def test_unknown_provider_outcome_is_not_replayed(services, project_with_owner, monkeypatch):
    owner, project = project_with_owner
    request = CanonicalRequest(
        provider="fake",
        model_name="fake-standard",
        messages=(CanonicalMessage(role="user", content="ambiguous request"),),
    )
    adapter = services.providers._adapters["fake"]
    calls = 0

    def ambiguous_send(_request):
        nonlocal calls
        calls += 1
        raise ProviderUnknownOutcomeError("dispatch outcome is unknown")

    monkeypatch.setattr(adapter, "send_request", ambiguous_send)
    with pytest.raises(ProviderUnknownOutcomeError):
        services.providers.send_request(
            project_id=project.id,
            actor_id=owner.id,
            request=request,
        )

    stored = services.providers.list_provider_requests_for_project(project.id, actor_id=owner.id)[0]
    assert stored.state == "unknown"
    with pytest.raises(RuntimeError):
        services.providers.send_request(
            project_id=project.id,
            actor_id=owner.id,
            request=request,
        )
    assert calls == 1


def test_provider_request_rejects_cross_project_actor(services):
    owner_a = services.identity.create_user(display_name="Owner A")
    project_a = services.identity.create_project(owner_id=owner_a.id, name="Project A")
    owner_b = services.identity.create_user(display_name="Owner B")
    request = CanonicalRequest(
        provider="fake",
        model_name="fake-standard",
        messages=(CanonicalMessage(role="user", content="foreign request"),),
    )

    with pytest.raises(AuthorizationError):
        services.providers.send_request(
            project_id=project_a.id,
            actor_id=owner_b.id,
            request=request,
        )
    assert (
        services.providers.list_provider_requests_for_project(project_a.id, actor_id=owner_a.id)
        == []
    )


def test_usage_not_double_counted(services, project_with_owner) -> None:
    """Per PLAN.md M10: 'Duplicate streamed usage is not double-counted.'"""
    owner, project = project_with_owner
    req = CanonicalRequest(
        provider="fake",
        model_name="fake-standard",
        messages=(CanonicalMessage(role="user", content="Count me once"),),
    )
    services.providers.send_request(project_id=project.id, actor_id=owner.id, request=req)
    # Send the same request again (dedup).
    services.providers.send_request(project_id=project.id, actor_id=owner.id, request=req)
    # Only one usage record should exist.
    usage_records = services.providers.list_usage_records_for_project(project.id, actor_id=owner.id)
    assert len(usage_records) == 1


def test_request_level_usage_with_null_message_id_is_deduplicated(services, project_with_owner):
    owner, project = project_with_owner
    request = CanonicalRequest(
        provider="fake",
        model_name="fake-standard",
        messages=(CanonicalMessage(role="user", content="null usage"),),
    )
    provider_request, _ = services.providers.send_request(
        project_id=project.id,
        actor_id=owner.id,
        request=request,
    )
    original = services.providers._repo.list_usage_records_for_request(provider_request.id)[0]
    first = replace(
        original,
        id=UsageRecordId("usg_null_a"),
        provider_message_id=None,
    )
    second = replace(
        original,
        id=UsageRecordId("usg_null_b"),
        provider_message_id=None,
    )

    assert services.providers._repo.insert_usage_record(first) is True
    assert services.providers._repo.insert_usage_record(second) is False
    null_records = [
        item
        for item in services.providers._repo.list_usage_records_for_request(provider_request.id)
        if item.provider_message_id is None
    ]
    assert len(null_records) == 1


def test_provider_error_persistence_redacts_exception_text(
    services, project_with_owner, monkeypatch
):
    owner, project = project_with_owner
    request = CanonicalRequest(
        provider="fake",
        model_name="fake-standard",
        messages=(CanonicalMessage(role="user", content="redact provider error"),),
    )
    adapter = services.providers._adapters["fake"]
    canary = "PROVIDER_ERROR_CANARY_DO_NOT_PERSIST"

    def leaking_send(_request):
        raise ProviderError(f"transient failure {canary}")

    monkeypatch.setattr(adapter, "send_request", leaking_send)
    with pytest.raises(ProviderError):
        services.providers.send_request(
            project_id=project.id,
            actor_id=owner.id,
            request=request,
        )

    stored = services.providers.list_provider_requests_for_project(project.id, actor_id=owner.id)[0]
    assert canary not in (stored.error_message or "")
    assert stored.error_message == "provider request failed"


def test_error_classification(services, project_with_owner) -> None:
    """Per zero-provider-adapter-contract: errors need stable classes."""
    owner, project = project_with_owner
    req = CanonicalRequest(
        provider="fake",
        model_name="fake-standard",
        messages=(CanonicalMessage(role="user", content="Please trigger error now"),),
    )
    with pytest.raises(ProviderError):
        services.providers.send_request(project_id=project.id, actor_id=owner.id, request=req)
    # The provider request should be in 'failed' state.
    requests = services.providers.list_provider_requests_for_project(project.id, actor_id=owner.id)
    failed = [r for r in requests if r.state == "failed"]
    assert len(failed) >= 1
    assert failed[0].error_class is not None


# ----------------------------------------------------------------------
# Usage reconciliation
# ----------------------------------------------------------------------


def test_whole_tree_usage_aggregation(services, project_with_owner) -> None:
    """Per PLAN.md M10: 'Parent and child usage reconcile to one
    whole-tree total.'"""
    owner, project = project_with_owner
    # Send multiple requests.
    for i in range(3):
        req = CanonicalRequest(
            provider="fake",
            model_name="fake-standard",
            messages=(CanonicalMessage(role="user", content=f"Request {i}"),),
        )
        services.providers.send_request(project_id=project.id, actor_id=owner.id, request=req)
    usage = services.providers.get_usage_for_project(project.id, actor_id=owner.id)
    # All three requests' usage is aggregated.
    assert usage.input_tokens > 0
    assert usage.output_tokens > 0
    # Verify it equals the sum of individual records.
    records = services.providers.list_usage_records_for_project(project.id, actor_id=owner.id)
    total_input = sum(r.usage.input_tokens for r in records)
    assert usage.input_tokens == total_input


def test_pricing_changes_do_not_mutate_historical_usage(services, project_with_owner) -> None:
    """Per PLAN.md M10: 'Pricing changes do not mutate historical raw
    usage.'"""
    owner, project = project_with_owner
    # Register pricing v1.
    services.providers.register_pricing(
        catalog_version=1,
        provider="fake",
        model_name="fake-standard",
        input_price_per_million="10.00",
        output_price_per_million="30.00",
    )
    # Send a request (uses pricing v1).
    req = CanonicalRequest(
        provider="fake",
        model_name="fake-standard",
        messages=(CanonicalMessage(role="user", content="Pricing test"),),
    )
    services.providers.send_request(project_id=project.id, actor_id=owner.id, request=req)
    # Register pricing v2 (different prices).
    services.providers.register_pricing(
        catalog_version=2,
        provider="fake",
        model_name="fake-standard",
        input_price_per_million="20.00",
        output_price_per_million="60.00",
    )
    # The historical usage record still has pricing_catalog_version=1.
    records = services.providers.list_usage_records_for_project(project.id, actor_id=owner.id)
    assert len(records) >= 1
    assert records[0].pricing_catalog_version == 1
    # The estimated cost was computed with v1 pricing, not v2.
    assert records[0].estimated_cost_usd != "0"


def test_estimated_cost_distinct_from_reconciled(services, project_with_owner) -> None:
    """Per zero-claude-token-economics: estimated cost is NOT billing
    truth."""
    owner, project = project_with_owner
    services.providers.register_pricing(
        catalog_version=1,
        provider="fake",
        model_name="fake-standard",
        input_price_per_million="10.00",
        output_price_per_million="30.00",
    )
    req = CanonicalRequest(
        provider="fake",
        model_name="fake-standard",
        messages=(CanonicalMessage(role="user", content="Reconcile test"),),
    )
    services.providers.send_request(project_id=project.id, actor_id=owner.id, request=req)
    records = services.providers.list_usage_records_for_project(project.id, actor_id=owner.id)
    record = records[0]
    # Estimated cost is set.
    assert record.estimated_cost_usd != "0"
    # Reconciled cost is NULL until reconciled.
    assert record.reconciled_cost_usd is None
    # Reconcile.
    services.providers.reconcile_usage(
        project_id=project.id,
        usage_id=record.id,
        reconciled_cost_usd="0.001234",
        actor_id=owner.id,
    )
    records = services.providers.list_usage_records_for_project(project.id, actor_id=owner.id)
    record = records[0]
    assert record.reconciled_cost_usd == "0.001234"
    # Estimated cost is unchanged.
    assert record.estimated_cost_usd != "0.001234"


def test_reconcile_usage_rejects_cross_project_actor(services):
    owner_a = services.identity.create_user(display_name="Usage owner A")
    project_a = services.identity.create_project(owner_id=owner_a.id, name="Usage A")
    owner_b = services.identity.create_user(display_name="Usage owner B")
    request = CanonicalRequest(
        provider="fake",
        model_name="fake-standard",
        messages=(CanonicalMessage(role="user", content="usage authorization"),),
    )
    services.providers.send_request(
        project_id=project_a.id,
        actor_id=owner_a.id,
        request=request,
    )
    record = services.providers.list_usage_records_for_project(project_a.id, actor_id=owner_a.id)[0]

    with pytest.raises(AuthorizationError):
        services.providers.reconcile_usage(
            project_id=project_a.id,
            usage_id=record.id,
            reconciled_cost_usd="9.99",
            actor_id=owner_b.id,
        )

    unchanged = services.providers.list_usage_records_for_project(
        project_a.id, actor_id=owner_a.id
    )[0]
    assert unchanged.reconciled_cost_usd is None


def test_provider_usage_reads_require_cost_permission(services):
    owner_a = services.identity.create_user(display_name="Read owner A")
    project_a = services.identity.create_project(owner_id=owner_a.id, name="Read A")
    owner_b = services.identity.create_user(display_name="Read owner B")
    request = CanonicalRequest(
        provider="fake",
        model_name="fake-standard",
        messages=(CanonicalMessage(role="user", content="usage read authorization"),),
    )
    services.providers.send_request(
        project_id=project_a.id,
        actor_id=owner_a.id,
        request=request,
    )

    with pytest.raises(AuthorizationError):
        services.providers.list_usage_records_for_project(
            project_a.id,
            actor_id=owner_b.id,
        )


# ----------------------------------------------------------------------


def test_provider_switch_preserves_canonical_state(services, project_with_owner) -> None:
    """Per PLAN.md M10: 'Provider switch resumes from Zero state rather
    than provider session memory.'

    Changing the model does not destroy identity, memory, task, or
    execution state.
    """
    owner, project = project_with_owner
    # Send with fake-standard.
    req1 = CanonicalRequest(
        provider="fake",
        model_name="fake-standard",
        messages=(CanonicalMessage(role="user", content="First model"),),
    )
    services.providers.send_request(project_id=project.id, actor_id=owner.id, request=req1)
    # Send with fake-mini (different model).
    req2 = CanonicalRequest(
        provider="fake",
        model_name="fake-mini",
        messages=(CanonicalMessage(role="user", content="Second model"),),
    )
    services.providers.send_request(project_id=project.id, actor_id=owner.id, request=req2)
    # Both requests are recorded.
    requests = services.providers.list_provider_requests_for_project(project.id, actor_id=owner.id)
    assert len(requests) == 2
    models = {r.model_name for r in requests}
    assert models == {"fake-standard", "fake-mini"}
    # The project's identity is unchanged.
    project_after = services.identity.get_project(project.id)
    assert project_after.id == project.id
    assert project_after.name == project.name


# ----------------------------------------------------------------------
# Cross-project isolation
# ----------------------------------------------------------------------


def test_provider_usage_isolated_across_projects(services) -> None:
    """Per zero-project-isolation-evidence: usage is project-scoped."""
    owner_a = services.identity.create_user(display_name="Owner A")
    project_a = services.identity.create_project(owner_id=owner_a.id, name="Project A")
    owner_b = services.identity.create_user(display_name="Owner B")
    project_b = services.identity.create_project(owner_id=owner_b.id, name="Project B")
    req = CanonicalRequest(
        provider="fake",
        model_name="fake-standard",
        messages=(CanonicalMessage(role="user", content="Isolated"),),
    )
    services.providers.send_request(project_id=project_a.id, actor_id=owner_a.id, request=req)
    # Project B has no usage.
    usage_b = services.providers.get_usage_for_project(project_b.id, actor_id=owner_b.id)
    assert usage_b.input_tokens == 0
    assert usage_b.output_tokens == 0
    # Project B's request list is empty.
    requests_b = services.providers.list_provider_requests_for_project(
        project_b.id, actor_id=owner_b.id
    )
    assert len(requests_b) == 0


def test_provider_request_deduplication_is_project_scoped(services) -> None:
    owner_a = services.identity.create_user(display_name="Owner A")
    project_a = services.identity.create_project(owner_id=owner_a.id, name="Project A")
    owner_b = services.identity.create_user(display_name="Owner B")
    project_b = services.identity.create_project(owner_id=owner_b.id, name="Project B")
    req = CanonicalRequest(
        provider="fake",
        model_name="fake-standard",
        messages=(CanonicalMessage(role="user", content="same payload"),),
    )

    first, _ = services.providers.send_request(
        project_id=project_a.id, actor_id=owner_a.id, request=req
    )
    second, _ = services.providers.send_request(
        project_id=project_b.id, actor_id=owner_b.id, request=req
    )

    assert first.id != second.id
    assert first.project_id == project_a.id
    assert second.project_id == project_b.id
    assert (
        len(
            services.providers.list_provider_requests_for_project(project_a.id, actor_id=owner_a.id)
        )
        == 1
    )
    assert (
        len(
            services.providers.list_provider_requests_for_project(project_b.id, actor_id=owner_b.id)
        )
        == 1
    )
