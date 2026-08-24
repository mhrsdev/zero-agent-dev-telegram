"""Service bundle — wires all Phase 6 services together.

This module is the single place where Phase 6 services are constructed
and wired. It depends on every layer: ``domain`` types, ``app``
services, ``persistence`` repositories, and the :class:`Database`
connection. It is imported by ``app/api.py`` (which builds the FastAPI
app) and by tests.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from zero.adapters.messaging import HttpTransport
from zero.app.agent_runtime import AgentRuntime
from zero.app.agent_type_service import AgentTypeService
from zero.app.artifact_service import ArtifactService
from zero.app.audit_service import AuditService
from zero.app.auth_service import AuthService
from zero.app.authorization_service import AuthorizationService
from zero.app.compaction_service import CompactionService
from zero.app.identity_service import IdentityService
from zero.app.integration_service import IntegrationService
from zero.app.interface_service import InterfaceAdapterService
from zero.app.interface_transport_service import InterfaceTransportService
from zero.app.observability_service import (
    BackupService,
    MetricsService,
    RecoveryService,
    SecretCanaryScan,
)
from zero.app.plan_service import PlanService
from zero.app.planner_service import PlannerService
from zero.app.provider_adapter import (
    AnthropicMessagesProviderAdapter,
    OpenAICompatibleProviderAdapter,
)
from zero.app.provider_service import ProviderService
from zero.app.result_delivery_service import ResultDeliveryService
from zero.app.retrieval_service import ContextBuilder, RetrievalRouter
from zero.app.scheduler_service import SchedulerService
from zero.app.secret_service import SecretService
from zero.app.tool_service import ToolService
from zero.app.worker_service import WorkerService
from zero.app.worktree_service import WorktreeService
from zero.config import Settings
from zero.persistence.connection import Database
from zero.persistence.repositories.agent_type_repository import (
    AgentTypeRepository,
)
from zero.persistence.repositories.artifact_repository import (
    ArtifactRepository,
)
from zero.persistence.repositories.audit_repository import AuditRepository
from zero.persistence.repositories.context_repository import (
    ContextRepository,
)
from zero.persistence.repositories.execution_repository import (
    ExecutionRepository,
)
from zero.persistence.repositories.identity_repository import (
    IdentityRepository,
)
from zero.persistence.repositories.integration_repository import (
    IntegrationRepository,
)
from zero.persistence.repositories.interface_repository import (
    InterfaceRepository,
)
from zero.persistence.repositories.plan_repository import PlanRepository
from zero.persistence.repositories.provider_repository import (
    ProviderRepository,
)
from zero.persistence.repositories.secret_repository import (
    SecretRepository,
)
from zero.persistence.repositories.tool_repository import ToolRepository
from zero.persistence.repositories.worktree_repository import (
    WorktreeRepository,
)


@dataclass
class Services:
    """The bundle of all Phase 6 application services."""

    database: Database
    identity: IdentityService
    authorization: AuthorizationService
    audit: AuditService
    secrets: SecretService
    tools: ToolService
    plans: PlanService
    planner: PlannerService | None
    worker: WorkerService
    worktree: WorktreeService
    agent_types: AgentTypeService
    artifacts: ArtifactService
    retrieval: RetrievalRouter
    context_builder: ContextBuilder
    compaction: CompactionService
    providers: ProviderService
    integration: IntegrationService
    interfaces: InterfaceAdapterService
    result_delivery: ResultDeliveryService
    metrics: MetricsService
    backup: BackupService
    canary: SecretCanaryScan
    recovery: RecoveryService
    auth: AuthService
    scheduler: SchedulerService | None = None
    runtime: AgentRuntime | None = None
    interface_transports: InterfaceTransportService | None = None


def build_services(
    settings: Settings,
    database: Database,
    *,
    messaging_transport: HttpTransport | None = None,
) -> Services:
    """Construct all Phase 6 services wired to the given database."""
    identity_repo = IdentityRepository(database)
    audit_repo = AuditRepository(database)
    secret_repo = SecretRepository(database)
    tool_repo = ToolRepository(database)
    plan_repo = PlanRepository(database)
    execution_repo = ExecutionRepository(database)
    worktree_repo = WorktreeRepository(database)
    agent_type_repo = AgentTypeRepository(database)
    artifact_repo = ArtifactRepository(database)
    context_repo = ContextRepository(database)
    provider_repo = ProviderRepository(database)
    integration_repo = IntegrationRepository(database)
    interface_repo = InterfaceRepository(database)

    authorization_service = AuthorizationService(identity_repo, audit_repo)
    audit_service = AuditService(audit_repo, authorization_service)
    identity_service = IdentityService(identity_repo, audit_repo, authorization_service)
    auth_service = AuthService(database, identity_service, audit_repo, settings)
    secret_service = SecretService(secret_repo, audit_repo, settings, authorization_service)
    metrics_service = MetricsService()
    tool_service = ToolService(
        tool_repo, audit_repo, authorization_service, metrics=metrics_service
    )
    plan_service = PlanService(plan_repo, audit_repo, authorization_service)
    worker_service = WorkerService(
        execution_repo,
        plan_repo,
        audit_repo,
        artifact_repo,
        authorization_service,
        metrics=metrics_service,
        task_max_attempts=settings.task_max_attempts,
    )
    worktree_service = WorktreeService(
        worktree_repo,
        audit_repo,
        authorization_service,
        execution_repo=execution_repo,
        worktree_root=settings.worktree_root,
        allowed_commands=settings.worktree_allowed_commands,
        isolation_mode=settings.worktree_isolation_mode,
    )
    if not settings.is_test:
        tool_service.register_worktree_tools(worktree_service)
    agent_type_service = AgentTypeService(agent_type_repo, audit_repo, authorization_service)
    artifact_service = ArtifactService(
        artifact_repo, agent_type_repo, audit_repo, authorization_service
    )
    retrieval_router = RetrievalRouter(
        artifact_repo, agent_type_repo, context_repo, authorization_service
    )
    context_builder = ContextBuilder(retrieval_router, context_repo)
    compaction_service = CompactionService(context_repo, artifact_service, authorization_service)
    provider_service = ProviderService(
        provider_repo,
        artifact_service,
        audit_repo,
        authorization_service,
        include_fake=settings.is_test,
        metrics=metrics_service,
    )
    if settings.openai_api_key is not None:
        provider_service.register_adapter(
            OpenAICompatibleProviderAdapter(
                api_key=settings.openai_api_key.get_secret_value(),
                base_url=settings.openai_base_url,
                timeout_seconds=settings.openai_timeout_seconds,
            )
        )
    if settings.anthropic_api_key is not None:
        provider_service.register_adapter(
            AnthropicMessagesProviderAdapter(
                api_key=settings.anthropic_api_key.get_secret_value(),
                base_url=settings.anthropic_base_url,
                timeout_seconds=settings.anthropic_timeout_seconds,
            )
        )
    # Provider fallback routing: when more than one adapter is
    # registered, later registrations act as ordered fallbacks for
    # retryable failures (Hermes parity, Phase 6).
    if len(provider_service.registered_provider_names) > 1:
        provider_service.set_fallback_chain(tuple(provider_service.registered_provider_names))

    def _llm_compaction_summarizer(*, project_id, execution_id, actor_id, messages):
        """LLM checkpoint summarizer for compaction (Hermes parity).

        Uses the first registered provider; returns ``None`` when no
        provider exists so the deterministic fallback summary applies.
        The transcript is bounded and passed as data-only material.
        """
        provider_names = provider_service.registered_provider_names
        if not provider_names:
            return None
        from zero.app.compaction_service import COMPACTION_SUMMARIZER_SYSTEM
        from zero.domain.providers import CanonicalMessage, CanonicalRequest

        lines = [
            f"{record.get('role', 'unknown')}: {str(record.get('content', ''))[:400]}"
            for record in messages[:60]
        ]
        transcript_text = "\n".join(lines)[:48_000]
        import hashlib

        primary = provider_names[0]
        if primary == "anthropic":
            model_name = settings.anthropic_model
        elif primary == "openai-compatible":
            model_name = settings.openai_model
        else:
            # Test fakes register their own catalog names.
            model_name = "fake-standard"
        _request, response = provider_service.send_request(
            project_id=project_id,
            actor_id=actor_id,
            request=CanonicalRequest(
                provider=provider_names[0],
                model_name=model_name,
                system_message=COMPACTION_SUMMARIZER_SYSTEM,
                messages=(CanonicalMessage(role="user", content=transcript_text),),
                max_tokens=4096,
                temperature=0.0,
            ),
            idempotency_key=(
                f"compaction:{execution_id.value}:"
                f"{hashlib.sha256(transcript_text.encode('utf-8')).hexdigest()[:16]}"
            ),
            permission="execution.start",
            source="system",
        )
        return response.content or None

    compaction_service.summarizer = _llm_compaction_summarizer
    planner_service = (
        PlannerService(plan_service, provider_service)
        if settings.openai_api_key is not None
        else None
    )
    integration_service = IntegrationService(
        integration_repo,
        worktree_repo,
        audit_repo,
        authorization_service,
        execution_repo,
        allowed_commands=settings.worktree_allowed_commands,
    )
    interface_service = InterfaceAdapterService(
        interface_repo,
        audit_repo,
        plan_service,
        authorization_service,
        identity_repo,
        secret_service,
        planner=planner_service,
        planner_provider="openai-compatible",
        planner_model=settings.openai_model,
    )
    interface_transport = (
        messaging_transport
        if messaging_transport is not None
        else (None if settings.is_test else httpx.Client())
    )
    interface_transport_service = InterfaceTransportService(
        interface_service,
        interface_repo,
        settings,
        secret_service=secret_service,
        transport=interface_transport,
    )
    result_delivery_service = ResultDeliveryService(
        interface_repo,
        execution_repo,
        authorization_service,
        interface_transport_service,
    )
    agent_runtime = AgentRuntime(
        worker=worker_service,
        providers=provider_service,
        artifacts=artifact_service,
        authorization=authorization_service,
        tools=tool_service,
        worktrees=worktree_service,
        context_builder=context_builder,
        agent_type_repo=agent_type_repo,
        compaction=compaction_service,
    )
    scheduler_service = SchedulerService(
        plans=plan_service,
        worker=worker_service,
        runtime=agent_runtime,
        authorization=authorization_service,
        integration=integration_service,
        result_delivery=result_delivery_service,
        agent_type_repo=agent_type_repo,
        task_max_attempts=settings.task_max_attempts,
    )
    backup_service = BackupService(database)
    canary_service = SecretCanaryScan(
        Services(
            database=database,
            identity=identity_service,
            authorization=authorization_service,
            audit=audit_service,
            secrets=secret_service,
            tools=tool_service,
            plans=plan_service,
            planner=planner_service,
            worker=worker_service,
            worktree=worktree_service,
            agent_types=agent_type_service,
            artifacts=artifact_service,
            retrieval=retrieval_router,
            context_builder=context_builder,
            compaction=compaction_service,
            providers=provider_service,
            integration=integration_service,
            interfaces=interface_service,
            result_delivery=result_delivery_service,
            metrics=metrics_service,
            backup=backup_service,
            canary=None,  # filled below
            recovery=None,  # filled below
            auth=auth_service,
        )
    )
    recovery_service = RecoveryService(
        Services(
            database=database,
            identity=identity_service,
            authorization=authorization_service,
            audit=audit_service,
            secrets=secret_service,
            tools=tool_service,
            plans=plan_service,
            planner=planner_service,
            worker=worker_service,
            worktree=worktree_service,
            agent_types=agent_type_service,
            artifacts=artifact_service,
            retrieval=retrieval_router,
            context_builder=context_builder,
            compaction=compaction_service,
            providers=provider_service,
            integration=integration_service,
            interfaces=interface_service,
            result_delivery=result_delivery_service,
            metrics=metrics_service,
            backup=backup_service,
            canary=canary_service,
            recovery=None,
            auth=auth_service,
        )
    )

    return Services(
        database=database,
        identity=identity_service,
        authorization=authorization_service,
        audit=audit_service,
        secrets=secret_service,
        tools=tool_service,
        plans=plan_service,
        planner=planner_service,
        worker=worker_service,
        worktree=worktree_service,
        agent_types=agent_type_service,
        artifacts=artifact_service,
        retrieval=retrieval_router,
        context_builder=context_builder,
        compaction=compaction_service,
        providers=provider_service,
        integration=integration_service,
        interfaces=interface_service,
        result_delivery=result_delivery_service,
        metrics=metrics_service,
        backup=backup_service,
        canary=canary_service,
        recovery=recovery_service,
        auth=auth_service,
        scheduler=scheduler_service,
        runtime=agent_runtime,
        interface_transports=interface_transport_service,
    )
