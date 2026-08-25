"""Provider routes extracted from app.api."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request, status

from zero.app.routers.deps import authorized_actor
from zero.app.routers.models import ReconcileProviderRequestRequest
from zero.app.services import Services
from zero.domain.identity import (
    ProjectId,
)


def _provider_model_payload(model: Any) -> dict[str, Any]:
    return {
        "id": model.id.value,
        "provider": model.provider,
        "model_name": model.model_name,
        "context_window": model.context_window,
        "max_output_tokens": model.max_output_tokens,
        "capabilities": list(model.capabilities),
        "is_active": model.is_active,
        "created_at": model.created_at,
    }


def _provider_request_payload(item: Any) -> dict[str, Any]:
    return {
        "id": item.id.value,
        "project_id": item.project_id.value,
        "execution_id": item.execution_id.value if item.execution_id else None,
        "provider": item.provider,
        "model_name": item.model_name,
        "request_hash": item.request_hash,
        "state": item.state,
        "error_class": item.error_class,
        "error_message": item.error_message,
        "response_artifact_id": item.response_artifact_id.value
        if item.response_artifact_id
        else None,
        "started_at": item.started_at,
        "completed_at": item.completed_at,
    }


def _usage_payload(item: Any) -> dict[str, Any]:
    usage = item.usage
    return {
        "id": item.id.value,
        "project_id": item.project_id.value,
        "provider_request_id": item.provider_request_id.value,
        "execution_id": item.execution_id.value if item.execution_id else None,
        "provider_message_id": item.provider_message_id,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_creation_input_tokens": usage.cache_creation_input_tokens,
        "cache_read_input_tokens": usage.cache_read_input_tokens,
        "estimated_cost_usd": item.estimated_cost_usd,
        "pricing_catalog_version": item.pricing_catalog_version,
        "created_at": item.created_at,
    }


def register_provider_routes(app: FastAPI, services: Services) -> None:
    @app.get("/providers", tags=["providers"])
    def list_provider_models() -> list[dict[str, Any]]:
        return [_provider_model_payload(item) for item in services.providers.list_models()]

    @app.get("/providers/{provider}/{model_name}", tags=["providers"])
    def get_provider_model(provider: str, model_name: str) -> dict[str, Any]:
        try:
            return _provider_model_payload(services.providers.get_model(provider, model_name))
        except Exception as exc:
            raise HTTPException(status_code=404, detail="Provider model not found") from exc

    @app.get("/projects/{project_id}/providers", tags=["providers"])
    def list_project_provider_models(request: Request, project_id: str) -> list[dict[str, Any]]:
        authorized_actor(request, services, project_id, "model.change")
        return [_provider_model_payload(item) for item in services.providers.list_models()]

    @app.get("/projects/{project_id}/providers/requests", tags=["providers"])
    def list_provider_requests(request: Request, project_id: str) -> list[dict[str, Any]]:

        actor = authorized_actor(request, services, project_id, "cost.view")
        return [
            _provider_request_payload(item)
            for item in services.providers.list_provider_requests_for_project(
                ProjectId(project_id),
                actor_id=actor,
                source="web",
            )
        ]

    @app.get("/projects/{project_id}/providers/requests/unknown", tags=["providers"])
    def list_unknown_provider_requests(request: Request, project_id: str) -> list[dict[str, Any]]:
        """Operator queue: provider requests awaiting reconciliation."""

        authorized_actor(request, services, project_id, "cost.view")
        return [
            _provider_request_payload(item)
            for item in services.providers.list_unknown_requests(ProjectId(project_id))
        ]

    @app.post(
        "/projects/{project_id}/providers/requests/{request_id}/reconcile", tags=["providers"]
    )
    def reconcile_provider_request(
        request: Request,
        project_id: str,
        request_id: str,
        req: ReconcileProviderRequestRequest,
    ) -> dict[str, Any]:
        """Record an operator decision for one unknown provider outcome."""
        from zero.domain.providers import ProviderRequestId

        actor = authorized_actor(request, services, project_id, "execution.start")
        try:
            services.providers.reconcile_provider_request(
                project_id=ProjectId(project_id),
                request_id=ProviderRequestId(request_id),
                actor_id=actor,
                resolution=req.resolution,
                note=req.note,
                source="web",
            )
        except ValueError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request failed")
        return {"status": "reconciled", "resolution": req.resolution}

    @app.get("/projects/{project_id}/providers/usage", tags=["providers"])
    def list_provider_usage(request: Request, project_id: str) -> list[dict[str, Any]]:

        actor = authorized_actor(request, services, project_id, "cost.view")
        return [
            _usage_payload(item)
            for item in services.providers.list_usage_records_for_project(
                ProjectId(project_id),
                actor_id=actor,
                source="web",
            )
        ]
