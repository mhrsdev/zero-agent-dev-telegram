"""Authorization routes extracted from app.api."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, status

from zero.app.auth_service import (
    authenticated_actor,
)
from zero.app.services import Services
from zero.domain.identity import (
    ProjectId,
)


def register_authorization_routes(app: FastAPI, services: Services) -> None:
    @app.post(
        "/projects/{project_id}/authorize",
        tags=["authorization"],
    )
    def authorize(
        project_id: str,
        actor_id: str = "",
        permission: str = "project.view",
    ) -> dict[str, Any]:
        """Check whether an actor may perform a permission on a project.

        This endpoint is for diagnostics and tests. Real mutations
        call :meth:`AuthorizationService.require_permission` internally
        rather than going through this endpoint.
        """

        try:
            decision = services.authorization.authorize(
                actor_id=authenticated_actor(actor_id),
                project_id=ProjectId(project_id),
                permission=permission,  # type: ignore[arg-type]
                source="web",
            )
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="request failed")
        return {
            "allowed": decision.allowed,
            "actor_id": decision.actor_id.value if decision.actor_id else None,
            "project_id": decision.project_id.value,
            "permission": decision.permission,
            "role": decision.role,
            "reason": decision.reason,
        }
