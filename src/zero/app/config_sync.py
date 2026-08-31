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
        # GAP 9 fix (2026-08-31, Hermes parity audit): an env-driven
        # deployment (ZERO_OPENAI_API_KEY / ZERO_TELEGRAM_BOT_TOKEN in the
        # environment, no config.yaml) used to boot with NO management
        # project, NO telegram binding and NO routing pin — the polling
        # worker found zero targets and the bot could never receive or
        # reply to a single message even though every credential was
        # present. When the operator's environment carries credentials,
        # synthesize a config.yaml from them once, then fall through to
        # the battle-tested config.yaml sync below.
        if _bootstrap_config_from_env(settings, home, cfgsvc):
            logger.info(
                "config sync: synthesized %s from environment credentials "
                "(GAP 9 env bootstrap)",
                home / "config.yaml",
            )
        else:
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

    # GAP 9: swap ENV: sentinel references (written by the env bootstrap)
    # for durable encrypted secret rows before the provider/binding sync
    # resolves them.
    if (
        any((p.api_key_ref or "").startswith("ENV:") for p in cfg.providers)
        or (cfg.telegram.bot_token_ref or "").startswith("ENV:")
    ):
        _resolve_env_sentinel_refs(services, project, owner_id, cfg, cfgsvc)

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
    _sync_telegram_bindings(services, project, owner_id, cfg, cfgsvc, settings)
    _sync_planner(services, cfg)
    # M16 (2026-08-31, mega-run live-found): the per-project tool floors
    # (workspace tools + internet_search) used to be granted for the
    # MANAGEMENT project only. Operator-created projects — the entire
    # point of multi-project scale — had ZERO grants, so every task
    # agent's read_file/write_file/run_command/capture_diff was denied
    # ("No grant for tool ... in scope ...") and coding tasks failed
    # honestly with empty diff evidence. The floors are per-project
    # boot invariants: grant them for EVERY project, idempotently.
    _ensure_per_project_tool_floors(services)

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


def _bootstrap_config_from_env(settings: Settings, home, cfgsvc) -> bool:
    """Synthesize ``config.yaml`` from environment credentials (GAP 9).

    Called ONLY when no config.yaml exists. Builds the same shape the
    setup wizard writes — providers from the env API keys, the Telegram
    bot token, groups from ``ZERO_TELEGRAM_GROUP_IDS`` (comma-separated
    chat ids), and routing pinned to ``ZERO_OPENAI_MODEL`` — so an
    env-only deployment boots fully wired. Idempotent by construction:
    once the file exists the normal sync path owns everything.
    """
    import yaml as _yaml

    from zero.manage.core.config import GroupPolicy, ProviderCfg, ZeroConfig

    env_token = os.environ.get("ZERO_TELEGRAM_BOT_TOKEN", "").strip()
    env_openai_key = os.environ.get("ZERO_OPENAI_API_KEY", "").strip()
    env_anthropic_key = os.environ.get("ZERO_ANTHROPIC_API_KEY", "").strip()
    if not (env_token or env_openai_key or env_anthropic_key):
        return False

    cfg = ZeroConfig()
    primary_model = settings.openai_model

    if env_openai_key:
        cfg.providers.append(
            ProviderCfg(
                id="openai",
                protocol="openai_compatible",
                base_url=settings.openai_base_url,
                api_key_ref="ENV:ZERO_OPENAI_API_KEY",
                models=[settings.openai_model],
                enabled=True,
            )
        )
        primary_model = settings.openai_model
    if env_anthropic_key:
        cfg.providers.append(
            ProviderCfg(
                id="anthropic",
                protocol="anthropic",
                base_url=settings.anthropic_base_url,
                api_key_ref="ENV:ZERO_ANTHROPIC_API_KEY",
                models=[settings.anthropic_model],
                enabled=True,
            )
        )
    cfg.routing.primary_model = primary_model

    if env_token:
        cfg.telegram.bot_token_ref = "ENV:ZERO_TELEGRAM_BOT_TOKEN"
    group_ids = [
        part.strip()
        for part in os.environ.get("ZERO_TELEGRAM_GROUP_IDS", "").split(",")
        if part.strip()
    ]
    for chat_id in group_ids:
        cfg.access.groups.append(
            GroupPolicy(
                chat_id=str(int(chat_id)),
                title=f"chat {chat_id}",
                kind="supergroup",
                enabled=True,
            )
        )

    home.mkdir(parents=True, exist_ok=True)
    payload = cfg.model_dump(mode="json")
    # The ENV: prefix is the sentinel the sync step resolves below; the
    # file intentionally stores NO raw secret value.
    try:
        with open(home / "config.yaml", "w", encoding="utf-8") as handle:
            _yaml.safe_dump(payload, handle, sort_keys=False, allow_unicode=True)
        return True
    except OSError as exc:
        logger.error("config sync: could not write synthesized config.yaml: %s", exc)
        return False


