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

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from zero import __version__
from zero.app.auth_service import (
    AuthenticationError,
    bind_actor,
    reset_actor,
)
from zero.app.background_workers import BackgroundWorkerHost
from zero.app.health import HealthService
from zero.app.routers.artifact import register_artifact_routes
from zero.app.routers.audit import register_audit_routes
from zero.app.routers.auth import register_auth_routes
from zero.app.routers.authorization import register_authorization_routes
from zero.app.routers.execution import register_execution_routes
from zero.app.routers.health import register_health_routes
from zero.app.routers.identity import register_identity_routes
from zero.app.routers.integration import register_integration_routes
from zero.app.routers.interface import register_interface_routes
from zero.app.routers.plan import register_plan_routes
from zero.app.routers.provider import register_provider_routes
from zero.app.routers.secret import register_secret_routes
from zero.app.routers.tool import register_tool_routes
from zero.app.routers.topology import register_topology_routes
from zero.app.routers.webhook import register_webhook_routes
from zero.app.routers.worktree import register_worktree_routes
from zero.app.services import Services, build_services
from zero.config import Settings
from zero.domain.authorization import AuthorizationError
from zero.domain.identity import (
    ProjectId,
)
from zero.persistence.connection import Database
from zero.persistence.migrations import (
    apply_migrations,
    count_applied_migrations,
)

# ----------------------------------------------------------------------
# Request/response models (Pydantic v2)
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
        from zero.persistence.connection import open_database

        database = open_database(settings)
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

    # GAP 5: process-wide fan-out hub for execution stream events.
    from zero.app.stream_hub import ExecutionStreamHub

    app.state.stream_hub = ExecutionStreamHub()

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

    register_health_routes(app, health_service, services=services, settings=settings)
    register_auth_routes(app, services)
    register_identity_routes(app, services)
    register_authorization_routes(app, services)
    register_secret_routes(app, services)
    register_tool_routes(app, services)
    register_audit_routes(app, services)
    register_plan_routes(app, services)
    register_execution_routes(app, services)
    register_artifact_routes(app, services)
    register_topology_routes(app, services)
    register_provider_routes(app, services)
    register_worktree_routes(app, services)
    register_integration_routes(app, services)
    register_interface_routes(app, services)
    register_webhook_routes(app, services)

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
            from zero.manage.core.config import ConfigService, zero_home
            from zero.manage.services.backup_daemon import BackupDaemon

            home = zero_home()
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


def _register_auth_middleware(app: FastAPI, services: Services, settings: Settings) -> None:
    @app.middleware("http")
    async def authenticate_request(request: Request, call_next):
        if not settings.auth_required:
            return await call_next(request)
        path = request.url.path
        # /admin/* runs its own scrypt-password + CSRF scheme
        # (zero.manage.web); routing it through the bearer-token
        # middleware would make the admin GUI unreachable in production
        # while adding no protection. Audit finding S6.
        if path in _PUBLIC_PATHS or path.startswith(
            ("/static/", "/webhooks/", "/admin", "/admin/")
        ):
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
