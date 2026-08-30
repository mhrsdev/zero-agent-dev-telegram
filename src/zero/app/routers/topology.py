"""Topology routes extracted from app.api."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request, status

from zero.app.routers.deps import authorized_actor
from zero.app.routers.models import AddKnowledgeRequest, CreateAgentTypeRequest
from zero.app.services import Services
from zero.domain.identity import (
    ProjectId,
)


def _agent_type_payload(agent_type: Any) -> dict[str, Any]:
    return {
        "id": agent_type.id.value,
        "project_id": agent_type.project_id.value,
        "name": agent_type.name,
        "responsibility": agent_type.responsibility,
        "memory_scope": agent_type.memory_scope,
        "permitted_tools": list(agent_type.permitted_tools),
        "model_policy": agent_type.model_policy,
        "context_budget_tokens": agent_type.context_budget_tokens,
        "max_concurrent_instances": agent_type.max_concurrent_instances,
        "state": agent_type.state,
        "version": agent_type.version,
        "superseded_by": agent_type.superseded_by.value if agent_type.superseded_by else None,
        "created_at": agent_type.created_at,
        "updated_at": agent_type.updated_at,
    }


def _knowledge_payload(record: Any) -> dict[str, Any]:
    return {
        "id": record.id.value,
        "project_id": record.project_id.value,
        "agent_type_id": record.agent_type_id.value if record.agent_type_id else None,
        "kind": record.kind,
        "content": record.content,
        "content_hash": record.content_hash,
        "provenance": record.provenance,
        "state": record.state,
        "superseded_by": record.superseded_by.value if record.superseded_by else None,
        "migrated_from": record.migrated_from.value if record.migrated_from else None,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def register_topology_routes(app: FastAPI, services: Services) -> None:
    @app.get("/projects/{project_id}/agent-types", tags=["topology"])
    def list_agent_types(request: Request, project_id: str) -> list[dict[str, Any]]:

        authorized_actor(request, services, project_id, "project.view")
        return [
            _agent_type_payload(item)
            for item in services.agent_types.list_types(ProjectId(project_id))
        ]

    @app.post(
        "/projects/{project_id}/agent-types",
        tags=["topology"],
        status_code=status.HTTP_201_CREATED,
    )
    def create_agent_type(
        request: Request, project_id: str, req: CreateAgentTypeRequest
    ) -> dict[str, Any]:

        actor = authorized_actor(request, services, project_id, "agent.manage")
        try:
            item = services.agent_types.create_type(
                project_id=ProjectId(project_id),
                actor_id=actor,
                name=req.name,
                responsibility=req.responsibility,
                memory_scope=req.memory_scope,
                permitted_tools=tuple(req.permitted_tools),
                model_policy=req.model_policy,
                context_budget_tokens=req.context_budget_tokens,
                max_concurrent_instances=req.max_concurrent_instances,
                source="web",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="request failed") from exc
        return _agent_type_payload(item)

    @app.get("/projects/{project_id}/agent-types/{type_id}", tags=["topology"])
    def get_agent_type(request: Request, project_id: str, type_id: str) -> dict[str, Any]:
        from zero.domain.agent_types import AgentTypeId

        authorized_actor(request, services, project_id, "project.view")
        try:
            item = services.agent_types.get_type(ProjectId(project_id), AgentTypeId(type_id))
        except Exception as exc:
            raise HTTPException(status_code=404, detail="Agent type not found") from exc
        return _agent_type_payload(item)

    @app.get("/projects/{project_id}/agent-types/{type_id}/knowledge", tags=["topology"])
    def list_knowledge(request: Request, project_id: str, type_id: str) -> list[dict[str, Any]]:
        from zero.domain.agent_types import AgentTypeId

        actor = authorized_actor(request, services, project_id, "project.view")
        try:
            records = services.agent_types.list_knowledge_for_type(
                ProjectId(project_id), AgentTypeId(type_id), actor_id=actor
            )
        except Exception as exc:
            raise HTTPException(status_code=404, detail="Knowledge not found") from exc
        return [_knowledge_payload(item) for item in records]

    @app.post(
        "/projects/{project_id}/agent-types/{type_id}/knowledge",
        tags=["topology"],
        status_code=status.HTTP_201_CREATED,
    )
    def add_knowledge(
        request: Request, project_id: str, type_id: str, req: AddKnowledgeRequest
    ) -> dict[str, Any]:
        from zero.domain.agent_types import AgentTypeId

        actor = authorized_actor(request, services, project_id, "agent.manage")
        try:
            record = services.agent_types.add_knowledge(
                project_id=ProjectId(project_id),
                type_id=AgentTypeId(type_id),
                actor_id=actor,
                kind=req.kind,  # type: ignore[arg-type]
                content=req.content,
                provenance=req.provenance,
                state=req.state,  # type: ignore[arg-type]
                source="web",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="request failed") from exc
        return _knowledge_payload(record)

    @app.get("/projects/{project_id}/topology", tags=["topology"])
    def list_topology_snapshots(request: Request, project_id: str) -> list[dict[str, Any]]:

        authorized_actor(request, services, project_id, "project.view")
        snapshots = services.agent_types.list_snapshots(ProjectId(project_id))
        return [
            {
                "id": item.id.value,
                "project_id": item.project_id.value,
                "snapshot_version": item.snapshot_version,
                "reason": item.reason,
                "topology_state": item.topology_state,
                "created_at": item.created_at,
            }
            for item in snapshots
        ]
