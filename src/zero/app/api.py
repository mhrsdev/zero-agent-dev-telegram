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

import os
import re
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from zero import __version__
from zero.adapters.messaging import UnsupportedUpdateError, WebhookAuthError
from zero.app.auth_service import (
    AuthenticationError,
    BootstrapError,
    authenticated_actor,
    bind_actor,
    request_actor,
    reset_actor,
)
from zero.app.background_workers import BackgroundWorkerHost
from zero.app.capabilities import capabilities_payload
from zero.app.health import HealthService
from zero.app.interface_transport_service import (
    InterfaceScopeError,
    InterfaceTransportNotConfigured,
)
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
from zero.domain.interfaces import InterfaceBindingId
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
    agent_scope: str = Field(..., pattern="^(main_planner|main_worker|sub_agent_type|integration)$")
    max_invocations: int | None = None
    timeout_seconds: int | None = None


class InvokeToolRequest(BaseModel):
    tool_name: str
    input_data: dict[str, Any]
    agent_scope: str = Field(..., pattern="^(main_planner|main_worker|sub_agent_type|integration)$")


class _StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StoreArtifactRequest(_StrictRequest):
    kind: str = Field(..., min_length=1, max_length=40)
    content: str = Field(..., min_length=1)
    producer: str | None = Field(default=None, max_length=200)
    provenance: str | None = None
    media_type: str = Field(default="text/plain", max_length=100)


class StoreRagDocumentRequest(_StrictRequest):
    source_type: str = Field(..., min_length=1, max_length=40)
    source_id: str = Field(..., min_length=1, max_length=200)
    title: str = Field(..., min_length=1, max_length=500)
    content: str = Field(..., min_length=1)
    state: str = Field(default="candidate", pattern="^(candidate|approved)$")


class CreateAgentTypeRequest(_StrictRequest):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    name: str = Field(..., min_length=1, max_length=200)
    responsibility: str = Field(..., min_length=1, max_length=2000)
    memory_scope: str = Field(default="", max_length=2000)
    permitted_tools: list[str] = Field(default_factory=list)
    model_policy: dict[str, str] = Field(default_factory=dict)
    context_budget_tokens: int = Field(default=100000, ge=1)
    max_concurrent_instances: int = Field(default=1, ge=1)


class AddKnowledgeRequest(_StrictRequest):
    kind: str = Field(..., min_length=1, max_length=40)
    content: str = Field(..., min_length=1)
    provenance: str | None = None
    state: str = Field(default="approved", pattern="^(candidate|approved)$")


class RegisterRepositoryRequest(_StrictRequest):
    name: str = Field(..., min_length=1, max_length=200)
    local_path: str = Field(..., min_length=1, max_length=4096)
    default_base_revision: str | None = None


class CreateInterfaceBindingRequest(_StrictRequest):
    platform: str = Field(..., pattern="^(telegram|discord|other)$")
    chat_id: str = Field(..., min_length=1, max_length=200)
    topic_id: str | None = Field(default=None, max_length=200)
    bot_token_ref: str | None = Field(default=None, max_length=200)
    is_enabled: bool = False


class CreateIntegrationReviewRequest(_StrictRequest):
    execution_id: str = Field(..., min_length=1)
    source_task_ids: list[str] = Field(..., min_length=1)


class CreateMergeProposalRequest(_StrictRequest):
    review_id: str = Field(..., min_length=1)
    execution_id: str = Field(..., min_length=1)
    source_tasks: list[str] = Field(..., min_length=1)
    source_diffs: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


class ReconcileProviderRequestRequest(_StrictRequest):
    resolution: str = Field(
        ...,
        pattern="^(confirmed_not_dispatched|confirmed_dispatched)$",
    )
    note: str = Field(default="", max_length=500)


# ----------------------------------------------------------------------
# App factory
# ----------------------------------------------------------------------


def build_application_services(
    settings: Settings,
    database: Database | None = None,
):
    """Build the service bundle and run startup recovery.

    Shared by :func:`create_app` and the operational CLI so there is
    exactly one composition path.
    """
    if database is None:
        database = Database(settings)
    apply_migrations(database)
    services = build_services(settings, database)
    if not settings.is_test:
        # Recovery is part of the real startup contract. Unknown provider
        # outcomes remain explicitly unreplayed; active execution/worktree
        # recovery is fenced by the worker's project/actor checks.
        services.recovery.run_all_recovery()
    return database, services


