"""Tool routes extracted from app.api."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from pydantic import BaseModel, Field

from zero.app.routers.deps import request_project_actor
from zero.app.services import Services
from zero.domain.identity import (
    ProjectId,
)
from zero.domain.tools import ToolError


class GrantToolRequest(BaseModel):
    tool_id: str
    agent_scope: str = Field(..., pattern="^(main_planner|main_worker|sub_agent_type|integration)$")
    max_invocations: int | None = None
    timeout_seconds: int | None = None


class InvokeToolRequest(BaseModel):
    tool_name: str
    input_data: dict[str, Any]
    agent_scope: str = Field(..., pattern="^(main_planner|main_worker|sub_agent_type|integration)$")


def register_tool_routes(app: FastAPI, services: Services) -> None:
    @app.get("/tools", tags=["tools"])
    def list_tools() -> list[dict[str, Any]]:
        tools = services.tools.list_tools()
        return [
            {
                "id": t.id.value,
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
                "output_schema": t.output_schema,
            }
            for t in tools
        ]

    @app.post(
        "/projects/{project_id}/tool-grants",
        tags=["tools"],
        status_code=status.HTTP_201_CREATED,
    )
    def grant_tool(request: Request, project_id: str, req: GrantToolRequest) -> dict[str, Any]:
        from zero.domain.tools import ToolId

        try:
            grant = services.tools.grant_tool(
                project_id=ProjectId(project_id),
                actor_id=request_project_actor(request, services, project_id),
                tool_id=ToolId(req.tool_id),
                agent_scope=req.agent_scope,  # type: ignore[arg-type]
                max_invocations=req.max_invocations,
                timeout_seconds=req.timeout_seconds,
                source="web",
            )
        except (ToolError, ValueError):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="request failed")
        return {
            "id": grant.id.value,
            "project_id": grant.project_id.value,
            "tool_id": grant.tool_id.value,
            "agent_scope": grant.agent_scope,
            "max_invocations": grant.max_invocations,
            "timeout_seconds": grant.timeout_seconds,
        }

    @app.delete(
        "/projects/{project_id}/tool-grants/{tool_id}",
        tags=["tools"],
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def revoke_tool_grant(
        request: Request,
        project_id: str,
        tool_id: str,
        agent_scope: str,
    ):
        """Revoke a project tool grant; takes effect immediately."""
        from zero.domain.tools import ToolId

        services.tools.revoke_tool_grant(
            project_id=ProjectId(project_id),
            actor_id=request_project_actor(request, services, project_id),
            tool_id=ToolId(tool_id),
            agent_scope=agent_scope,  # type: ignore[arg-type]
            source="web",
        )

    @app.post(
        "/projects/{project_id}/tool-invocations",
        tags=["tools"],
    )
    def invoke_tool(request: Request, project_id: str, req: InvokeToolRequest) -> dict[str, Any]:

        try:
            result = services.tools.invoke(
                project_id=ProjectId(project_id),
                actor_id=request_project_actor(request, services, project_id),
                agent_scope=req.agent_scope,  # type: ignore[arg-type]
                tool_name=req.tool_name,
                input_data=req.input_data,
                source="web",
                secret_service=services.secrets,
            )
        except ToolError as exc:
            # Map typed tool errors to HTTP statuses.
            from zero.domain.tools import (
                ToolInputValidationError,
                ToolInvocationDeniedError,
                ToolNotFoundError,
            )

            if isinstance(exc, ToolInputValidationError):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"error": "request validation failed", "errors": []},
                )
            if isinstance(exc, ToolInvocationDeniedError):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="request failed")
            if isinstance(exc, ToolNotFoundError):
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request failed")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Tool invocation failed",
            ) from exc
        return {
            "tool_id": result.tool_id.value,
            "status": result.status,
            "output": result.output,
            "model_facing": result.model_facing,
            "duration_ms": result.duration_ms,
        }
