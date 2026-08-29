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
    if cfg.owner_project_id is None:
        cfg.owner_project_id = project.id.value
        try:
            cfgsvc.save(cfg)
            logger.info(
                "config sync: persisted owner_project_id=%s into config.yaml",
                project.id.value,
            )
        except Exception as exc:  # noqa: BLE001 - must not crash boot
            logger.warning(
                "config sync: could not persist owner_project_id: %s: %s",
                type(exc).__name__,
                exc,
            )

    _sync_providers(settings, services, project, owner_id, cfg)
    _sync_telegram_bindings(services, project, owner_id, cfg)
    _sync_planner(services, cfg)

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
            logger.warning(
                "config sync: provider %s api_key resolve failed: %s: %s",
                prov.id,
                type(exc).__name__,
                exc,
            )
            continue

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
    try:
        services.secrets.resolve_value(
            project_id=project.id,
            secret_id=SecretReferenceId(token_ref),
            actor_id=owner_id,
            source="system",
        )
    except Exception as exc:  # noqa: BLE001 - boot must survive a bad secret
        logger.warning(
            "config sync: telegram bot token resolve failed: %s: %s",
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


__all__ = ["sync_management_config"]