def create_app(settings: Settings) -> FastAPI:
    """Wire concrete implementations together and return the ASGI app."""
    database, services = build_application_services(settings)
    health_service = HealthService(
        version=__version__,
        environment=settings.zero_env,
        database=database,
        migration_counter=count_applied_migrations,
    )
    worker_host = BackgroundWorkerHost(settings, services)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await worker_host.start()
        try:
            yield
        finally:
            await worker_host.stop()

    app = FastAPI(
        title="Zero Develop",
        version=__version__,
        description=(
            "Multi-agent control plane for concurrent software "
            "development. Phase 7: primary website vertical slices."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.database = database
    app.state.services = services
    app.state.health_service = health_service
    app.state.worker_host = worker_host

    @app.exception_handler(AuthorizationError)
    async def authorization_denied(_request: Request, exc: AuthorizationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"detail": "request failed"},
        )

    _register_auth_middleware(app, services, settings)

    # Serve static files (CSS, JS).
    from pathlib import Path

    from fastapi.staticfiles import StaticFiles

    static_dir = Path(__file__).parent.parent / "web" / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    _register_health_routes(app, health_service, services=services, settings=settings)
    _register_auth_routes(app, services)
    _register_identity_routes(app, services)
    _register_authorization_routes(app, services)
    _register_secret_routes(app, services)
    _register_tool_routes(app, services)
    _register_audit_routes(app, services)
    _register_plan_routes(app, services)
    _register_execution_routes(app, services)
    _register_artifact_routes(app, services)
    _register_topology_routes(app, services)
    _register_provider_routes(app, services)
    _register_worktree_routes(app, services)
    _register_integration_routes(app, services)
    _register_interface_routes(app, services)
    _register_webhook_routes(app, services)

    # Register the web controller (HTML pages).
    from zero.web.controller import create_web_router

    app.include_router(create_web_router(services, settings))

    if services.interface_transports is not None:
        app.router.add_event_handler("shutdown", services.interface_transports.close)

    # Management layer: local admin GUI (loopback-first) + backup daemon.
    manage_enabled = os.environ.get("ZERO_MANAGE_GUI", "1") != "0"
    if settings.zero_env != "test" and manage_enabled:
        try:
            from zero.manage.web import register_admin

            register_admin(app, services)
        except ImportError:  # pragma: no cover - manage layer optional
            pass
        try:
            from pathlib import Path as _P

            from zero.manage.core.config import ConfigService
            from zero.manage.services.backup_daemon import BackupDaemon

            home = _P(os.environ.get("ZERO_HOME", Path.home() / ".zero"))
            cfgsvc = ConfigService(home)
            mcfg = cfgsvc.load() if cfgsvc.exists() else None
            if mcfg is not None and mcfg.backups.schedule != "off":

                def _runner() -> str:
                    ts = time.strftime("%Y%m%d-%H%M%S")
                    dest = home / "backups"
                    dest.mkdir(parents=True, exist_ok=True)
                    archive = dest / f"zero-backup-{ts}.enc"
                    services.backup.backup_to_file(str(archive))
                    return str(archive)

                daemon = BackupDaemon(
                    home=home,
                    schedule=mcfg.backups.schedule,
                    retention=mcfg.backups.retention,
                    backup_runner=_runner,
                )
                thread, stop_ev = daemon.start_thread()
                app.state.backup_daemon = daemon

                @app.router.on_shutdown
                async def _stop_backup_daemon() -> None:
                    stop_ev.set()
                    thread.join(timeout=5)
        except Exception as exc:  # noqa: BLE001 - management must never break boot
            import logging as _logging

            _logging.getLogger(__name__).warning(
                "management layer init skipped: %s", type(exc).__name__
            )

    return app


# ----------------------------------------------------------------------
# Authentication boundary
# ----------------------------------------------------------------------


_PUBLIC_PATHS = {
    "/",
    "/healthz",
    "/readyz",
    "/capabilities",
    "/openapi.json",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
    "/auth/bootstrap",
    "/web/login",
}
_PROJECT_PATH = re.compile(r"^/(?:web/)?projects/([^/]+)")


def _request_project_actor(request: Request, services: Services, project_id: str) -> UserId:
    try:
        project = services.identity.get_project(ProjectId(project_id))
    except (ProjectNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from exc
    return getattr(request.state, "user_id", project.owner_user_id)


def _authorized_actor(
    request: Request,
    services: Services,
    project_id: str,
    permission: str,
) -> UserId:
    """Resolve the request principal and authorize the target project."""
    actor = _request_project_actor(request, services, project_id)
    services.authorization.require_permission(
        actor_id=actor,
        project_id=ProjectId(project_id),
        permission=permission,  # type: ignore[arg-type]
        source="web",
    )
    return actor


def _register_auth_middleware(app: FastAPI, services: Services, settings: Settings) -> None:
    @app.middleware("http")
    async def authenticate_request(request: Request, call_next):
        if not settings.auth_required:
            return await call_next(request)
        path = request.url.path
        if path in _PUBLIC_PATHS or path.startswith(("/static/", "/webhooks/")):
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
            if token_source == "cookie" and request.method not in {"GET", "HEAD", "OPTIONS"}:
                origin = request.headers.get("origin")
                expected_origin = f"{request.url.scheme}://{request.headers.get('host', '')}"
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
    def revoke_current_token(request: Request) -> None:
        services.auth.revoke(request.state.access_token, request_actor(request))


# ----------------------------------------------------------------------
# Health routes
# ----------------------------------------------------------------------


def _register_health_routes(
    app: FastAPI,
    health_service: HealthService,
    *,
    services: Services,
    settings: Settings,
) -> None:
    @app.get("/healthz", tags=["health"])
    def healthz() -> dict[str, Any]:
        report = health_service.report()
        return report.to_dict()

    @app.get("/readyz", tags=["health"])
    def readyz() -> JSONResponse:
        report = health_service.report()
        if report.status == "ok":
            return JSONResponse(status_code=status.HTTP_200_OK, content=report.to_dict())
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=report.to_dict(),
        )

    @app.get("/capabilities", tags=["health"])
    def capabilities() -> dict[str, Any]:
        """Declare what this deployment can and cannot do, and why."""
        payload = capabilities_payload(settings)
        worker_host: BackgroundWorkerHost | None = getattr(app.state, "worker_host", None)
        if worker_host is not None:
            payload["workers"] = worker_host.status.to_dict()
        return payload

    @app.get("/metrics", tags=["observability"])
    def metrics() -> dict[str, Any]:
        """Export low-cardinality runtime metrics.

        Per PLAN.md M14: counters and duration summaries only; raw
        prompts, source files, tool parameters/results, credentials,
        and private messages are excluded by construction (the label
        vocabulary is closed).
        """
        counters = services.metrics.get_counters()
        histograms = {
            name: services.metrics.get_histogram_summary(name)
            for name in services.metrics.histogram_names()
        }
        worker_host_status: dict[str, object] | None = None
        host: BackgroundWorkerHost | None = getattr(app.state, "worker_host", None)
        if host is not None:
            worker_host_status = host.status.to_dict()
        return {
            "counters": counters,
            "histograms": histograms,
            "workers": worker_host_status,
        }

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
        from zero.domain.identity import ProjectId

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
        from zero.domain.identity import ProjectId

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
        from zero.app.auth_service import request_actor

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


# ----------------------------------------------------------------------
# Secret routes
# ----------------------------------------------------------------------


def _register_secret_routes(app: FastAPI, services: Services) -> None:
    @app.post(
        "/projects/{project_id}/secrets",
        tags=["secrets"],
        status_code=status.HTTP_201_CREATED,
    )
    def store_secret(request: Request, project_id: str, req: StoreSecretRequest) -> dict[str, Any]:
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
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="request failed")
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
        except (SecretError, ValueError):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request failed")


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
    def grant_tool(request: Request, project_id: str, req: GrantToolRequest) -> dict[str, Any]:
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
    ) -> None:
        """Revoke a project tool grant; takes effect immediately."""
        from zero.domain.identity import ProjectId
        from zero.domain.tools import ToolId

        services.tools.revoke_tool_grant(
            project_id=ProjectId(project_id),
            actor_id=_request_project_actor(request, services, project_id),
            tool_id=ToolId(tool_id),
            agent_scope=agent_scope,  # type: ignore[arg-type]
            source="web",
        )

    @app.post(
        "/projects/{project_id}/tool-invocations",
        tags=["tools"],
    )
    def invoke_tool(request: Request, project_id: str, req: InvokeToolRequest) -> dict[str, Any]:
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
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="request failed")
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="request failed")
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
        except (PlanError, ValueError):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="request failed")
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
                source_event_ids=tuple(ConversationEventId(eid) for eid in req.source_event_ids),
            )
            revision = services.plans.propose_revision(
                plan_id=PlanId(plan_id),
                project_id=ProjectId(project_id),
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
                    detail={"error": "request validation failed", "errors": []},
                )
            if isinstance(exc, PlanNotFoundError):
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request failed")
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="request failed")
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
                project_id=ProjectId(project_id),
                actor_id=authenticated_actor(req.actor_id),
                expected_revision_number=req.expected_revision_number,
                idempotency_key=req.idempotency_key,
                redacted_reason=req.redacted_reason,
            )
        except StaleRevisionError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "stale revision",
                    "expected_revision": exc.expected_revision,
                    "actual_revision": exc.actual_revision,
                },
            )
        except (PlanError, ValueError):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="request failed")
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
                project_id=ProjectId(project_id),
                actor_id=authenticated_actor(req.actor_id),
                expected_revision_number=req.expected_revision_number,
                idempotency_key=req.idempotency_key,
                redacted_reason=req.redacted_reason,
            )
        except StaleRevisionError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "stale revision",
                    "expected_revision": exc.expected_revision,
                    "actual_revision": exc.actual_revision,
                },
            )
        except (PlanError, ValueError):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="request failed")
        return {
            "approval": {
                "id": approval.id.value,
                "result": approval.result,
                "approved_by": approval.approved_by.value,
                "created_at": approval.created_at,
            }
        }

    @app.get("/projects/{project_id}/plans", tags=["plans"])
    def list_plans(project_id: str, actor_id: str) -> list[dict[str, Any]]:
        from zero.domain.identity import ProjectId

        typed_project_id = ProjectId(project_id)
        plans = services.plans.list_plans_for_project(
            typed_project_id,
            actor_id=authenticated_actor(actor_id),
            source="web",
        )
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
    def list_revisions(
        project_id: str,
        plan_id: str,
        actor_id: str,
    ) -> list[dict[str, Any]]:
        from zero.domain.plans import PlanId

        typed_project_id = ProjectId(project_id)
        typed_plan_id = PlanId(plan_id)
        revisions = services.plans.list_revisions(
            typed_plan_id,
            project_id=typed_project_id,
            actor_id=authenticated_actor(actor_id),
            source="web",
        )
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
    agent_type_id: str | None = Field(default=None, min_length=1, max_length=200)