def _resolve_env_sentinel_refs(services, project, owner_id, cfg, cfgsvc) -> None:
    """Resolve ``ENV:VAR`` sentinel references into real secret rows.

    The env bootstrap writes ``api_key_ref: ENV:ZERO_OPENAI_API_KEY`` and
    ``bot_token_ref: ENV:ZERO_TELEGRAM_BOT_TOKEN``. This helper swaps
    each sentinel for a durable encrypted secret reference (idempotent:
    existing resolving rows are reused), so restarts without the env
    keep working.
    """
    for prov in cfg.providers:
        ref = prov.api_key_ref or ""
        if not ref.startswith("ENV:"):
            continue
        env_name = ref[4:]
        env_value = os.environ.get(env_name, "").strip()
        if not env_value:
            continue
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
    token_ref = cfg.telegram.bot_token_ref or ""
    if token_ref.startswith("ENV:"):
        env_name = token_ref[4:]
        env_value = os.environ.get(env_name, "").strip()
        if env_value:
            new_ref = _recover_secret_from_env(
                services,
                project,
                owner_id,
                name="telegram-bot-token",
                secret_type="token",
                env_value=env_value,
                label="telegram bot token",
            )
            if new_ref:
                cfg.telegram.bot_token_ref = new_ref
    try:
        cfgsvc.save(cfg)
    except Exception as exc:  # noqa: BLE001 - boot must survive
        logger.warning(
            "config sync: could not persist resolved sentinel refs: %s: %s",
            type(exc).__name__,
            exc,
        )


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

    # NOTE (live-run 2026-08-30): the gateway tool-calling capability is
    # observed LAZILY on the first tool-ful request
    # (``ProviderService.tool_call_support``), not here at boot — a boot
    # probe would open a real network connection inside every unit test
    # that runs a config sync (leaked-socket warnings) and add a round
    # trip to every engine start. The lazy probe records the observed
    # truth once per (provider, model) and strips the model's
    # ``native_tools`` capability when the gateway strips tools, which
    # routes chat, tasks, decomposition, and planning to the text tool
    # protocol.


