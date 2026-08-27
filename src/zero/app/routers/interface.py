"""Interface routes extracted from app.api."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request, status

from zero.app.routers.deps import authorized_actor
from zero.app.routers.models import CreateInterfaceBindingRequest
from zero.app.services import Services
from zero.domain.identity import (
    ProjectId,
)
from zero.domain.interfaces import InterfaceBindingId


def _binding_payload(item: Any) -> dict[str, Any]:
    return {
        "id": item.id.value,
        "project_id": item.project_id.value,
        "platform": item.platform,
        "bot_token_ref": item.bot_token_ref,
        "chat_id": item.chat_id,
        "topic_id": item.topic_id,
        "is_enabled": item.is_enabled,
        "created_by": item.created_by.value,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def _interface_event_payload(item: Any) -> dict[str, Any]:
    return {
        "id": item.id.value,
        "project_id": item.project_id.value if item.project_id else None,
        "platform": item.platform,
        "external_event_id": item.external_event_id,
        "external_actor_id": item.external_actor_id,
        "resolved_user_id": item.resolved_user_id.value if item.resolved_user_id else None,
        "chat_id": item.chat_id,
        "topic_id": item.topic_id,
        "event_kind": item.event_kind,
        "event_content": getattr(item, "event_content", None),
        "processing_result": item.processing_result,
        "processing_detail": item.processing_detail,
        "created_at": item.created_at,
    }


def _result_delivery_payload(item: Any) -> dict[str, Any]:
    return {
        "id": item.id.value,
        "project_id": item.project_id.value,
        "execution_id": item.execution_id,
        "binding_id": item.binding_id.value,
        "created_by": item.created_by.value,
        "delivery_key": item.delivery_key,
        "state": item.state,
        "attempt_count": item.attempt_count,
        "claim_token": None,
        "lease_expires_at": item.lease_expires_at,
        "next_attempt_at": item.next_attempt_at,
        "external_message_id": item.external_message_id,
        "last_error": item.last_error,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def register_interface_routes(app: FastAPI, services: Services) -> None:
    @app.get("/projects/{project_id}/result-deliveries", tags=["interfaces"])
    def list_result_deliveries(request: Request, project_id: str) -> list[dict[str, Any]]:

        authorized_actor(request, services, project_id, "execution.view_diffs")
        return [
            _result_delivery_payload(item)
            for item in services.result_delivery.list_for_project(ProjectId(project_id))
        ]

    @app.post("/projects/{project_id}/result-deliveries/drain", tags=["interfaces"])
    def drain_result_delivery(request: Request, project_id: str) -> dict[str, Any]:

        authorized_actor(request, services, project_id, "execution.view_diffs")
        if not services.result_delivery.is_outbound_configured:
            raise HTTPException(status_code=503, detail="outbound result delivery unavailable")
        item = services.result_delivery.drain_once(project_id=ProjectId(project_id))
        return {"status": "empty"} if item is None else _result_delivery_payload(item)

    @app.get("/projects/{project_id}/interfaces", tags=["interfaces"])
    def list_bindings(request: Request, project_id: str) -> list[dict[str, Any]]:

        authorized_actor(request, services, project_id, "project.view")
        return [
            _binding_payload(item)
            for item in services.interfaces.list_bindings(ProjectId(project_id))
        ]

    @app.post(
        "/projects/{project_id}/interfaces",
        tags=["interfaces"],
        status_code=status.HTTP_201_CREATED,
    )
    def create_binding(
        request: Request, project_id: str, req: CreateInterfaceBindingRequest
    ) -> dict[str, Any]:

        actor = authorized_actor(request, services, project_id, "agent.manage")
        try:
            item = services.interfaces.create_binding(
                project_id=ProjectId(project_id),
                actor_id=actor,
                platform=req.platform,  # type: ignore[arg-type]
                chat_id=req.chat_id,
                topic_id=req.topic_id,
                bot_token_ref=req.bot_token_ref,
                is_enabled=req.is_enabled,
                source="web",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="request failed") from exc
        return _binding_payload(item)

    @app.get("/projects/{project_id}/interfaces/events", tags=["interfaces"])
    def list_interface_events(
        request: Request, project_id: str, limit: int = 100
    ) -> list[dict[str, Any]]:

        authorized_actor(request, services, project_id, "project.view")
        if not 1 <= limit <= 500:
            raise HTTPException(status_code=400, detail="limit must be between 1 and 500")
        return [
            _interface_event_payload(item)
            for item in services.interfaces.list_event_log(ProjectId(project_id), limit=limit)
        ]

    def _set_binding(
        request: Request, project_id: str, binding_id: str, enabled: bool
    ) -> dict[str, Any]:

        actor = authorized_actor(request, services, project_id, "agent.manage")
        try:
            method = (
                services.interfaces.enable_binding
                if enabled
                else services.interfaces.disable_binding
            )
            item = method(
                project_id=ProjectId(project_id),
                binding_id=InterfaceBindingId(binding_id),
                actor_id=actor,
                source="web",
            )
        except Exception as exc:
            raise HTTPException(status_code=404, detail="Interface binding not found") from exc
        return _binding_payload(item)

    @app.post("/projects/{project_id}/interfaces/{binding_id}/enable", tags=["interfaces"])
    def enable_binding(request: Request, project_id: str, binding_id: str) -> dict[str, Any]:
        return _set_binding(request, project_id, binding_id, True)

    @app.post("/projects/{project_id}/interfaces/{binding_id}/disable", tags=["interfaces"])
    def disable_binding(request: Request, project_id: str, binding_id: str) -> dict[str, Any]:
        return _set_binding(request, project_id, binding_id, False)
