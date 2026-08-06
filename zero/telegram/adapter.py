"""Zero v2 Telegram adapter — Phase 4 T-4.2.

Bot API only (no Telethon/MTProto). Polling or webhook.
Reads ``is_forum``, ``message_thread_id``, ``is_topic_message``,
``forum_topic_created/edited/closed/reopened``,
``general_forum_topic_hidden/unhidden``.

``message_thread_id`` is mandatory in the internal event model (was missing
in v1: W-5/R-04).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from zero.core.scope import Scope
from zero.core.secret import SecretResolver, SecretValue
from zero.messaging import (
    Attachment,
    IncomingMessage,
    OutgoingMessage,
    Participant,
    Platform,
    PlatformConnection,
)

__all__ = [
    "TelegramAdapter",
    "TelegramBotConfig",
    "TelegramUpdate",
]


@dataclass(slots=True)
class TelegramBotConfig:
    """Configuration for the Telegram adapter."""

    bot_token_ref: str  # secret://env/TELEGRAM_BOT_TOKEN
    bot_username: str | None = None
    webhook_url: str | None = None
    webhook_secret_ref: str | None = None
    allowed_updates: list[str] = field(
        default_factory=lambda: ["message", "edited_message", "callback_query"]
    )
    drop_pending_updates: bool = False
    polling_timeout_seconds: int = 30


@dataclass(frozen=True, slots=True)
class TelegramUpdate:
    """Normalized Telegram update.

    ``message_thread_id`` is always set (0 for non-Forum groups).
    """

    chat_id: int
    chat_type: Literal["private", "group", "supergroup"]
    chat_title: str | None
    is_forum: bool
    message_id: int
    message_thread_id: int  # 0 for non-Forum or General topic
    is_topic_message: bool
    topic_created: bool
    topic_edited: bool
    topic_closed: bool
    topic_reopened: bool
    general_forum_topic_hidden: bool
    general_forum_topic_unhidden: bool
    from_user_id: int
    from_username: str | None
    from_first_name: str | None
    is_bot: bool
    text: str | None
    edit_date: int | None
    raw: dict[str, Any] = field(compare=False, hash=False)


class TelegramAdapter(PlatformConnection):
    """Async adapter for Telegram Bot API.

    Wraps httpx calls for the Bot API. The main bot loop uses aiogram 3.x
    (see :class:`zero.telegram.bot.TelegramBot`), but this adapter provides
    a lightweight alternative for testing and simple integrations.
    """

    platform = Platform.TELEGRAM

    def __init__(
        self,
        *,
        config: TelegramBotConfig,
        resolver: SecretResolver,
    ) -> None:
        self._config = config
        self._resolver = resolver
        self._token: str | None = None
        self._api_base = "https://api.telegram.org"

    async def _get_token(self) -> str:
        if self._token is None:
            secret: SecretValue = self._resolver.resolve(self._config.bot_token_ref)
            self._token = secret.reveal()
        return self._token

    async def send(self, msg: OutgoingMessage) -> str:
        """Send a message. Returns the Telegram message_id as string."""
        import httpx  # noqa: PLC0415

        token = await self._get_token()
        url = f"{self._api_base}/bot{token}/sendMessage"
        body: dict[str, Any] = {
            "chat_id": int(msg.external_chat_id),
            "text": msg.text,
            "parse_mode": msg.parse_mode.upper() if msg.parse_mode != "plain" else None,
            "disable_notification": msg.disable_notification,
        }
        if msg.topic_id is not None and msg.topic_id > 0:
            body["message_thread_id"] = msg.topic_id
        if msg.reply_to_message_id is not None:
            body["reply_to_message_id"] = int(msg.reply_to_message_id)
        body = {k: v for k, v in body.items() if v is not None}

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=body)
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                raise RuntimeError(f"Telegram API error: {data}")
            return str(data["result"]["message_id"])

    async def edit(self, external_message_id: str, new_text: str) -> None:

        token = await self._get_token()
        url = f"{self._api_base}/bot{token}/editMessageText"
        # This method requires chat_id which is not available from message_id alone.
        # Use send_edit(chat_id, message_id, text) instead.
        raise NotImplementedError("edit requires chat_id — use send_edit(chat_id, message_id, text)")

    async def delete(self, external_message_id: str) -> None:
        raise NotImplementedError("delete requires chat_id — use delete_message(chat_id, message_id)")

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        import httpx  # noqa: PLC0415

        token = await self._get_token()
        url = f"{self._api_base}/bot{token}/answerCallbackQuery"
        body: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            body["text"] = text
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=body)
            resp.raise_for_status()

    # ------------------------------------------------------------------ parsing

    @staticmethod
    def parse_update(raw: dict[str, Any]) -> TelegramUpdate | None:
        """Parse a raw Telegram update into a :class:`TelegramUpdate`.

        Returns None for non-message updates (callback_query alone, etc.)
        when the message is missing.
        """
        message = raw.get("message") or raw.get("edited_message")
        if not message:
            return None

        chat = message.get("chat", {})
        user = message.get("from", {})

        chat_type = chat.get("type", "private")
        # Normalize: "group" or "supergroup" — both are group chats.
        is_forum = bool(chat.get("is_forum", False))

        message_thread_id = int(message.get("message_thread_id", 0))
        is_topic_message = bool(message.get("is_topic_message", False))

        # Forum topic lifecycle events.
        topic_created = "forum_topic_created" in message
        topic_edited = "forum_topic_edited" in message
        topic_closed = "forum_topic_closed" in message
        topic_reopened = "forum_topic_reopened" in message
        general_hidden = "general_forum_topic_hidden" in message
        general_unhidden = "general_forum_topic_unhidden" in message

        return TelegramUpdate(
            chat_id=int(chat.get("id", 0)),
            chat_type=chat_type,
            chat_title=chat.get("title"),
            is_forum=is_forum,
            message_id=int(message.get("message_id", 0)),
            message_thread_id=message_thread_id,
            is_topic_message=is_topic_message,
            topic_created=topic_created,
            topic_edited=topic_edited,
            topic_closed=topic_closed,
            topic_reopened=topic_reopened,
            general_forum_topic_hidden=general_hidden,
            general_forum_topic_unhidden=general_unhidden,
            from_user_id=int(user.get("id", 0)),
            from_username=user.get("username"),
            from_first_name=user.get("first_name"),
            is_bot=bool(user.get("is_bot", False)),
            text=message.get("text"),
            edit_date=message.get("edit_date"),
            raw=message,
        )

    def to_incoming_message(
        self,
        update: TelegramUpdate,
        *,
        scope: Scope,
    ) -> IncomingMessage:
        """Convert a :class:`TelegramUpdate` to a platform-neutral :class:`IncomingMessage`.

        ``scope`` must already be resolved via
        :func:`zero.telegram.topic_binding.resolve_mode` — we don't re-derive it here.

        Handles photo, voice, document, video, sticker attachments.
        """
        sender = Participant(
            external_id=str(update.from_user_id),
            display_name=update.from_first_name or update.from_username or str(update.from_user_id),
            is_bot=update.is_bot,
            username=update.from_username,
        )
        text = update.text or ""
        attachments: list[Attachment] = []
        raw = update.raw

        # Photo (list of sizes; take largest).
        if "photo" in raw and raw["photo"]:
            sizes = raw["photo"]
            largest = max(sizes, key=lambda s: s.get("file_size", 0))
            attachments.append(Attachment(
                kind="image",
                external_id=str(largest.get("file_id", "")),
                mime_type="image/jpeg",
                size_bytes=largest.get("file_size"),
                file_id=largest.get("file_id"),
            ))
            if not text and "caption" in raw:
                text = raw["caption"] or ""

        # Voice (Opus OGG).
        if "voice" in raw and raw["voice"]:
            voice = raw["voice"]
            attachments.append(Attachment(
                kind="voice",
                external_id=str(voice.get("file_id", "")),
                mime_type=voice.get("mime_type", "audio/ogg"),
                size_bytes=voice.get("file_size"),
                file_id=voice.get("file_id"),
            ))

        # Document (arbitrary file).
        if "document" in raw and raw["document"]:
            doc = raw["document"]
            attachments.append(Attachment(
                kind="file",
                external_id=str(doc.get("file_id", "")),
                mime_type=doc.get("mime_type"),
                size_bytes=doc.get("file_size"),
                file_id=doc.get("file_id"),
            ))
            if not text and "caption" in raw:
                text = raw["caption"] or ""

        # Video.
        if "video" in raw and raw["video"]:
            video = raw["video"]
            attachments.append(Attachment(
                kind="video",
                external_id=str(video.get("file_id", "")),
                mime_type=video.get("mime_type", "video/mp4"),
                size_bytes=video.get("file_size"),
                file_id=video.get("file_id"),
            ))

        # Sticker.
        if "sticker" in raw and raw["sticker"]:
            sticker = raw["sticker"]
            attachments.append(Attachment(
                kind="sticker",
                external_id=str(sticker.get("file_id", "")),
                mime_type=sticker.get("mime_type", "image/webp"),
                file_id=sticker.get("file_id"),
            ))
            if not text and "emoji" in sticker:
                text = sticker["emoji"]

        return IncomingMessage(
            platform=Platform.TELEGRAM,
            external_chat_id=str(update.chat_id),
            external_message_id=str(update.message_id),
            topic_id=update.message_thread_id,
            sender=sender,
            text=text,
            scope=scope,
            attachments=attachments,
            is_edit=update.edit_date is not None,
            raw_metadata={"chat_type": update.chat_type, "is_forum": update.is_forum},
        )