def _sync_telegram_bindings(
    services: Services,
    project,
    owner_id,
    cfg,
    cfgsvc=None,
    settings: Settings | None = None,
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
    # GAP 4 (2026-08-31): user-session mode binds the personal account —
    # NO bot token. Create the same bindings (token-less) so scope,
    # policy, and the outbound session-adapter path all engage; polling
    # is owned by the MTProto worker, not getUpdates.
    if getattr(settings, "telegram_mode", "bot_api") == "user_session":
        existing = {
            (b.platform, b.chat_id): b
            for b in services.interfaces.list_bindings(project.id)
        }
        targets = [
            (str(g.chat_id), g.topic_id)
            for g in cfg.access.groups
            if g.enabled
        ]
        if not targets:
            targets.append(("0", None))
            logger.warning(
                "config sync: user-session mode with no groups configured — "
                "created a session-only binding (chat_id='0')"
            )
        for chat_id, topic_id in targets:
            existing_binding = existing.get(("telegram", chat_id))
            if existing_binding is not None:
                if not existing_binding.is_enabled:
                    services.interfaces.enable_binding(
                        project_id=project.id,
                        binding_id=existing_binding.id,
                        actor_id=owner_id,
                        source="system",
                    )
                continue
            try:
                services.interfaces.create_binding(
                    project_id=project.id,
                    actor_id=owner_id,
                    platform="telegram",
                    chat_id=chat_id,
                    topic_id=topic_id,
                    bot_token_ref=None,
                    is_enabled=True,
                    source="system",
                )
                logger.info(
                    "config sync: created user-session binding for chat %s",
                    chat_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "config sync: user-session binding create failed for "
                    "chat %s: %s: %s",
                    chat_id,
                    type(exc).__name__,
                    exc,
                )
        return
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

    # Round-7 live fix: the SCHEDULER TICK (task execution + LLM
    # decomposition) also resolved its model from ``settings.openai_model``
    # — the routing table never reached the tasks. The operator's gateway
    # then stopped serving the gpt-4o-mini default outright (every
    # decomposition/task call died with a CDN-edge 403 while the aligned
    # planner and chat kept succeeding), so approved plans could never
    # execute. Pin the tick to the SAME routing truth.
    scheduler = getattr(services, "scheduler", None)
    if scheduler is not None:
        scheduler.set_tick_routing(
            provider=(
                "anthropic" if provider_protocol == "anthropic" else "openai-compatible"
            ),
            model_name=primary,
        )
        logger.info(
            "config sync: scheduler tick (tasks + decomposition) aligned "
            "with routing.primary_model=%s",
            primary,
        )

    # GAP H (round-9 live fix): the compaction LLM summarizer was the
    # LAST routing consumer still resolving its model from
    # ``settings.openai_model`` (the gpt-4o-mini default). On the
    # operator's gateway every summarizer call then failed, compaction
    # silently degraded to the deterministic template, and LLM-gated
    # memory deltas (GAP 9) could never be extracted. Pin the
    # summarizer to the SAME routing truth.
    compaction = getattr(services, "compaction", None)
    if compaction is not None and compaction.summarizer is not None:
        compaction.summarizer_routing = {
            "provider": (
                "anthropic" if provider_protocol == "anthropic" else "openai-compatible"
            ),
            "model": primary,
        }
        logger.info(
            "config sync: compaction summarizer aligned with "
            "routing.primary_model=%s",
            primary,
        )


def _ensure_per_project_tool_floors(services: Services) -> None:
    """M16: apply the per-project tool floors to EVERY project.

    The tool-capability model requires a grant per (project, tool,
    agent_scope); the floors (internet_search + the four workspace
    tools) are boot invariants per project, not per deployment. Each
    project is isolated: a failure grants nothing for that project but
    never crashes boot.
    """
    try:
        projects = services.identity.list_projects()
    except Exception as exc:  # noqa: BLE001 - boot must survive
        logger.warning(
            "config sync: could not list projects for tool floors: %s: %s",
            type(exc).__name__,
            exc,
        )
        return
    for project in projects:
        owner_id = project.owner_user_id
        for ensure in (_ensure_web_search_tool, _ensure_workspace_tool_grants):
            try:
                ensure(services, project, owner_id)
            except Exception as exc:  # noqa: BLE001 - per-project isolation
                logger.warning(
                    "config sync: tool floor failed for project %s: %s: %s",
                    project.id.value,
                    type(exc).__name__,
                    exc,
                )


def _ensure_workspace_tool_grants(services: Services, project, owner_id) -> None:
    """Grant the four workspace tools to every execution agent scope.

    Live-run fix B14 (2026-08-31): the tool-capability model REQUIRES a
    grant per (project, tool, agent_scope) — ``ToolService.invoke``
    denies anything ungranted ("No grant for tool ... in scope ...").
    Only ``internet_search`` was ever auto-granted, so EVERY task
    agent's ``read_file`` / ``write_file`` / ``run_command`` /
    ``capture_diff`` call was denied for the whole live deployment: the
    agents could literally do nothing (the transcript said "Every
    workspace tool call was denied by policy in this session"), while
    the runtime's own evidence commands (which bypass ToolService) kept
    working — masking the gap. The deterministic tests granted tools in
    fixtures, so the hole never surfaced there.

    Grants are idempotent (existing grant short-circuits) and scoped to
    the project. ``main_worker`` and ``sub_agent_type`` are the scopes
    the execution runtime and the delegate tool actually use.
    """
    scopes = ("main_worker", "sub_agent_type")
    for tool_name in ("read_file", "write_file", "run_command", "capture_diff"):
        try:
            tool = services.tools._tool_repo.get_tool_by_name(tool_name)
        except Exception:  # noqa: BLE001 - tool registry row missing
            logger.debug("config sync: workspace tool %s not registered", tool_name)
            continue
        for scope in scopes:
            try:
                existing = [
                    grant
                    for grant in services.tools._tool_repo.list_grants_for_project(
                        project.id
                    )
                    if grant.tool_id == tool.id and grant.agent_scope == scope
                ]
                if existing:
                    continue
                services.tools.grant_tool(
                    project_id=project.id,
                    actor_id=owner_id,
                    tool_id=tool.id,
                    agent_scope=scope,
                    source="system",
                )
                logger.info(
                    "config sync: %s granted to %s (project %s)",
                    tool_name,
                    scope,
                    project.id.value,
                )
            except Exception as exc:  # noqa: BLE001 - grant must not crash boot
                logger.warning(
                    "config sync: %s grant failed for scope %s: %s: %s",
                    tool_name,
                    scope,
                    type(exc).__name__,
                    exc,
                )


def _ensure_web_search_tool(services: Services, project, owner_id) -> None:
    """Make the keyless web-search tool real and granted (round 5).

    ``WebSearchCfg`` was a stub referenced by the wizard and the doctor
    while no runtime tool existed. The tool is registered once (by
    name), granted to the management project's ``main_worker`` scope,
    and backed by the inline DuckDuckGo-Lite handler so it runs under
    the standard grant/redaction/audit pipeline like every other tool.
    Idempotent across restarts: an existing tool row and an existing
    grant short-circuit the registration.

    The registry name is ``internet_search`` (live-run 2026-08-30): the
    operator's gateway POISONS the tool name ``web_search`` — it is an
    Anthropic server-side tool name, and through this gateway any tool
    so named silently never produces tool_calls (the model answers with
    hallucinated "search results" instead). Renaming the registered
    tool keeps the same handler and pipeline while the model actually
    calls it. A legacy ``web_search`` row from an older database still
    resolves and stays granted.
    """
    tool = None
    for candidate_name in ("internet_search", "web_search"):
        try:
            tool = services.tools._tool_repo.get_tool_by_name(candidate_name)
        except Exception:  # noqa: BLE001 - not registered yet
            tool = None
        if tool is not None:
            break
    if tool is None:
        from zero.app.tools_websearch import (
            WEB_SEARCH_INPUT_SCHEMA,
            WEB_SEARCH_OUTPUT_SCHEMA,
            make_web_search_handler,
        )

        tool = services.tools.register_tool(
            name="internet_search",
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
        logger.info(
            "config sync: internet_search tool registered (id=%s)", tool.id.value
        )
    else:
        # Handler-rebind fix (live-run 2026-08-31): the tool row persists
        # across restarts but handlers are process-local callables. Without
        # this rebind every restart after the first left internet_search
        # with NO handler — HTTP invokes 500'd and agent tool rounds failed
        # with "No handler registered" until the row was deleted by hand.
        from zero.app.tools_websearch import make_web_search_handler

        services.tools.rebind_server_handler(
            tool, handler=make_web_search_handler(), inline=True
        )
        logger.debug(
            "config sync: internet_search handler rebound (id=%s)", tool.id.value
        )
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
