"""Production composition for inbound messaging transports.

The provider adapters remain thin edge translators.  This service owns the
composition boundary: it selects a configured verifier, checks the requested
project/binding scope, and only then delegates the canonical event to the
application interface service.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from zero.adapters.discord import DiscordAdapter
from zero.adapters.messaging import (
    HttpTransport,
    PermanentTransportError,
    TransportError,
)
from zero.adapters.telegram import TelegramAdapter
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
        if settings.telegram_webhook_secret is not None:
            self._adapters["telegram"] = TelegramAdapter(
                event_handler=interface_service.process_inbound_event,
                transport=transport,
                webhook_secret=settings.telegram_webhook_secret.get_secret_value(),
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

        return adapter.handle_webhook(body, headers=headers)  # type: ignore[union-attr]

    def send_message(
        self,
        *,
        project_id: ProjectId,
        binding_id: InterfaceBindingId,
        actor_id: UserId,
        text: str,
        chat_id: str | None = None,
        topic_id: str | None = None,
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
        """
        if self._secret_service is None or self._transport is None:
            raise InterfaceTransportNotConfigured("outbound messaging is not configured")
        try:
            binding = self._interface_repo.get_binding_by_id(project_id, binding_id)
        except (InterfaceBindingNotFoundError, ValueError) as exc:
            raise InterfaceScopeError("interface binding is not available") from exc
        if not binding.is_enabled:
            raise InterfaceScopeError("interface binding is not enabled")
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
                )
                response = adapter.send_message(
                    chat_id=chat_id if chat_id is not None else binding.chat_id,
                    topic_id=topic_id if topic_id is not None else binding.topic_id,
                    text=text,
                )
            elif binding.platform == "discord":
                adapter = DiscordAdapter(
                    event_handler=lambda _event: None,
                    transport=self._transport,
                    bot_token=token,
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
            raise InterfaceTransportError("provider rejected outbound message") from exc
        except TransportError as exc:
            raise InterfaceTransportUnknownOutcome(
                "provider response outcome is ambiguous"
            ) from exc
        except InterfaceTransportError:
            raise
        except Exception as exc:
            raise InterfaceTransportError("outbound messaging request failed") from exc

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
