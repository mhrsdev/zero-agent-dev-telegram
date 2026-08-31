"""Per-call tool approval REST surface (GAP 8b/G2, Hermes parity).

Reads follow the project-view membership boundary; resolutions require
``tool.manage`` (owners/administrators) so a member cannot self-approve
a dangerous call. Pending requests are created by the AgentRuntime when
``ZERO_TOOL_APPROVAL_MODE=manual``; this surface is how humans answer.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, status

from zero.app.approval_gate import (
    ApprovalError,
    ToolNotFoundDuringApproval,
)
from zero.app.auth_service import authenticated_actor
from zero.app.services import Services
from zero.domain.authorization import AuthorizationError
from zero.domain.identity import ProjectId


def register_tool_approval_routes(app: FastAPI, services: Services) -> None:
    def _gate_or_409() -> Any:
        """Resolve the gate; 409 when the posture is off (fix 14).

        The gate is now constructed in every mode, so "disabled" means
        the operator retuned ``approvals.mode`` to ``off`` — the REST
        surface must keep refusing to look like approvals exist.
        """
        gate = services.approval_gate
        if gate is None or gate.mode == "off":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "tool_approval_disabled",
                    "hint": "Set approvals.mode=manual (config.yaml) or "
                    "ZERO_TOOL_APPROVAL_MODE=manual to enable the gate.",
                },
            )
        return gate

    @app.get(
        "/projects/{project_id}/tool-approvals",
        tags=["tool-approvals"],
    )
    def list_tool_approvals(project_id: str, actor_id: str) -> dict[str, Any]:
        gate = _gate_or_409()
        try:
            decision = services.authorization.authorize(
                actor_id=authenticated_actor(actor_id),
                project_id=ProjectId(project_id),
                permission="project.view",
                source="web",
            )
        except AuthorizationError:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden") from None
        if not decision.allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
        try:
            pending = gate.list_pending(project_id=project_id)
        except ApprovalError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return {
            "pending": [
                {
                    "id": r.id,
                    "execution_id": r.execution_id,
                    "tool_name": r.tool_name,
                    "grain": r.grain,
                    "created_at": r.created_at,
                }
                for r in pending
            ]
        }

    @app.post(
        "/projects/{project_id}/tool-approvals/{request_id}/resolve",
        tags=["tool-approvals"],
    )
    def resolve_tool_approval(
        project_id: str,
        request_id: str,
        req: dict[str, Any],
    ) -> dict[str, Any]:
        gate = _gate_or_409()
        claimed_actor = str(req.get("actor_id", "")).strip() or None
        try:
            services.authorization.require_permission(
                actor_id=authenticated_actor(claimed_actor),
                project_id=ProjectId(project_id),
                permission="tool.manage",
                source="web",
            )
        except AuthorizationError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden") from exc
        except LookupError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="actor_id is required for resolve",
            ) from None
        try:
            resolved = gate.resolve(
                request_id,
                decision=str(req.get("decision", "")).strip().lower(),
                decided_by_user_id=claimed_actor,
                grain=str(req.get("grain", "once") or "once").strip().lower(),
                reason=(str(req["reason"]) if req.get("reason") else None),
            )
        except ToolNotFoundDuringApproval as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ApprovalError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return {
            "approval": {
                "id": resolved.id,
                "decision": resolved.decision,
                "grain": resolved.grain,
                "decided_by_user_id": resolved.decided_by_user_id,
                "resolved_at": resolved.resolved_at,
            }
        }
