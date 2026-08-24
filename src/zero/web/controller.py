"""Web controller — serves HTML pages backed by real backend APIs.

Per ``zero-web-control-surface`` SKILL.md:
- The browser holds a projection. Every protected mutation reaches a
  backend operation that revalidates actor, project, revision, and
  transition.
- URL structure is not authorization. Server-side loaders apply the
  same policy as API and messaging adapters.
- Accessible semantics are the minimum interface.
- Mobile is a real control surface.

Per PLAN.md M12 slice order:
1. Account identity and project selection.
2. Project membership and current permissions.
3. Plan proposal, revision, approval, and rejection.
4. Execution graph and live status.
5. Task diffs, tests, blockers, and integration decision.
6. Agent topology, tools, model/provider policy, usage, and audit views.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from zero.app.auth_service import AuthenticationError, authenticated_actor
from zero.app.services import Services
from zero.config import Settings
from zero.domain.authorization import AuthorizationError
from zero.domain.execution import (
    ExecutionError,
    ExecutionId,
    InvalidExecutionTransitionError,
)
from zero.domain.identity import (
    IdentityError,
    MembershipAlreadyExistsError,
    ProjectId,
    ProjectNotFoundError,
    UserId,
    UserNotFoundError,
)
from zero.domain.plans import (
    ConversationEventId,
    InvalidPlanTransitionError,
    PlanContentValidationError,
    PlanId,
    PlanNotFoundError,
    PlanRevisionContent,
    StaleRevisionError,
)


def create_web_router(services: Services, settings: Settings) -> APIRouter:
    templates_dir = Path(__file__).parent / "templates"
    templates = Jinja2Templates(directory=str(templates_dir))
    router = APIRouter(prefix="/web", tags=["web"])

    def _ctx(request: Request, **kw: Any) -> dict:
        ctx = {"request": request}
        ctx.update(kw)
        return ctx

    def _plan_in_project(project_id: str, plan_id: str, actor_id: UserId):
        return services.plans.get_plan(
            PlanId(plan_id),
            project_id=ProjectId(project_id),
            actor_id=actor_id,
            source="web",
        )

    def _web_actor(request: Request, project_id: str, claimed_id: str | None = None) -> UserId:
        if getattr(request.state, "user_id", None) is not None:
            authenticated = authenticated_actor(
                request.state.user_id.value if claimed_id is None else claimed_id
            )
            if claimed_id or authenticated.value != "zu_system":
                return authenticated
        if claimed_id:
            return UserId(claimed_id)
        return services.identity.get_project(ProjectId(project_id)).owner_user_id

    def _execution_in_project(
        project_id: str,
        execution_id: str,
        request: Request,
        claimed_actor_id: str | None = None,
    ):
        project = ProjectId(project_id)
        actor = _web_actor(request, project_id, claimed_actor_id)
        execution = services.worker.get_execution(
            ExecutionId(execution_id),
            project_id=project,
            actor_id=actor,
            source="web",
        )
        return execution, actor

    @router.get("/login", response_class=HTMLResponse)
    def login_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "login.html",
            _ctx(request),
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/login", response_class=HTMLResponse)
    def login(request: Request, access_token: str = Form(...)) -> HTMLResponse:
        origin = request.headers.get("origin")
        expected_origin = f"{request.url.scheme}://{request.headers.get('host', '')}"
        if origin != expected_origin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cross-origin login refused",
            )
        try:
            services.auth.authenticate(access_token)
        except AuthenticationError:
            return templates.TemplateResponse(
                request,
                "login.html",
                _ctx(request, error="Invalid or expired access token"),
                status_code=status.HTTP_401_UNAUTHORIZED,
                headers={"Cache-Control": "no-store"},
            )
        response = RedirectResponse(
            url="/web/",
            status_code=status.HTTP_303_SEE_OTHER,
        )
        response.set_cookie(
            "zero_access_token",
            access_token,
            max_age=86400,
            httponly=True,
            secure=settings.is_production,
            samesite="strict",
            path="/",
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.post("/logout", response_class=HTMLResponse)
    def logout(request: Request) -> HTMLResponse:
        services.auth.revoke(request.state.access_token, authenticated_actor())
        response = RedirectResponse(
            url="/web/login",
            status_code=status.HTTP_303_SEE_OTHER,
        )
        response.delete_cookie("zero_access_token", path="/")
        response.headers["Cache-Control"] = "no-store"
        return response

    # ------------------------------------------------------------------
    # Dashboard
    # ------------------------------------------------------------------

    def _project_rows(limit: int | None = None) -> list[dict]:
        conn = services.database.connect()
        if settings.auth_required:
            cursor = conn.execute(
                "SELECT p.id, p.name, p.owner_user_id, p.created_at "
                "FROM projects AS p JOIN project_memberships AS m "
                "ON m.project_id = p.id WHERE m.user_id = ? "
                "ORDER BY p.created_at DESC" + (" LIMIT ?" if limit else ""),
                ((authenticated_actor().value, limit) if limit else (authenticated_actor().value,)),
            )
        else:
            cursor = conn.execute(
                "SELECT id, name, owner_user_id, created_at FROM projects "
                "ORDER BY created_at DESC" + (" LIMIT ?" if limit else ""),
                ((limit,) if limit else ()),
            )
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "owner_user_id": row["owner_user_id"],
                "created_at": row["created_at"],
            }
            for row in cursor.fetchall()
        ]

    @router.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> HTMLResponse:
        health = request.app.state.health_service.report()
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            _ctx(request, health=health.to_dict(), projects=_project_rows(10)),
        )

    # ------------------------------------------------------------------
    # Users (Slice 1)
    # ------------------------------------------------------------------

    def _all_users() -> list[dict]:
        conn = services.database.connect()
        if settings.auth_required:
            actor = authenticated_actor().value
            cursor = conn.execute(
                "SELECT DISTINCT u.id, u.display_name, u.status, u.created_at "
                "FROM users AS u WHERE u.id = ? OR EXISTS ("
                "SELECT 1 FROM project_memberships AS mine "
                "JOIN project_memberships AS peer "
                "ON peer.project_id = mine.project_id "
                "WHERE mine.user_id = ? AND peer.user_id = u.id) "
                "ORDER BY u.created_at DESC",
                (actor, actor),
            )
        else:
            cursor = conn.execute(
                "SELECT id, display_name, status, created_at FROM users ORDER BY created_at DESC"
            )
        return [
            {
                "id": row["id"],
                "display_name": row["display_name"],
                "status": row["status"],
                "created_at": row["created_at"],
            }
            for row in cursor.fetchall()
        ]

    @router.get("/users", response_class=HTMLResponse)
    def list_users(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "users.html",
            _ctx(request, users=_all_users()),
        )

    @router.post("/users", response_class=HTMLResponse)
    def create_user(request: Request, display_name: str = Form(...)) -> HTMLResponse:
        try:
            services.identity.create_user(display_name=display_name, source="web")
            return RedirectResponse(
                url="/web/users",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        except (IdentityError, ValueError):
            return templates.TemplateResponse(
                request,
                "users.html",
                _ctx(request, users=_all_users(), error="request failed"),
                status_code=status.HTTP_400_BAD_REQUEST,
            )

    # ------------------------------------------------------------------
    # Projects (Slice 1)
    # ------------------------------------------------------------------

    def _all_projects() -> list[dict]:
        return _project_rows()

    @router.get("/projects", response_class=HTMLResponse)
    def list_projects(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "projects.html",
            _ctx(request, projects=_all_projects()),
        )

    @router.post("/projects", response_class=HTMLResponse)
    def create_project(
        request: Request,
        owner_id: str = Form(...),
        name: str = Form(...),
    ) -> HTMLResponse:
        try:
            project = services.identity.create_project(
                owner_id=authenticated_actor(owner_id), name=name, source="web"
            )
            return RedirectResponse(
                url=f"/web/projects/{project.id.value}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        except (UserNotFoundError, ValueError):
            return templates.TemplateResponse(
                request,
                "projects.html",
                _ctx(request, projects=_all_projects(), error="request failed"),
                status_code=status.HTTP_400_BAD_REQUEST,
            )

    # ------------------------------------------------------------------
    # Project detail (Slice 2 + 4 + 6)
    # ------------------------------------------------------------------

    @router.get("/projects/{project_id}", response_class=HTMLResponse)
    def project_detail(request: Request, project_id: str) -> HTMLResponse:
        try:
            project = services.identity.get_project(ProjectId(project_id))
        except (ProjectNotFoundError, ValueError):
            raise HTTPException(status_code=404, detail="request failed")

        actor_id = _web_actor(request, project_id)
        members_raw = services.identity.list_members(project.id, actor_id)
        members = [
            {"user_id": m.user_id.value, "role": m.role, "created_at": m.created_at}
            for m in members_raw
        ]
        plans_raw = services.plans.list_plans_for_project(
            project.id, actor_id=actor_id, source="web"
        )
        plans = [
            {
                "id": p.id.value,
                "current_state": p.current_state,
                "current_revision_number": p.current_revision_number,
                "created_at": p.created_at,
            }
            for p in plans_raw
        ]
        conn = services.database.connect()
        cursor = conn.execute(
            "SELECT id, plan_id, state, blocker_reason, created_at "
            "FROM executions WHERE project_id = ? "
            "ORDER BY created_at DESC",
            (project_id,),
        )
        executions = [
            {
                "id": row["id"],
                "plan_id": row["plan_id"],
                "state": row["state"],
                "blocker_reason": row["blocker_reason"],
                "created_at": row["created_at"],
            }
            for row in cursor.fetchall()
        ]
        agent_types_raw = services.agent_types.list_types(project.id, include_archived=True)
        agent_types = [
            {
                "id": t.id.value,
                "name": t.name,
                "responsibility": t.responsibility,
                "state": t.state,
                "max_concurrent_instances": t.max_concurrent_instances,
            }
            for t in agent_types_raw
        ]
        try:
            audit_raw = services.audit.list_for_project(
                project_id=project.id,
                actor_id=actor_id,
                limit=20,
                source="web",
            )
        except AuthorizationError:
            audit_raw = []
        audit_events = [
            {
                "created_at": e.created_at,
                "operation": e.operation,
                "target_id": e.target_id,
                "result": e.result,
            }
            for e in audit_raw
        ]
        return templates.TemplateResponse(
            request,
            "project_detail.html",
            _ctx(
                request,
                project={
                    "id": project.id.value,
                    "name": project.name,
                    "owner_user_id": project.owner_user_id.value,
                    "created_at": project.created_at,
                },
                members=members,
                plans=plans,
                executions=executions,
                agent_types=agent_types,
                audit_events=audit_events,
            ),
        )

    @router.post("/projects/{project_id}/members", response_class=HTMLResponse)
    def add_member(
        request: Request,
        project_id: str,
        member_id: str = Form(...),
        role: str = Form(...),
    ) -> HTMLResponse:
        try:
            project = services.identity.get_project(ProjectId(project_id))
            services.identity.add_member(
                project_id=project.id,
                actor_id=authenticated_actor(project.owner_user_id.value),
                member_id=UserId(member_id),
                role=role,  # type: ignore[arg-type]
                source="web",
            )
            return RedirectResponse(
                url=f"/web/projects/{project_id}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        except (UserNotFoundError, ProjectNotFoundError, ValueError):
            raise HTTPException(status_code=400, detail="request failed")
        except MembershipAlreadyExistsError:
            raise HTTPException(status_code=409, detail="request failed")

    # ------------------------------------------------------------------
    # Plans (Slice 3)
    # ------------------------------------------------------------------

    @router.post("/projects/{project_id}/plans", response_class=HTMLResponse)
    def create_plan(request: Request, project_id: str, actor_id: str = Form(...)) -> HTMLResponse:
        try:
            plan = services.plans.create_plan(
                project_id=ProjectId(project_id),
                actor_id=authenticated_actor(actor_id),
                source="web",
            )
            return RedirectResponse(
                url=f"/web/projects/{project_id}/plans/{plan.id.value}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        except (AuthorizationError, ValueError):
            raise HTTPException(status_code=403, detail="request failed")

    @router.get(
        "/projects/{project_id}/plans/{plan_id}",
        response_class=HTMLResponse,
    )
    def plan_detail(request: Request, project_id: str, plan_id: str) -> HTMLResponse:
        try:
            project = services.identity.get_project(ProjectId(project_id))
            actor = _web_actor(request, project_id)
            plan = _plan_in_project(project_id, plan_id, actor)
            revisions_raw = services.plans.list_revisions(
                PlanId(plan_id),
                project_id=project.id,
                actor_id=actor,
                source="web",
            )
            revisions = [
                {
                    "revision_number": r.revision_number,
                    "state": r.state,
                    "objective": r.content.objective,
                    "created_at": r.created_at,
                }
                for r in revisions_raw
            ]
            handoff = None
            if plan.current_state == "approved":
                current_rev = services.plans.get_current_revision(
                    PlanId(plan_id),
                    project_id=project.id,
                    actor_id=actor,
                    source="web",
                )
                h = services.plans.get_handoff_for_revision(
                    current_rev.id,
                    project_id=project.id,
                    actor_id=actor,
                    source="web",
                )
                if h:
                    handoff = {
                        "id": h.id.value,
                        "execution_id": h.execution_id,
                        "created_at": h.created_at,
                    }
        except (PlanNotFoundError, ProjectNotFoundError, ValueError):
            raise HTTPException(status_code=404, detail="request failed")
        return templates.TemplateResponse(
            request,
            "plan_detail.html",
            _ctx(
                request,
                project={
                    "id": project.id.value,
                    "name": project.name,
                    "owner_user_id": project.owner_user_id.value,
                },
                plan={
                    "id": plan.id.value,
                    "current_state": plan.current_state,
                    "current_revision_number": plan.current_revision_number,
                    "created_at": plan.created_at,
                },
                revisions=revisions,
                handoff=handoff,
            ),
        )

    @router.post(
        "/projects/{project_id}/plans/{plan_id}/revisions",
        response_class=HTMLResponse,
    )
    def propose_revision(
        request: Request,
        project_id: str,
        plan_id: str,
        actor_id: str = Form(...),
        objective: str = Form(...),
        acceptance_criteria: str = Form(...),
        source_event_ids: str = Form(...),
    ) -> HTMLResponse:
        try:
            _plan_in_project(project_id, plan_id, authenticated_actor(actor_id))
            criteria = tuple(
                line.strip() for line in acceptance_criteria.strip().splitlines() if line.strip()
            )
            event_ids = tuple(
                ConversationEventId(eid.strip())
                for eid in source_event_ids.split(",")
                if eid.strip()
            )
            content = PlanRevisionContent(
                objective=objective,
                scope=(),
                constraints=(),
                acceptance_criteria=criteria,
                risks=(),
                unresolved_questions=(),
                source_event_ids=event_ids,
            )
            services.plans.propose_revision(
                plan_id=PlanId(plan_id),
                project_id=ProjectId(project_id),
                actor_id=authenticated_actor(actor_id),
                content=content,
                source="web",
            )
            return RedirectResponse(
                url=f"/web/projects/{project_id}/plans/{plan_id}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        except (PlanContentValidationError, ValueError):
            raise HTTPException(status_code=400, detail="request failed")
        except AuthorizationError:
            raise HTTPException(status_code=403, detail="request failed")

    @router.post(
        "/projects/{project_id}/plans/{plan_id}/approve",
        response_class=HTMLResponse,
    )
    def approve_plan(
        request: Request,
        project_id: str,
        plan_id: str,
        actor_id: str = Form(...),
        expected_revision_number: int = Form(...),
        idempotency_key: str = Form(...),
    ) -> HTMLResponse:
        try:
            _plan_in_project(project_id, plan_id, authenticated_actor(actor_id))
            services.plans.approve_revision(
                plan_id=PlanId(plan_id),
                project_id=ProjectId(project_id),
                actor_id=authenticated_actor(actor_id),
                expected_revision_number=expected_revision_number,
                idempotency_key=idempotency_key,
                source="web",
            )
            return RedirectResponse(
                url=f"/web/projects/{project_id}/plans/{plan_id}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        except StaleRevisionError:
            raise HTTPException(status_code=409, detail="stale revision")
        except AuthorizationError:
            raise HTTPException(status_code=403, detail="request failed")
        except (InvalidPlanTransitionError, ValueError):
            raise HTTPException(status_code=400, detail="request failed")

    @router.post(
        "/projects/{project_id}/plans/{plan_id}/reject",
        response_class=HTMLResponse,
    )
    def reject_plan(
        request: Request,
        project_id: str,
        plan_id: str,
        actor_id: str = Form(...),
        expected_revision_number: int = Form(...),
        idempotency_key: str = Form(...),
    ) -> HTMLResponse:
        try:
            _plan_in_project(project_id, plan_id, authenticated_actor(actor_id))
            services.plans.reject_revision(
                plan_id=PlanId(plan_id),
                project_id=ProjectId(project_id),
                actor_id=authenticated_actor(actor_id),
                expected_revision_number=expected_revision_number,
                idempotency_key=idempotency_key,
                source="web",
            )
            return RedirectResponse(
                url=f"/web/projects/{project_id}/plans/{plan_id}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        except StaleRevisionError:
            raise HTTPException(status_code=409, detail="request failed")
        except AuthorizationError:
            raise HTTPException(status_code=403, detail="request failed")

    # ------------------------------------------------------------------
    # Executions (Slice 4)
    # ------------------------------------------------------------------

    @router.get(
        "/projects/{project_id}/executions/{execution_id}",
        response_class=HTMLResponse,
    )
    def execution_detail(request: Request, project_id: str, execution_id: str) -> HTMLResponse:
        try:
            project = services.identity.get_project(ProjectId(project_id))
            execution, actor = _execution_in_project(project_id, execution_id, request)
            tasks_raw = services.worker.list_tasks(
                ExecutionId(execution_id),
                project_id=ProjectId(project_id),
                actor_id=actor,
                source="web",
            )
            tasks = [
                {
                    "id": t.id.value,
                    "objective": t.objective,
                    "state": t.state,
                    "blocker_reason": t.blocker_reason,
                    "created_at": t.created_at,
                }
                for t in tasks_raw
            ]
        except (ExecutionError, ProjectNotFoundError, ValueError):
            raise HTTPException(status_code=404, detail="request failed")
        return templates.TemplateResponse(
            request,
            "execution_detail.html",
            _ctx(
                request,
                project={
                    "id": project.id.value,
                    "name": project.name,
                    "owner_user_id": project.owner_user_id.value,
                },
                execution={
                    "id": execution.id.value,
                    "plan_id": execution.plan_id.value,
                    "state": execution.state,
                    "blocker_reason": execution.blocker_reason,
                    "created_at": execution.created_at,
                },
                tasks=tasks,
            ),
        )

    @router.post(
        "/projects/{project_id}/executions/{execution_id}/cancel",
        response_class=HTMLResponse,
    )
    def cancel_execution(
        request: Request,
        project_id: str,
        execution_id: str,
        actor_id: str = Form(...),
    ) -> HTMLResponse:
        try:
            _execution, actor = _execution_in_project(
                project_id,
                execution_id,
                request,
                actor_id,
            )
            services.worker.cancel_execution(
                execution_id=ExecutionId(execution_id),
                project_id=ProjectId(project_id),
                actor_id=actor,
                source="web",
            )
            return RedirectResponse(
                url=f"/web/projects/{project_id}/executions/{execution_id}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        except AuthorizationError:
            raise HTTPException(status_code=403, detail="request failed")
        except InvalidExecutionTransitionError:
            raise HTTPException(status_code=409, detail="request failed")

    @router.post(
        "/projects/{project_id}/executions/{execution_id}/recover",
        response_class=HTMLResponse,
    )
    def recover_execution(
        request: Request,
        project_id: str,
        execution_id: str,
        actor_id: str = Form(...),
    ) -> HTMLResponse:
        try:
            _execution, actor = _execution_in_project(
                project_id,
                execution_id,
                request,
                actor_id,
            )
            services.worker.recover_after_restart(
                execution_id=ExecutionId(execution_id),
                project_id=ProjectId(project_id),
                actor_id=actor,
                source="web",
            )
            return RedirectResponse(
                url=f"/web/projects/{project_id}/executions/{execution_id}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        except (AuthenticationError, AuthorizationError, ExecutionError, ValueError):
            raise HTTPException(status_code=400, detail="request failed")

    # ------------------------------------------------------------------
    # Audit log (Slice 6)
    # ------------------------------------------------------------------

    @router.get("/audit", response_class=HTMLResponse)
    def audit_log(request: Request) -> HTMLResponse:
        if settings.auth_required:
            actor_id = authenticated_actor()
            raw_events = []
            for project in _project_rows():
                try:
                    raw_events.extend(
                        services.audit.list_for_project(
                            project_id=ProjectId(project["id"]),
                            actor_id=actor_id,
                            limit=100,
                            source="web",
                        )
                    )
                except AuthorizationError:
                    continue
            raw_events.sort(key=lambda event: event.created_at, reverse=True)
            events = [
                {
                    "id": event.id.value,
                    "project_id": event.project_id.value if event.project_id else None,
                    "actor_id": event.actor_id.value if event.actor_id else None,
                    "source": event.source,
                    "operation": event.operation,
                    "target_id": event.target_id,
                    "result": event.result,
                    "redacted_summary": event.redacted_summary,
                    "created_at": event.created_at,
                }
                for event in raw_events[:100]
            ]
        else:
            cursor = services.database.connect().execute(
                "SELECT id, project_id, actor_id, source, operation, "
                "target_type, target_id, result, correlation_id, "
                "redacted_summary, created_at FROM audit_events "
                "ORDER BY created_at DESC LIMIT 100"
            )
            events = [
                {
                    "id": row["id"],
                    "project_id": row["project_id"],
                    "actor_id": row["actor_id"],
                    "source": row["source"],
                    "operation": row["operation"],
                    "target_id": row["target_id"],
                    "result": row["result"],
                    "redacted_summary": row["redacted_summary"],
                    "created_at": row["created_at"],
                }
                for row in cursor.fetchall()
            ]
        return templates.TemplateResponse(
            request,
            "audit.html",
            _ctx(request, events=events),
        )

    return router
