"""Health and observability routes extracted from app.api."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, status
from fastapi.responses import JSONResponse

from zero import __version__
from zero.app.background_workers import BackgroundWorkerHost
from zero.app.capabilities import capabilities_payload
from zero.app.health import HealthService
from zero.app.services import Services
from zero.config import Settings


def register_health_routes(
    app: FastAPI,
    health_service: HealthService,
    *,
    services: Services,
    settings: Settings,
) -> None:
    @app.get("/healthz", tags=["health"])
    def healthz() -> dict[str, Any]:
        report = health_service.report()
        return report.to_dict()

    @app.get("/readyz", tags=["health"])
    def readyz() -> JSONResponse:
        report = health_service.report()
        if report.status == "ok":
            return JSONResponse(status_code=status.HTTP_200_OK, content=report.to_dict())
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=report.to_dict(),
        )

    @app.get("/capabilities", tags=["health"])
    def capabilities() -> dict[str, Any]:
        """Declare what this deployment can and cannot do, and why."""
        payload = capabilities_payload(settings)
        worker_host: BackgroundWorkerHost | None = getattr(app.state, "worker_host", None)
        if worker_host is not None:
            payload["workers"] = worker_host.status.to_dict()
        return payload

    @app.get("/metrics", tags=["observability"])
    def metrics() -> dict[str, Any]:
        """Export low-cardinality runtime metrics.

        Per PLAN.md M14: counters and duration summaries only; raw
        prompts, source files, tool parameters/results, credentials,
        and private messages are excluded by construction (the label
        vocabulary is closed).
        """
        counters = services.metrics.get_counters()
        histograms = {
            name: services.metrics.get_histogram_summary(name)
            for name in services.metrics.histogram_names()
        }
        worker_host_status: dict[str, object] | None = None
        host: BackgroundWorkerHost | None = getattr(app.state, "worker_host", None)
        if host is not None:
            worker_host_status = host.status.to_dict()
        # S7 recovery analytics (GAP-10/8-s7b): low-cardinality per-model
        # decomposition outcomes incl. the typo_rate_per_graph headline.
        analytics_service = getattr(services, "decomposition_analytics", None)
        decomposition_analytics = (
            analytics_service.snapshot() if analytics_service is not None else None
        )
        return {
            "counters": counters,
            "histograms": histograms,
            "workers": worker_host_status,
            "decomposition_analytics": decomposition_analytics,
        }

    @app.get("/", tags=["root"])
    def root() -> dict[str, str]:
        return {
            "name": "Zero Develop",
            "version": __version__,
            "environment": app.state.settings.zero_env,
            "docs": "/docs",
            "health": "/healthz",
        }
