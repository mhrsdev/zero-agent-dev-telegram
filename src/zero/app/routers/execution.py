"""Execution routes extracted from app.api."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from zero.app.auth_service import (
    request_actor,
)
from zero.app.services import Services
from zero.domain.authorization import AuthorizationError
from zero.domain.execution import ExecutionError
from zero.domain.identity import (
    ProjectId,
    UserId,
)
from zero.domain.plans import PlanError


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


def register_execution_routes(app: FastAPI, services: Services) -> None:
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
                "next_retry_at": t.next_retry_at,
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
            hub = getattr(app.state, "stream_hub", None)

            def _publish_stream(execution_value: str, payload: dict) -> None:
                if hub is not None:
                    hub.publish(execution_value, payload)

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
                stream_callback=_publish_stream,
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
