"""Conversational Telegram fallback (Hermes session parity, round 5).

The gap this closes, in one sentence: on the historical Telegram path a
message either became a plan proposal (invisible in chat until somebody
opened the web UI) or produced NOTHING — so everyday chat, follow-up
questions, and "what can you do?" made the bot look dead. Hermes replies
to every message through a per-chat session; this bridge brings that
behavior to Zero's Telegram surface without touching the plan pipeline:

- When the planner proposes a revision, the plan card + approval buttons
  go out (see ``InterfaceAdapterService._send_plan_card``) and the
  bridge is NOT used.
- When the planner classifies the message as NOT actionable, the bridge
  runs one bounded conversational turn (tools included, grants apply)
  and delivers the answer back to the SAME chat, threaded under the
  source message.

Session memory: per ``(platform, chat, topic)`` scope, durable
(``chat_messages`` table), bounded rolling window, sanitized before
every provider call. Restarting the engine no longer amputates the
conversation.

Media (real, not simulated):
- photos are downloaded through the bot API (getFile) and passed to the
  model as ``image_url`` data-URL parts — verified live against the
  operator's gateway (claude-opus-5 answered a 1-px-image color probe);
- text-like documents under the size cap are decoded and appended to
  the message;
- voice/video/sticker are honestly acknowledged (no STT provider is
  configured in this environment — claiming transcription would be a
  lie the model cannot support).

Typing indicator: a daemon thread refreshes ``sendChatAction`` while the
turn runs (Hermes ``_keep_typing`` parity), capped and always stopped.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import threading
from typing import Any

from zero.app.chat_history_repository import ChatHistoryRepository
from zero.app.chat_service import ChatService
from zero.app.telegram_live import TelegramLiveStream
from zero.app.interface_transport_service import (
    InterfaceTransportService,
    InterfaceTransportUnknownOutcome,
)
from zero.domain.interfaces import InterfaceBinding, MediaAttachment, NormalizedEvent
from zero.domain.identity import UserId

logger = logging.getLogger(__name__)

#: Largest photo we hand to the model as a base64 data URL (bytes).
_MAX_PHOTO_BYTES = 5 * 1024 * 1024
#: Largest text-like document decoded inline (bytes).
_MAX_DOCUMENT_BYTES = 200 * 1024
#: Characters of document text appended to the message.
_MAX_DOCUMENT_CHARS = 40_000
#: Rolling transcript window per scope.
_DEFAULT_HISTORY_TURNS = 12
#: Typing refresh cadence / total cap (seconds).
_TYPING_REFRESH_SECONDS = 5.0
_TYPING_MAX_SECONDS = 90.0

_TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".rst", ".py", ".js", ".ts", ".tsx", ".jsx",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".csv", ".tsv",
    ".sql", ".sh", ".bash", ".zsh", ".ps1", ".bat", ".html", ".css",
    ".xml", ".svg", ".log", ".c", ".cpp", ".h", ".hpp", ".java", ".go",
    ".rs", ".rb", ".php", ".lua", ".swift", ".kt", ".diff", ".patch",
}

_SUPPORTED_TEXT_MIME_PREFIXES = ("text/",)
_SUPPORTED_TEXT_MIMES = {
    "application/json", "application/xml", "application/javascript",
    "application/x-yaml", "application/toml", "application/sql",
}


def _is_text_like(attachment: MediaAttachment) -> bool:
    name = (attachment.file_name or "").lower()
    for ext in _TEXT_EXTENSIONS:
        if name.endswith(ext):
            return True
    mime = attachment.mime_type or (mimetypes.guess_type(name)[0] or "")
    return mime.startswith(_SUPPORTED_TEXT_MIME_PREFIXES) or mime in _SUPPORTED_TEXT_MIMES


class TelegramChatBridge:
    """One bounded conversational turn on the Telegram path."""

    def __init__(
        self,
        *,
        chat_service: ChatService,
        transport_service: InterfaceTransportService,
        history: ChatHistoryRepository,
        provider: str,
        model_name: str,
        history_turns: int = _DEFAULT_HISTORY_TURNS,
    ) -> None:
        self._chat_service = chat_service
        self._transport = transport_service
        self._history = history
        self._provider = provider
        self._model = model_name
        self._history_turns = max(2, int(history_turns))

    def update_model(self, model_name: str) -> None:
        """Align the conversational model with ``routing.primary_model``.

        ``config_sync`` calls this after resolving the operator's real
        primary model: the bridge must call exactly what the planner
        calls, or every chat turn dies with an auth failure on a
        single-model gateway (observed live: gpt-4o-mini default vs.
        claude-opus-5 gateway → 403 on every non-actionable message).
        """
        candidate = str(model_name or "").strip()
        if candidate:
            self._model = candidate

    def update_provider(self, provider: str) -> None:
        """Align the conversational PROVIDER with the routing primary.

        Live audit fix (2026-08-31): ``config_sync._sync_planner``
        realigned the planner's provider (``anthropic`` vs
        ``openai-compatible``) but only ever called ``update_model`` on
        this bridge. An Anthropic-primary deployment therefore kept a
        bridge pinned to ``openai-compatible`` — a provider name with no
        registered adapter — and every conversational turn failed with
        ``ProviderNotFoundError`` while the planner kept succeeding.
        The bridge must follow the same protocol resolution as the
        planner.
        """
        candidate = str(provider or "").strip()
        if candidate:
            self._provider = candidate

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def handle_message(
        self,
        *,
        binding: InterfaceBinding,
        event: NormalizedEvent,
        user_id: UserId,
    ) -> str:
        """Run one conversational turn. Never raises: failures are
        logged and surfaced as a returned detail string — an inbound
        event must not fail because its chat reply failed."""
        typing_stop = threading.Event()
        first_beat = threading.Event()
        typing_thread = threading.Thread(
            target=self._typing_loop,
            args=(binding, event, user_id, typing_stop, first_beat),
            daemon=True,
            name="zero-telegram-typing",
        )
        typing_thread.start()
        try:
            # Deterministic first heartbeat: the reply-ordering contract
            # (typing bubble visible before the turn runs) must not
            # depend on thread scheduling. Bounded wait — a dead
            # transport must never delay the answer itself.
            first_beat.wait(timeout=2.0)
            return self._run_turn(binding=binding, event=event, user_id=user_id)
        finally:
            typing_stop.set()

    # ------------------------------------------------------------------
    # Turn internals
    # ------------------------------------------------------------------

    def _run_turn(
        self,
        *,
        binding: InterfaceBinding,
        event: NormalizedEvent,
        user_id: UserId,
    ) -> str:
        message = (event.content or "").strip()
        image_urls: tuple[str, ...] = ()
        media_notes: list[str] = []

        photos = [m for m in event.media if m.kind == "photo"]
        for photo in photos[:1]:  # one image per turn keeps the payload sane
            data_url = self._photo_data_url(binding=binding, user_id=user_id, photo=photo)
            if data_url is not None:
                image_urls = (data_url,)
            else:
                media_notes.append("a photo arrived but could not be downloaded")

        for attachment in event.media:
            if attachment.kind == "document" and _is_text_like(attachment):
                extracted = self._document_text(binding=binding, user_id=user_id, attachment=attachment)
                if extracted is not None:
                    message = (
                        f"{message}\n\n[document: {attachment.file_name or 'unnamed'}]\n"
                        f"{extracted}"
                        if message
                        else f"[document: {attachment.file_name or 'unnamed'}]\n{extracted}"
                    )
            elif attachment.kind in ("voice", "video", "audio", "sticker"):
                media_notes.append(
                    f"a {attachment.kind} message arrived (not processed in this mode yet)"
                )

        if not message and not image_urls:
            if media_notes:
                reply = (
                    "I received your media: " + "; ".join(media_notes) + ". "
                    "I can read text, photos, and documents — but this media "
                    "type is not processed yet. Send text, a photo, or a text "
                    "document and I'll respond."
                )
                self._send(binding=binding, event=event, user_id=user_id, text=reply)
                return f"media-only ack sent ({'; '.join(media_notes)})"
            return "empty message; no reply"

        result: Any = None
        live_streamed = False
        # Live audit fix (2026-08-31): ``live`` was first assigned deep
        # inside the ``try`` — after ``self._history.recent(...)`` which
        # can raise. The ``except`` handler references ``live``, so a
        # history-read failure crashed the handler with ``NameError``
        # and the user received NO answer at all. Bind it before the
        # protected region, exactly like ``result``/``live_streamed``.
        live: TelegramLiveStream | None = None
        try:
            from zero.domain.providers import CanonicalMessage

            prior = self._history.recent(
                platform=binding.platform,
                chat_id=event.chat_id,
                topic_id=event.topic_id,
                limit=self._history_turns,
            )
            history_messages = tuple(
                CanonicalMessage(role=item["role"], content=item["content"])
                for item in prior
            )
            turn_kwargs = dict(
                project_id=binding.project_id,
                actor_id=user_id,
                message=message,
                provider=self._provider,
                model_name=self._model,
                source=binding.platform,
                history=history_messages,
                image_data_urls=image_urls,
            )
            # Hermes live-stream parity (gap A+B): the answer streams
            # INTO a Telegram message that is progressively edited as
            # text deltas and tool calls arrive, then converges to the
            # final content (overflow split included). A chat service
            # without a streaming surface (or a dead transport) degrades
            # to the historical single-shot path — the durable chunked
            # send below remains the delivery fallback either way.
            complete_stream = getattr(self._chat_service, "complete_stream", None)
            live = None
            if callable(complete_stream):
                try:
                    adapter = self._transport.build_telegram_adapter(
                        project_id=binding.project_id,
                        binding_id=binding.id,
                        actor_id=user_id,
                    )
                    live = TelegramLiveStream(
                        adapter=adapter,
                        chat_id=str(event.chat_id),
                        topic_id=str(event.topic_id) if event.topic_id else None,
                        header="✍️ Zero is thinking…",
                    )
                except Exception as exc:  # noqa: BLE001 - streaming is best-effort
                    logger.debug("live stream unavailable: %s", type(exc).__name__)
                    live = None
            if callable(complete_stream):
                result = complete_stream(
                    **turn_kwargs,
                    event_cb=(
                        None
                        if live is None
                        else lambda payload: self._route_stream_event(live, payload)
                    ),
                )
            else:
                result = self._chat_service.complete(**turn_kwargs)
            answer = (result.content or "").strip() or "(the model returned an empty answer)"
            if live is not None:
                live_streamed = bool(
                    live.finalize(
                        answer,
                        tool_names=[
                            call["tool_name"] for call in result.tool_calls_executed
                        ],
                    )
                )
        except Exception as exc:  # noqa: BLE001 - chat failure must not crash intake
            logger.warning(
                "conversational turn failed: %s: %s", type(exc).__name__, str(exc)[:200]
            )
            apology = (
                "I could not answer that just now "
                f"(provider error: {type(exc).__name__}). "
                "Try again in a moment, or rephrase the request."
            )
            if live is not None:
                # A stale "thinking…" bubble must never outlive a failed
                # turn: converge it to the honest failure text.
                try:
                    live.finalize(apology)
                except Exception:  # noqa: BLE001
                    pass
            self._send(
                binding=binding,
                event=event,
                user_id=user_id,
                text=apology,
            )
            return f"conversational fallback failed ({type(exc).__name__}); apology sent"

        try:
            self._history.append(
                project_id=binding.project_id.value,
                platform=binding.platform,
                chat_id=event.chat_id,
                topic_id=event.topic_id,
                role="user",
                content=message[:8_000],
                created_at=_now_iso(),
            )
            self._history.append(
                project_id=binding.project_id.value,
                platform=binding.platform,
                chat_id=event.chat_id,
                topic_id=event.topic_id,
                role="assistant",
                content=answer[:8_000],
                created_at=_now_iso(),
            )
        except Exception as exc:  # noqa: BLE001 - history loss is degraded, not fatal
            logger.warning("chat history append failed: %s", type(exc).__name__)

        suffix = ""
        if media_notes:
            suffix = "\n\n(Media received: " + "; ".join(media_notes) + ".)"
            # Media notes ride a SHORT follow-up when the answer itself
            # already streamed into the live bubble (no duplication).
            self._send(
                binding=binding,
                event=event,
                user_id=user_id,
                text="(Media received: " + "; ".join(media_notes) + ".)",
            )
        if not live_streamed:
            # The live bubble never opened: the durable chunked send is
            # the only delivery path (historical behavior, unchanged).
            self._send(
                binding=binding,
                event=event,
                user_id=user_id,
                text=answer + suffix,
            )
        tool_note = (
            f"; tools used: {len(result.tool_calls_executed)}"
            if result.tool_calls_executed
            else ""
        )
        stream_note = "; live-streamed" if live_streamed else ""
        return f"conversational reply sent{tool_note}{stream_note}"

    # ------------------------------------------------------------------
    # Stream routing
    # ------------------------------------------------------------------

    @staticmethod
    def _route_stream_event(live: Any, payload: Any) -> None:
        """Route one provider/chat stream event into the live bubble.

        Shaped exactly like the provider ``stream_observer`` payloads:
        ``text_delta`` (incremental text), ``tool_call`` (name + parsed
        arguments), ``message_end`` (ignored — finalize handles it).
        Unknown shapes are ignored; routing never raises.
        """
        if not isinstance(payload, dict) or live is None:
            return
        kind = payload.get("type")
        if kind == "text_delta":
            live.on_text_delta(str(payload.get("text") or ""))
        elif kind == "text_reset":
            live.on_text_reset()
        elif kind == "tool_call":
            live.on_tool_call(
                str(payload.get("name") or "tool"),
                payload.get("arguments"),
                replace=bool(payload.get("replace")),
            )
        elif kind == "tool_result":
            live.on_tool_result(
                str(payload.get("name") or "tool"), bool(payload.get("ok", True))
            )

    # ------------------------------------------------------------------
    # Media helpers
    # ------------------------------------------------------------------

    def _photo_data_url(
        self,
        *,
        binding: InterfaceBinding,
        user_id: UserId,
        photo: MediaAttachment,
    ) -> str | None:
        """Download one photo and return an ``image_url`` data URL."""
        try:
            adapter = self._transport.build_telegram_adapter(
                project_id=binding.project_id,
                binding_id=binding.id,
                actor_id=user_id,
            )
            info = adapter.get_file(file_id=photo.file_id)
            file_path = str(info.get("file_path") or "")
            if not file_path:
                return None
            blob = adapter.download_file_bytes(file_path=file_path)
            if len(blob) > _MAX_PHOTO_BYTES:
                logger.info("photo skipped for vision: %d bytes exceeds cap", len(blob))
                return None
            mime = photo.mime_type or "image/jpeg"
            return f"data:{mime};base64," + base64.b64encode(blob).decode("ascii")
        except Exception as exc:  # noqa: BLE001 - media is best-effort
            logger.info("photo download failed: %s", type(exc).__name__)
            return None

    def _document_text(
        self,
        *,
        binding: InterfaceBinding,
        user_id: UserId,
        attachment: MediaAttachment,
    ) -> str | None:
        """Decode a text-like document into a bounded inline block."""
        try:
            adapter = self._transport.build_telegram_adapter(
                project_id=binding.project_id,
                binding_id=binding.id,
                actor_id=user_id,
            )
            info = adapter.get_file(file_id=attachment.file_id)
            file_path = str(info.get("file_path") or "")
            if not file_path:
                return None
            blob = adapter.download_file_bytes(file_path=file_path)
            if len(blob) > _MAX_DOCUMENT_BYTES:
                return f"(document too large to read inline: {len(blob)} bytes)"
            text = blob.decode("utf-8", errors="replace")
            if len(text) > _MAX_DOCUMENT_CHARS:
                text = text[:_MAX_DOCUMENT_CHARS] + "\n...(truncated)"
            return text
        except Exception as exc:  # noqa: BLE001 - media is best-effort
            logger.info("document download failed: %s", type(exc).__name__)
            return None

    # ------------------------------------------------------------------
    # Delivery + typing
    # ------------------------------------------------------------------

    def _send(
        self,
        *,
        binding: InterfaceBinding,
        event: NormalizedEvent,
        user_id: UserId,
        text: str,
    ) -> None:
        try:
            self._transport.send_message(
                project_id=binding.project_id,
                binding_id=binding.id,
                actor_id=user_id,
                text=text,
                chat_id=str(event.chat_id),
                topic_id=str(event.topic_id) if event.topic_id else None,
                reply_to_message_id=event.message_id,
            )
        except InterfaceTransportUnknownOutcome as exc:
            # The message may or may not have arrived (ambiguous
            # transport failure). Never raise into the intake path.
            logger.warning("conversational reply outcome unknown: %s", str(exc)[:160])
        except Exception as exc:  # noqa: BLE001
            logger.warning("conversational reply failed: %s", type(exc).__name__)

    def _typing_loop(
        self,
        binding: InterfaceBinding,
        event: NormalizedEvent,
        user_id: UserId,
        stop: threading.Event,
        first_beat: threading.Event,
    ) -> None:
        """Refresh the typing indicator while the turn runs."""
        deadline = _monotonic() + _TYPING_MAX_SECONDS
        while not stop.is_set() and _monotonic() < deadline:
            self._transport.send_typing(
                project_id=binding.project_id,
                binding_id=binding.id,
                actor_id=user_id,
                chat_id=str(event.chat_id),
                topic_id=str(event.topic_id) if event.topic_id else None,
            )
            first_beat.set()
            stop.wait(_TYPING_REFRESH_SECONDS)


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _monotonic() -> float:
    import time

    return time.monotonic()


__all__ = ["TelegramChatBridge"]