class DependencySpecModel(BaseModel):
    task_key: str
    depends_on_key: str


class CreateExecutionRequest(BaseModel):
    actor_id: str
    task_specs: list[TaskSpecModel] = Field(..., min_length=1)
    dependency_specs: list[DependencySpecModel] = []


class RunReadyTasksRequest(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    actor_id: str
    lease_owner: str = Field(..., min_length=1, max_length=200)
    provider: str = Field(..., min_length=1, max_length=100)
    model_name: str = Field(..., min_length=1, max_length=200)
    agent_scope: str = Field(
        "main_worker",
        pattern="^(main_planner|main_worker|sub_agent_type|integration)$",
    )
    tool_names: list[str] = Field(default_factory=list, max_length=32)
    repository_id: str | None = Field(default=None, min_length=1, max_length=200)
    max_tasks: int = Field(1, ge=1, le=32)


class SchedulerTickRequest(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    actor_id: str
    lease_owner: str = Field(..., min_length=1, max_length=200)
    provider: str = Field(..., min_length=1, max_length=100)
    model_name: str = Field(..., min_length=1, max_length=200)
    repository_id: str | None = Field(default=None, min_length=1, max_length=200)
    combined_test_command: str | None = Field(default=None, min_length=1, max_length=100)
    combined_test_args: list[str] = Field(default_factory=list, max_length=64)
    combined_test_timeout_seconds: int = Field(300, ge=1, le=300)
    max_handoffs: int = Field(8, ge=1, le=64)
    max_tasks: int = Field(16, ge=1, le=128)


def _register_execution_routes(app: FastAPI, services: Services) -> None:
    from zero.domain.execution import ExecutionId
    from zero.domain.worktrees import RepositoryId

    def route_actor(request: Request, project_id: str, claimed_id: str | None = None) -> UserId:
        if getattr(request.state, "user_id", None) is not None:
            authenticated = request_actor(request, claimed_id or None)
            if claimed_id or authenticated.value != "zu_system":
                return authenticated
        if claimed_id:
            return UserId(claimed_id)
        return services.identity.get_project(ProjectId(project_id)).owner_user_id

    def execution_in_project(
        project_id: str,
        execution_id: str,
        request: Request,
        claimed_actor_id: str | None = None,
    ):
        project = ProjectId(project_id)
        actor = route_actor(request, project_id, claimed_actor_id)
        try:
            return services.worker.get_execution(
                ExecutionId(execution_id),
                project_id=project,
                actor_id=actor,
                source="web",
            ), actor
        except (ExecutionError, AuthorizationError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="request failed"
            ) from exc

    @app.post(
        "/projects/{project_id}/handoffs/{handoff_id}/executions",
        tags=["executions"],
        status_code=status.HTTP_201_CREATED,
    )
    def create_execution(
        project_id: str,
        handoff_id: str,
        req: CreateExecutionRequest,
        request: Request,
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
                    agent_type_id=ts.agent_type_id,
                )
                for ts in req.task_specs
            ]
            dep_specs = [
                DependencySpec(task_key=d.task_key, depends_on_key=d.depends_on_key)
                for d in req.dependency_specs
            ]
            execution = services.worker.create_execution_from_handoff(
                handoff_id=PlanHandoffId(handoff_id),
                actor_id=route_actor(request, project_id, req.actor_id),
                project_id=ProjectId(project_id),
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
                    status_code=status.HTTP_400_BAD_REQUEST, detail="request failed"
                )
            if isinstance(exc, PlanNotApprovedError):
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="request failed")
            if isinstance(exc, PlanNotFoundError):
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request failed")
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="request failed")
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
    def get_execution(
        request: Request,
        project_id: str,
        execution_id: str,
    ) -> dict[str, Any]:
        execution, _actor = execution_in_project(project_id, execution_id, request)
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
        request: Request,
        project_id: str,
        execution_id: str,
    ) -> list[dict[str, Any]]:
        _execution, actor = execution_in_project(project_id, execution_id, request)
        try:
            tasks = services.worker.list_tasks(
                ExecutionId(execution_id),
                project_id=ProjectId(project_id),
                actor_id=actor,
                source="web",
            )
        except (ExecutionError, ValueError):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request failed")
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
        request: Request,
        project_id: str,
        execution_id: str,
    ) -> list[dict[str, Any]]:
        _execution, actor = execution_in_project(project_id, execution_id, request)
        try:
            tasks = services.worker.list_ready_tasks(
                ExecutionId(execution_id),
                project_id=ProjectId(project_id),
                actor_id=actor,
                source="web",
            )
        except (ExecutionError, ValueError):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request failed")
        return [{"id": t.id.value, "objective": t.objective, "state": t.state} for t in tasks]

    @app.post(
        "/projects/{project_id}/executions/{execution_id}/run-ready",
        tags=["executions"],
    )
    def run_ready_tasks(
        request: Request,
        project_id: str,
        execution_id: str,
        req: RunReadyTasksRequest,
    ) -> dict[str, Any]:
        from zero.app.agent_runtime import RuntimeErrorBase

        execution, actor = execution_in_project(project_id, execution_id, request, req.actor_id)
        if services.runtime is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="agent runtime is not configured",
            )
        try:
            results = services.runtime.run_ready_tasks(
                execution_id=execution.id,
                project_id=ProjectId(project_id),
                actor_id=actor,
                lease_owner=req.lease_owner,
                provider=req.provider,
                model_name=req.model_name,
                agent_scope=req.agent_scope,  # type: ignore[arg-type]
                tool_names=tuple(req.tool_names),
                repository_id=RepositoryId(req.repository_id) if req.repository_id else None,
                max_tasks=req.max_tasks,
                source="web",
            )
        except RuntimeErrorBase as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="request failed",
            ) from exc
        return {
            "execution_id": execution.id.value,
            "results": [
                {
                    "task_id": result.task.id.value,
                    "attempt_id": result.attempt.id.value,
                    "task_state": result.task.state,
                    "attempt_state": result.attempt.state,
                    "provider_request_id": result.provider_request_id.value,
                    "evidence_artifact_id": result.evidence_artifact_id.value,
                    "evidence_artifact_ids": [
                        artifact.value for artifact in result.evidence_artifact_ids
                    ],
                    "worktree_id": result.worktree_id.value if result.worktree_id else None,
                    "context_ledger_id": result.context_ledger_id,
                    "content": result.response.content[:4_000],
                    "finish_reason": result.response.finish_reason,
                }
                for result in results
            ],
        }

    @app.post(
        "/projects/{project_id}/scheduler/tick",
        tags=["executions"],
    )
    def scheduler_tick(
        request: Request,
        project_id: str,
        req: SchedulerTickRequest,
    ) -> dict[str, Any]:
        actor = route_actor(request, project_id, req.actor_id)
        if services.scheduler is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="scheduler is not configured",
            )
        try:
            result = services.scheduler.run_once(
                project_id=ProjectId(project_id),
                actor_id=actor,
                lease_owner=req.lease_owner,
                provider=req.provider,
                model_name=req.model_name,
                repository_id=RepositoryId(req.repository_id) if req.repository_id else None,
                combined_test_command=req.combined_test_command,
                combined_test_args=tuple(req.combined_test_args),
                combined_test_timeout_seconds=req.combined_test_timeout_seconds,
                max_handoffs=req.max_handoffs,
                max_tasks=req.max_tasks,
                source="web",
            )
        except (ExecutionError, AuthorizationError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="request failed"
            ) from exc
        return {
            "handoffs_claimed": result.handoffs_claimed,
            "executions_seen": result.executions_seen,
            "tasks_run": result.tasks_run,
            "integration_review_ids": list(result.integration_review_ids),
            "merge_proposal_ids": list(result.merge_proposal_ids),
            "result_delivery_ids": list(result.result_delivery_ids),
            "errors": list(result.errors),
            "results": [
                {
                    "task_id": item.task.id.value,
                    "attempt_id": item.attempt.id.value,
                    "task_state": item.task.state,
                    "attempt_state": item.attempt.state,
                    "evidence_artifact_ids": [a.value for a in item.evidence_artifact_ids],
                }
                for item in result.task_results
            ],
        }

    @app.post(
        "/projects/{project_id}/executions/{execution_id}/cancel",
        tags=["executions"],
    )
    def cancel_execution(
        request: Request,
        project_id: str,
        execution_id: str,
        actor_id: str = "",
    ) -> dict[str, Any]:
        _execution, actor = execution_in_project(project_id, execution_id, request, actor_id)
        try:
            execution = services.worker.cancel_execution(
                execution_id=ExecutionId(execution_id),
                project_id=ProjectId(project_id),
                actor_id=actor,
                source="web",
            )
        except (ExecutionError, ValueError) as exc:
            from zero.domain.execution import InvalidExecutionTransitionError

            if isinstance(exc, InvalidExecutionTransitionError):
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="request failed")
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="request failed")
        return {
            "id": execution.id.value,
            "state": execution.state,
        }

    @app.post(
        "/projects/{project_id}/executions/{execution_id}/recover",
        tags=["executions"],
    )
    def recover_execution(
        request: Request,
        project_id: str,
        execution_id: str,
        actor_id: str = "",
    ) -> dict[str, Any]:
        _execution, actor = execution_in_project(project_id, execution_id, request, actor_id)
        try:
            execution = services.worker.recover_after_restart(
                execution_id=ExecutionId(execution_id),
                project_id=ProjectId(project_id),
                actor_id=actor,
                source="web",
            )
        except (ExecutionError, ValueError):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="request failed")
        return {
            "id": execution.id.value,
            "state": execution.state,
            "blocker_reason": execution.blocker_reason,
        }


