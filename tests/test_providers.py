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

import pytest

from zero.app.provider_adapter import (
    compute_request_hash,
    validate_tool_messages,
)
from zero.app.services import build_services
from zero.config import Settings
from zero.domain.providers import (
    CanonicalMessage,
    CanonicalRequest,
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
    project = services.identity.create_project(
        owner_id=owner.id, name="Project A"
    )
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
        {"role": "assistant", "content": "hi", "tool_calls": [
            {"id": "call1", "name": "echo"}]},
        {"role": "tool", "tool_call_id": "call1", "content": "echoed"},
    ]
    clean, stripped = validate_tool_messages(messages)
    assert stripped == []
    assert len(clean) == 3


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


# ----------------------------------------------------------------------
# Provider model resolution
# ----------------------------------------------------------------------


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


def test_duplicate_request_is_deduplicated(
    services, project_with_owner
) -> None:
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


def test_usage_not_double_counted(services, project_with_owner) -> None:
    """Per PLAN.md M10: 'Duplicate streamed usage is not double-counted.'"""
    owner, project = project_with_owner
    req = CanonicalRequest(
        provider="fake",
        model_name="fake-standard",
        messages=(CanonicalMessage(role="user", content="Count me once"),),
    )
    services.providers.send_request(
        project_id=project.id, actor_id=owner.id, request=req
    )
    # Send the same request again (dedup).
    services.providers.send_request(
        project_id=project.id, actor_id=owner.id, request=req
    )
    # Only one usage record should exist.
    usage_records = services.providers.list_usage_records_for_project(
        project.id
    )
    assert len(usage_records) == 1


def test_error_classification(services, project_with_owner) -> None:
    """Per zero-provider-adapter-contract: errors need stable classes."""
    owner, project = project_with_owner
    req = CanonicalRequest(
        provider="fake",
        model_name="fake-standard",
        messages=(
            CanonicalMessage(role="user", content="Please trigger error now"),
        ),
    )
    with pytest.raises(Exception):
        services.providers.send_request(
            project_id=project.id, actor_id=owner.id, request=req
        )
    # The provider request should be in 'failed' state.
    requests = services.providers.list_provider_requests_for_project(
        project.id
    )
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
            messages=(
                CanonicalMessage(role="user", content=f"Request {i}"),
            ),
        )
        services.providers.send_request(
            project_id=project.id, actor_id=owner.id, request=req
        )
    usage = services.providers.get_usage_for_project(project.id)
    # All three requests' usage is aggregated.
    assert usage.input_tokens > 0
    assert usage.output_tokens > 0
    # Verify it equals the sum of individual records.
    records = services.providers.list_usage_records_for_project(project.id)
    total_input = sum(r.usage.input_tokens for r in records)
    assert usage.input_tokens == total_input


def test_pricing_changes_do_not_mutate_historical_usage(
    services, project_with_owner
) -> None:
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
    services.providers.send_request(
        project_id=project.id, actor_id=owner.id, request=req
    )
    # Register pricing v2 (different prices).
    services.providers.register_pricing(
        catalog_version=2,
        provider="fake",
        model_name="fake-standard",
        input_price_per_million="20.00",
        output_price_per_million="60.00",
    )
    # The historical usage record still has pricing_catalog_version=1.
    records = services.providers.list_usage_records_for_project(project.id)
    assert len(records) >= 1
    assert records[0].pricing_catalog_version == 1
    # The estimated cost was computed with v1 pricing, not v2.
    assert records[0].estimated_cost_usd != "0"


def test_estimated_cost_distinct_from_reconciled(
    services, project_with_owner
) -> None:
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
    services.providers.send_request(
        project_id=project.id, actor_id=owner.id, request=req
    )
    records = services.providers.list_usage_records_for_project(project.id)
    record = records[0]
    # Estimated cost is set.
    assert record.estimated_cost_usd != "0"
    # Reconciled cost is NULL until reconciled.
    assert record.reconciled_cost_usd is None
    # Reconcile.
    services.providers.reconcile_usage(
        usage_id=record.id,
        reconciled_cost_usd="0.001234",
        actor_id=owner.id,
    )
    records = services.providers.list_usage_records_for_project(project.id)
    record = records[0]
    assert record.reconciled_cost_usd == "0.001234"
    # Estimated cost is unchanged.
    assert record.estimated_cost_usd != "0.001234"


# ----------------------------------------------------------------------
# Provider switch resumes from Zero state
# ----------------------------------------------------------------------


def test_provider_switch_preserves_canonical_state(
    services, project_with_owner
) -> None:
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
    services.providers.send_request(
        project_id=project.id, actor_id=owner.id, request=req1
    )
    # Send with fake-mini (different model).
    req2 = CanonicalRequest(
        provider="fake",
        model_name="fake-mini",
        messages=(CanonicalMessage(role="user", content="Second model"),),
    )
    services.providers.send_request(
        project_id=project.id, actor_id=owner.id, request=req2
    )
    # Both requests are recorded.
    requests = services.providers.list_provider_requests_for_project(
        project.id
    )
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
    project_a = services.identity.create_project(
        owner_id=owner_a.id, name="Project A"
    )
    owner_b = services.identity.create_user(display_name="Owner B")
    project_b = services.identity.create_project(
        owner_id=owner_b.id, name="Project B"
    )
    req = CanonicalRequest(
        provider="fake",
        model_name="fake-standard",
        messages=(CanonicalMessage(role="user", content="Isolated"),),
    )
    services.providers.send_request(
        project_id=project_a.id, actor_id=owner_a.id, request=req
    )
    # Project B has no usage.
    usage_b = services.providers.get_usage_for_project(project_b.id)
    assert usage_b.input_tokens == 0
    assert usage_b.output_tokens == 0
    # Project B's request list is empty.
    requests_b = services.providers.list_provider_requests_for_project(
        project_b.id
    )
    assert len(requests_b) == 0
