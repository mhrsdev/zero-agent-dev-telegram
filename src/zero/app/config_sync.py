"""Sync management config (``$ZERO_HOME/config.yaml``) into the live engine.

Bug fix (real server run, 2026-08-29): the engine's ``Settings`` load
only reads environment variables, but the setup wizard writes provider
API keys and the Telegram bot token as secret REFERENCES inside
``config.yaml``. Until this module existed, ``zero start`` booted a
server with:

  - **no provider adapters registered** — ``settings.openai_api_key``
    was ``None`` because the operator never exported ``ZERO_OPENAI_API_KEY``;
    the wizard-stored ``sec_...`` reference was never resolved.
  - **no Telegram polling targets** — the polling worker only inspects
    ``interface_bindings`` rows in the database, and the wizard never
    created one. The bot token sat unused in the encrypted secret store.

The result: the server logged "background workers started" and the
HTTP health check passed, but the bot could neither receive nor reply
to a single Telegram message. Every operator who finished ``zero setup``
and then ran ``zero start`` hit this exact silent failure.

This module is invoked once at app startup
(:func:`zero.app.api.build_application_services`). It is idempotent —
safe to call on every boot — and silently returns when no
``config.yaml`` exists (developer ``zero-develop serve`` path).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from zero.app.services import Services
from zero.config import Settings
from zero.domain.secrets import SecretReferenceId

logger = logging.getLogger(__name__)


def sync_management_config(settings: Settings, services: Services) -> None:
    """Resolve ``config.yaml`` into live engine wiring.

    Idempotent. No-op when:
      - the environment is ``test`` (tests construct services explicitly);
      - no ``config.yaml`` exists (developer ``zero-develop serve`` path);
      - ``config.yaml`` fails to load (logged as a warning).

    Otherwise resolves:
      1. The management project (creates one if missing — defensive,
         the wizard already creates it).
      2. Each enabled provider's ``api_key_ref`` against the secret store
         and registers the appropriate adapter on ``services.providers``.
      3. The Telegram ``bot_token_ref`` against the secret store and
         creates (or refreshes) an enabled ``interface_binding`` for
         every group listed in ``access.groups``. When no groups are
         configured, a single polling-only binding is created so the
         polling worker can at least drain pending updates; messages
         still require a matching binding to be processed.
      4. The planner service — created on demand using the now-registered
         providers, and wired into ``services.interfaces`` with the
         primary model from ``routing.primary_model``.
    """
    if settings.is_test:
        return
    from zero.manage.core.config import ConfigService, zero_home

    home = zero_home()
    cfgsvc = ConfigService(home)
    if not cfgsvc.exists():
        return
    try:
        cfg = cfgsvc.load()
    except Exception as exc:  # noqa: BLE001 - config errors must not crash boot
        logger.warning(
            "config sync skipped (config.yaml invalid): %s: %s",
            type(exc).__name__,
            exc,
        )
        return

    project = _ensure_management_project(services)
    owner_id = project.owner_user_id

    # Bootstrap: persist the management project id into config.yaml so
    # the policy gate (which reads config.yaml on every call) can
    # resolve the owner's linked telegram identity. The wizard never
    # set this field — without it, ``owner_only`` mode denied every
    # message because ``owner_external_id`` stayed None forever.
    #
    # Repair (2026-08-29, dead-bot session): a database-drift repair
    # (``zero doctor --fix``) repoints the engine at the database that
    # holds the real secrets — but config.yaml may still carry the
    # owner_project_id of a GHOST "Zero Management" project the drifted
    # engine created in the wrong database. The policy gate then failed
    # closed on every message (project lookup misses → owner_external_id
    # None). Realign the field with the project that actually exists in
    # the engine database on every boot.
    if cfg.owner_project_id != project.id.value:
        stale = cfg.owner_project_id
        cfg.owner_project_id = project.id.value
        try:
            cfgsvc.save(cfg)
            if stale is None:
                logger.info(
                    "config sync: persisted owner_project_id=%s into config.yaml",
                    project.id.value,
                )
            else:
                logger.warning(
                    "config sync: repaired stale owner_project_id %s -> %s "
                    "(the previous project does not exist in the engine "
                    "database; a database drift was repaired)",
                    stale,
                    project.id.value,
                )
        except Exception as exc:  # noqa: BLE001 - must not crash boot
            logger.warning(
                "config sync: could not persist owner_project_id: %s: %s",
                type(exc).__name__,
                exc,
            )

    _sync_providers(settings, services, project, owner_id, cfg, cfgsvc)
    _sync_telegram_bindings(services, project, owner_id, cfg, cfgsvc)
    _sync_planner(services, cfg)
    _ensure_web_search_tool(services, project, owner_id)

    # Bootstrap helper: in owner_only mode, the first sender to message
    # the bot is auto-linked as the project owner. This closes the
    # ``zero setup`` → ``zero start`` → message-the-bot loop without a
    # manual identity-linking CLI step. Other access modes keep the
    # historical strict path.
    if cfg.access.mode == "owner_only":
        services.interfaces.auto_link_owner_project_id = project.id.value
        logger.info(
            "config sync: auto-link-owner enabled for project %s "
            "(first sender becomes the owner)",
            project.id.value,
        )


# ----------------------------------------------------------------------
# Internals
# ----------------------------------------------------------------------


def _recover_secret_from_env(
    services: Services,
    project,
    owner_id,
    *,
    name: str,
    secret_type: str,
    env_value: str,
    label: str,
) -> str | None:
    """Self-heal: store an env-provided credential as a fresh secret ref.

    Bug fix (2026-08-29, dead-bot session): when a configured ``sec_...``
    reference cannot be resolved (stale CWD-relative database), the
    operator's documented escape hatch is to export the credential via
    environment variables. Until this helper existed, config sync only
    LOGGED the failure and disabled the feature anyway — the env var was
    silently ignored. Now the value is persisted into the encrypted
    store and the config reference is repointed, so the fix survives
    restarts without the env var.

    Idempotent: when a secret under ``name`` already resolves, it is
    reused instead of duplicating rows.
    """
    import zero.domain.secrets as secrets_domain

    # Reuse a previous recovery row when it still resolves.
    try:
        existing = services.secrets.get_reference_by_name(
            project_id=project.id,
            name=name,
            actor_id=owner_id,
            source="system",
        )
        if not existing.is_revoked:
            services.secrets.resolve_value(
                project_id=project.id,
                secret_id=existing.id,
                actor_id=owner_id,
                source="system",
            )
            logger.info(
                "config sync: reused existing secret %s for %s",
                existing.id.value,
                label,
            )
            return existing.id.value
    except secrets_domain.SecretError:
        pass
    except Exception:  # noqa: BLE001 - fall through to a fresh store
        pass
    try:
        ref = services.secrets.store(
            project_id=project.id,
            name=name,
            secret_type=secret_type,  # type: ignore[arg-type]
            value=env_value,
            actor_id=owner_id,
        )
        logger.info(
            "config sync: recovered %s from environment into secret %s",
            label,
            ref.id.value,
        )
        return ref.id.value
    except Exception as exc:  # noqa: BLE001 - recovery is best-effort
        logger.error(
            "config sync: could not store recovered %s: %s: %s",
            label,
            type(exc).__name__,
            exc,
        )
        return None


def _ensure_management_project(services: Services):
    """Return the ``Zero Management`` project, creating it if missing.

    The setup wizard creates this project to scope operator secrets.
    We re-create it defensively so a fresh database still bootstraps
    correctly when ``config.yaml`` exists but the DB was wiped.
    """
    for p in services.identity.list_projects():
        if p.name == "Zero Management":
            return p
    op = services.identity.create_user(display_name="Zero Operator")
    return services.identity.create_project(owner_id=op.id, name="Zero Management")


def _sync_providers(
    settings: Settings,
    services: Services,
    project,
    owner_id,
    cfg,
    cfgsvc=None,
) -> None:
    """Register provider adapters from config.yaml secret references."""
    from zero.app.provider_adapter import (
        AnthropicMessagesProviderAdapter,
        OpenAICompatibleProviderAdapter,
    )

    registered = set(services.providers.registered_provider_names)
    for prov in cfg.providers:
        if not prov.enabled:
            continue
        if not prov.api_key_ref:
            logger.warning(
                "config sync: provider %s has no api_key_ref — skipped", prov.id
            )
            continue
        try:
            api_key = services.secrets.resolve_value(
                project_id=project.id,
                secret_id=SecretReferenceId(prov.api_key_ref),
                actor_id=owner_id,
                source="system",
            )
        except Exception as exc:  # noqa: BLE001 - one bad provider must not block others
            env_names = (
                ("ZERO_ANTHROPIC_API_KEY",)
                if prov.protocol == "anthropic"
                else ("ZERO_OPENAI_API_KEY",)
            )
            env_value = next(
                (os.environ[n].strip() for n in env_names if os.environ.get(n, "").strip()),
                "",
            )
            recovered_key: str | None = None
            if env_value and cfgsvc is not None:
                new_ref = _recover_secret_from_env(
                    services,
                    project,
                    owner_id,
                    name=f"{prov.id}-api-key",
                    secret_type="api_key",
                    env_value=env_value,
                    label=f"provider {prov.id} api key",
                )
                if new_ref:
                    cfg.providers = [
                        p if p.id != prov.id else p.model_copy(update={"api_key_ref": new_ref})
                        for p in cfg.providers
                    ]
                    try:
                        cfgsvc.save(cfg)
                    except Exception as exc2:  # noqa: BLE001 - boot must survive
                        logger.warning(
                            "config sync: could not persist recovered provider ref: %s: %s",
                            type(exc2).__name__,
                            exc2,
                        )
                    else:
                        logger.error(
                            "config sync: provider %s api_key_ref %s failed to resolve "
                            "(%s: %s) — RECOVERED from %s and repointed config.yaml to a "
                            "fresh secret; restart once more without the env var to "
                            "confirm persistence",
                            prov.id,
                            prov.api_key_ref,
                            type(exc).__name__,
                            exc,
                            env_names[0],
                        )
                        prov = next(p for p in cfg.providers if p.api_key_ref == new_ref)
                        recovered_key = env_value
            if recovered_key is None:
                logger.error(
                    "config sync: provider %s api_key_ref %s failed to resolve (%s: %s) "
                    "— the engine database does not contain the secret 'zero setup' "
                    "stored; LLM calls will fail. Fix with ONE of: 'zero doctor --fix' "
                    "(auto-repair), re-run 'zero setup', or export %s and restart.",
                    prov.id,
                    prov.api_key_ref,
                    type(exc).__name__,
                    exc,
                    env_names[0],
                )
                continue
            api_key = recovered_key  # proceed to adapter registration

        if prov.protocol == "anthropic":
            timeout = settings.anthropic_timeout_seconds
            adapter: Any = AnthropicMessagesProviderAdapter(
                api_key=api_key,
                base_url=prov.base_url,
                timeout_seconds=timeout,
            )
        else:
            timeout = settings.openai_timeout_seconds
            adapter = OpenAICompatibleProviderAdapter(
                api_key=api_key,
                base_url=prov.base_url,
                timeout_seconds=timeout,
            )

        name = adapter.provider_name
        if name in registered:
            logger.debug(
                "config sync: provider %s already registered as %s — skipped",
                prov.id,
                name,
            )
            continue
        services.providers.register_adapter(adapter)
        registered.add(name)
        logger.info(
            "config sync: registered provider %s (%s, base_url=%s)",
            prov.id,
            name,
            prov.base_url,
        )

    # Multi-adapter fallback chain (Hermes parity).
    names = services.providers.registered_provider_names
    if len(names) > 1:
        services.providers.set_fallback_chain(tuple(names))

    # Model-level fallback routing from config.yaml.
    if cfg.routing.fallback_models:
        services.providers.set_fallback_models(tuple(cfg.routing.fallback_models))


def _sync_telegram_bindings(
    services: Services,
    project,
    owner_id,
    cfg,
    cfgsvc=None,
) -> None:
    """Create/refresh enabled Telegram bindings from config.yaml.

    The polling worker in ``background_workers._telegram_poll_targets``
    only inspects ``interface_bindings`` rows. The wizard writes
    ``telegram.bot_token_ref`` into config.yaml but never creates a
    binding — so without this sync the polling loop finds zero targets
    and the bot never receives a single update.

    Bindings are created for every enabled group in ``access.groups``.
    When no groups are configured, a single polling-only binding is
    created with ``chat_id="0"`` so the polling worker can drain
    pending Telegram updates. Messages from unconfigured chats still
    require a matching binding to be processed (they will be logged as
    ``ignored_disabled``); the operator can add the chat via
    ``zero telegram groups add --chat-id <id>``.
    """
    token_ref = cfg.telegram.bot_token_ref
    if not token_ref:
        logger.warning(
            "config sync: no telegram.bot_token_ref in config.yaml — "
            "polling disabled. Run 'zero telegram add-bot' to configure one."
        )
        return

    # Verify the secret actually resolves before creating bindings.
    token: str | None
    try:
        token = services.secrets.resolve_value(
            project_id=project.id,
            secret_id=SecretReferenceId(token_ref),
            actor_id=owner_id,
            source="system",
        )
        logger.debug(
            "config sync: telegram bot token resolved (value withheld, "
            "length=%d)",
            len(token),
        )
    except Exception as exc:  # noqa: BLE001 - boot must survive a bad secret
        env_token = os.environ.get("ZERO_TELEGRAM_BOT_TOKEN", "").strip()
        if env_token and cfgsvc is not None:
            new_ref = _recover_secret_from_env(
                services,
                project,
                owner_id,
                name="telegram-bot-token",
                secret_type="token",
                env_value=env_token,
                label="telegram bot token",
            )
            if new_ref:
                cfg.telegram.bot_token_ref = new_ref
                try:
                    cfgsvc.save(cfg)
                except Exception as exc2:  # noqa: BLE001 - boot must survive
                    logger.warning(
                        "config sync: could not persist recovered bot token ref: %s: %s",
                        type(exc2).__name__,
                        exc2,
                    )
                else:
                    logger.error(
                        "config sync: telegram bot_token_ref %s failed to resolve "
                        "(%s: %s) — RECOVERED from ZERO_TELEGRAM_BOT_TOKEN and "
                        "repointed config.yaml; restart once more without the env "
                        "var to confirm persistence",
                        token_ref,
                        type(exc).__name__,
                        exc,
                    )
                    token_ref = new_ref
                    token = env_token
                # Fall through to binding creation with the recovered ref.
            else:
                return
        else:
            logger.error(
                "config sync: TELEGRAM POLLING IS DISABLED — bot_token_ref %s "
                "failed to resolve (%s: %s). The engine database does not "
                "contain the secret 'zero setup' stored (the database location "
                "drifted between runs). The bot will not respond to /start or "
                "any message until this is fixed. Fix with ONE of: "
                "'zero doctor --fix' (locates and pins the right database "
                "automatically), re-run 'zero setup' in the directory you run "
                "the service from, or export ZERO_TELEGRAM_BOT_TOKEN and restart.",
                token_ref,
                type(exc).__name__,
                exc,
            )
            return

    existing = {
        (b.platform, b.chat_id): b
        for b in services.interfaces.list_bindings(project.id)
    }

    targets: list[tuple[str, str | None]] = []
    for g in cfg.access.groups:
        if not g.enabled:
            continue
        targets.append((str(g.chat_id), g.topic_id))

    if not targets:
        # No groups configured: create a single polling-only binding so
        # the polling worker has SOMETHING to poll. Messages from any
        # real chat will still be ignored until the operator adds the
        # chat as a group (or a binding is created for it).
        targets.append(("0", None))
        logger.warning(
            "config sync: no telegram groups configured — created a "
            "polling-only binding (chat_id='0'). Add your group with: "
            "zero telegram groups add --chat-id <id>"
        )

    for chat_id, topic_id in targets:
        key = ("telegram", chat_id)
        existing_binding = existing.get(key)
        if existing_binding is not None:
            # Refresh: enable if disabled, and ensure bot_token_ref is set.
            if not existing_binding.is_enabled:
                services.interfaces.enable_binding(
                    project_id=project.id,
                    binding_id=existing_binding.id,
                    actor_id=owner_id,
                    source="system",
                )
            if existing_binding.bot_token_ref is None:
                services.interfaces._repo.update_binding_token_ref(
                    existing_binding.id,
                    token_ref,
                    project_id=project.id,
                )
            continue
        try:
            services.interfaces.create_binding(
                project_id=project.id,
                actor_id=owner_id,
                platform="telegram",
                chat_id=chat_id,
                topic_id=topic_id,
                bot_token_ref=token_ref,
                is_enabled=True,
                source="system",
            )
            logger.info(
                "config sync: created telegram binding for chat %s", chat_id
            )
        except Exception as exc:  # noqa: BLE001 - one bad binding must not block others
            logger.warning(
                "config sync: telegram binding create failed for chat %s: %s: %s",
                chat_id,
                type(exc).__name__,
                exc,
            )


def _sync_planner(services: Services, cfg) -> None:
    """Wire the planner service with the configured primary model.

    Bug fix: ``services.planner`` was ``None`` whenever
    ``settings.openai_api_key`` was unset — exactly the operator path
    where the API key lives in config.yaml, not the environment. The
    planner is now built on demand using the providers registered by
    ``_sync_providers``, and the planner provider/model are aligned to
    ``routing.primary_model`` so the planner actually calls the model
    the operator configured.
    """
    primary = cfg.routing.primary_model
    if not primary:
        return

    # Find which protocol offers the primary model.
    provider_protocol = None
    for prov in cfg.providers:
        if primary in prov.models:
            provider_protocol = prov.protocol
            break
    if provider_protocol is None:
        # Primary model not in any provider's catalog — still try with
        # the first registered provider; the adapter will pass the
        # model name through and the gateway will either accept it or
        # return a clear error.
        registered = services.providers.registered_provider_names
        if not registered:
            logger.warning(
                "config sync: routing.primary_model=%s but no provider "
                "is registered — planner stays disabled",
                primary,
            )
            return
        provider_protocol = "openai_compatible"

    # Create the planner on demand if it was not constructed at
    # build_services time (i.e. settings.openai_api_key was None).
    registered = services.providers.registered_provider_names
    if not registered:
        logger.error(
            "config sync: planner wired to model %s but NO provider adapter is "
            "registered (see the provider errors above) — LLM replies will "
            "fail until the provider api key resolves or is recovered",
            primary,
        )
    if services.interfaces._planner is None:
        from zero.app.planner_service import PlannerService

        planner = PlannerService(services.plans, services.providers)
        services.interfaces._planner = planner
        # Also publish on the Services bundle so other consumers see it.
        try:
            services.planner = planner  # type: ignore[misc]
        except Exception:  # noqa: BLE001 - dataclass might be frozen-ish
            pass

    services.interfaces._planner_provider = (
        "anthropic" if provider_protocol == "anthropic" else "openai-compatible"
    )
    services.interfaces._planner_model = primary
    logger.info(
        "config sync: planner wired (provider=%s, model=%s)",
        services.interfaces._planner_provider,
        primary,
    )

    # Align the conversational chat bridge with the primary model
    # (round-5 live fix): the bridge was wired at build_services time
    # with ``settings.openai_model`` (the gpt-4o-mini default) while the
    # operator's gateway only serves the configured claude-opus-5 —
    # every non-actionable chat turn then died with a 403
    # auth_failure before the user saw any reply. The bridge must call
    # exactly what the planner calls.
    chat_bridge = getattr(services.interfaces, "chat_bridge", None)
    if chat_bridge is not None:
        chat_bridge.update_model(primary)
        logger.info(
            "config sync: conversational chat bridge aligned with "
            "routing.primary_model=%s",
            primary,
        )


def _ensure_web_search_tool(services: Services, project, owner_id) -> None:
    """Make the keyless ``web_search`` tool real and granted (round 5).

    ``WebSearchCfg`` was a stub referenced by the wizard and the doctor
    while no runtime tool existed. The tool is registered once (by
    name), granted to the management project's ``main_worker`` scope,
    and backed by the inline DuckDuckGo-Lite handler so it runs under
    the standard grant/redaction/audit pipeline like every other tool.
    Idempotent across restarts: an existing tool row and an existing
    grant short-circuit the registration.
    """
    try:
        tool = services.tools._tool_repo.get_tool_by_name("web_search")
    except Exception:  # noqa: BLE001 - not registered yet
        tool = None
    if tool is None:
        from zero.app.tools_websearch import (
            WEB_SEARCH_INPUT_SCHEMA,
            WEB_SEARCH_OUTPUT_SCHEMA,
            make_web_search_handler,
        )

        tool = services.tools.register_tool(
            name="web_search",
            description=(
                "Search the public web (keyless DuckDuckGo backend) and "
                "return up to 5 results with title, URL, and snippet."
            ),
            input_schema=WEB_SEARCH_INPUT_SCHEMA,
            output_schema=WEB_SEARCH_OUTPUT_SCHEMA,
            handler_key="web_search",
            handler=make_web_search_handler(),
            inline=True,
        )
        logger.info("config sync: web_search tool registered (id=%s)", tool.id.value)
    try:
        existing_grants = [
            grant
            for grant in services.tools._tool_repo.list_grants_for_project(project.id)
            if grant.tool_id == tool.id and grant.agent_scope == "main_worker"
        ]
        if existing_grants:
            return
        services.tools.grant_tool(
            project_id=project.id,
            actor_id=owner_id,
            tool_id=tool.id,
            agent_scope="main_worker",
            source="system",
        )
        logger.info(
            "config sync: web_search granted to main_worker (project %s)",
            project.id.value,
        )
    except Exception as exc:  # noqa: BLE001 - grant must not crash boot
        logger.warning(
            "config sync: web_search grant failed: %s: %s",
            type(exc).__name__,
            exc,
        )


__all__ = ["sync_management_config"]
