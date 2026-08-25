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
    bind_actor,
    reset_actor,
)
from zero.app.background_workers import BackgroundWorkerHost
from zero.app.capabilities import capabilities_payload
from zero.app.health import HealthService
from zero.app.interface_transport_service import (
    InterfaceScopeError,
    InterfaceTransportNotConfigured,
)
from zero.app.routers.audit import register_audit_routes
from zero.app.routers.auth import register_auth_routes
from zero.app.routers.authorization import register_authorization_routes
from zero.app.routers.deps import authorized_actor
from zero.app.routers.execution import register_execution_routes
from zero.app.routers.identity import register_identity_routes
from zero.app.routers.plan import register_plan_routes
from zero.app.routers.secret import register_secret_routes
from zero.app.routers.tool import register_tool_routes
from zero.app.services import Services, build_services
from zero.config import Settings
from zero.domain.authorization import AuthorizationError
from zero.domain.identity import (
    ProjectId,
)
from zero.domain.interfaces import InterfaceBindingId
from zero.persistence.connection import Database
from zero.persistence.migrations import (
    apply_migrations,
    count_applied_migrations,
)

# ----------------------------------------------------------------------
# Request/response models (Pydantic v2)
# ----------------------------------------------------------------------


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

    _register_health_routes(app, health_service, services=services, settings=settings)
    register_auth_routes(app, services)
    register_identity_routes(app, services)
    register_authorization_routes(app, services)
    register_secret_routes(app, services)
    register_tool_routes(app, services)
    register_audit_routes(app, services)
    register_plan_routes(app, services)
    register_execution_routes(app, services)
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


# ----------------------------------------------------------------------
# Authorization routes
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

        actor = authorized_actor(request, services, project_id, "project.view")
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

        actor = authorized_actor(request, services, project_id, "agent.manage")
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

        actor = authorized_actor(request, services, project_id, "agent.manage")
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

        authorized_actor(request, services, project_id, "project.view")
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

        actor = authorized_actor(request, services, project_id, "agent.manage")
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

        authorized_actor(request, services, project_id, "project.view")
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

        authorized_actor(request, services, project_id, "project.view")
        if not 1 <= limit <= 100:
            raise HTTPException(status_code=400, detail="limit must be between 1 and 100")
        results = services.artifacts.search_rag(
            project_id=ProjectId(project_id), query=query, limit=limit
        )
        return [{"document": _rag_payload(document), "score": score} for document, score in results]

    @app.post("/projects/{project_id}/rag/rebuild", tags=["rag"])
    def rebuild_rag(request: Request, project_id: str) -> dict[str, Any]:
        from zero.domain.identity import ProjectId

        actor = authorized_actor(request, services, project_id, "agent.manage")
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

        authorized_actor(request, services, project_id, "project.view")
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

        actor = authorized_actor(request, services, project_id, "agent.manage")
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

        authorized_actor(request, services, project_id, "project.view")
        try:
            item = services.agent_types.get_type(ProjectId(project_id), AgentTypeId(type_id))
        except Exception as exc:
            raise HTTPException(status_code=404, detail="Agent type not found") from exc
        return _agent_type_payload(item)

    @app.get("/projects/{project_id}/agent-types/{type_id}/knowledge", tags=["topology"])
    def list_knowledge(request: Request, project_id: str, type_id: str) -> list[dict[str, Any]]:
        from zero.domain.agent_types import AgentTypeId
        from zero.domain.identity import ProjectId

        authorized_actor(request, services, project_id, "project.view")
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

        actor = authorized_actor(request, services, project_id, "agent.manage")
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

        authorized_actor(request, services, project_id, "project.view")
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
        authorized_actor(request, services, project_id, "model.change")
        return [_provider_model_payload(item) for item in services.providers.list_models()]

    @app.get("/projects/{project_id}/providers/requests", tags=["providers"])
    def list_provider_requests(request: Request, project_id: str) -> list[dict[str, Any]]:
        from zero.domain.identity import ProjectId

        actor = authorized_actor(request, services, project_id, "cost.view")
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

        authorized_actor(request, services, project_id, "cost.view")
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

        actor = authorized_actor(request, services, project_id, "execution.start")
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

        actor = authorized_actor(request, services, project_id, "cost.view")
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
        from zero.domain.identity import ProjectId

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
        from zero.domain.identity import ProjectId

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
        from zero.domain.identity import ProjectId
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
        authorized_actor(request, services, project_id, "project.view")
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

        authorized_actor(request, services, project_id, "project.view")
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

        actor = authorized_actor(request, services, project_id, "integration.authorize_merge")
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

        authorized_actor(request, services, project_id, "project.view")
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

        actor = authorized_actor(request, services, project_id, "integration.authorize_merge")
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

        authorized_actor(request, services, project_id, "project.view")
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

        actor = authorized_actor(request, services, project_id, "integration.authorize_merge")
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

        authorized_actor(request, services, project_id, "project.view")
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

        actor = authorized_actor(request, services, project_id, "integration.authorize_merge")
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

        authorized_actor(request, services, project_id, "execution.view_diffs")
        return [
            _result_delivery_payload(item)
            for item in services.result_delivery.list_for_project(ProjectId(project_id))
        ]

    @app.post("/projects/{project_id}/result-deliveries/drain", tags=["interfaces"])
    def drain_result_delivery(request: Request, project_id: str) -> dict[str, Any]:
        from zero.domain.identity import ProjectId

        authorized_actor(request, services, project_id, "execution.view_diffs")
        if not services.result_delivery.is_outbound_configured:
            raise HTTPException(status_code=503, detail="outbound result delivery unavailable")
        item = services.result_delivery.drain_once(project_id=ProjectId(project_id))
        return {"status": "empty"} if item is None else _result_delivery_payload(item)

    @app.get("/projects/{project_id}/interfaces", tags=["interfaces"])
    def list_bindings(request: Request, project_id: str) -> list[dict[str, Any]]:
        from zero.domain.identity import ProjectId

        authorized_actor(request, services, project_id, "project.view")
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

        actor = authorized_actor(request, services, project_id, "agent.manage")
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

        authorized_actor(request, services, project_id, "project.view")
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

        actor = authorized_actor(request, services, project_id, "agent.manage")
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
