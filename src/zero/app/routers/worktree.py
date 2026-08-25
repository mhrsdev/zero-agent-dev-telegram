"""Worktree routes extracted from app.api."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request, status

from zero.app.routers.deps import authorized_actor
from zero.app.routers.models import RegisterRepositoryRequest
from zero.app.services import Services
from zero.domain.identity import (
    ProjectId,
)


def _repository_payload(item: Any) -> dict[str, Any]:
    return {
        "id": item.id.value,
        "project_id": item.project_id.value,
        "name": item.name,
        "local_path": item.local_path,
        "default_base_revision": item.default_base_revision,
        "created_at": item.created_at,
    }


def _worktree_payload(item: Any) -> dict[str, Any]:
    return {
        "id": item.id.value,
        "project_id": item.project_id.value,
        "repository_id": item.repository_id.value,
        "execution_id": item.execution_id.value,
        "task_id": item.task_id.value,
        "branch_name": item.branch_name,
        "worktree_path": item.worktree_path,
        "base_revision": item.base_revision,
        "state": item.state,
        "cleanup_eligible_at": item.cleanup_eligible_at,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def register_worktree_routes(app: FastAPI, services: Services) -> None:
    @app.get("/projects/{project_id}/repositories", tags=["worktrees"])
    def list_repositories(request: Request, project_id: str) -> list[dict[str, Any]]:

        actor = authorized_actor(request, services, project_id, "project.view")
        return [
            _repository_payload(item)
            for item in services.worktree.list_repositories(
                ProjectId(project_id),
                actor_id=actor,
                source="web",
            )
        ]

    @app.post(
        "/projects/{project_id}/repositories",
        tags=["worktrees"],
        status_code=status.HTTP_201_CREATED,
    )
    def register_repository(
        request: Request, project_id: str, req: RegisterRepositoryRequest
    ) -> dict[str, Any]:

        actor = authorized_actor(request, services, project_id, "execution.start")
        try:
            item = services.worktree.register_repository(
                project_id=ProjectId(project_id),
                actor_id=actor,
                name=req.name,
                local_path=req.local_path,
                default_base_revision=req.default_base_revision,
                source="web",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="request failed") from exc
        return _repository_payload(item)

    @app.get("/projects/{project_id}/worktrees", tags=["worktrees"])
    def list_worktrees(
        request: Request, project_id: str, execution_id: str | None = None
    ) -> list[dict[str, Any]]:
        from zero.domain.execution import ExecutionId

        actor = authorized_actor(request, services, project_id, "project.view")
        if execution_id:
            items = services.worktree.list_worktrees_for_execution(
                ProjectId(project_id),
                ExecutionId(execution_id),
                actor_id=actor,
                source="web",
            )
        else:
            items = services.worktree.list_worktrees_for_project(
                ProjectId(project_id),
                actor_id=actor,
                source="web",
            )
        return [
            _worktree_payload(item) for item in items if item.project_id == ProjectId(project_id)
        ]

    @app.get("/projects/{project_id}/worktrees/{worktree_id}", tags=["worktrees"])
    def get_worktree(request: Request, project_id: str, worktree_id: str) -> dict[str, Any]:
        from zero.domain.worktrees import WorktreeId

        actor = authorized_actor(request, services, project_id, "project.view")
        try:
            item = services.worktree.get_worktree(
                ProjectId(project_id),
                WorktreeId(worktree_id),
                actor_id=actor,
                source="web",
            )
        except Exception as exc:
            raise HTTPException(status_code=404, detail="Worktree not found") from exc
        return _worktree_payload(item)
