"""Production composition for inbound messaging transports.

The provider adapters remain thin edge translators.  This service owns the
composition boundary: it selects a configured verifier, checks the requested
project/binding scope, and only then delegates the canonical event to the
application interface service.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Any

from zero.adapters.discord import DiscordAdapter
from zero.adapters.messaging import (
    HttpTransport,
    PermanentTransportError,
    RetryPolicy,
    TransportError,
)
from zero.adapters.telegram import TelegramAdapter, _callback_outcome_text
from zero.app.secret_service import SecretService
from zero.config import Settings
from zero.domain.identity import ProjectId, UserId
from zero.domain.interfaces import (
    InterfaceBindingId,
    InterfaceBindingNotFoundError,
    InterfaceEventLogEntry,
    Platform,
)
from zero.domain.secrets import SecretReferenceId
from zero.persistence.repositories.interface_repository import InterfaceRepository

from .interface_service import InterfaceAdapterService

logger = logging.getLogger(__name__)


class InterfaceTransportError(RuntimeError):
    """Base class for composed transport failures."""


class InterfaceTransportNotConfigured(InterfaceTransportError):
    """The requested provider has no configured verifier."""


class InterfaceTransportUnknownOutcome(InterfaceTransportError):
    """Provider acceptance cannot be ruled out after the response boundary."""


class InterfaceScopeError(InterfaceTransportError):
    """The requested binding is missing, disabled, or belongs to another platform."""


class InterfaceTransportService:
    """Wire configured Telegram/Discord verifiers to canonical event intake."""

    def __init__(
        self,
        interface_service: InterfaceAdapterService,
        interface_repo: InterfaceRepository,
        settings: Settings,
        *,
        secret_service: SecretService | None = None,
        transport: HttpTransport | None = None,
    ) -> None:
        self._interface_service = interface_service
        self._interface_repo = interface_repo
        self._secret_service = secret_service
        self._transport = transport
        self._adapters: dict[str, TelegramAdapter | DiscordAdapter] = {}
        # GAP 4 (2026-08-31): when the user-session worker connects, its
        # adapter is attached here so bindings WITHOUT a bot token (the
        # user-session scope) can still deliver replies — through the
        # personal account, rate-limited by the adapter's token bucket.
        self._session_adapter: Any = None
        if settings.telegram_webhook_secret is not None:
            self._adapters["telegram"] = TelegramAdapter(
                event_handler=interface_service.process_inbound_event,
                transport=transport,
                webhook_secret=settings.telegram_webhook_secret.get_secret_value(),
                # Uniformity fix (2026-08-31): the verifier adapter never
                # calls the Bot API itself (no token), but every OTHER
                # adapter construction honors ZERO_TELEGRAM_API_BASE — the
                # webhook path now does too, so a self-hosted Bot API
                # gateway behaves identically on both intake paths.
                api_base_url=os.environ.get(
                    "ZERO_TELEGRAM_API_BASE", "https://api.telegram.org"
                ).rstrip("/"),
            )
        if settings.discord_application_public_key is not None:
            self._adapters["discord"] = DiscordAdapter(
                event_handler=interface_service.process_inbound_event,
                transport=transport,
                application_public_key=(settings.discord_application_public_key.get_secret_value()),
            )

    def process_webhook(
        self,
        *,
        platform: Platform,
        project_id: ProjectId,
        binding_id: InterfaceBindingId,
        body: bytes,
        headers: Mapping[str, str],
    ) -> InterfaceEventLogEntry | dict[str, Any] | None:
        adapter = self._adapters.get(platform)
        if adapter is None:
            raise InterfaceTransportNotConfigured(
                f"{platform} webhook verification is not configured"
            )

        # Verify the provider envelope before looking up the binding.  The
        # binding identifier is not an authorization credential and must not
        # become a scope oracle for unauthenticated callers.
        if platform == "telegram":
            adapter.verify_webhook(headers)  # type: ignore[union-attr]
        else:
            adapter.verify_webhook(body, headers)  # type: ignore[union-attr]

        try:
            binding = self._interface_repo.get_binding_by_id(project_id, binding_id)
        except (InterfaceBindingNotFoundError, ValueError) as exc:
            raise InterfaceScopeError("interface binding is not available") from exc
        if binding.platform != platform or not binding.is_enabled:
            raise InterfaceScopeError("interface binding is not enabled for this platform")

        decoded = adapter.decode_payload(body)
        if platform == "telegram":
            event = adapter.normalize_update(decoded)  # type: ignore[union-attr]
        else:
            event = adapter.normalize_interaction(decoded)  # type: ignore[union-attr]
        if event is not None and (
            event.chat_id != binding.chat_id or event.topic_id != binding.topic_id
        ):
            raise InterfaceScopeError("webhook payload does not match interface binding")

        # Round-7 fix (inline keyboard FULLY): the webhook adapter holds
        # NO bot credential — tokens are per-binding secrets resolved at
        # action time — so the adapter's own answer attempt can never
        # reach the Bot API. The transport service owns the webhook-path
        # acknowledgement: after the durable dispatch, resolve the
        # binding's token and answer the press with the outcome toast
        # (Hermes ``query.answer(text=...)`` parity). The crash path is
        # answered too, then the exception propagates unchanged.
        try:
            result = adapter.handle_webhook(body, headers=headers)  # type: ignore[union-attr]
        except Exception:
            self._answer_callback_for_binding(
                platform=platform,
                binding=binding,
                event=event,
                result=None,
                failed=True,
            )
            raise
        self._answer_callback_for_binding(
            platform=platform, binding=binding, event=event, result=result
        )
        return result

    def _answer_callback_for_binding(
        self,
        *,
        platform: Platform,
        binding,
        event,
        result: Any,
        failed: bool = False,
    ) -> None:
        """Answer a webhook-delivered button press with its outcome.

        Best-effort (mirrors send_typing): any failure — missing
        credential, disabled binding, provider hiccup — is swallowed at
        DEBUG because the durable event log remains authoritative.
        Presses from UNRESOLVED actors (strangers denied at the identity
        gate have ``resolved_user_id=None``) are not answered — the
        durable denial is the authoritative record and the Telegram
        client clears its own spinner after the query timeout.
        """
        if platform != "telegram" or event is None:
            return
        if event.event_kind != "callback_query" or not event.callback_query_id:
            return
        if self._secret_service is None or self._transport is None:
            return
        actor_id = getattr(result, "resolved_user_id", None)
        if actor_id is None:
            logger.debug(
                "callback answer skipped: press has no resolved actor "
                "(callback_query_id=%s)",
                event.callback_query_id,
            )
            return
        text = (
            "⚠️ Failed — see logs" if failed else _callback_outcome_text(result)
        )
        try:
            answerer = self.build_telegram_adapter(
                project_id=binding.project_id,
                binding_id=binding.id,
                actor_id=actor_id,
            )
            answerer.answer_callback_query(event.callback_query_id, text=text)
        except Exception as exc:  # noqa: BLE001 - acknowledgement is best-effort
            logger.debug(
                "callback answer skipped: %s (callback_query_id=%s)",
                type(exc).__name__,
                event.callback_query_id,
            )

    def attach_session_adapter(self, adapter: Any) -> None:
        """Attach the MTProto user-session adapter as an outbound path."""
        self._session_adapter = adapter

    def send_message(
        self,
        *,
        project_id: ProjectId,
        binding_id: InterfaceBindingId,
        actor_id: UserId,
        text: str,
        chat_id: str | None = None,
        topic_id: str | None = None,
        reply_to_message_id: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> str:
        """Send one bounded result through a project-scoped binding.

        The binding stores only a secret reference. The raw token is resolved
        immediately before adapter I/O and is never returned or included in
        an exception raised by this composition boundary.

        ``chat_id``/``topic_id`` override the binding's own chat scope.
        This is required for replies to events consumed through the
        polling-only binding (chat_id="0"): the reply must go to the chat
        the event actually came from, not the synthetic scope (bug fix,
        dead-bot session 2026-08-29 — the /start welcome reply used to be
        sent to chat "0" and rejected by Telegram with HTTP 400).

        ``reply_to_message_id`` threads the reply under the source
        message (Hermes reply-anchoring parity, round 5); the adapter
        drops a dead anchor on Telegram 400 and still delivers.
        ``reply_markup`` carries inline keyboards (the plan card's
        approve/reject buttons).
        """
        if self._secret_service is None or self._transport is None:
            raise InterfaceTransportNotConfigured("outbound messaging is not configured")
        try:
            binding = self._interface_repo.get_binding_by_id(project_id, binding_id)
        except (InterfaceBindingNotFoundError, ValueError) as exc:
            raise InterfaceScopeError("interface binding is not available") from exc
        if not binding.is_enabled:
            raise InterfaceScopeError("interface binding is not enabled")
        # GAP 4 outbound (2026-08-31): a user-session binding stores NO bot
        # token. When the MTProto adapter is attached, its replies flow
        # through the personal account (rate-limited by the adapter's token
        # bucket); without it the historical error is honest.
        if (
            binding.platform == "telegram"
            and binding.bot_token_ref is None
        ):
            if self._session_adapter is None:
                raise InterfaceTransportNotConfigured(
                    "telegram binding has no bot credential and no "
                    "user-session adapter is attached"
                )
            try:
                sent = self._session_adapter.send_message(
                    chat_id=str(
                        chat_id if chat_id is not None else binding.chat_id
                    ),
                    text=text,
                )
            except Exception as exc:
                raise InterfaceTransportError(
                    f"user-session outbound send failed: {type(exc).__name__}"
                ) from exc
            message_id = getattr(sent, "id", None)
            if message_id is None:
                raise InterfaceTransportUnknownOutcome(
                    "user-session send returned no message id"
                )
            return str(message_id)
        if binding.bot_token_ref is None:
            raise InterfaceTransportNotConfigured(
                "interface binding has no bot credential reference"
            )
        try:
            secret_id = SecretReferenceId(binding.bot_token_ref)
            token = self._secret_service.resolve_value(
                project_id=project_id,
                secret_id=secret_id,
                actor_id=actor_id,
                source="system",
            )
        except Exception as exc:
            raise InterfaceTransportError("interface credential could not be resolved") from exc

        try:
            if binding.platform == "telegram":
                import os as _os

                adapter = TelegramAdapter(
                    event_handler=lambda _event: None,
                    transport=self._transport,
                    bot_token=token,
                    # Honor the same gateway escape hatch the setup/doctor
                    # probes and the polling worker use — without this the
                    # delivery path silently bypassed a configured Bot API
                    # gateway (found via the real-process e2e, 2026-08-29).
                    api_base_url=_os.environ.get(
                        "ZERO_TELEGRAM_API_BASE", "https://api.telegram.org"
                    ).rstrip("/"),
                    # Flaky-network fix (2026-08-29): the historical default
                    # policy (3 attempts x 10s) could block the delivery
                    # drain for ~30s per message on a slow/broken network.
                    # One clean attempt with a real budget; durable retry
                    # (ResultDeliveryService exponential retry_after) owns
                    # the second chance.
                    retry_policy=RetryPolicy(
                        attempts=1,
                        backoff_seconds=0.5,
                        timeout_seconds=30.0,
                    ),
                )
                response = adapter.send_message(
                    chat_id=chat_id if chat_id is not None else binding.chat_id,
                    topic_id=topic_id if topic_id is not None else binding.topic_id,
                    text=text,
                    reply_to_message_id=reply_to_message_id,
                    reply_markup=reply_markup,
                )
            elif binding.platform == "discord":
                adapter = DiscordAdapter(
                    event_handler=lambda _event: None,
                    transport=self._transport,
                    bot_token=token,
                    retry_policy=RetryPolicy(
                        attempts=1,
                        backoff_seconds=0.5,
                        timeout_seconds=30.0,
                    ),
                )
                response = adapter.send_message(
                    channel_id=binding.chat_id,
                    thread_id=binding.topic_id,
                    text=text,
                )
            else:
                raise InterfaceTransportNotConfigured(
                    f"outbound platform {binding.platform!r} is not supported"
                )
            payload = response.json() if callable(response.json) else response.json
            if not isinstance(payload, Mapping):
                raise InterfaceTransportError("provider response was not an object")
            if binding.platform == "telegram":
                result = payload.get("result")
                message_id = result.get("message_id") if isinstance(result, Mapping) else None
            else:
                message_id = payload.get("id")
            if message_id is None:
                raise InterfaceTransportUnknownOutcome(
                    "provider response did not include a message id"
                )
            return str(message_id)
        except InterfaceTransportUnknownOutcome:
            raise
        except PermanentTransportError as exc:
            # exc text is provider status only; safe to carry through.
            raise InterfaceTransportError(f"provider rejected outbound message: {exc}") from exc
        except TransportError as exc:
            # TransportError text is already token-redacted at the adapter
            # boundary; carrying the cause summary makes the durable
            # delivery record diagnosable instead of "ambiguous".
            raise InterfaceTransportUnknownOutcome(
                f"provider response outcome is ambiguous: {exc}"
            ) from exc
        except InterfaceTransportError:
            raise
        except Exception as exc:
            raise InterfaceTransportError(
                f"outbound messaging request failed: {type(exc).__name__}"
            ) from exc

    def build_telegram_adapter(
        self,
        *,
        project_id: ProjectId,
        binding_id: InterfaceBindingId,
        actor_id: UserId,
    ) -> TelegramAdapter:
        """Build a short-lived Telegram adapter for a binding's credential.

        Used by the conversational bridge for media downloads (getFile +
        file bytes) and typing actions outside the sendMessage path.
        The resolved token lives only inside the returned adapter and is
        never returned to the caller (same contract as send_message).
        """
        if self._secret_service is None or self._transport is None:
            raise InterfaceTransportNotConfigured("outbound messaging is not configured")
        try:
            binding = self._interface_repo.get_binding_by_id(project_id, binding_id)
        except (InterfaceBindingNotFoundError, ValueError) as exc:
            raise InterfaceScopeError("interface binding is not available") from exc
        if not binding.is_enabled:
            raise InterfaceScopeError("interface binding is not enabled")
        if binding.platform != "telegram":
            raise InterfaceScopeError("binding is not a Telegram scope")
        if binding.bot_token_ref is None:
            raise InterfaceTransportNotConfigured(
                "interface binding has no bot credential reference"
            )
        try:
            secret_id = SecretReferenceId(binding.bot_token_ref)
            token = self._secret_service.resolve_value(
                project_id=project_id,
                secret_id=secret_id,
                actor_id=actor_id,
                source="system",
            )
        except Exception as exc:
            raise InterfaceTransportError("interface credential could not be resolved") from exc
        return TelegramAdapter(
            event_handler=lambda _event: None,
            transport=self._transport,
            bot_token=token,
            api_base_url=os.environ.get(
                "ZERO_TELEGRAM_API_BASE", "https://api.telegram.org"
            ).rstrip("/"),
            retry_policy=RetryPolicy(attempts=1, backoff_seconds=0.5, timeout_seconds=30.0),
        )

    def send_typing(
        self,
        *,
        project_id: ProjectId,
        binding_id: InterfaceBindingId,
        actor_id: UserId,
        chat_id: str | None = None,
        topic_id: str | None = None,
    ) -> None:
        """Best-effort typing indicator; NEVER raises.

        A chat answer that takes ten seconds feels dead without the
        typing bubble (Hermes ``_keep_typing`` parity, round 5). But the
        indicator is pure cosmetics: any failure — missing credential,
        disabled binding, provider hiccup — is swallowed at this
        boundary so the real reply is never jeopardized.
        """
        try:
            if self._secret_service is None or self._transport is None:
                return
            try:
                binding = self._interface_repo.get_binding_by_id(project_id, binding_id)
            except (InterfaceBindingNotFoundError, ValueError):
                return
            if not binding.is_enabled or binding.platform != "telegram":
                return
            if binding.bot_token_ref is None:
                return
            adapter = self.build_telegram_adapter(
                project_id=project_id, binding_id=binding_id, actor_id=actor_id
            )
            adapter.send_chat_action(
                chat_id=chat_id if chat_id is not None else binding.chat_id,
                topic_id=topic_id if topic_id is not None else binding.topic_id,
                action="typing",
            )
        except Exception as exc:  # noqa: BLE001 - typing must never break a turn
            logger.debug("typing indicator skipped: %s", type(exc).__name__)

    def close(self) -> None:
        """Close the owned HTTP client during application shutdown."""
        closer = getattr(self._transport, "close", None)
        if callable(closer):
            closer()

    @property
    def is_outbound_configured(self) -> bool:
        """Whether outbound provider I/O has an injected HTTP transport."""
        return self._transport is not None

    @property
    def http_transport(self) -> HttpTransport | None:
        """The injected HTTP transport for adapters hosted by workers."""
        return self._transport


__all__ = [
    "InterfaceScopeError",
    "InterfaceTransportError",
    "InterfaceTransportNotConfigured",
    "InterfaceTransportService",
    "InterfaceTransportUnknownOutcome",
]
