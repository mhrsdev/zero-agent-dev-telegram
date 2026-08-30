"""Service bundle — wires all Phase 6 services together.

This module is the single place where Phase 6 services are constructed
and wired. It depends on every layer: ``domain`` types, ``app``
services, ``persistence`` repositories, and the :class:`Database`
connection. It is imported by ``app/api.py`` (which builds the FastAPI
app) and by tests.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

from zero.adapters.messaging import HttpTransport
from zero.app.agent_runtime import AgentRuntime
from zero.app.agent_type_service import AgentTypeService
from zero.app.approval_gate import ToolApprovalGate
from zero.app.artifact_service import ArtifactService
from zero.app.audit_service import AuditService
from zero.app.auth_service import AuthService
from zero.app.authorization_service import AuthorizationService
from zero.app.chat_service import ChatService
from zero.app.compaction_service import CompactionService
from zero.app.decomposition_analytics import DecompositionAnalytics
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
from zero.app.task_decomposition import TaskDecomposer
from zero.app.tool_service import ToolService
from zero.app.worker_service import WorkerService
from zero.app.worktree_service import WorktreeService
from zero.config import ConfigError, Settings

logger = logging.getLogger(__name__)
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
    decomposition_analytics: DecompositionAnalytics | None = None
    #: Optional per-call tool approval gate; present only when
    #: ZERO_TOOL_APPROVAL_MODE != off.
    approval_gate: ToolApprovalGate | None = None


def _build_messaging_http_client(settings: Settings) -> httpx.Client:
    """Shared HTTP client for Telegram/Discord adapter I/O.

    Bug fix (2026-08-29, flaky-network session): the client used to be a
    bare ``httpx.Client()`` —

    - the httpx default 5s all-category timeout silently bounded every
      outbound ``sendMessage`` on slow networks (polling passed its own
      per-request timeout, so only the delivery path suffered);
    - there was no way to route Telegram traffic through a proxy, so an
      operator on a filtered network (api.telegram.org intermittently
      unreachable) had no option but a system-wide VPN.

    The client now gets an explicit generous timeout budget and honors
    ``settings.telegram_proxy_url`` (``ZERO_TELEGRAM_PROXY_URL``: http,
    https, socks5, socks5h — the last resolves DNS through the proxy,
    which matters when local DNS is poisoned). Proxy URLs are validated
    fail-closed in ``Settings.load``; a construction failure here still
    fails the boot loudly instead of degrading into mystery transport
    errors at request time.
    """
    proxy = settings.telegram_proxy_url or None
    # Long-poll requests override this per call; this budget governs the
    # remaining adapter calls (sendMessage / editMessageText / getMe).
    timeout = httpx.Timeout(35.0, connect=15.0)
    try:
        if proxy:
            return httpx.Client(proxy=proxy, timeout=timeout)
        return httpx.Client(timeout=timeout)
    except Exception as exc:  # pragma: no cover - guarded by Settings validation
        raise ConfigError(
            f"Telegram HTTP transport could not be constructed "
            f"(proxy={settings.telegram_proxy_url!r}): {type(exc).__name__}: {exc}"
        ) from exc


def _build_policy_gate(identity_repo, settings):
    """Optional access-policy gate from the management config file.

    Returns None when no managed config exists, keeping legacy env-only
    behavior untouched. Live-reloads on every call (cheap YAML read).
    """
    from zero.manage.core.config import zero_home

    home = zero_home()
    cfg_path = home / "config.yaml"
    if not cfg_path.exists():
        return None
    try:
        from zero.manage.core.policy import build_gate as _build
    except ImportError:  # pragma: no cover - manage layer optional
        return None

    def _load_access():
        import yaml

        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        return raw.get("access")

    def _owner_external(project_id_value: str | None) -> str | None:
        if not project_id_value:
            return None
        try:
            from zero.domain.identity import ProjectId

            project = identity_repo.get_project(ProjectId(str(project_id_value)))
            owner_id = project.owner_user_id
            links = identity_repo.list_external_identities_for_user(owner_id)
            for link in links:
                if link.platform == "telegram" and link.verified_at:
                    return link.external_id
        except Exception as exc:  # noqa: BLE001 - gate must never crash intake
            # Log, never swallow silently: a broken owner lookup must be
            # diagnosable while still failing closed.
            logger.warning(
                "policy-gate owner lookup failed for project %s: %s",
                project_id_value,
                type(exc).__name__,
            )
            return None
        return None

    class _CfgView:  # tiny adapter matching build_gate's expectations
        def __init__(self, access: dict):
            self.mode = access.get("mode", "owner_only")
            # Root-level owner_project_id resolution happens lazily in
            # _cfg_getter when the access section omits it.
            self.owner_project_id = access.get("owner_project_id")
            self.allow_users = list(access.get("allow_users") or [])
            # Plain dicts: build_gate/decide consume dict-shaped groups
            # exclusively (no ad-hoc attribute wrappers).
            self.groups = [
                {
                    "chat_id": g.get("chat_id"),
                    "title": g.get("title"),
                    "enabled": g.get("enabled", True),
                    "allowed_features": g.get("allowed_features", ["chat"]),
                }
                for g in access.get("groups", [])
            ]

    def _cfg_getter():
        data = _load_access()
        if not data:
            return None
        view = _CfgView(data)
        # resolve owner project lazily from telegram section if unset
        if getattr(view, "owner_project_id", None) is None:
            root = yaml_safe(cfg_path)
            view.owner_project_id = (root or {}).get("owner_project_id")
        return view

    def yaml_safe(path):  # local helper to avoid double-read complexity
        import yaml

        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    return _build(_cfg_getter, _owner_external)


def _build_sandbox_executor(settings):
    """Resolve the GAP 3 sandbox backend, probing availability fail-closed."""
    from zero.app.executors.sandbox import SandboxUnavailableError, build_command_executor

    try:
        return build_command_executor(
            settings.sandbox_executor,
            sandbox_image=settings.sandbox_image,
        )
    except (SandboxUnavailableError, ValueError) as exc:
        raise ConfigError(str(exc)) from exc


class _PluginSecretFacade:
    """Name-based read-only secret access for plugins (audit S12).

    Resolves only within the management project so a plugin cannot walk
    arbitrary project scopes.
    """

    def __init__(self, secret_service, project) -> None:
        self._svc = secret_service
        self._project = project

    def resolve_by_name(self, name: str) -> str | None:
        ref = self._svc.get_reference_by_name(
            project_id=self._project.id,
            name=name,
            actor_id=self._project.owner_user_id,
        )
        if ref is None or ref.is_revoked:
            return None
        return self._svc.resolve_value(
            project_id=self._project.id,
            secret_id=ref.id,
            actor_id=self._project.owner_user_id,
        )


def _load_extensions(tool_service, *, secret_service=None, identity=None) -> None:
    """Wire MCP servers and plugins (GAP 7); failures are logged only."""
    try:
        from zero.manage.core.mcp_client import get_mcp_manager

        manager = get_mcp_manager()
        if manager.load_from_env():
            registered = manager.register_tools(tool_service)
            logger.info("MCP extension tools registered: %s", registered)
    except Exception as exc:  # noqa: BLE001 - extensions must not crash startup
        logger.warning("MCP extension loading skipped: %s", type(exc).__name__)
    try:
        from zero.manage.plugins.registry import load_plugins

        config = None
        facade = None
        try:
            from zero.manage.core.config import ConfigService, zero_home

            home = zero_home()
            cfgsvc = ConfigService(home)
            if cfgsvc.exists():
                config = cfgsvc.load()
        except Exception as exc:  # noqa: BLE001
            logger.debug("plugin config unavailable: %s", type(exc).__name__)
        if secret_service is not None and identity is not None:
            for p in identity.list_projects():
                if p.name == "Zero Management":
                    facade = _PluginSecretFacade(secret_service, p)
                    break
        loaded = load_plugins(
            tool_service,
            config=config,
            secret_store=facade,
        )
        if loaded:
            logger.info("plugins loaded: %s", loaded)
    except Exception as exc:  # noqa: BLE001
        logger.warning("plugin loading skipped: %s", type(exc).__name__)


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
        agent_type_repo=agent_type_repo,
    )
    worktree_service = WorktreeService(
        worktree_repo,
        audit_repo,
        authorization_service,
        execution_repo=execution_repo,
        worktree_root=settings.worktree_root,
        allowed_commands=settings.worktree_allowed_commands,
        isolation_mode=settings.worktree_isolation_mode,
        command_executor=_build_sandbox_executor(settings),
    )
    if not settings.is_test:
        tool_service.register_worktree_tools(worktree_service)
        # GAP 7: extension loading is opt-in and never fatal. MCP servers
        # require explicit ZERO_MCP_SERVERS entries; plugins load from
        # $ZERO_HOME/plugins and /opt/zero/plugins when present.
        _load_extensions(
            tool_service,
            secret_service=secret_service,
            identity=identity_service,
        )
    agent_type_service = AgentTypeService(agent_type_repo, audit_repo, authorization_service)
    artifact_service = ArtifactService(
        artifact_repo, agent_type_repo, audit_repo, authorization_service
    )
    retrieval_router = RetrievalRouter(
        artifact_repo, agent_type_repo, context_repo, authorization_service
    )
    context_builder = ContextBuilder(retrieval_router, context_repo)
    compaction_service = CompactionService(
        context_repo,
        artifact_service,
        authorization_service,
        agent_type_service=agent_type_service,
    )
    provider_service = ProviderService(
        provider_repo,
        artifact_service,
        audit_repo,
        authorization_service,
        include_fake=settings.is_test,
        metrics=metrics_service,
        provider_max_attempts=settings.provider_max_attempts,
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
    # Model-level fallback routing (Hermes parity, audit 2026-08-28):
    # the setup wizard writes ``routing.fallback_models`` into
    # config.yaml, promising alternative models on the same gateway.
    # ZERO_OPENAI_FALLBACK_MODELS carries that contract into the
    # runtime so a primary-model outage routes to the next model
    # instead of failing the task.
    if settings.openai_fallback_models:
        provider_service.set_fallback_models(settings.openai_fallback_models)

    def _llm_compaction_summarizer(*, project_id, execution_id, actor_id, messages):
        """LLM checkpoint summarizer for compaction (Hermes parity).

        Uses the first registered provider; returns ``None`` when no
        provider exists so the deterministic fallback summary applies.
        The transcript is bounded and passed as data-only material.

        GAP H (round-9 live fix): when config sync pinned a routing
        override (``routing.primary_model``), the summarizer calls
        exactly that provider/model — the same truth the planner, the
        chat bridge, and the scheduler tick already follow. Without the
        override the historical settings-derived choice applies.
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

        routing = compaction_service.summarizer_routing or {}
        primary = str(routing.get("provider") or "").strip() or provider_names[0]
        if primary not in provider_names:
            # The routed provider adapter is not registered (e.g. a
            # config edit removed it) — degrade to the first registered
            # adapter rather than dropping the summarizer entirely.
            primary = provider_names[0]
        if routing.get("model"):
            model_name = str(routing["model"])
        elif primary == "anthropic":
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
                provider=primary,
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
        identity_service=identity_service,
        auto_verify_linked=True,
        policy_gate=_build_policy_gate(identity_repo, settings),
    )
    interface_transport = (
        messaging_transport
        if messaging_transport is not None
        else (None if settings.is_test else _build_messaging_http_client(settings))
    )
    interface_transport_service = InterfaceTransportService(
        interface_service,
        interface_repo,
        settings,
        secret_service=secret_service,
        transport=interface_transport,
    )
    # Late-bound outbound hook for the /start and /help command replies
    # (dead-bot session fix: a healthy bot used to stay silent on /start).
    interface_service.direct_reply_transport = interface_transport_service
    # Conversational fallback (Hermes session parity, round 5): plain
    # chat used to produce NOTHING on the Telegram surface — a message
    # either became a plan proposal (invisible until someone opened the
    # web UI) or silence. The bridge runs one bounded conversational
    # turn (tools included, grants apply) with a durable per-scope
    # transcript so restarts no longer amputate the conversation. The
    # model below is the settings default; ``config_sync`` aligns it
    # with ``routing.primary_model`` when a management config exists.
    from zero.app.chat_history_repository import ChatHistoryRepository
    from zero.app.telegram_chat import TelegramChatBridge

    chat_service = ChatService(
        providers=provider_service,
        authorization=authorization_service,
        tools=tool_service,
    )
    chat_history_repo = ChatHistoryRepository(database)
    chat_bridge = TelegramChatBridge(
        chat_service=chat_service,
        transport_service=interface_transport_service,
        history=chat_history_repo,
        provider="openai-compatible",
        model_name=settings.openai_model,
    )
    interface_service.chat_bridge = chat_bridge
    logger.info(
        "conversational chat bridge wired (provider=openai-compatible, "
        "model=%s) — non-actionable chat now answers in the chat",
        settings.openai_model,
    )
    result_delivery_service = ResultDeliveryService(
        interface_repo,
        execution_repo,
        authorization_service,
        interface_transport_service,
    )
    # GAP 8b/G2 Hermes parity: opt-in per-call tool approval gate.
    # ``off`` keeps the historical plan-level-only posture byte-for-byte.
    approval_gate = (
        ToolApprovalGate(database, mode=settings.tool_approval_mode)
        if settings.tool_approval_mode == "manual"
        else None
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
        approval_gate=approval_gate,
        metrics=metrics_service,
        compaction=compaction_service,
        enable_delegation=True,
        audit_repo=audit_repo,
        # Bug fix (real run, 2026-08-28): the runtime used to fall back to
        # a hidden default test command ("pytest -q") that the worktree
        # command policy does not allowlist — every task whose
        # expected_evidence required a test report failed with
        # "command 'pytest' is not permitted". The evidence command is now
        # explicit operator configuration; when unset, evidence-demanding
        # tasks fail closed with a configuration hint instead of a policy
        # violation.
        test_command=settings.evidence_test_command or None,
    )
    # GAP 10 / S7: wire the LLM task decomposer into the production
    # scheduler. The flag (ZERO_DECOMPOSITION_ENABLED) still gates the
    # behavior — without it the scheduler takes its historical
    # single-task path byte-for-byte. Recovery analytics capture every
    # decompose() outcome (per-model typo rates, degradations,
    # fallbacks) and optionally append JSONL evidence to the sink path.
    decomposition_analytics = DecompositionAnalytics.get_or_create(
        sink_path=(
            Path(settings.decomposition_analytics_path)
            if settings.decomposition_analytics_path
            else None
        )
    )

    def _resolve_single_repository(project_id, actor_id):
        """Return the project's sole registered repository id, else None.

        Wired as the scheduler's repository_resolver (real-run fix): the
        managed worker host never passes repository_id, so coding tasks
        used to run delegate-only with no file/shell tools.
        """
        repos = worktree_service.list_repositories(
            project_id, actor_id=actor_id, source="system"
        )
        return repos[0].id if len(repos) == 1 else None

    scheduler_service = SchedulerService(
        plans=plan_service,
        worker=worker_service,
        runtime=agent_runtime,
        authorization=authorization_service,
        integration=integration_service,
        result_delivery=result_delivery_service,
        agent_type_repo=agent_type_repo,
        task_max_attempts=settings.task_max_attempts,
        decomposer=TaskDecomposer(providers=provider_service, analytics=decomposition_analytics),
        parallel_executions=settings.tick_parallel_executions,
        repository_resolver=_resolve_single_repository,
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
        decomposition_analytics=decomposition_analytics,
        approval_gate=approval_gate,
    )
