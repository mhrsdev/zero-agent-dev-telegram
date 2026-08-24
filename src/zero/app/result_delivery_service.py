"""Durable execution-result delivery boundary.

Scheduler completion records a project-scoped outbound intent here. A separate
bounded drain is responsible for provider I/O and records a receipt or a
retryable failure; a provider response is never inferred from queue state.
"""

from __future__ import annotations

from threading import Event

from zero.app.authorization_service import AuthorizationService
from zero.app.interface_transport_service import (
    InterfaceTransportError,
    InterfaceTransportService,
    InterfaceTransportUnknownOutcome,
)
from zero.domain.audit import AuditSource, redact_sensitive_text
from zero.domain.execution import ExecutionId
from zero.domain.identity import ProjectId, UserId
from zero.domain.ids import generate_interface_delivery_id
from zero.domain.interfaces import (
    InterfaceBindingId,
    InterfaceDeliveryId,
    ResultDelivery,
)
from zero.persistence.repositories.execution_repository import ExecutionRepository
from zero.persistence.repositories.interface_repository import InterfaceRepository


class ResultDeliveryError(RuntimeError):
    """A durable result could not be queued or delivered."""


class ResultDeliveryService:
    """Queue, claim, and reconcile outbound execution-result deliveries."""

    def __init__(
        self,
        interface_repo: InterfaceRepository,
        execution_repo: ExecutionRepository,
        authorization: AuthorizationService,
        transport: InterfaceTransportService,
    ) -> None:
        self._interface_repo = interface_repo
        self._execution_repo = execution_repo
        self._authorization = authorization
        self._transport = transport

    def enqueue_execution_result(
        self,
        *,
        project_id: ProjectId,
        execution_id: ExecutionId,
        binding_id: InterfaceBindingId,
        actor_id: UserId,
        content: str,
        source: AuditSource = "system",
    ) -> ResultDelivery:
        self._authorization.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="execution.view_diffs",
            source=source,
        )
        if not content or len(content) > 32_000:
            raise ValueError("delivery content must contain between 1 and 32000 characters")
        execution = self._execution_repo.get_execution(execution_id, project_id=project_id)
        if execution.state not in {"completed", "failed", "cancelled"}:
            raise ResultDeliveryError(
                f"execution {execution.id.value} is not terminal; result delivery is deferred"
            )
        binding = self._interface_repo.get_binding_by_id(project_id, binding_id)
        if not binding.is_enabled:
            raise ResultDeliveryError("result binding is disabled")
        delivery_key = f"execution:{execution.id.value}:binding:{binding.id.value}:result:v1"
        delivery = ResultDelivery(
            id=InterfaceDeliveryId(generate_interface_delivery_id()),
            project_id=project_id,
            execution_id=execution.id.value,
            binding_id=binding.id,
            created_by=actor_id,
            delivery_key=delivery_key,
            content=content,
            state="pending",
            attempt_count=0,
        )
        return self._interface_repo.insert_result_delivery(delivery)

    def list_pending(self, project_id: ProjectId) -> list[ResultDelivery]:
        return self._interface_repo.list_result_deliveries(project_id, state="pending")

    def list_for_project(self, project_id: ProjectId) -> list[ResultDelivery]:
        return self._interface_repo.list_result_deliveries(project_id)

    def list_enabled_bindings(self, project_id: ProjectId):
        return [
            binding
            for binding in self._interface_repo.list_bindings_for_project(project_id)
            if binding.is_enabled
        ]

    def drain_once(
        self,
        *,
        project_id: ProjectId,
        lease_seconds: int = 300,
        max_attempts: int = 5,
    ) -> ResultDelivery | None:
        delivery = self._interface_repo.claim_result_delivery(
            project_id,
            lease_seconds=lease_seconds,
            max_attempts=max_attempts,
        )
        if delivery is None:
            return None
        try:
            external_message_id = self._transport.send_message(
                project_id=project_id,
                binding_id=delivery.binding_id,
                actor_id=delivery.created_by,
                text=delivery.content,
            )
        except InterfaceTransportUnknownOutcome as exc:
            self._interface_repo.mark_result_delivery_unknown(
                project_id,
                delivery.id,
                claim_token=delivery.claim_token or "",
                error=redact_sensitive_text(str(exc)),
            )
            return self._interface_repo.get_result_delivery(project_id, delivery.id)
        except InterfaceTransportError as exc:
            retry_after_seconds = min(3_600, 2 ** min(delivery.attempt_count, 10))
            self._interface_repo.fail_result_delivery(
                project_id,
                delivery.id,
                claim_token=delivery.claim_token or "",
                error=redact_sensitive_text(str(exc)),
                retry_after_seconds=retry_after_seconds,
            )
            return self._interface_repo.get_result_delivery(project_id, delivery.id)
        self._interface_repo.complete_result_delivery(
            project_id,
            delivery.id,
            claim_token=delivery.claim_token or "",
            external_message_id=external_message_id,
        )
        return self._interface_repo.get_result_delivery(project_id, delivery.id)

    def recover_stale(self, project_id: ProjectId | None = None) -> int:
        return self._interface_repo.recover_result_deliveries(project_id)

    @property
    def is_outbound_configured(self) -> bool:
        return self._transport.is_outbound_configured

    def run_forever(
        self,
        *,
        project_id: ProjectId,
        stop_event: Event,
        interval_seconds: float = 1.0,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("delivery interval must be positive")
        while not stop_event.is_set():
            self.drain_once(project_id=project_id)
            stop_event.wait(interval_seconds)


__all__ = ["ResultDeliveryError", "ResultDeliveryService"]
