"""Thin Telegram webhook/polling adapter with injected HTTP transport."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from threading import Event
from typing import Any

from zero.domain.interfaces import NormalizedEvent

from .messaging import (
    AdapterError,
    BaseMessagingAdapter,
    HttpResponse,
    HttpTransport,
    RetryPolicy,
    UnsupportedUpdateError,
    WebhookAuthError,
    _cursor_get,
    _cursor_set,
    safe_render_text,
    verify_secret_header,
)

logger = logging.getLogger(__name__)


class TelegramAdapter(BaseMessagingAdapter):
    """Normalize Telegram updates and dispatch durable application events.

    ``event_handler`` is normally ``InterfaceAdapterService.process_inbound_event``.
    The adapter never receives a raw bot token from application state; callers
    inject one only at the transport boundary and tests use a fake token.
    """

    platform = "telegram"

    def __init__(
        self,
        event_handler=None,
        *,
        event_sink=None,
        transport: HttpTransport | None = None,
        bot_token: str | None = None,
        webhook_secret: str | None = None,
        cursor_store: Any = None,
        api_base_url: str = "https://api.telegram.org",
        poll_timeout_seconds: int = 25,
        acknowledge_callbacks: bool = True,
        retry_policy: RetryPolicy | None = None,
        retry_attempts: int | None = None,
        retry_backoff_seconds: float | None = None,
        sleeper=None,
    ) -> None:
        if poll_timeout_seconds < 0 or poll_timeout_seconds > 50:
            raise ValueError("poll_timeout_seconds must be between 0 and 50")
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
        self._webhook_secret = webhook_secret
        self._cursor_store = cursor_store
        self._api_base_url = api_base_url.rstrip("/")
        self._poll_timeout_seconds = poll_timeout_seconds
        self._acknowledge_callbacks = acknowledge_callbacks

    def verify_webhook(self, headers: Mapping[str, str]) -> None:
        if self._webhook_secret is None:
            raise WebhookAuthError("Telegram webhook verification is not configured")
        verify_secret_header(
            headers,
            header_name="X-Telegram-Bot-Api-Secret-Token",
            expected=self._webhook_secret,
        )

    def normalize_update(self, update: Mapping[str, Any]) -> NormalizedEvent | None:
        """Convert a Telegram message or callback update to a canonical event."""
        if not isinstance(update, Mapping):
            raise UnsupportedUpdateError("Telegram update must be an object")
        update_id = update.get("update_id")
        if update_id is None:
            raise UnsupportedUpdateError("Telegram update_id is required")
        message = (
            update.get("message")
            or update.get("edited_message")
            or update.get("channel_post")
            or update.get("edited_channel_post")
        )
        callback = update.get("callback_query")
        if callback is not None:
            if not isinstance(callback, Mapping):
                raise UnsupportedUpdateError("callback_query must be an object")
            message = callback.get("message") or {}
            actor = callback.get("from") or {}
            data = callback.get("data")
            if not isinstance(message, Mapping) or not isinstance(actor, Mapping):
                raise UnsupportedUpdateError("callback query has malformed scope")
            chat = message.get("chat") or {}
            if not isinstance(chat, Mapping) or actor.get("id") is None or chat.get("id") is None:
                raise UnsupportedUpdateError("callback query lacks actor or chat")
            return NormalizedEvent(
                platform="telegram",
                external_event_id=str(update_id),
                external_actor_id=str(actor["id"]),
                chat_id=str(chat["id"]),
                topic_id=(
                    str(message["message_thread_id"])
                    if message.get("message_thread_id") is not None
                    else None
                ),
                event_kind="callback_query",
                content=str(data or ""),
                callback_token=str(data) if data is not None else None,
            )
        if not isinstance(message, Mapping):
            return None
        actor = message.get("from") or {}
        chat = message.get("chat") or {}
        if not isinstance(actor, Mapping) or not isinstance(chat, Mapping):
            raise UnsupportedUpdateError("Telegram message has malformed actor or chat")
        if actor.get("id") is None or chat.get("id") is None:
            return None
        content = message.get("text")
        if content is None:
            content = message.get("caption")
        if content is None:
            content = ""
        content = str(content)
        kind = "command" if content.startswith("/") else "message"
        return NormalizedEvent(
            platform="telegram",
            external_event_id=str(update_id),
            external_actor_id=str(actor["id"]),
            chat_id=str(chat["id"]),
            topic_id=(
                str(message["message_thread_id"])
                if message.get("message_thread_id") is not None
                else None
            ),
            event_kind=kind,  # type: ignore[arg-type]
            content=content,
        )

    parse_update = normalize_update

    def handle_webhook(
        self, payload: Mapping[str, Any] | bytes | str, *, headers: Mapping[str, str]
    ) -> Any:
        self.verify_webhook(headers)
        update = self._decode_payload(payload)
        event = self.normalize_update(update)
        if event is None:
            return None
        callback = update.get("callback_query")
        if (
            callback
            and self._acknowledge_callbacks
            and self._transport is not None
            and isinstance(callback, Mapping)
            and callback.get("id") is not None
        ):
            # A callback acknowledgement must be attempted before slow domain
            # work. It remains best-effort because the durable event is still
            # authoritative if Telegram is unavailable.
            try:
                self.answer_callback_query(str(callback["id"]))
            except AdapterError as exc:
                logger.debug("Telegram callback acknowledgement failed: %s", type(exc).__name__)
        return self._dispatch(event)

    def _api_url(self, method: str) -> str:
        if not self._bot_token:
            raise WebhookAuthError("Telegram bot credential is not configured")
        return f"{self._api_base_url}/bot{self._bot_token}/{method}"

    def _call_api(self, method: str, payload: dict[str, Any]) -> HttpResponse:
        response = self._request("POST", self._api_url(method), payload=payload)
        data = self._response_json(response)
        if not isinstance(data, Mapping) or data.get("ok") is False:
            raise RuntimeError("Telegram API returned an unsuccessful response")
        return response

    def send_message(
        self,
        *,
        chat_id: str,
        text: str,
        topic_id: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> HttpResponse:
        payload: dict[str, Any] = {
            "chat_id": str(chat_id),
            "text": safe_render_text(text, platform="telegram", limit=4096),
            "parse_mode": "HTML",
        }
        if topic_id is not None:
            payload["message_thread_id"] = str(topic_id)
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self._call_api("sendMessage", payload)

    def edit_message(
        self,
        *,
        chat_id: str,
        message_id: str,
        text: str,
        topic_id: str | None = None,
    ) -> HttpResponse:
        payload: dict[str, Any] = {
            "chat_id": str(chat_id),
            "message_id": str(message_id),
            "text": safe_render_text(text, platform="telegram", limit=4096),
            "parse_mode": "HTML",
        }
        if topic_id is not None:
            payload["message_thread_id"] = str(topic_id)
        return self._call_api("editMessageText", payload)

    def answer_callback_query(
        self, callback_query_id: str, *, text: str | None = None
    ) -> HttpResponse:
        payload: dict[str, Any] = {"callback_query_id": str(callback_query_id)}
        if text:
            payload["text"] = safe_render_text(text, platform="telegram", limit=200)
        return self._call_api("answerCallbackQuery", payload)

    def poll_once(self, *, scope_key: str = "default") -> list[Any]:
        """Fetch one bounded polling batch and persist the next offset."""
        cursor = _cursor_get(self._cursor_store, "telegram", scope_key)
        payload: dict[str, Any] = {
            "timeout": self._poll_timeout_seconds,
            "allowed_updates": ["message", "edited_message", "callback_query"],
        }
        if cursor is not None:
            payload["offset"] = int(cursor)
        response = self._call_api("getUpdates", payload)
        data = self._response_json(response)
        updates = data.get("result") if isinstance(data, Mapping) else None
        if not isinstance(updates, list):
            raise UnsupportedUpdateError("Telegram getUpdates result is not a list")
        results: list[Any] = []
        max_update_id: int | None = None
        for update in updates:
            if not isinstance(update, Mapping):
                continue
            update_id = update.get("update_id")
            if update_id is None:
                logger.warning("Telegram polling skipped update without an update id")
                continue
            try:
                numeric_update_id = int(update_id)
            except (TypeError, ValueError):
                logger.warning("Telegram polling skipped update with invalid update id")
                continue
            max_update_id = max(numeric_update_id, max_update_id or numeric_update_id)
            try:
                event = self.normalize_update(update)
            except UnsupportedUpdateError as exc:
                # The provider has assigned a durable offset to this poison
                # update.  Record the bounded type only and advance past it so
                # one malformed payload cannot stall polling forever.
                logger.warning("Telegram polling skipped malformed update: %s", type(exc).__name__)
                continue
            if event is not None:
                result = self._dispatch(event)
                if getattr(result, "processing_result", None) == "error":
                    # The durable event log already captured the failure and
                    # this update owns a durable offset. Record it and advance
                    # so one poisoned event cannot stall the whole polling
                    # batch or kill the polling loop; recovery replays claims
                    # through the same durable boundary.
                    logger.warning(
                        "Telegram polling recorded an errored inbound event "
                        "(update_id=%s); continuing",
                        numeric_update_id,
                    )
                results.append(result)
        if max_update_id is not None:
            _cursor_set(self._cursor_store, "telegram", scope_key, str(max_update_id + 1))
        return results

    def poll_forever(
        self,
        *,
        scope_key: str = "default",
        stop_event: Event | None = None,
        retry_delay_seconds: float = 1.0,
    ) -> None:
        """Run the durable polling loop until cancellation is requested."""
        if retry_delay_seconds < 0 or retry_delay_seconds > 60:
            raise ValueError("retry_delay_seconds must be between 0 and 60")
        stop_event = stop_event or Event()
        while not stop_event.is_set():
            try:
                self.poll_once(scope_key=scope_key)
            except (AdapterError, ConnectionError, OSError, TimeoutError) as exc:
                logger.warning("Telegram polling iteration failed: %s", type(exc).__name__)
                if stop_event.wait(retry_delay_seconds):
                    break


__all__ = ["TelegramAdapter", "WebhookAuthError"]
