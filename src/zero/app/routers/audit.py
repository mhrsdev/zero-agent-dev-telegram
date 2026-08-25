"""Audit routes extracted from app.api."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request

from zero.app.routers.deps import request_project_actor
from zero.app.services import Services
from zero.domain.identity import (
    ProjectId,
)


def register_audit_routes(app: FastAPI, services: Services) -> None:
    @app.get("/projects/{project_id}/audit", tags=["audit"])
    def list_audit_events(
        request: Request,
        project_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:

        events = services.audit.list_for_project(
            project_id=ProjectId(project_id),
            actor_id=request_project_actor(request, services, project_id),
            limit=limit,
            offset=offset,
            source="web",
        )
        return [
            {
                "id": e.id.value,
                "project_id": e.project_id.value if e.project_id else None,
                "actor_id": e.actor_id.value if e.actor_id else None,
                "source": e.source,
                "operation": e.operation,
                "target_type": e.target_type,
                "target_id": e.target_id,
                "result": e.result,
                "correlation_id": e.correlation_id,
                "redacted_summary": e.redacted_summary,
                "created_at": e.created_at,
            }
            for e in events
        ]