# ----------------------------------------------------------------------
# Artifact and Project RAG routes (Gate D)
# ----------------------------------------------------------------------


def _artifact_payload(artifact: Any, *, include_content: bool = False) -> dict[str, Any]:
    payload = {
        "id": artifact.id.value,
        "project_id": artifact.project_id.value,
        "content_hash": artifact.content_hash,
        "kind": artifact.kind,
        "media_type": artifact.media_type,
        "size_bytes": artifact.size_bytes,
        "producer": artifact.producer,
        "provenance": artifact.provenance,
        "created_at": artifact.created_at,
    }
    if include_content:
        payload["content"] = artifact.content
    return payload


def _rag_payload(document: Any, *, include_content: bool = False) -> dict[str, Any]:
    payload = {
        "id": document.id.value,
        "project_id": document.project_id.value,
        "source_type": document.source_type,
        "source_id": document.source_id,
        "title": document.title,
        "content_hash": document.content_hash,
        "state": document.state,
        "superseded_by": document.superseded_by.value if document.superseded_by else None,
        "index_version": document.index_version,
        "created_at": document.created_at,
        "updated_at": document.updated_at,
    }
    if include_content:
        payload["content"] = document.content
    return payload


def _register_artifact_routes(app: FastAPI, services: Services) -> None:
    @app.get("/projects/{project_id}/artifacts", tags=["artifacts"])
    def list_artifacts(
        request: Request, project_id: str, kind: str | None = None
    ) -> list[dict[str, Any]]:
        from zero.domain.identity import ProjectId

        actor = _authorized_actor(request, services, project_id, "project.view")
        artifacts = services.artifacts.list_artifacts(
            project_id=ProjectId(project_id),
            actor_id=actor,
            kind=kind,  # type: ignore[arg-type]
        )
        return [_artifact_payload(item) for item in artifacts]

    @app.post(
        "/projects/{project_id}/artifacts",
        tags=["artifacts"],
        status_code=status.HTTP_201_CREATED,
    )
    def store_artifact(
        request: Request, project_id: str, req: StoreArtifactRequest
    ) -> dict[str, Any]:
        from zero.domain.identity import ProjectId

        actor = _authorized_actor(request, services, project_id, "agent.manage")
        try:
            artifact = services.artifacts.store_artifact(
                project_id=ProjectId(project_id),
                actor_id=actor,
                kind=req.kind,  # type: ignore[arg-type]
                content=req.content,
                producer=req.producer,
                provenance=req.provenance,
                media_type=req.media_type,
                source="web",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="request failed") from exc
        return _artifact_payload(artifact)

    @app.get("/projects/{project_id}/artifacts/{artifact_id}", tags=["artifacts"])
    def get_artifact(request: Request, project_id: str, artifact_id: str) -> dict[str, Any]:
        from zero.domain.artifacts import ArtifactId
        from zero.domain.identity import ProjectId

        actor = _authorized_actor(request, services, project_id, "agent.manage")
        try:
            artifact = services.artifacts.get_artifact(
                project_id=ProjectId(project_id),
                artifact_id=ArtifactId(artifact_id),
                actor_id=actor,
                source="web",
            )
        except Exception as exc:
            raise HTTPException(status_code=404, detail="Artifact not found") from exc
        return _artifact_payload(artifact, include_content=True)

    @app.get("/projects/{project_id}/rag", tags=["rag"])
    def list_rag_documents(
        request: Request, project_id: str, state: str | None = None
    ) -> list[dict[str, Any]]:
        from zero.domain.identity import ProjectId

        _authorized_actor(request, services, project_id, "project.view")
        documents = services.artifacts.list_rag_documents(
            ProjectId(project_id),
            state=state,  # type: ignore[arg-type]
        )
        return [_rag_payload(item) for item in documents]

    @app.post(
        "/projects/{project_id}/rag",
        tags=["rag"],
        status_code=status.HTTP_201_CREATED,
    )
    def ingest_rag_document(
        request: Request, project_id: str, req: StoreRagDocumentRequest
    ) -> dict[str, Any]:
        from zero.domain.identity import ProjectId

        actor = _authorized_actor(request, services, project_id, "agent.manage")
        try:
            document = services.artifacts.ingest_rag_document(
                project_id=ProjectId(project_id),
                actor_id=actor,
                source_type=req.source_type,  # type: ignore[arg-type]
                source_id=req.source_id,
                title=req.title,
                content=req.content,
                state=req.state,  # type: ignore[arg-type]
                source="web",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="request failed") from exc
        return _rag_payload(document)

    @app.get("/projects/{project_id}/rag/{doc_id}", tags=["rag"])
    def get_rag_document(request: Request, project_id: str, doc_id: str) -> dict[str, Any]:
        from zero.domain.artifacts import RagDocumentId
        from zero.domain.identity import ProjectId

        _authorized_actor(request, services, project_id, "project.view")
        try:
            document = services.artifacts.get_rag_document(
                ProjectId(project_id), RagDocumentId(doc_id)
            )
        except Exception as exc:
            raise HTTPException(status_code=404, detail="RAG document not found") from exc
        return _rag_payload(document, include_content=True)

    @app.post("/projects/{project_id}/rag/search", tags=["rag"])
    def search_rag(
        request: Request, project_id: str, query: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        from zero.domain.identity import ProjectId

        _authorized_actor(request, services, project_id, "project.view")
        if not 1 <= limit <= 100:
            raise HTTPException(status_code=400, detail="limit must be between 1 and 100")
        results = services.artifacts.search_rag(
            project_id=ProjectId(project_id), query=query, limit=limit
        )
        return [{"document": _rag_payload(document), "score": score} for document, score in results]

    @app.post("/projects/{project_id}/rag/rebuild", tags=["rag"])
    def rebuild_rag(request: Request, project_id: str) -> dict[str, Any]:
        from zero.domain.identity import ProjectId

        actor = _authorized_actor(request, services, project_id, "agent.manage")
        count = services.artifacts.rebuild_rag_index(
            project_id=ProjectId(project_id), actor_id=actor, source="web"
        )
        return {"project_id": project_id, "indexed_documents": count}


# ----------------------------------------------------------------------
# Topology and dynamic agent type routes (Gate D)
# ----------------------------------------------------------------------


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


def _register_topology_routes(app: FastAPI, services: Services) -> None:
    @app.get("/projects/{project_id}/agent-types", tags=["topology"])
    def list_agent_types(request: Request, project_id: str) -> list[dict[str, Any]]:
        from zero.domain.identity import ProjectId

        _authorized_actor(request, services, project_id, "project.view")
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
        from zero.domain.identity import ProjectId

        actor = _authorized_actor(request, services, project_id, "agent.manage")
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
        from zero.domain.identity import ProjectId

        _authorized_actor(request, services, project_id, "project.view")
        try:
            item = services.agent_types.get_type(ProjectId(project_id), AgentTypeId(type_id))
        except Exception as exc:
            raise HTTPException(status_code=404, detail="Agent type not found") from exc
        return _agent_type_payload(item)

    @app.get("/projects/{project_id}/agent-types/{type_id}/knowledge", tags=["topology"])
    def list_knowledge(request: Request, project_id: str, type_id: str) -> list[dict[str, Any]]:
        from zero.domain.agent_types import AgentTypeId
        from zero.domain.identity import ProjectId

        _authorized_actor(request, services, project_id, "project.view")
        try:
            records = services.agent_types.list_knowledge_for_type(
                ProjectId(project_id), AgentTypeId(type_id)
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
        from zero.domain.identity import ProjectId

        actor = _authorized_actor(request, services, project_id, "agent.manage")
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
        from zero.domain.identity import ProjectId

        _authorized_actor(request, services, project_id, "project.view")
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


# ----------------------------------------------------------------------
# Provider/model and usage routes (Gate D)
# ----------------------------------------------------------------------


def _provider_model_payload(model: Any) -> dict[str, Any]:
    return {
        "id": model.id.value,
        "provider": model.provider,
        "model_name": model.model_name,
        "context_window": model.context_window,
        "max_output_tokens": model.max_output_tokens,
        "capabilities": list(model.capabilities),
        "is_active": model.is_active,
        "created_at": model.created_at,
    }


def _provider_request_payload(item: Any) -> dict[str, Any]:
    return {
        "id": item.id.value,
        "project_id": item.project_id.value,
        "execution_id": item.execution_id.value if item.execution_id else None,
        "provider": item.provider,
        "model_name": item.model_name,
        "request_hash": item.request_hash,
        "state": item.state,
        "error_class": item.error_class,
        "error_message": item.error_message,
        "response_artifact_id": item.response_artifact_id.value
        if item.response_artifact_id
        else None,
        "started_at": item.started_at,
        "completed_at": item.completed_at,
    }


def _usage_payload(item: Any) -> dict[str, Any]:
    usage = item.usage
    return {
        "id": item.id.value,
        "project_id": item.project_id.value,
        "provider_request_id": item.provider_request_id.value,
        "execution_id": item.execution_id.value if item.execution_id else None,
        "provider_message_id": item.provider_message_id,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_creation_input_tokens": usage.cache_creation_input_tokens,
        "cache_read_input_tokens": usage.cache_read_input_tokens,
        "estimated_cost_usd": item.estimated_cost_usd,
        "pricing_catalog_version": item.pricing_catalog_version,
        "created_at": item.created_at,
    }


def _register_provider_routes(app: FastAPI, services: Services) -> None:
    @app.get("/providers", tags=["providers"])
    def list_provider_models() -> list[dict[str, Any]]:
        return [_provider_model_payload(item) for item in services.providers.list_models()]

    @app.get("/providers/{provider}/{model_name}", tags=["providers"])
    def get_provider_model(provider: str, model_name: str) -> dict[str, Any]:
        try:
            return _provider_model_payload(services.providers.get_model(provider, model_name))
        except Exception as exc:
            raise HTTPException(status_code=404, detail="Provider model not found") from exc

    @app.get("/projects/{project_id}/providers", tags=["providers"])
    def list_project_provider_models(request: Request, project_id: str) -> list[dict[str, Any]]:
        _authorized_actor(request, services, project_id, "model.change")
        return [_provider_model_payload(item) for item in services.providers.list_models()]

    @app.get("/projects/{project_id}/providers/requests", tags=["providers"])
    def list_provider_requests(request: Request, project_id: str) -> list[dict[str, Any]]:
        from zero.domain.identity import ProjectId

        actor = _authorized_actor(request, services, project_id, "cost.view")
        return [
            _provider_request_payload(item)
            for item in services.providers.list_provider_requests_for_project(
                ProjectId(project_id),
                actor_id=actor,
                source="web",
            )
        ]

    @app.get("/projects/{project_id}/providers/requests/unknown", tags=["providers"])
    def list_unknown_provider_requests(request: Request, project_id: str) -> list[dict[str, Any]]:
        """Operator queue: provider requests awaiting reconciliation."""
        from zero.domain.identity import ProjectId

        _authorized_actor(request, services, project_id, "cost.view")
        return [
            _provider_request_payload(item)
            for item in services.providers.list_unknown_requests(ProjectId(project_id))
        ]

    @app.post(
        "/projects/{project_id}/providers/requests/{request_id}/reconcile", tags=["providers"]
    )
    def reconcile_provider_request(
        request: Request,
        project_id: str,
        request_id: str,
        req: ReconcileProviderRequestRequest,
    ) -> dict[str, Any]:
        """Record an operator decision for one unknown provider outcome."""
        from zero.domain.identity import ProjectId
        from zero.domain.providers import ProviderRequestId

        actor = _authorized_actor(request, services, project_id, "execution.start")
        try:
            services.providers.reconcile_provider_request(
                project_id=ProjectId(project_id),
                request_id=ProviderRequestId(request_id),
                actor_id=actor,
                resolution=req.resolution,
                note=req.note,
                source="web",
            )
        except ValueError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request failed")
        return {"status": "reconciled", "resolution": req.resolution}

    @app.get("/projects/{project_id}/providers/usage", tags=["providers"])
    def list_provider_usage(request: Request, project_id: str) -> list[dict[str, Any]]:
        from zero.domain.identity import ProjectId

        actor = _authorized_actor(request, services, project_id, "cost.view")
        return [
            _usage_payload(item)
            for item in services.providers.list_usage_records_for_project(
                ProjectId(project_id),
                actor_id=actor,
                source="web",
            )
        ]


# ----------------------------------------------------------------------
# Repository and worktree routes (Gate D)
# ----------------------------------------------------------------------


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


def _register_worktree_routes(app: FastAPI, services: Services) -> None:
    @app.get("/projects/{project_id}/repositories", tags=["worktrees"])
    def list_repositories(request: Request, project_id: str) -> list[dict[str, Any]]:
        from zero.domain.identity import ProjectId

        actor = _authorized_actor(request, services, project_id, "project.view")
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
        from zero.domain.identity import ProjectId

        actor = _authorized_actor(request, services, project_id, "execution.start")
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
        from zero.domain.identity import ProjectId

        actor = _authorized_actor(request, services, project_id, "project.view")
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
        from zero.domain.identity import ProjectId
        from zero.domain.worktrees import WorktreeId

        actor = _authorized_actor(request, services, project_id, "project.view")
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


# ----------------------------------------------------------------------
# Integration review and merge proposal routes (Gate D)
# ----------------------------------------------------------------------


def _review_payload(item: Any) -> dict[str, Any]:
    return {
        "id": item.id.value,
        "project_id": item.project_id.value,
        "execution_id": item.execution_id.value,
        "source_task_ids": [task.value for task in item.source_task_ids],
        "impact_set": [
            {
                "file_path": entry.file_path,
                "change_type": entry.change_type,
                "is_contract": entry.is_contract,
            }
            for entry in item.impact_set
        ],
        "touched_contracts": list(item.touched_contracts),
        "combined_test_result": item.combined_test_result,
        "conflict_classification": item.conflict_classification,
        "conflict_details": [
            {
                "conflict_type": detail.conflict_type,
                "description": detail.description,
                "source_tasks": list(detail.source_tasks),
            }
            for detail in item.conflict_details
        ],
        "state": item.state,
        "integration_worktree_id": item.integration_worktree_id,
        "reviewed_by": item.reviewed_by.value if item.reviewed_by else None,
        "redacted_summary": item.redacted_summary,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def _proposal_payload(item: Any) -> dict[str, Any]:
    return {
        "id": item.id.value,
        "project_id": item.project_id.value,
        "integration_review_id": item.integration_review_id.value,
        "execution_id": item.execution_id.value,
        "source_tasks": [task.value for task in item.source_tasks],
        "source_diffs": list(item.source_diffs),
        "checks_passed": item.checks_passed,
        "risks": list(item.risks),
        "state": item.state,
        "approved_by": item.approved_by.value if item.approved_by else None,
        "merged_at": item.merged_at,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def _register_integration_routes(app: FastAPI, services: Services) -> None:
    @app.get("/projects/{project_id}/integration", tags=["integration"])
    def integration_status(request: Request, project_id: str) -> dict[str, Any]:
        _authorized_actor(request, services, project_id, "project.view")
        return {
            "project_id": project_id,
            "reviews": "/integration/reviews",
            "proposals": "/integration/proposals",
        }

    @app.get("/projects/{project_id}/integration/reviews", tags=["integration"])
    def list_reviews(
        request: Request, project_id: str, execution_id: str | None = None
    ) -> list[dict[str, Any]]:
        from zero.domain.execution import ExecutionId
        from zero.domain.identity import ProjectId

        _authorized_actor(request, services, project_id, "project.view")
        if not execution_id:
            return []
        items = services.integration.list_reviews(
            ExecutionId(execution_id), project_id=ProjectId(project_id)
        )
        return [_review_payload(item) for item in items]

    @app.post(
        "/projects/{project_id}/integration/reviews",
        tags=["integration"],
        status_code=status.HTTP_201_CREATED,
    )
    def create_review(
        request: Request, project_id: str, req: CreateIntegrationReviewRequest
    ) -> dict[str, Any]:
        from zero.domain.execution import ExecutionId, TaskId
        from zero.domain.identity import ProjectId

        actor = _authorized_actor(request, services, project_id, "integration.authorize_merge")
        try:
            item = services.integration.create_review(
                project_id=ProjectId(project_id),
                execution_id=ExecutionId(req.execution_id),
                source_task_ids=tuple(TaskId(task) for task in req.source_task_ids),
                actor_id=actor,
                source="web",
            )
        except Exception as exc:
            raise HTTPException(
                status_code=400, detail="integration review request failed"
            ) from exc
        return _review_payload(item)

    @app.get("/projects/{project_id}/integration/reviews/{review_id}", tags=["integration"])
    def get_review(request: Request, project_id: str, review_id: str) -> dict[str, Any]:
        from zero.domain.identity import ProjectId
        from zero.domain.integration import IntegrationReviewId

        _authorized_actor(request, services, project_id, "project.view")
        try:
            item = services.integration.get_review(
                ProjectId(project_id), IntegrationReviewId(review_id)
            )
        except Exception as exc:
            raise HTTPException(status_code=404, detail="Integration review not found") from exc
        return _review_payload(item)

    @app.post(
        "/projects/{project_id}/integration/reviews/{review_id}/combined-test", tags=["integration"]
    )
    def record_combined_test(
        request: Request, project_id: str, review_id: str, result: str
    ) -> dict[str, Any]:
        from zero.domain.identity import ProjectId
        from zero.domain.integration import IntegrationReviewId

        actor = _authorized_actor(request, services, project_id, "integration.authorize_merge")
        if result not in {"pass", "fail", "not_run"}:
            raise HTTPException(status_code=400, detail="invalid combined test result")
        try:
            item = services.integration.record_combined_test_result(
                project_id=ProjectId(project_id),
                review_id=IntegrationReviewId(review_id),
                result=result,  # type: ignore[arg-type]
                actor_id=actor,
                source="web",
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail="combined test request failed") from exc
        return _review_payload(item)

    @app.get("/projects/{project_id}/integration/proposals", tags=["integration"])
    def list_proposals(
        request: Request, project_id: str, execution_id: str | None = None
    ) -> list[dict[str, Any]]:
        from zero.domain.execution import ExecutionId
        from zero.domain.identity import ProjectId

        _authorized_actor(request, services, project_id, "project.view")
        if not execution_id:
            return []
        items = services.integration.list_proposals(
            ExecutionId(execution_id), project_id=ProjectId(project_id)
        )
        return [_proposal_payload(item) for item in items]

    @app.post(
        "/projects/{project_id}/integration/proposals",
        tags=["integration"],
        status_code=status.HTTP_201_CREATED,
    )
    def create_proposal(
        request: Request, project_id: str, req: CreateMergeProposalRequest
    ) -> dict[str, Any]:
        from zero.domain.execution import ExecutionId, TaskId
        from zero.domain.identity import ProjectId
        from zero.domain.integration import IntegrationReviewId

        actor = _authorized_actor(request, services, project_id, "integration.authorize_merge")
        try:
            item = services.integration.create_merge_proposal(
                project_id=ProjectId(project_id),
                review_id=IntegrationReviewId(req.review_id),
                execution_id=ExecutionId(req.execution_id),
                source_tasks=tuple(TaskId(task) for task in req.source_tasks),
                source_diffs=tuple(req.source_diffs),
                risks=tuple(req.risks),
                actor_id=actor,
                source="web",
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail="merge proposal request failed") from exc
        return _proposal_payload(item)

    @app.get("/projects/{project_id}/integration/proposals/{proposal_id}", tags=["integration"])
    def get_proposal(request: Request, project_id: str, proposal_id: str) -> dict[str, Any]:
        from zero.domain.identity import ProjectId
        from zero.domain.integration import MergeProposalId

        _authorized_actor(request, services, project_id, "project.view")
        try:
            item = services.integration.get_proposal(
                ProjectId(project_id), MergeProposalId(proposal_id)
            )
        except Exception as exc:
            raise HTTPException(status_code=404, detail="Merge proposal not found") from exc
        return _proposal_payload(item)

    def _proposal_action(
        request: Request, project_id: str, proposal_id: str, action: str
    ) -> dict[str, Any]:
        from zero.domain.identity import ProjectId
        from zero.domain.integration import MergeProposalId

        actor = _authorized_actor(request, services, project_id, "integration.authorize_merge")
        try:
            typed_project = ProjectId(project_id)
            typed_proposal = MergeProposalId(proposal_id)
            if action == "approve":
                item = services.integration.approve_merge(
                    project_id=typed_project,
                    proposal_id=typed_proposal,
                    actor_id=actor,
                    source="web",
                )
            elif action == "reject":
                item = services.integration.reject_merge(
                    project_id=typed_project,
                    proposal_id=typed_proposal,
                    actor_id=actor,
                    source="web",
                )
            else:
                item = services.integration.execute_merge(
                    project_id=typed_project,
                    proposal_id=typed_proposal,
                    actor_id=actor,
                    source="web",
                )
        except Exception as exc:
            raise HTTPException(status_code=400, detail="merge proposal action failed") from exc
        return _proposal_payload(item)

    @app.post(
        "/projects/{project_id}/integration/proposals/{proposal_id}/approve", tags=["integration"]
    )
    def approve_proposal(request: Request, project_id: str, proposal_id: str) -> dict[str, Any]:
        return _proposal_action(request, project_id, proposal_id, "approve")

    @app.post(
        "/projects/{project_id}/integration/proposals/{proposal_id}/reject", tags=["integration"]
    )
    def reject_proposal(request: Request, project_id: str, proposal_id: str) -> dict[str, Any]:
        return _proposal_action(request, project_id, proposal_id, "reject")

    @app.post(
        "/projects/{project_id}/integration/proposals/{proposal_id}/execute", tags=["integration"]
    )
    def execute_proposal(request: Request, project_id: str, proposal_id: str) -> dict[str, Any]:
        return _proposal_action(request, project_id, proposal_id, "execute")


# ----------------------------------------------------------------------
# Interface binding and durable event routes (Gate D)
# ----------------------------------------------------------------------


def _binding_payload(item: Any) -> dict[str, Any]:
    return {
        "id": item.id.value,
        "project_id": item.project_id.value,
        "platform": item.platform,
        "bot_token_ref": item.bot_token_ref,
        "chat_id": item.chat_id,
        "topic_id": item.topic_id,
        "is_enabled": item.is_enabled,
        "created_by": item.created_by.value,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def _interface_event_payload(item: Any) -> dict[str, Any]:
    return {
        "id": item.id.value,
        "project_id": item.project_id.value if item.project_id else None,
        "platform": item.platform,
        "external_event_id": item.external_event_id,
        "external_actor_id": item.external_actor_id,
        "resolved_user_id": item.resolved_user_id.value if item.resolved_user_id else None,
        "chat_id": item.chat_id,
        "topic_id": item.topic_id,
        "event_kind": item.event_kind,
        "processing_result": item.processing_result,
        "processing_detail": item.processing_detail,
        "created_at": item.created_at,
    }


def _result_delivery_payload(item: Any) -> dict[str, Any]:
    return {
        "id": item.id.value,
        "project_id": item.project_id.value,
        "execution_id": item.execution_id,
        "binding_id": item.binding_id.value,
        "created_by": item.created_by.value,
        "delivery_key": item.delivery_key,
        "state": item.state,
        "attempt_count": item.attempt_count,
        "claim_token": None,
        "lease_expires_at": item.lease_expires_at,
        "next_attempt_at": item.next_attempt_at,
        "external_message_id": item.external_message_id,
        "last_error": item.last_error,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def _register_interface_routes(app: FastAPI, services: Services) -> None:
    @app.get("/projects/{project_id}/result-deliveries", tags=["interfaces"])
    def list_result_deliveries(request: Request, project_id: str) -> list[dict[str, Any]]:
        from zero.domain.identity import ProjectId

        _authorized_actor(request, services, project_id, "execution.view_diffs")
        return [
            _result_delivery_payload(item)
            for item in services.result_delivery.list_for_project(ProjectId(project_id))
        ]

    @app.post("/projects/{project_id}/result-deliveries/drain", tags=["interfaces"])
    def drain_result_delivery(request: Request, project_id: str) -> dict[str, Any]:
        from zero.domain.identity import ProjectId

        _authorized_actor(request, services, project_id, "execution.view_diffs")
        if not services.result_delivery.is_outbound_configured:
            raise HTTPException(status_code=503, detail="outbound result delivery unavailable")
        item = services.result_delivery.drain_once(project_id=ProjectId(project_id))
        return {"status": "empty"} if item is None else _result_delivery_payload(item)

    @app.get("/projects/{project_id}/interfaces", tags=["interfaces"])
    def list_bindings(request: Request, project_id: str) -> list[dict[str, Any]]:
        from zero.domain.identity import ProjectId

        _authorized_actor(request, services, project_id, "project.view")
        return [
            _binding_payload(item)
            for item in services.interfaces.list_bindings(ProjectId(project_id))
        ]

    @app.post(
        "/projects/{project_id}/interfaces",
        tags=["interfaces"],
        status_code=status.HTTP_201_CREATED,
    )
    def create_binding(
        request: Request, project_id: str, req: CreateInterfaceBindingRequest
    ) -> dict[str, Any]:
        from zero.domain.identity import ProjectId

        actor = _authorized_actor(request, services, project_id, "agent.manage")
        try:
            item = services.interfaces.create_binding(
                project_id=ProjectId(project_id),
                actor_id=actor,
                platform=req.platform,  # type: ignore[arg-type]
                chat_id=req.chat_id,
                topic_id=req.topic_id,
                bot_token_ref=req.bot_token_ref,
                is_enabled=req.is_enabled,
                source="web",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="request failed") from exc
        return _binding_payload(item)

    @app.get("/projects/{project_id}/interfaces/events", tags=["interfaces"])
    def list_interface_events(
        request: Request, project_id: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        from zero.domain.identity import ProjectId

        _authorized_actor(request, services, project_id, "project.view")
        if not 1 <= limit <= 500:
            raise HTTPException(status_code=400, detail="limit must be between 1 and 500")
        return [
            _interface_event_payload(item)
            for item in services.interfaces.list_event_log(ProjectId(project_id), limit=limit)
        ]

    def _set_binding(
        request: Request, project_id: str, binding_id: str, enabled: bool
    ) -> dict[str, Any]:
        from zero.domain.identity import ProjectId
        from zero.domain.interfaces import InterfaceBindingId

        actor = _authorized_actor(request, services, project_id, "agent.manage")
        try:
            method = (
                services.interfaces.enable_binding
                if enabled
                else services.interfaces.disable_binding
            )
            item = method(
                project_id=ProjectId(project_id),
                binding_id=InterfaceBindingId(binding_id),
                actor_id=actor,
                source="web",
            )
        except Exception as exc:
            raise HTTPException(status_code=404, detail="Interface binding not found") from exc
        return _binding_payload(item)

    @app.post("/projects/{project_id}/interfaces/{binding_id}/enable", tags=["interfaces"])
    def enable_binding(request: Request, project_id: str, binding_id: str) -> dict[str, Any]:
        return _set_binding(request, project_id, binding_id, True)

    @app.post("/projects/{project_id}/interfaces/{binding_id}/disable", tags=["interfaces"])
    def disable_binding(request: Request, project_id: str, binding_id: str) -> dict[str, Any]:
        return _set_binding(request, project_id, binding_id, False)


def _register_webhook_routes(app: FastAPI, services: Services) -> None:
    @app.post("/webhooks/{platform}/{project_id}/{binding_id}", tags=["webhooks"])
    async def receive_webhook(
        platform: str,
        project_id: str,
        binding_id: str,
        request: Request,
    ) -> JSONResponse:
        transport_service = services.interface_transports
        if transport_service is None:
            raise HTTPException(status_code=503, detail="interface transport unavailable")
        if platform not in {"telegram", "discord"}:
            raise HTTPException(status_code=404, detail="interface platform not found")
        body = await request.body()
        try:
            result = await run_in_threadpool(
                transport_service.process_webhook,
                platform=platform,  # type: ignore[arg-type]
                project_id=ProjectId(project_id),
                binding_id=InterfaceBindingId(binding_id),
                body=body,
                headers=dict(request.headers),
            )
        except WebhookAuthError as exc:
            raise HTTPException(status_code=401, detail="webhook authentication failed") from exc
        except InterfaceTransportNotConfigured as exc:
            raise HTTPException(
                status_code=503, detail="webhook verification is not configured"
            ) from exc
        except InterfaceScopeError as exc:
            raise HTTPException(status_code=404, detail="interface binding not found") from exc
        except UnsupportedUpdateError as exc:
            raise HTTPException(status_code=400, detail="unsupported webhook payload") from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="interface binding not found") from exc
        if isinstance(result, dict):
            return JSONResponse(content=result)
        if result is None:
            return JSONResponse(content={"status": "accepted"})
        return JSONResponse(content=_interface_event_payload(result))
