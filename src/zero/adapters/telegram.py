"""Thin Telegram webhook/polling adapter with injected HTTP transport."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from threading import Event
from typing import Any

from zero.domain.interfaces import MediaAttachment, NormalizedEvent

from .messaging import (
    AdapterError,
    BaseMessagingAdapter,
    HttpResponse,
    HttpTransport,
    PermanentTransportError,
    RetryPolicy,
    TransportError,
    UnsupportedUpdateError,
    WebhookAuthError,
    _cursor_get,
    _cursor_set,
    safe_render_text,
    verify_secret_header,
)
from .telegram_render import (
    TELEGRAM_MESSAGE_LIMIT,
    chunk_telegram_text,
    render_telegram_html,
    render_telegram_html_bounded,
)

logger = logging.getLogger(__name__)


def _noop_http_response() -> HttpResponse:
    """A synthetic 200 used when a redundant edit is treated as success."""

    class _NoopResponse:
        status_code = 200
        content = b'{"ok":true,"result":true}'
        headers: dict[str, str] = {}

    return _NoopResponse()  # type: ignore[return-value]


def _parse_retry_after(message: str) -> float | None:
    """Extract a Bot API RetryAfter seconds value from an error string."""
    import re as _re

    match = _re.search(r"retry after (\d+)", str(message).lower())
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return None
    return None


def _callback_outcome_text(result: Any) -> str:
    """Map a dispatch result onto the button-press toast text.

    Hermes parity (``test_telegram_approval_buttons.py`` asserts the
    answer TEXT): the Telegram client shows this string as a toast on
    the pressed button, so it must say what HAPPENED — not just stop
    the spinner. The dispatch result is the durable event log entry
    (``processing_result`` + ``processing_detail``); unknown shapes
    degrade to a neutral acknowledgment.
    """
    outcome = str(getattr(result, "processing_result", "") or "")
    detail = str(getattr(result, "processing_detail", "") or "").lower()
    if outcome == "processed":
        if "approve" in detail:
            return "✅ Plan approved"
        if "reject" in detail:
            return "✖️ Plan rejected"
        return "✅ Done"
    if outcome == "denied":
        return "⛔ Not allowed"
    if outcome == "error":
        return "⚠️ Failed — see logs"
    return "✅ Done"


def _extract_media(message: Mapping[str, Any]) -> list[MediaAttachment]:
    """Pull media references off a raw Telegram message payload.

    Hermes parity (round 5 gap 3): the canonical envelope used to drop
    every attachment silently, so photos/documents sent to the bot
    reached the model as empty text. Telegram photos arrive as an array
    of progressively larger sizes — the largest variant wins.
    """
    media: list[MediaAttachment] = []

    photos = message.get("photo")
    if isinstance(photos, list) and photos:
        best: Mapping[str, Any] | None = None
        best_key: tuple[int, int] = (-1, -1)
        for candidate in photos:
            if not isinstance(candidate, Mapping) or not candidate.get("file_id"):
                continue
            width = int(candidate.get("width") or 0)
            height = int(candidate.get("height") or 0)
            size = int(candidate.get("file_size") or 0)
            key = (width * height, size)
            if key > best_key:
                best_key, best = key, candidate
        if best is not None:
            media.append(
                MediaAttachment(
                    kind="photo",
                    file_id=str(best["file_id"]),
                    mime_type=str(best.get("mime_type") or "image/jpeg"),
                    file_size=(int(best["file_size"]) if best.get("file_size") else None),
                )
            )

    document = message.get("document")
    if isinstance(document, Mapping) and document.get("file_id"):
        media.append(
            MediaAttachment(
                kind="document",
                file_id=str(document["file_id"]),
                file_name=(str(document["file_name"]) if document.get("file_name") else None),
                mime_type=(str(document["mime_type"]) if document.get("mime_type") else None),
                file_size=(int(document["file_size"]) if document.get("file_size") else None),
            )
        )

    for kind in ("voice", "video", "audio", "sticker"):
        item = message.get(kind)
        if isinstance(item, Mapping) and item.get("file_id"):
            media.append(
                MediaAttachment(
                    kind=kind,
                    file_id=str(item["file_id"]),
                    mime_type=(str(item["mime_type"]) if item.get("mime_type") else None),
                    file_size=(int(item["file_size"]) if item.get("file_size") else None),
                )
            )
    return media


class TelegramConflictError(AdapterError):
    """Another getUpdates consumer is polling this bot token (HTTP 409).

    Bug fix (2026-08-29, dead-bot session): two live engines (`zero start`
    + `zero-develop serve`) long-polled the SAME bot token. Telegram
    rejects the loser with 409, which used to surface as an anonymous
    PermanentTransportError and hot-looped at full polling speed. The
    polling worker now recognizes this typed error and backs off with a
    clear, one-time explanation instead of error-spamming.
    """


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
                callback_query_id=(
                    str(callback["id"]) if callback.get("id") is not None else None
                ),
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
        media = _extract_media(message)
        reply_anchor = message.get("reply_to_message")
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
            media=tuple(media),
            message_id=(
                str(message["message_id"])
                if message.get("message_id") is not None
                else None
            ),
            reply_to_message_id=(
                str(reply_anchor["message_id"])
                if isinstance(reply_anchor, Mapping)
                and reply_anchor.get("message_id") is not None
                else None
            ),
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
        try:
            result = self._dispatch(event)
        except Exception:
            # A crashed dispatch must still stop the client's spinner —
            # answer with an honest failure toast, then re-raise so the
            # intake boundary records the exception as before.
            self._answer_callback_failure(event)
            raise
        self._answer_callback_outcome(event, result)
        return result

    def _answer_callback(self, event: NormalizedEvent, text: str) -> None:
        """Answer a button press ONCE with an explicit toast text.

        Best-effort in the STRONGEST sense: the durable event log remains
        authoritative, and NO answer failure may ever break intake —
        including a real Bot API 400 (``QUERY_ID_INVALID``) for a stale
        or already-answered query, which surfaces as a plain RuntimeError
        from ``_call_api`` (ok=false), NOT as an AdapterError. Letting it
        escape killed the whole polling worker (round-7 live finding) —
        one expired button press would have taken the bot offline.
        An adapter WITHOUT a bot token (the webhook composition holds
        none — tokens are per-binding secrets) must SKIP instead: the
        transport service owns the webhook-path answer
        (``_answer_callback_for_binding``), while token-holding adapters
        (the polling worker) answer inline.
        """
        if (
            not self._acknowledge_callbacks
            or event.event_kind != "callback_query"
            or not event.callback_query_id
            or self._transport is None
            or not self._bot_token
        ):
            return
        try:
            self.answer_callback_query(event.callback_query_id, text=text)
        except Exception as exc:  # noqa: BLE001 - acknowledgement is best-effort
            logger.debug(
                "Telegram callback acknowledgement skipped: %s", type(exc).__name__
            )

    def _answer_callback_outcome(self, event: NormalizedEvent, result: Any) -> None:
        """Answer a button press AFTER processing, with the outcome.

        Hermes parity (``test_telegram_approval_buttons.py``): every
        callback query is answered with visible feedback — success,
        denial, or failure — so the Telegram client never leaves the
        loading clock spinning on the button. Both intake paths (webhook
        and polling) share this single acknowledge point.
        """
        self._answer_callback(event, _callback_outcome_text(result))

    def _answer_callback_failure(self, event: NormalizedEvent) -> None:
        """Answer a press whose processing CRASHED, honestly.

        The spinner must stop on EVERY path — including exceptions —
        with feedback that does not pretend success.
        """
        self._answer_callback(event, "⚠️ Failed — see logs")

    def _api_url(self, method: str) -> str:
        if not self._bot_token:
            raise WebhookAuthError("Telegram bot credential is not configured")
        return f"{self._api_base_url}/bot{self._bot_token}/{method}"

    def _call_api(self, method: str, payload: dict[str, Any]) -> HttpResponse:
        try:
            response = self._request("POST", self._api_url(method), payload=payload)
        except PermanentTransportError as exc:
            # Translate "HTTP status 409" into a typed conflict error so
            # the polling worker can back off instead of hot-looping.
            message = str(exc)
            if "status 409" in message:
                raise TelegramConflictError(
                    "another getUpdates consumer (a second Zero instance, or "
                    "another process using this bot token) is already "
                    "long-polling Telegram — only ONE poller per bot token "
                    "is allowed"
                ) from exc
            raise
        data = self._response_json(response)
        if not isinstance(data, Mapping) or data.get("ok") is False:
            raise RuntimeError("Telegram API returned an unsuccessful response")
        return response

    def get_me(self) -> dict[str, Any]:
        """Fetch the bot's own identity via ``getMe``."""
        response = self._call_api("getMe", {})
        data = self._response_json(response)
        if not isinstance(data, Mapping) or not isinstance(data.get("result"), Mapping):
            raise TypeError("Telegram getMe returned an unexpected payload")
        return dict(data["result"])

    def send_message(
        self,
        *,
        chat_id: str,
        text: str,
        topic_id: str | None = None,
        reply_markup: dict[str, Any] | None = None,
        reply_to_message_id: str | None = None,
    ) -> HttpResponse:
        """Render, chunk, and deliver one outbound message.

        Hermes parity (round 5 gaps 1+2):
        - markdown is rendered to Telegram-safe HTML (escape-first),
          instead of HTML-escaping raw markdown into literal ``**``;
        - long text is split into UTF-16-bounded chunks with code-fence
          preservation, instead of being silently truncated at 4096;
        - chunk indicators ``(i/n)`` are appended when the reply spans
          multiple messages; a single-chunk reply stays clean;
        - the reply anchor rides the FIRST chunk only (Hermes
          ``_should_thread_reply`` default "first") and inline buttons
          ride the LAST chunk so the card stays actionable;
        - a dead reply anchor (Telegram 400 "message to be replied not
          found", observed live when replying to webhook-synthesized
          ids) is dropped and the chunk re-sent — the content and the
          buttons must survive a stale anchor.
        """
        chunks = chunk_telegram_text(str(text or ""), with_indicators=True)
        if not chunks:
            raise ValueError("cannot send an empty Telegram message")
        total = len(chunks)
        response: HttpResponse | None = None
        for index, chunk in enumerate(chunks):
            payload: dict[str, Any] = {
                "chat_id": str(chat_id),
                "text": render_telegram_html(chunk),
                "parse_mode": "HTML",
            }
            if topic_id is not None:
                payload["message_thread_id"] = str(topic_id)
            if reply_markup is not None and index == total - 1:
                payload["reply_markup"] = reply_markup
            anchor = reply_to_message_id if index == 0 else None
            if anchor is not None:
                payload["reply_to_message_id"] = str(anchor)
            try:
                response = self._call_api("sendMessage", payload)
            except PermanentTransportError as exc:
                if anchor is None or "status 400" not in str(exc):
                    raise
                logger.warning(
                    "Telegram rejected the reply anchor (400) — re-sending "
                    "the chunk without it (thread-not-found fallback)"
                )
                # A fresh dict: the payload already handed to the
                # transport must stay immutable from the caller's view.
                fallback_payload = {
                    key: value for key, value in payload.items()
                    if key != "reply_to_message_id"
                }
                response = self._call_api("sendMessage", fallback_payload)
        assert response is not None
        return response

    def get_file(self, *, file_id: str) -> dict[str, Any]:
        """Resolve a Telegram file reference via ``getFile``."""
        response = self._call_api("getFile", {"file_id": str(file_id)})
        data = self._response_json(response)
        result = data.get("result") if isinstance(data, Mapping) else None
        if not isinstance(result, Mapping):
            raise AdapterError("Telegram getFile returned an unexpected payload")
        return dict(result)

    def download_file_bytes(self, *, file_path: str) -> bytes:
        """Download file bytes from the Bot API file endpoint.

        The caller supplies ``file_path`` from :meth:`get_file`. Content
        is returned raw (not JSON) and never logged.
        """
        if not self._bot_token:
            raise WebhookAuthError("Telegram bot credential is not configured")
        clean_path = str(file_path).lstrip("/")
        if not clean_path or "/bot" in clean_path:
            raise AdapterError("Telegram file_path is malformed")
        url = f"{self._api_base_url}/file/bot{self._bot_token}/{clean_path}"
        response = self._request("GET", url)
        content = getattr(response, "content", None)
        if content is None:
            raise PermanentTransportError("file download returned no content")
        return bytes(content)

    def send_chat_action(
        self,
        *,
        chat_id: str,
        action: str = "typing",
        topic_id: str | None = None,
    ) -> HttpResponse:
        """Send a chat action (the typing indicator)."""
        payload: dict[str, Any] = {"chat_id": str(chat_id), "action": str(action)}
        if topic_id is not None:
            payload["message_thread_id"] = str(topic_id)
        return self._call_api("sendChatAction", payload)

    def edit_message(
        self,
        *,
        chat_id: str,
        message_id: str,
        text: str,
        topic_id: str | None = None,
        disable_web_page_preview: bool = True,
    ) -> HttpResponse:
        """Edit one message in place (Hermes streaming parity).

        Hardened for LIVE streaming use (gap D, Hermes audit 2026-08-29):
        - ``render_telegram_html_bounded`` renders the conservative
          markdown subset (bold/code/fences/links) instead of raw
          escaping, so a streaming preview shows formatted output rather
          than literal ``**`` markers, and bounds the SOURCE rather than
          slicing rendered HTML — a slice can cut a tag or entity in half
          and Telegram then rejects the frame with 400 "can't parse
          entities";
        - a Telegram 400 "message is not modified" is treated as
          SUCCESS — a repeated identical frame is a no-op, not an error
          (Hermes: "Message is not modified" — content identical);
        - Bot API RetryAfter (flood control) sleeps the demanded bound
          (capped) once and retries the edit;
        - ``disable_web_page_preview`` keeps progressive frames from
          flashing link previews.
        """
        payload: dict[str, Any] = {
            "chat_id": str(chat_id),
            "message_id": str(message_id),
            "text": render_telegram_html_bounded(text, TELEGRAM_MESSAGE_LIMIT),
            "parse_mode": "HTML",
        }
        if disable_web_page_preview:
            payload["link_preview_options"] = {"is_disabled": True}
        if topic_id is not None:
            payload["message_thread_id"] = str(topic_id)
        try:
            return self._call_api("editMessageText", payload)
        except PermanentTransportError as exc:
            message = str(exc)
            if "not modified" in message.lower():
                return _noop_http_response()
            retry_after = _parse_retry_after(message)
            if "retry after" in message.lower() and retry_after is not None:
                import time as _time

                wait = min(float(retry_after), 15.0)
                logger.warning(
                    "Telegram flood control on edit: waiting %.1fs (bounded)",
                    wait,
                )
                _time.sleep(wait)
                return self._call_api("editMessageText", payload)
            raise
        except TransportError as exc:
            # 429 "Too Many Requests: retry after N" arrives as a
            # (retryable) TransportError — the same flood tolerance
            # applies, bounded, once.
            message = str(exc)
            retry_after = _parse_retry_after(message)
            if "retry after" not in message.lower() or retry_after is None:
                raise
            import time as _time

            wait = min(float(retry_after), 15.0)
            logger.warning(
                "Telegram flood control on edit: waiting %.1fs (bounded)",
                wait,
            )
            _time.sleep(wait)
            return self._call_api("editMessageText", payload)
        except RuntimeError as exc:
            # ok=false responses surface as plain RuntimeError; the same
            # tolerances apply (identical frame / flood wait).
            message = str(exc)
            if "not modified" in message.lower():
                return _noop_http_response()
            if "retry after" in message.lower():
                retry_after = _parse_retry_after(message)
                wait = min(float(retry_after or 1.0), 15.0)
                logger.warning(
                    "Telegram flood control on edit: waiting %.1fs (bounded)",
                    wait,
                )
                import time as _time

                _time.sleep(wait)
                return self._call_api("editMessageText", payload)
            raise

    def answer_callback_query(
        self, callback_query_id: str, *, text: str | None = None
    ) -> HttpResponse:
        payload: dict[str, Any] = {"callback_query_id": str(callback_query_id)}
        if text:
            payload["text"] = safe_render_text(text, platform="telegram", limit=200)
        return self._call_api("answerCallbackQuery", payload)

    def poll_once(self, *, scope_key: str = "default", background_dispatch=None) -> list[Any]:
        """Fetch one bounded polling batch and persist the next offset.

        ``background_dispatch`` (Hermes parity, gap E) is an optional
        ``submit(fn) -> None`` sink. When provided, MESSAGE events are
        handed to it as zero-argument callables and NOT awaited — a long
        agent turn (LLM call) can no longer stall the polling loop or
        its heartbeat while it runs. Callback queries stay inline: they
        are fast, durable, and their answer toast must not be delayed
        behind a queued turn. The durable event claim still serializes
        duplicate deliveries, so at-least-once offsets remain safe.
        """
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
                if background_dispatch is not None and event.event_kind != "callback_query":
                    self._submit_background(
                        background_dispatch, event, numeric_update_id
                    )
                    results.append({"dispatched": "background", "update_id": numeric_update_id})
                    continue
                try:
                    result = self._dispatch(event)
                except Exception:
                    # A crashed dispatch must still stop the client's
                    # spinner (same invariant as the webhook path).
                    self._answer_callback_failure(event)
                    raise
                # Hermes parity: a button press that arrives via POLLING
                # gets the same outcome feedback as one arriving via
                # webhook — otherwise the client shows a loading clock
                # on the pressed button until Telegram times the query
                # out (~10s) with no feedback at all.
                self._answer_callback_outcome(event, result)
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

    def _submit_background(self, background_dispatch: Any, event: Any, update_id: int) -> None:
        """Hand one message event to the background dispatch sink.

        The submitted callable owns the FULL dispatch contract of the
        inline path: durable claim, processing, and crash-honest
        callback feedback. A rejected submission (saturated queue) is
        logged and DROPPED — the durable offset has already advanced, and
        the event's claim stays unclaimed for recovery replay, so nothing
        is silently lost.
        """

        def _run() -> None:
            try:
                self._dispatch(event)
            except Exception as exc:  # noqa: BLE001 - a turn crash must not kill the worker
                logger.warning(
                    "background dispatch of update %s failed: %s",
                    update_id,
                    type(exc).__name__,
                )

        # Per-chat serialization contract (gap E): the dispatch sink keys
        # its lanes by this attribute, preserving per-chat order while
        # different chats proceed in parallel.
        try:
            _run.chat_id = event.chat_id  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        try:
            background_dispatch(_run)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "background dispatch rejected update %s: %s: %s",
                update_id,
                type(exc).__name__,
                str(exc)[:200],
            )

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
