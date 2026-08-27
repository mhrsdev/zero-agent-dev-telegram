"""Secret routes extracted from app.api."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from pydantic import BaseModel, Field, SecretStr

from zero.app.routers.deps import request_project_actor
from zero.app.services import Services
from zero.domain.identity import (
    ProjectId,
)
from zero.domain.secrets import SecretError


class StoreSecretRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    secret_type: str = Field(..., pattern="^(api_key|token|password|other)$")
    # SecretStr keeps the plaintext out of reprs/logs by construction
    # (the same convention as Settings) without version-fragile
    # Field(repr=...) metadata.
    value: SecretStr = Field(..., min_length=1)


def register_secret_routes(app: FastAPI, services: Services) -> None:
    @app.post(
        "/projects/{project_id}/secrets",
        tags=["secrets"],
        status_code=status.HTTP_201_CREATED,
    )
    def store_secret(request: Request, project_id: str, req: StoreSecretRequest) -> dict[str, Any]:

        try:
            secret_ref = services.secrets.store(
                project_id=ProjectId(project_id),
                name=req.name,
                secret_type=req.secret_type,  # type: ignore[arg-type]
                value=req.value.get_secret_value(),
                actor_id=request_project_actor(request, services, project_id),
                source="web",
            )
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="request failed")
        except SecretError as exc:
            # Fail closed without leaking internals, but give the
            # operator the one fact that matters for recovery (usually
            # missing/blank ZERO_SECRET_KEY material).
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    "Failed to store secret: the encrypted secret store "
                    "refused this write. Most common cause is a server "
                    "without configured ZERO_SECRET_KEY key material; "
                    "run 'zero setup' or configure the key, then retry."
                ),
            ) from exc
        # IMPORTANT: we never return the value. We return only metadata.
        return {
            "id": secret_ref.id.value,
            "project_id": secret_ref.project_id.value,
            "name": secret_ref.name,
            "secret_type": secret_ref.secret_type,
            "created_at": secret_ref.created_at,
            "revoked_at": secret_ref.revoked_at,
        }

    @app.get("/projects/{project_id}/secrets", tags=["secrets"])
    def list_secrets(request: Request, project_id: str) -> list[dict[str, Any]]:

        refs = services.secrets.list_for_project(
            project_id=ProjectId(project_id),
            actor_id=request_project_actor(request, services, project_id),
            source="web",
        )
        return [
            {
                "id": r.id.value,
                "project_id": r.project_id.value,
                "name": r.name,
                "secret_type": r.secret_type,
                "created_at": r.created_at,
                "revoked_at": r.revoked_at,
            }
            for r in refs
        ]

    @app.post(
        "/projects/{project_id}/secrets/{secret_id}/revoke",
        tags=["secrets"],
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def revoke_secret(request: Request, project_id: str, secret_id: str):
        from zero.domain.secrets import SecretReferenceId

        try:
            services.secrets.revoke(
                project_id=ProjectId(project_id),
                secret_id=SecretReferenceId(secret_id),
                actor_id=request_project_actor(request, services, project_id),
                source="web",
            )
        except (SecretError, ValueError):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request failed")
