"""Plan routes extracted from app.api."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field

from zero.app.auth_service import (
    authenticated_actor,
)
from zero.app.services import Services
from zero.domain.identity import (
    ProjectId,
)
from zero.domain.plans import PlanError


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


def register_plan_routes(app: FastAPI, services: Services) -> None:
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
