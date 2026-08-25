"""Auth routes extracted from app.api."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from zero.app.auth_service import (
    AuthenticationError,
    BootstrapError,
    request_actor,
)
from zero.app.services import Services
from zero.domain.identity import (
    UserId,
)


class BootstrapRequest(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=200)
    existing_user_id: str | None = None


def register_auth_routes(app: FastAPI, services: Services) -> None:
    @app.post(
        "/auth/bootstrap",
        tags=["auth"],
        status_code=status.HTTP_201_CREATED,
    )
    def bootstrap(req: BootstrapRequest, request: Request) -> JSONResponse:
        try:
            existing = UserId(req.existing_user_id) if req.existing_user_id else None
            user, token, expires_at = services.auth.bootstrap(
                display_name=req.display_name,
                supplied_secret=request.headers.get("x-zero-bootstrap-token", ""),
                existing_user_id=existing,
            )
        except AuthenticationError:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="request failed")
        except BootstrapError:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="request failed")
        response = JSONResponse(
            status_code=status.HTTP_201_CREATED,
            content={
                "user": {
                    "id": user.id.value,
                    "display_name": user.display_name,
                    "status": user.status,
                },
                "access_token": token,
                "token_type": "bearer",
                "expires_at": expires_at,
            },
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/auth/tokens", tags=["auth"], status_code=status.HTTP_201_CREATED)
    def issue_token(request: Request) -> JSONResponse:
        actor = request_actor(request)
        token, expires_at = services.auth.issue_access_token(actor)
        response = JSONResponse(
            status_code=status.HTTP_201_CREATED,
            content={
                "access_token": token,
                "token_type": "bearer",
                "expires_at": expires_at,
            },
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.delete("/auth/tokens/current", tags=["auth"], status_code=204)
    def revoke_current_token(request: Request):
        services.auth.revoke(request.state.access_token, request_actor(request))
