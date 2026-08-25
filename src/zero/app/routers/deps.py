"""Shared request-scoped dependencies for the per-domain router modules.

Every project-scoped handler pairs the same two steps: resolve the
principal behind the request, then enforce a permission on the target
project. Forty-eight call sites across the domain seams depend on that
pairing, so both helpers live here instead of being duplicated per
router module.
"""

from __future__ import annotations

from fastapi import HTTPException, Request, status

from zero.app.services import Services
from zero.domain.identity import ProjectId, ProjectNotFoundError, UserId


def request_project_actor(request: Request, services: Services, project_id: str) -> UserId:
    try:
        project = services.identity.get_project(ProjectId(project_id))
    except (ProjectNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from exc
    return getattr(request.state, "user_id", project.owner_user_id)


def authorized_actor(
    request: Request,
    services: Services,
    project_id: str,
    permission: str,
) -> UserId:
    """Resolve the request principal and authorize the target project."""
    actor = request_project_actor(request, services, project_id)
    services.authorization.require_permission(
        actor_id=actor,
        project_id=ProjectId(project_id),
        permission=permission,  # type: ignore[arg-type]
        source="web",
    )
    return actor
