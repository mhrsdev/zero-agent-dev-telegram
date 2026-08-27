"""Observability wiring tests.

Per the release audit (§5.5): MetricsService must have real runtime call
sites and an export surface; the canary scan must cover every claimed
surface.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from zero.app.services import build_services
from zero.config import Settings
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations


@pytest.fixture
def services(test_settings: Settings):
    database = Database(test_settings)
    apply_migrations(database)
    return build_services(test_settings, database)


def _invoke_denied_and_allowed(services) -> None:
    owner = services.identity.create_user(display_name="Metrics owner")
    project = services.identity.create_project(owner_id=owner.id, name="Metrics")
    tool = services.tools.register_echo_tool()
    services.tools.grant_tool(
        project_id=project.id,
        actor_id=owner.id,
        tool_id=tool.id,
        agent_scope="main_worker",
    )
    services.tools.invoke(
        project_id=project.id,
        actor_id=owner.id,
        agent_scope="main_worker",
        tool_name=tool.name,
        input_data={"message": "hello"},
    )


def test_tool_invocations_increment_counters(services) -> None:
    _invoke_denied_and_allowed(services)
    counters = services.metrics.get_counters()
    assert any(key.startswith("tool_invocations_total") for key in counters)
    summary = services.metrics.get_histogram_summary("tool_invocation_duration_ms")
    assert summary is not None and summary["count"] >= 1


def test_canary_scan_covers_all_claimed_surfaces(services) -> None:
    results = services.canary.scan_all()
    expected = {
        "audit_events",
        "artifacts",
        "conversation_events",
        "knowledge_records",
        "provider_requests",
        "context_versions",
        "result_deliveries",
        "interface_event_log",
        "metrics",
    }
    assert expected <= set(results)
    for surface, findings in results.items():
        assert isinstance(findings, list), surface


def test_metrics_endpoint_exports_runtime_data() -> None:
    settings = Settings.load_for_test()
    database = Database(settings)
    apply_migrations(database)

    from zero.app.api import create_app

    app = create_app(settings)

    import asyncio

    async def call():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            health = await ac.get("/capabilities")
            metrics = await ac.get("/metrics")
            return health.status_code, metrics.status_code, metrics.json()

    status_code, metrics_status, payload = asyncio.run(call())
    assert status_code == 200
    assert metrics_status == 200
    assert "counters" in payload
    assert "histograms" in payload
    capabilities = payload.get("workers")
    assert capabilities is not None


def test_capabilities_endpoint_declares_worktree_execution() -> None:
    settings = Settings.load_for_test()
    database = Database(settings)
    apply_migrations(database)

    from zero.app.capabilities import compute_capabilities

    caps = compute_capabilities(settings)
    # Tests run in host_bounded mode by default: available.
    assert caps["worktree_execution"]["status"] == "available"

    production_settings = Settings(
        zero_env="production",
        database_url="sqlite:////var/lib/zero/zero.db",
        secret_key="x" * 64,
        auth_required=True,
        bootstrap_token="b" * 64,
    )
    prod_caps = compute_capabilities(production_settings)
    assert prod_caps["worktree_execution"]["status"] == "unavailable"
    assert "isolation backend" in prod_caps["worktree_execution"]["detail"]


def test_metrics_endpoint_exposes_decomposition_analytics_snapshot() -> None:
    """S7 recovery analytics are observable at /metrics, per model."""
    settings = Settings.load_for_test()
    database = Database(settings)
    apply_migrations(database)

    from zero.app.api import create_app
    from zero.app.decomposition_analytics import (
        DecompositionAnalytics,
        DecompositionOutcome,
    )

    app = create_app(settings)
    analytics: DecompositionAnalytics = app.state.services.decomposition_analytics  # type: ignore[attr-defined]
    analytics.record(
        DecompositionOutcome(
            ts_utc="2026-08-27T09:00:00+00:00",
            revision_id="pr_test_metrics",
            provider="openai-compatible",
            model_name="glm-4.6",
            outcome="native_first_ask",
            path="native",
            attempts_used=1,
            task_count=3,
            edge_count=2,
            elapsed_ms=21000,
        )
    )

    import asyncio

    async def call():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.get("/metrics")
            return response.status_code, response.json()

    metrics_status, payload = asyncio.run(call())
    assert metrics_status == 200
    snapshot = payload["decomposition_analytics"]
    assert snapshot is not None
    assert snapshot["total_outcomes"] >= 1
    model_stats = snapshot["models"]["openai-compatible:glm-4.6"]
    assert model_stats["graphs_validated"] >= 1
    assert "typo_rate_per_graph" in model_stats
    assert model_stats["typo_rate_per_graph"] == 0.0
