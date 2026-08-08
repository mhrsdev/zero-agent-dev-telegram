"""HTTP boundary — FastAPI router and endpoint definitions.

Per ``zero-modular-bootstrap`` §"One executable path is a design
asset": the smoke test starts the same ASGI app intended for later
deployment, using isolated configuration and persistence. The HTTP
boundary is therefore not a mock — it exercises the real persistence
layer through the application operations.

Per ``zero-interface-adapter-model`` §"The website is primary, not
privileged around policy": the HTTP layer is an adapter. It translates
HTTP requests into application operations and translates domain
results back into HTTP responses. It owns no business truth.

Per ``zero-control-plane-trust`` §"UI controls are not security": UI
visibility is a usability concern, not a security control. Every
protected mutation reaches a backend operation that revalidates
actor, project, revision, and transition.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from zero import __version__
from zero.app.auth_service import (
    AuthenticationError,
    BootstrapError,
    authenticated_actor,
    bind_actor,
    request_actor,
    reset_actor,
)
from zero.app.health import HealthService
from zero.app.services import Services, build_services
from zero.config import Settings
from zero.domain.authorization import AuthorizationError
from zero.domain.execution import ExecutionError
from zero.domain.identity import (
    IdentityError,
    MembershipAlreadyExistsError,
    MembershipNotFoundError,
    ProjectId,
    ProjectNotFoundError,
    UserId,
    UserNotFoundError,
)
from zero.domain.plans import PlanError
from zero.domain.secrets import SecretError
from zero.domain.tools import ToolError
from zero.persistence.connection import Database
from zero.persistence.migrations import (
    apply_migrations,
    count_applied_migrations,
)

# ----------------------------------------------------------------------
# Request/response models (Pydantic v2)
# ----------------------------------------------------------------------


class CreateUserRequest(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=200)


class BootstrapRequest(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=200)
    existing_user_id: str | None = None


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


class StoreSecretRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    secret_type: str = Field(..., pattern="^(api_key|token|password|other)$")
    value: str = Field(..., min_length=1, repr=False)  # never repr'd in logs


class GrantToolRequest(BaseModel):
    tool_id: str
    agent_scope: str = Field(
        ..., pattern="^(main_planner|main_worker|sub_agent_type|integration)$"
    )
    max_invocations: int | None = None
    timeout_seconds: int | None = None


class InvokeToolRequest(BaseModel):
    tool_name: str
    input_data: dict[str, Any]
    agent_scope: str = Field(
        ..., pattern="^(main_planner|main_worker|sub_agent_type|integration)$"
    )


# ----------------------------------------------------------------------
# App factory
# ----------------------------------------------------------------------


def create_app(settings: Settings) -> FastAPI:
    """Wire concrete implementations together and return the ASGI app."""
    database = Database(settings)
    apply_migrations(database)
    services = build_services(settings, database)
    health_service = HealthService(
        version=__version__,
        environment=settings.zero_env,
        database=database,
        migration_counter=count_applied_migrations,
    )

    app = FastAPI(
        title="Zero Develop",
        version=__version__,
        description=(
            "Multi-agent control plane for concurrent software "
            "development. Phase 7: primary website vertical slices."
        ),
    )
    app.state.settings = settings
    app.state.database = database
    app.state.services = services
    app.state.health_service = health_service

    @app.exception_handler(AuthorizationError)
    async def authorization_denied(
        _request: Request, exc: AuthorizationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"detail": str(exc)},
        )

    _register_auth_middleware(app, services, settings)

    # Serve static files (CSS, JS).
    from pathlib import Path

    from fastapi.staticfiles import StaticFiles

    static_dir = Path(__file__).parent.parent / "web" / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    _register_health_routes(app, health_service)
    _register_auth_routes(app, services)
    _register_identity_routes(app, services)
    _register_authorization_routes(app, services)
    _register_secret_routes(app, services)
    _register_tool_routes(app, services)
    _register_audit_routes(app, services)
    _register_plan_routes(app, services)
    _register_execution_routes(app, services)

    # Register the web controller (HTML pages).
    from zero.web.controller import create_web_router

    app.include_router(create_web_router(services, settings))

    return app


# ----------------------------------------------------------------------
# Authentication boundary
# ----------------------------------------------------------------------


_PUBLIC_PATHS = {
    "/",
    "/healthz",
    "/readyz",
    "/openapi.json",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
    "/auth/bootstrap",
    "/web/login",
}
_PROJECT_PATH = re.compile(r"^/(?:web/)?projects/([^/]+)")


def _request_project_actor(
    request: Request, services: Services, project_id: str
) -> UserId:
    try:
        project = services.identity.get_project(ProjectId(project_id))
    except (ProjectNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from exc
    return getattr(request.state, "user_id", project.owner_user_id)


def _register_auth_middleware(
    app: FastAPI, services: Services, settings: Settings
) -> None:
    @app.middleware("http")
    async def authenticate_request(request: Request, call_next):
        if not settings.auth_required:
            return await call_next(request)
        path = request.url.path
        if path in _PUBLIC_PATHS or path.startswith("/static/"):
            return await call_next(request)

        authorization = request.headers.get("authorization", "")
        token_source = "bearer"
        if authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
        else:
            token = request.cookies.get("zero_access_token", "")
            token_source = "cookie"
        try:
            actor_id = services.auth.authenticate(token)
        except AuthenticationError:
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Authentication required"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        request.state.user_id = actor_id
        request.state.access_token = token
        actor_context = bind_actor(actor_id)
        try:
            if token_source == "cookie" and request.method not in {
                "GET", "HEAD", "OPTIONS"
            }:
                origin = request.headers.get("origin")
                expected_origin = (
                    f"{request.url.scheme}://{request.headers.get('host', '')}"
                )
                if origin != expected_origin:
                    return JSONResponse(
                        status_code=status.HTTP_403_FORBIDDEN,
                        content={"detail": "Cross-origin mutation refused"},
                    )

            match = _PROJECT_PATH.match(path)
            if match:
                try:
                    services.authorization.require_permission(
                        actor_id=actor_id,
                        project_id=ProjectId(match.group(1)),
                        permission="project.view",
                        source="web",
                    )
                except (AuthorizationError, ValueError):
                    return JSONResponse(
                        status_code=status.HTTP_404_NOT_FOUND,
                        content={"detail": "Project not found"},
                    )
            return await call_next(request)
        finally:
            reset_actor(actor_context)


def _register_auth_routes(app: FastAPI, services: Services) -> None:
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
        except AuthenticationError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
            )
        except BootstrapError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
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
    def revoke_current_token(request: Request) -> None:
        services.auth.revoke(request.state.access_token, request_actor(request))


# ----------------------------------------------------------------------
# Health routes
# ----------------------------------------------------------------------


def _register_health_routes(app: FastAPI, health_service: HealthService) -> None:
    @app.get("/healthz", tags=["health"])
    def healthz() -> dict[str, Any]:
        report = health_service.report()
        return report.to_dict()

    @app.get("/readyz", tags=["health"])
    def readyz() -> JSONResponse:
        report = health_service.report()
        if report.status == "ok":
            return JSONResponse(
                status_code=status.HTTP_200_OK, content=report.to_dict()
            )
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=report.to_dict(),
        )

    @app.get("/", tags=["root"])
    def root() -> dict[str, str]:
        return {
            "name": "Zero Develop",
            "version": __version__,
            "environment": app.state.settings.zero_env,
            "docs": "/docs",
            "health": "/healthz",
        }


# ----------------------------------------------------------------------
# Identity routes
# ----------------------------------------------------------------------


def _register_identity_routes(app: FastAPI, services: Services) -> None:

    @app.post("/users", tags=["identity"], status_code=status.HTTP_201_CREATED)
    def create_user(req: CreateUserRequest) -> dict[str, Any]:
        user = services.identity.create_user(
            display_name=req.display_name, source="web"
        )
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
        except (UserNotFoundError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            )
        return {
            "id": user.id.value,
            "display_name": user.display_name,
            "status": user.status,
            "created_at": user.created_at,
        }

    @app.post(
        "/projects", tags=["identity"], status_code=status.HTTP_201_CREATED
    )
    def create_project(req: CreateProjectRequest) -> dict[str, Any]:

        try:
            project = services.identity.create_project(
                owner_id=authenticated_actor(req.owner_id),
                name=req.name,
                source="web",
            )
        except UserNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            )
        except IdentityError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            )
        return {
            "id": project.id.value,
            "name": project.name,
            "owner_user_id": project.owner_user_id.value,
            "created_at": project.created_at,
        }

    @app.get("/projects/{project_id}", tags=["identity"])
    def get_project(project_id: str) -> dict[str, Any]:
        from zero.domain.identity import ProjectId

        try:
            project = services.identity.get_project(ProjectId(project_id))
        except (ProjectNotFoundError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            )
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
        from zero.domain.identity import ProjectId, UserId

        try:
            project = services.identity.get_project(ProjectId(project_id))
            membership = services.identity.add_member(
                project_id=project.id,
                actor_id=authenticated_actor(project.owner_user_id.value),
                member_id=UserId(req.member_id),
                role=req.role,  # type: ignore[arg-type]
                source="web",
            )
        except UserNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            )
        except ProjectNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            )
        except MembershipAlreadyExistsError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(exc)
            )
        except IdentityError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            )
        return {
            "project_id": membership.project_id.value,
            "user_id": membership.user_id.value,
            "role": membership.role,
            "created_at": membership.created_at,
        }

    @app.get("/projects/{project_id}/members", tags=["identity"])
    def list_members(request: Request, project_id: str) -> list[dict[str, Any]]:
        from zero.domain.identity import ProjectId

        try:
            project = services.identity.get_project(ProjectId(project_id))
            memberships = services.identity.list_members(
                project.id,
                getattr(request.state, "user_id", project.owner_user_id),
            )
        except ProjectNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            )
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
    def remove_member(project_id: str, user_id: str) -> None:
        from zero.domain.identity import ProjectId, UserId

        try:
            project = services.identity.get_project(ProjectId(project_id))
            services.identity.remove_member(
                project_id=project.id,
                actor_id=authenticated_actor(project.owner_user_id.value),
                member_id=UserId(user_id),
                source="web",
            )
        except (ProjectNotFoundError, MembershipNotFoundError) as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            )

    @app.post(
        "/users/{user_id}/external-identities",
        tags=["identity"],
        status_code=status.HTTP_201_CREATED,
    )
    def link_external_identity(
        user_id: str, req: LinkExternalIdentityRequest
    ) -> dict[str, Any]:
        try:
            identity = services.identity.link_external_identity(
                user_id=authenticated_actor(user_id),
                platform=req.platform,  # type: ignore[arg-type]
                external_id=req.external_id,
                external_username=req.external_username,
                verified=False,
                source="web",
            )
        except UserNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            )
        except IdentityError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            )
        return {
            "id": identity.id.value,
            "user_id": identity.user_id.value,
            "platform": identity.platform,
            "external_username": identity.external_username,
            "verified_at": identity.verified_at,
            "created_at": identity.created_at,
        }


# ----------------------------------------------------------------------
# Authorization routes
# ----------------------------------------------------------------------


def _register_authorization_routes(app: FastAPI, services: Services) -> None:
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
        from zero.domain.identity import ProjectId

        try:
            decision = services.authorization.authorize(
                actor_id=authenticated_actor(actor_id),
                project_id=ProjectId(project_id),
                permission=permission,  # type: ignore[arg-type]
                source="web",
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            )
        return {
            "allowed": decision.allowed,
            "actor_id": decision.actor_id.value
            if decision.actor_id
            else None,
            "project_id": decision.project_id.value,
            "permission": decision.permission,
            "role": decision.role,
            "reason": decision.reason,
        }


# ----------------------------------------------------------------------
# Secret routes
# ----------------------------------------------------------------------


def _register_secret_routes(app: FastAPI, services: Services) -> None:
    @app.post(
        "/projects/{project_id}/secrets",
        tags=["secrets"],
        status_code=status.HTTP_201_CREATED,
    )
    def store_secret(
        request: Request, project_id: str, req: StoreSecretRequest
    ) -> dict[str, Any]:
        from zero.domain.identity import ProjectId

        try:
            secret_ref = services.secrets.store(
                project_id=ProjectId(project_id),
                name=req.name,
                secret_type=req.secret_type,  # type: ignore[arg-type]
                value=req.value,
                actor_id=_request_project_actor(request, services, project_id),
                source="web",
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            )
        except SecretError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to store secret",
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
        from zero.domain.identity import ProjectId

        refs = services.secrets.list_for_project(
            project_id=ProjectId(project_id),
            actor_id=_request_project_actor(request, services, project_id),
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
    def revoke_secret(request: Request, project_id: str, secret_id: str) -> None:
        from zero.domain.identity import ProjectId
        from zero.domain.secrets import SecretReferenceId

        try:
            services.secrets.revoke(
                project_id=ProjectId(project_id),
                secret_id=SecretReferenceId(secret_id),
                actor_id=_request_project_actor(request, services, project_id),
                source="web",
            )
        except (SecretError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            )


# ----------------------------------------------------------------------
# Tool routes
# ----------------------------------------------------------------------


def _register_tool_routes(app: FastAPI, services: Services) -> None:
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
    def grant_tool(
        request: Request, project_id: str, req: GrantToolRequest
    ) -> dict[str, Any]:
        from zero.domain.identity import ProjectId
        from zero.domain.tools import ToolId

        try:
            grant = services.tools.grant_tool(
                project_id=ProjectId(project_id),
                actor_id=_request_project_actor(request, services, project_id),
                tool_id=ToolId(req.tool_id),
                agent_scope=req.agent_scope,  # type: ignore[arg-type]
                max_invocations=req.max_invocations,
                timeout_seconds=req.timeout_seconds,
                source="web",
            )
        except (ToolError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            )
        return {
            "id": grant.id.value,
            "project_id": grant.project_id.value,
            "tool_id": grant.tool_id.value,
            "agent_scope": grant.agent_scope,
            "max_invocations": grant.max_invocations,
            "timeout_seconds": grant.timeout_seconds,
        }

    @app.post(
        "/projects/{project_id}/tool-invocations",
        tags=["tools"],
    )
    def invoke_tool(
        request: Request, project_id: str, req: InvokeToolRequest
    ) -> dict[str, Any]:
        from zero.domain.identity import ProjectId

        try:
            result = services.tools.invoke(
                project_id=ProjectId(project_id),
                actor_id=_request_project_actor(request, services, project_id),
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
                    detail={"error": str(exc), "errors": exc.errors},
                )
            if isinstance(exc, ToolInvocationDeniedError):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
                )
            if isinstance(exc, ToolNotFoundError):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
                )
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


# ----------------------------------------------------------------------
# Audit routes
# ----------------------------------------------------------------------


def _register_audit_routes(app: FastAPI, services: Services) -> None:
    @app.get("/projects/{project_id}/audit", tags=["audit"])
    def list_audit_events(
        request: Request,
        project_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        from zero.domain.identity import ProjectId

        events = services.audit.list_for_project(
            project_id=ProjectId(project_id),
            actor_id=_request_project_actor(request, services, project_id),
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


# ----------------------------------------------------------------------
# Plan routes (Phase 3, M4)
# ----------------------------------------------------------------------


class IngestConversationRequest(BaseModel):
    actor_id: str
    source: str = Field(..., pattern="^(web|telegram|discord|system|internal)$")
    origin_kind: str = Field(
        ...,
        pattern="^(authenticated_human|planner_injection|system_reminder|compaction_carrier|tool_result|auto_continue)$",
    )
    content: str = Field(..., min_length=1)
    external_event_id: str | None = None


class CreatePlanRequest(BaseModel):
    actor_id: str


class ProposeRevisionRequest(BaseModel):
    actor_id: str
    objective: str = Field(..., min_length=1)
    scope: list[str] = []
    constraints: list[str] = []
    acceptance_criteria: list[str] = []
    risks: list[str] = []
    unresolved_questions: list[str] = []
    source_event_ids: list[str] = Field(..., min_length=1)


class ApproveRevisionRequest(BaseModel):
    actor_id: str
    expected_revision_number: int = Field(..., ge=1)
    idempotency_key: str = Field(..., min_length=1)
    redacted_reason: str | None = None


class RejectRevisionRequest(BaseModel):
    actor_id: str
    expected_revision_number: int = Field(..., ge=1)
    idempotency_key: str = Field(..., min_length=1)
    redacted_reason: str | None = None


def _register_plan_routes(app: FastAPI, services: Services) -> None:
    @app.post(
        "/projects/{project_id}/conversation-events",
        tags=["plans"],
        status_code=status.HTTP_201_CREATED,
    )
    def ingest_conversation_event(
        project_id: str, req: IngestConversationRequest
    ) -> dict[str, Any]:
        from zero.domain.identity import ProjectId

        try:
            event = services.plans.ingest_conversation_event(
                project_id=ProjectId(project_id),
                actor_id=authenticated_actor(req.actor_id),
                source=req.source,  # type: ignore[arg-type]
                origin_kind=req.origin_kind,  # type: ignore[arg-type]
                content=req.content,
                external_event_id=req.external_event_id,
            )
        except (PlanError, ValueError) as exc:
            from zero.domain.plans import DuplicateConversationEventError

            if isinstance(exc, DuplicateConversationEventError):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail=str(exc)
                )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            )
        return {
            "id": event.id.value,
            "project_id": event.project_id.value,
            "actor_id": event.actor_id.value,
            "source": event.source,
            "origin_kind": event.origin_kind,
            "is_authenticated_human": event.is_authenticated_human,
            "created_at": event.created_at,
        }

    @app.post(
        "/projects/{project_id}/plans",
        tags=["plans"],
        status_code=status.HTTP_201_CREATED,
    )
    def create_plan(project_id: str, req: CreatePlanRequest) -> dict[str, Any]:
        from zero.domain.identity import ProjectId

        try:
            plan = services.plans.create_plan(
                project_id=ProjectId(project_id),
                actor_id=authenticated_actor(req.actor_id),
            )
        except (PlanError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            )
        return {
            "id": plan.id.value,
            "project_id": plan.project_id.value,
            "current_state": plan.current_state,
            "current_revision_number": plan.current_revision_number,
            "created_at": plan.created_at,
        }

    @app.post(
        "/projects/{project_id}/plans/{plan_id}/revisions",
        tags=["plans"],
        status_code=status.HTTP_201_CREATED,
    )
    def propose_revision(
        project_id: str, plan_id: str, req: ProposeRevisionRequest
    ) -> dict[str, Any]:
        from zero.domain.plans import (
            ConversationEventId,
            PlanId,
            PlanRevisionContent,
        )

        try:
            content = PlanRevisionContent(
                objective=req.objective,
                scope=tuple(req.scope),
                constraints=tuple(req.constraints),
                acceptance_criteria=tuple(req.acceptance_criteria),
                risks=tuple(req.risks),
                unresolved_questions=tuple(req.unresolved_questions),
                source_event_ids=tuple(
                    ConversationEventId(eid) for eid in req.source_event_ids
                ),
            )
            revision = services.plans.propose_revision(
                plan_id=PlanId(plan_id),
                actor_id=authenticated_actor(req.actor_id),
                content=content,
            )
        except (PlanError, ValueError) as exc:
            from zero.domain.plans import (
                PlanContentValidationError,
                PlanNotFoundError,
            )

            if isinstance(exc, PlanContentValidationError):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"error": str(exc), "errors": exc.errors},
                )
            if isinstance(exc, PlanNotFoundError):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
                )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            )
        return {
            "id": revision.id.value,
            "plan_id": revision.plan_id.value,
            "revision_number": revision.revision_number,
            "state": revision.state,
            "objective": revision.content.objective,
            "created_at": revision.created_at,
        }

    @app.post(
        "/projects/{project_id}/plans/{plan_id}/approve",
        tags=["plans"],
    )
    def approve_revision(
        project_id: str, plan_id: str, req: ApproveRevisionRequest
    ) -> dict[str, Any]:
        from zero.domain.plans import PlanId, StaleRevisionError

        try:
            approval, handoff = services.plans.approve_revision(
                plan_id=PlanId(plan_id),
                actor_id=authenticated_actor(req.actor_id),
                expected_revision_number=req.expected_revision_number,
                idempotency_key=req.idempotency_key,
                redacted_reason=req.redacted_reason,
            )
        except StaleRevisionError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": str(exc),
                    "expected_revision": exc.expected_revision,
                    "actual_revision": exc.actual_revision,
                },
            )
        except (PlanError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            )
        return {
            "approval": {
                "id": approval.id.value,
                "result": approval.result,
                "approved_by": approval.approved_by.value,
                "created_at": approval.created_at,
            },
            "handoff": {
                "id": handoff.id.value,
                "revision_id": handoff.revision_id.value,
                "execution_id": handoff.execution_id,
                "created_at": handoff.created_at,
            },
        }

    @app.post(
        "/projects/{project_id}/plans/{plan_id}/reject",
        tags=["plans"],
    )
    def reject_revision(
        project_id: str, plan_id: str, req: RejectRevisionRequest
    ) -> dict[str, Any]:
        from zero.domain.plans import PlanId, StaleRevisionError

        try:
            approval = services.plans.reject_revision(
                plan_id=PlanId(plan_id),
                actor_id=authenticated_actor(req.actor_id),
                expected_revision_number=req.expected_revision_number,
                idempotency_key=req.idempotency_key,
                redacted_reason=req.redacted_reason,
            )
        except StaleRevisionError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": str(exc),
                    "expected_revision": exc.expected_revision,
                    "actual_revision": exc.actual_revision,
                },
            )
        except (PlanError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            )
        return {
            "approval": {
                "id": approval.id.value,
                "result": approval.result,
                "approved_by": approval.approved_by.value,
                "created_at": approval.created_at,
            }
        }

    @app.get("/projects/{project_id}/plans", tags=["plans"])
    def list_plans(project_id: str) -> list[dict[str, Any]]:
        from zero.domain.identity import ProjectId

        plans = services.plans.list_plans_for_project(ProjectId(project_id))
        return [
            {
                "id": p.id.value,
                "current_state": p.current_state,
                "current_revision_number": p.current_revision_number,
                "created_at": p.created_at,
            }
            for p in plans
        ]

    @app.get("/projects/{project_id}/plans/{plan_id}/revisions", tags=["plans"])
    def list_revisions(project_id: str, plan_id: str) -> list[dict[str, Any]]:
        from zero.domain.plans import PlanId

        typed_plan_id = PlanId(plan_id)
        plan = services.plans.get_plan(typed_plan_id)
        if plan.project_id != ProjectId(project_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        revisions = services.plans.list_revisions(typed_plan_id)
        return [
            {
                "id": r.id.value,
                "revision_number": r.revision_number,
                "state": r.state,
                "objective": r.content.objective,
                "created_at": r.created_at,
            }
            for r in revisions
        ]


# ----------------------------------------------------------------------
# Execution routes (Phase 3, M5)
# ----------------------------------------------------------------------


class TaskSpecModel(BaseModel):
    key: str | None = None
    objective: str = Field(..., min_length=1)
    permitted_scope: list[str] = []
    expected_evidence: list[str] = []


class DependencySpecModel(BaseModel):
    task_key: str
    depends_on_key: str


class CreateExecutionRequest(BaseModel):
    actor_id: str
    task_specs: list[TaskSpecModel] = Field(..., min_length=1)
    dependency_specs: list[DependencySpecModel] = []


def _register_execution_routes(app: FastAPI, services: Services) -> None:
    from zero.domain.execution import ExecutionId

    def execution_in_project(project_id: str, execution_id: str):
        try:
            execution = services.worker.get_execution(ExecutionId(execution_id))
        except (ExecutionError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            ) from exc
        if execution.project_id != ProjectId(project_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return execution

    @app.post(
        "/projects/{project_id}/handoffs/{handoff_id}/executions",
        tags=["executions"],
        status_code=status.HTTP_201_CREATED,
    )
    def create_execution(
        project_id: str, handoff_id: str, req: CreateExecutionRequest
    ) -> dict[str, Any]:
        from zero.app.worker_service import DependencySpec, TaskSpec
        from zero.domain.plans import PlanHandoffId

        try:
            task_specs = [
                TaskSpec(
                    key=ts.key,
                    objective=ts.objective,
                    permitted_scope=tuple(ts.permitted_scope),
                    expected_evidence=tuple(ts.expected_evidence),
                )
                for ts in req.task_specs
            ]
            dep_specs = [
                DependencySpec(
                    task_key=d.task_key, depends_on_key=d.depends_on_key
                )
                for d in req.dependency_specs
            ]
            execution = services.worker.create_execution_from_handoff(
                handoff_id=PlanHandoffId(handoff_id),
                actor_id=authenticated_actor(req.actor_id),
                task_specs=task_specs,
                dependency_specs=dep_specs,
            )
        except (ExecutionError, PlanError, ValueError) as exc:
            from zero.domain.execution import (
                CycleError,
                MissingDependencyError,
                PlanNotApprovedError,
            )
            from zero.domain.plans import PlanNotFoundError

            if isinstance(exc, (CycleError, MissingDependencyError)):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
                )
            if isinstance(exc, PlanNotApprovedError):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail=str(exc)
                )
            if isinstance(exc, PlanNotFoundError):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
                )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            )
        return {
            "id": execution.id.value,
            "plan_id": execution.plan_id.value,
            "state": execution.state,
            "created_at": execution.created_at,
        }

    @app.get(
        "/projects/{project_id}/executions/{execution_id}",
        tags=["executions"],
    )
    def get_execution(project_id: str, execution_id: str) -> dict[str, Any]:
        execution = execution_in_project(project_id, execution_id)
        return {
            "id": execution.id.value,
            "plan_id": execution.plan_id.value,
            "state": execution.state,
            "blocker_reason": execution.blocker_reason,
            "created_at": execution.created_at,
            "updated_at": execution.updated_at,
        }

    @app.get(
        "/projects/{project_id}/executions/{execution_id}/tasks",
        tags=["executions"],
    )
    def list_execution_tasks(
        project_id: str, execution_id: str
    ) -> list[dict[str, Any]]:
        execution_in_project(project_id, execution_id)
        try:
            tasks = services.worker.list_tasks(ExecutionId(execution_id))
        except (ExecutionError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            )
        return [
            {
                "id": t.id.value,
                "objective": t.objective,
                "state": t.state,
                "blocker_reason": t.blocker_reason,
                "terminal_state_set_at": t.terminal_state_set_at,
                "created_at": t.created_at,
            }
            for t in tasks
        ]

    @app.get(
        "/projects/{project_id}/executions/{execution_id}/ready-tasks",
        tags=["executions"],
    )
    def list_ready_tasks(
        project_id: str, execution_id: str
    ) -> list[dict[str, Any]]:
        execution_in_project(project_id, execution_id)
        try:
            tasks = services.worker.list_ready_tasks(ExecutionId(execution_id))
        except (ExecutionError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            )
        return [
            {"id": t.id.value, "objective": t.objective, "state": t.state}
            for t in tasks
        ]

    @app.post(
        "/projects/{project_id}/executions/{execution_id}/cancel",
        tags=["executions"],
    )
    def cancel_execution(
        project_id: str,
        execution_id: str,
        actor_id: str = "",
    ) -> dict[str, Any]:
        execution_in_project(project_id, execution_id)
        try:
            execution = services.worker.cancel_execution(
                execution_id=ExecutionId(execution_id),
                actor_id=authenticated_actor(actor_id),
            )
        except (ExecutionError, ValueError) as exc:
            from zero.domain.execution import InvalidExecutionTransitionError

            if isinstance(exc, InvalidExecutionTransitionError):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail=str(exc)
                )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            )
        return {
            "id": execution.id.value,
            "state": execution.state,
        }

    @app.post(
        "/projects/{project_id}/executions/{execution_id}/recover",
        tags=["executions"],
    )
    def recover_execution(
        project_id: str,
        execution_id: str,
        actor_id: str = "",
    ) -> dict[str, Any]:
        execution_in_project(project_id, execution_id)
        try:
            execution = services.worker.recover_after_restart(
                execution_id=ExecutionId(execution_id),
                actor_id=authenticated_actor(actor_id),
            )
        except (ExecutionError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            )
        return {
            "id": execution.id.value,
            "state": execution.state,
            "blocker_reason": execution.blocker_reason,
        }
