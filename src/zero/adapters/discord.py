"""Thin Discord interaction/webhook adapter with injected HTTP transport."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from zero.domain.interfaces import NormalizedEvent

from .messaging import (
    BaseMessagingAdapter,
    HttpResponse,
    HttpTransport,
    RetryPolicy,
    UnsupportedUpdateError,
    WebhookAuthError,
    safe_render_text,
    verify_ed25519_signature,
    verify_secret_header,
)


class DiscordAdapter(BaseMessagingAdapter):
    """Normalize Discord interactions and deliver bounded responses."""

    platform = "discord"

    def __init__(
        self,
        event_handler=None,
        *,
        event_sink=None,
        transport: HttpTransport | None = None,
        bot_token: str | None = None,
        application_public_key: bytes | str | None = None,
        webhook_secret: str | None = None,
        application_id: str | None = None,
        api_base_url: str = "https://discord.com/api/v10",
        retry_policy: RetryPolicy | None = None,
        retry_attempts: int | None = None,
        retry_backoff_seconds: float | None = None,
        sleeper=None,
    ) -> None:
        super().__init__(
            event_handler,
            event_sink=event_sink,
            transport=transport,
            retry_policy=retry_policy,
            retry_attempts=retry_attempts,
            retry_backoff_seconds=retry_backoff_seconds,
            sleeper=sleeper or __import__("time").sleep,
        )
        self._bot_token = bot_token
        self._application_public_key = application_public_key
        self._webhook_secret = webhook_secret
        self._application_id = application_id
        self._api_base_url = api_base_url.rstrip("/")

    def verify_webhook(self, body: bytes, headers: Mapping[str, str]) -> None:
        if self._application_public_key is not None:
            verify_ed25519_signature(body, headers, public_key=self._application_public_key)
            return
        if self._webhook_secret is not None:
            verify_secret_header(
                headers,
                header_name="X-Discord-Bot-Secret",
                expected=self._webhook_secret,
            )
            return
        raise WebhookAuthError("Discord webhook verification is not configured")

    def normalize_interaction(self, payload: Mapping[str, Any]) -> NormalizedEvent | None:
        if not isinstance(payload, Mapping):
            raise UnsupportedUpdateError("Discord payload must be an object")
        event_id = payload.get("id")
        if event_id is None:
            raise UnsupportedUpdateError("Discord interaction id is required")
        try:
            interaction_type = int(payload.get("type", 0))
        except (TypeError, ValueError) as exc:
            raise UnsupportedUpdateError("Discord interaction type must be an integer") from exc
        if interaction_type == 1:  # Discord PING; caller returns a PONG.
            return None
        member = payload.get("member") or {}
        user = member.get("user") if isinstance(member, Mapping) else None
        if not isinstance(user, Mapping):
            user = payload.get("user") or {}
        actor_id = user.get("id") if isinstance(user, Mapping) else None
        channel_id = payload.get("channel_id")
        message = payload.get("message") or {}
        if channel_id is None and isinstance(message, Mapping):
            channel_id = message.get("channel_id")
        if actor_id is None or channel_id is None:
            raise UnsupportedUpdateError("Discord interaction lacks actor or channel")
        data = payload.get("data") or {}
        if not isinstance(data, Mapping):
            data = {}
        callback_token = data.get("custom_id")
        content = data.get("value") or data.get("name") or ""
        if callback_token is not None:
            event_kind = "callback_query"
            content = str(callback_token)
        elif interaction_type == 2:
            event_kind = "command"
        else:
            event_kind = "message"
        topic_id = payload.get("thread_id")
        if topic_id is None and isinstance(message, Mapping):
            topic_id = message.get("thread_id")
        return NormalizedEvent(
            platform="discord",
            external_event_id=str(event_id),
            external_actor_id=str(actor_id),
            chat_id=str(channel_id),
            topic_id=str(topic_id) if topic_id is not None else None,
            event_kind=event_kind,  # type: ignore[arg-type]
            content=str(content),
            callback_token=str(callback_token) if callback_token is not None else None,
            transport_interaction_id=str(event_id),
            transport_interaction_token=(
                str(payload["token"]) if payload.get("token") is not None else None
            ),
        )

    parse_interaction = normalize_interaction

    def handle_webhook(
        self, payload: Mapping[str, Any] | bytes | str, *, headers: Mapping[str, str]
    ) -> Any:
        if isinstance(payload, Mapping):
            raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        elif isinstance(payload, str):
            raw = payload.encode("utf-8")
        else:
            raw = payload
        self.verify_webhook(raw, headers)
        decoded = self._decode_payload(raw)
        try:
            ping_type = int(decoded.get("type", 0))
        except (TypeError, ValueError) as exc:
            raise UnsupportedUpdateError("Discord interaction type must be an integer") from exc
        if ping_type == 1:
            return {"type": 1}
        event = self.normalize_interaction(decoded)
        if event is None:
            return {"type": 1}
        if event.transport_interaction_token is not None:
            self.acknowledge_interaction(
                interaction_id=event.transport_interaction_id or event.external_event_id,
                interaction_token=event.transport_interaction_token,
            )
        return self._dispatch(event)

    def _headers(self) -> dict[str, str]:
        if not self._bot_token:
            raise WebhookAuthError("Discord bot credential is not configured")
        return {
            "Authorization": f"Bot {self._bot_token}",
            "Content-Type": "application/json",
        }

    def _call_api(self, method: str, path: str, payload: dict[str, Any]) -> HttpResponse:
        response = self._request(
            method,
            f"{self._api_base_url}/{path.lstrip('/')}",
            headers=self._headers(),
            payload=payload,
        )
        if response.status_code not in range(200, 300):
            raise RuntimeError("Discord API returned an unsuccessful response")
        return response

    def send_message(
        self,
        *,
        channel_id: str,
        text: str,
        thread_id: str | None = None,
    ) -> HttpResponse:
        payload: dict[str, Any] = {
            "content": safe_render_text(text, platform="discord", limit=2000),
        }
        # Discord threads are channels.  Using the thread ID as a
        # ``message_reference`` while posting to the parent channel creates a
        # reply in the parent rather than delivering into the thread.
        target_channel_id = thread_id or channel_id
        return self._call_api("POST", f"channels/{target_channel_id}/messages", payload)

    def edit_message(
        self,
        *,
        channel_id: str,
        message_id: str,
        text: str,
    ) -> HttpResponse:
        return self._call_api(
            "PATCH",
            f"channels/{channel_id}/messages/{message_id}",
            {"content": safe_render_text(text, platform="discord", limit=2000)},
        )

    def acknowledge_interaction(
        self,
        *,
        interaction_id: str,
        interaction_token: str,
        content: str | None = None,
    ) -> HttpResponse:
        payload: dict[str, Any] = {"type": 4}
        if content is not None:
            payload["data"] = {"content": safe_render_text(content, platform="discord", limit=2000)}
        return self._request(
            "POST",
            f"{self._api_base_url}/interactions/{interaction_id}/{interaction_token}/callback",
            headers={"Content-Type": "application/json"},
            payload=payload,
        )


__all__ = ["DiscordAdapter", "WebhookAuthError"]
