"""Identity routes extracted from app.api."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from pydantic import BaseModel, Field

from zero.app.auth_service import (
    authenticated_actor,
    request_actor,
)
from zero.app.services import Services
from zero.domain.identity import (
    IdentityError,
    MembershipAlreadyExistsError,
    MembershipNotFoundError,
    ProjectId,
    ProjectNotFoundError,
    UserId,
    UserNotFoundError,
)


class CreateUserRequest(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=200)


class CreateProjectRequest(BaseModel):
    owner_id: str = Field(..., description="The Zero User ID of the project owner.")
    name: str = Field(..., min_length=1, max_length=200)


class AddMemberRequest(BaseModel):
    member_id: str
    role: str = Field(..., pattern="^(owner|member|viewer)$")


class LinkExternalIdentityRequest(BaseModel):
    platform: str = Field(..., pattern="^(telegram|discord|web|email|other)$")
    external_id: str = Field(..., min_length=1, max_length=200)
    external_username: str | None = None


def register_identity_routes(app: FastAPI, services: Services) -> None:
    @app.post("/users", tags=["identity"], status_code=status.HTTP_201_CREATED)
    def create_user(req: CreateUserRequest) -> dict[str, Any]:
        user = services.identity.create_user(display_name=req.display_name, source="web")
        return {
            "id": user.id.value,
            "display_name": user.display_name,
            "status": user.status,
            "created_at": user.created_at,
        }

    @app.get("/users/{user_id}", tags=["identity"])
    def get_user(user_id: str) -> dict[str, Any]:
        try:
            user = services.identity.get_user(authenticated_actor(user_id))
        except (UserNotFoundError, ValueError):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request failed")
        return {
            "id": user.id.value,
            "display_name": user.display_name,
            "status": user.status,
            "created_at": user.created_at,
        }

    @app.post("/projects", tags=["identity"], status_code=status.HTTP_201_CREATED)
    def create_project(req: CreateProjectRequest) -> dict[str, Any]:
        try:
            project = services.identity.create_project(
                owner_id=authenticated_actor(req.owner_id),
                name=req.name,
                source="web",
            )
        except UserNotFoundError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request failed")
        except IdentityError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="request failed")
        return {
            "id": project.id.value,
            "name": project.name,
            "owner_user_id": project.owner_user_id.value,
            "created_at": project.created_at,
        }

    @app.get("/projects/{project_id}", tags=["identity"])
    def get_project(project_id: str) -> dict[str, Any]:

        try:
            project = services.identity.get_project(ProjectId(project_id))
        except (ProjectNotFoundError, ValueError):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request failed")
        return {
            "id": project.id.value,
            "name": project.name,
            "owner_user_id": project.owner_user_id.value,
            "created_at": project.created_at,
        }

    @app.post(
        "/projects/{project_id}/members",
        tags=["identity"],
        status_code=status.HTTP_201_CREATED,
    )
    def add_member(project_id: str, req: AddMemberRequest) -> dict[str, Any]:

        try:
            project = services.identity.get_project(ProjectId(project_id))
            membership = services.identity.add_member(
                project_id=project.id,
                actor_id=authenticated_actor(project.owner_user_id.value),
                member_id=UserId(req.member_id),
                role=req.role,  # type: ignore[arg-type]
                source="web",
            )
        except UserNotFoundError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request failed")
        except ProjectNotFoundError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request failed")
        except MembershipAlreadyExistsError:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="request failed")
        except IdentityError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="request failed")
        return {
            "project_id": membership.project_id.value,
            "user_id": membership.user_id.value,
            "role": membership.role,
            "created_at": membership.created_at,
        }

    @app.get("/projects/{project_id}/members", tags=["identity"])
    def list_members(request: Request, project_id: str) -> list[dict[str, Any]]:

        try:
            project = services.identity.get_project(ProjectId(project_id))
            memberships = services.identity.list_members(
                project.id,
                getattr(request.state, "user_id", project.owner_user_id),
            )
        except ProjectNotFoundError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request failed")
        return [
            {
                "project_id": m.project_id.value,
                "user_id": m.user_id.value,
                "role": m.role,
                "created_at": m.created_at,
            }
            for m in memberships
        ]

    @app.delete(
        "/projects/{project_id}/members/{user_id}",
        tags=["identity"],
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def remove_member(project_id: str, user_id: str):

        try:
            project = services.identity.get_project(ProjectId(project_id))
            services.identity.remove_member(
                project_id=project.id,
                actor_id=authenticated_actor(project.owner_user_id.value),
                member_id=UserId(user_id),
                source="web",
            )
        except (ProjectNotFoundError, MembershipNotFoundError):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request failed")
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="request failed")

    @app.post(
        "/users/{user_id}/external-identities/verify",
        tags=["identity"],
    )
    def verify_external_identity(
        request: Request, user_id: str, req: LinkExternalIdentityRequest
    ) -> dict[str, Any]:
        """Verify a linked platform identity (onboarding R5 fix).

        Links are created ``verified=False``; without a verification path
        no live Telegram message could ever pass the identity gate. The
        authenticated principal must own the identity.
        """

        actor = request_actor(request, user_id)
        if str(actor) != user_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
        try:
            identity = services.identity.verify_external_identity(
                platform=req.platform,  # type: ignore[arg-type]
                external_id=req.external_id,
                source="web",
            )
        except Exception as exc:
            from zero.domain.secrets import SecretError  # noqa: F401

            if isinstance(exc, (IdentityError, ValueError)):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="request failed"
                ) from exc
            raise
        return {
            "id": identity.id.value,
            "platform": identity.platform,
            "external_id": identity.external_id,
            "verified": True,
        }

    @app.post(
        "/users/{user_id}/external-identities",
        tags=["identity"],
        status_code=status.HTTP_201_CREATED,
    )
    def link_external_identity(user_id: str, req: LinkExternalIdentityRequest) -> dict[str, Any]:
        try:
            identity = services.identity.link_external_identity(
                user_id=authenticated_actor(user_id),
                platform=req.platform,  # type: ignore[arg-type]
                external_id=req.external_id,
                external_username=req.external_username,
                verified=False,
                source="web",
            )
        except UserNotFoundError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request failed")
        except IdentityError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="request failed")
        return {
            "id": identity.id.value,
            "user_id": identity.user_id.value,
            "platform": identity.platform,
            "external_username": identity.external_username,
            "verified_at": identity.verified_at,
            "created_at": identity.created_at,
        }
