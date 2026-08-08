"""Service bundle — wires all Phase 6 services together.

This module is the single place where Phase 6 services are constructed
and wired. It depends on every layer: ``domain`` types, ``app``
services, ``persistence`` repositories, and the :class:`Database`
connection. It is imported by ``app/api.py`` (which builds the FastAPI
app) and by tests.
"""

from __future__ import annotations

from dataclasses import dataclass

from zero.app.agent_type_service import AgentTypeService
from zero.app.artifact_service import ArtifactService
from zero.app.audit_service import AuditService
from zero.app.auth_service import AuthService
from zero.app.authorization_service import AuthorizationService
from zero.app.compaction_service import CompactionService
from zero.app.identity_service import IdentityService
from zero.app.integration_service import IntegrationService
from zero.app.interface_service import InterfaceAdapterService
from zero.app.observability_service import (
    BackupService,
    MetricsService,
    RecoveryService,
    SecretCanaryScan,
)
from zero.app.plan_service import PlanService
from zero.app.provider_service import ProviderService
from zero.app.retrieval_service import ContextBuilder, RetrievalRouter
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
    metrics: MetricsService
    backup: BackupService
    canary: SecretCanaryScan
    recovery: RecoveryService
    auth: AuthService


def build_services(settings: Settings, database: Database) -> Services:
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
    identity_service = IdentityService(
        identity_repo, audit_repo, authorization_service
    )
    auth_service = AuthService(database, identity_service, audit_repo, settings)
    secret_service = SecretService(
        secret_repo, audit_repo, settings, authorization_service
    )
    tool_service = ToolService(tool_repo, audit_repo, authorization_service)
    plan_service = PlanService(plan_repo, audit_repo, authorization_service)
    worker_service = WorkerService(
        execution_repo, plan_repo, audit_repo, authorization_service
    )
    worktree_service = WorktreeService(
        worktree_repo, audit_repo, authorization_service
    )
    agent_type_service = AgentTypeService(
        agent_type_repo, audit_repo, authorization_service
    )
    artifact_service = ArtifactService(
        artifact_repo, agent_type_repo, audit_repo, authorization_service
    )
    retrieval_router = RetrievalRouter(
        artifact_repo, agent_type_repo, context_repo
    )
    context_builder = ContextBuilder(retrieval_router, context_repo)
    compaction_service = CompactionService(context_repo, artifact_service)
    provider_service = ProviderService(
        provider_repo, artifact_service, audit_repo
    )
    integration_service = IntegrationService(
        integration_repo, worktree_repo, audit_repo, authorization_service
    )
    interface_service = InterfaceAdapterService(
        interface_repo, audit_repo, plan_service, authorization_service,
        identity_repo,
    )
    metrics_service = MetricsService()
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
        metrics=metrics_service,
        backup=backup_service,
        canary=canary_service,
        recovery=recovery_service,
        auth=auth_service,
    )
