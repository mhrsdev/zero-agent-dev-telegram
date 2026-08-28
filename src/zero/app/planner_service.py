"""Provider-backed, approval-gated Main Planner boundary.

The planner only proposes typed data. It never approves a revision, creates an
execution, or executes a tool. Provider output is untrusted and is accepted
only when it is bounded, valid JSON with the required plan fields.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

from zero.app.plan_service import PlanService
from zero.app.provider_service import ProviderService
from zero.domain.audit import AuditSource
from zero.domain.identity import UserId
from zero.domain.plans import (
    ConversationEventId,
    PlanRevision,
    PlanRevisionContent,
)
from zero.domain.providers import CanonicalMessage, CanonicalRequest

_MAX_MODEL_OUTPUT = 64 * 1024
_CODE_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.IGNORECASE | re.DOTALL)


class PlannerOutputError(ValueError):
    """Provider output is not a safe actionable plan document."""


class PlannerService:
    """Convert authenticated human events into proposed plan revisions."""

    def __init__(self, plans: PlanService, providers: ProviderService) -> None:
        self._plans = plans
        self._providers = providers

    def propose_from_event(
        self,
        *,
        event_id: ConversationEventId,
        actor_id: UserId,
        provider: str,
        model_name: str,
        source: AuditSource = "system",
    ) -> PlanRevision | None:
        event = self._plans.get_conversation_event_scoped(
            event_id, actor_id=actor_id, source=source
        )
        if event.actor_id != actor_id or not event.is_authenticated_human:
            raise PlannerOutputError("planner input is not an authenticated human event")
        existing = self._plans.find_revision_by_source_event(
            project_id=event.project_id,
            event_id=event.id,
            actor_id=actor_id,
            source=source,
        )
        if existing is not None:
            return existing

        request = CanonicalRequest(
            provider=provider,
            model_name=model_name,
            # Prompt fix (real run, 2026-08-28): a frontier model
            # intermittently returned a COMPLETE plan with
            # "actionable": false, conflating "never approve or execute"
            # (a safety rule about the planner's own powers) with "a
            # request that needs execution is not actionable". Say both
            # parts explicitly: actionable = concrete implementable work,
            # and proposing a plan executes nothing because approval is
            # a separate human step downstream.
            system_message=(
                "You are Zero's Main Planner. Decide whether the "
                "authenticated request is actionable: concrete, "
                "implementable engineering work that can be decomposed "
                "into tasks with an objective, scope and acceptance "
                "criteria. Casual chat, questions, or requests with no "
                "implementable intent are not actionable. Producing a "
                "plan executes nothing: every plan is reviewed and "
                "approved by a human before any work begins, so "
                "actionability never depends on who will run the work. "
                "You yourself never approve, execute, or claim "
                "completion. Return JSON only. Schema: actionable:boolean, "
                "objective:string, scope:string[], constraints:string[], "
                "acceptance_criteria:string[], risks:string[], "
                "unresolved_questions:string[]."
            ),
            messages=(CanonicalMessage(role="user", content=event.content[:16_384]),),
            max_tokens=4096,
            temperature=0.0,
        )
        _request, response = self._providers.send_request(
            project_id=event.project_id,
            actor_id=actor_id,
            request=request,
            idempotency_key=f"planner:{event.id.value}",
            permission="plan.propose",
            source=source,
        )
        if len(response.content.encode("utf-8")) > _MAX_MODEL_OUTPUT:
            raise PlannerOutputError("planner output exceeds the maximum size")
        payload = self._parse_payload(response.content)
        if not bool(payload.get("actionable", False)):
            return None
        content = PlanRevisionContent(
            objective=self._required_text(payload, "objective", 8_192),
            scope=self._string_tuple(payload, "scope", required=True),
            constraints=self._string_tuple(payload, "constraints"),
            acceptance_criteria=self._string_tuple(payload, "acceptance_criteria", required=True),
            risks=self._string_tuple(payload, "risks"),
            unresolved_questions=self._string_tuple(payload, "unresolved_questions"),
            source_event_ids=(event.id,),
        )
        plan = self._plans.create_plan(
            project_id=event.project_id,
            actor_id=actor_id,
            source=source,
        )
        return self._plans.propose_revision(
            plan_id=plan.id,
            project_id=plan.project_id,
            actor_id=actor_id,
            content=content,
            source=source,
        )

    @staticmethod
    def _parse_payload(content: str) -> Mapping[str, object]:
        candidate = content.strip()
        match = _CODE_FENCE.match(candidate)
        if match:
            candidate = match.group(1)
        try:
            payload = json.loads(candidate)
        except (TypeError, json.JSONDecodeError) as exc:
            raise PlannerOutputError("planner output is not valid JSON") from exc
        if not isinstance(payload, Mapping):
            raise PlannerOutputError("planner output must be a JSON object")
        return payload

    @staticmethod
    def _required_text(payload: Mapping[str, object], key: str, limit: int) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise PlannerOutputError(f"planner field {key!r} must be a bounded non-empty string")
        return value.strip()

    @staticmethod
    def _string_tuple(
        payload: Mapping[str, object],
        key: str,
        *,
        required: bool = False,
    ) -> tuple[str, ...]:
        value = payload.get(key)
        if value is None and not required:
            return ()
        if not isinstance(value, list) or len(value) > 64:
            raise PlannerOutputError(f"planner field {key!r} must be a bounded string array")
        result: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item.strip() or len(item) > 4_096:
                raise PlannerOutputError(f"planner field {key!r} contains an invalid item")
            result.append(item.strip())
        if required and not result:
            raise PlannerOutputError(f"planner field {key!r} must not be empty")
        return tuple(result)


__all__ = ["PlannerOutputError", "PlannerService"]
