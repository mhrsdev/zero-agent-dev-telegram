"""Integration routes extracted from app.api."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request, status

from zero.app.routers.deps import authorized_actor
from zero.app.routers.models import CreateIntegrationReviewRequest, CreateMergeProposalRequest
from zero.app.services import Services
from zero.domain.identity import (
    ProjectId,
)


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


def register_integration_routes(app: FastAPI, services: Services) -> None:
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
