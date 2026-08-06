"""Zero v2 messaging — Phase 4 T-4.1.

Platform-neutral ``PlatformConnection`` abstraction. Telegram adapter lives
in ``zero.telegram``.

Per ADR T-4.1 acceptance:
    - Workspace, Channel, Conversation, Participant, Message, Attachment
    - Platform-neutral from day one
    - Dedup key = (platform, external_chat_id, external_message_id)
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum, StrEnum
from typing import Any, Literal

from zero.core.scope import Scope

__all__ = [
    "Attachment",
    "IncomingMessage",
    "MessageRole",
    "OutgoingMessage",
    "Participant",
    "Platform",
    "PlatformConnection",
]


class Platform(StrEnum):
    TELEGRAM = "telegram"
    DISCORD = "discord"  # future
    SLACK = "slack"      # future
    WEB = "web"          # dashboard


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


@dataclass(frozen=True, slots=True)
class Attachment:
    """A message attachment (image, file, voice, etc.)."""

    kind: Literal["image", "file", "voice", "video", "sticker"]
    external_id: str
    mime_type: str | None = None
    size_bytes: int | None = None
    # URL or local path (resolve lazily; respect SSRF rules before fetch)
    url: str | None = None
    file_id: str | None = None  # platform-specific (e.g. Telegram file_id)


@dataclass(frozen=True, slots=True)
class Participant:
    """A participant in a conversation."""

    external_id: str  # platform user id
    display_name: str
    is_bot: bool = False
    username: str | None = None


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    """An incoming message from any platform.

    Carries the resolved Scope (computed by telegram/topic_binding.py).
    """

    platform: Platform
    external_chat_id: str  # Telegram: chat_id; Discord: channel_id; etc.
    external_message_id: str  # Telegram: message_id; etc.
    topic_id: int | None  # Telegram: message_thread_id; None for non-Forum
    sender: Participant
    text: str
    scope: Scope
    attachments: list[Attachment] = field(default_factory=list)
    is_edit: bool = False
    raw_metadata: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def dedup_key(self) -> str:
        """Per ADR T-4.1: dedup on (platform, external_chat_id, external_message_id)."""
        return f"{self.platform.value}:{self.external_chat_id}:{self.external_message_id}"


@dataclass(frozen=True, slots=True)
class OutgoingMessage:
    """An outgoing message to a platform."""

    platform: Platform
    external_chat_id: str
    text: str
    topic_id: int | None = None
    reply_to_message_id: str | None = None
    attachments: list[Attachment] = field(default_factory=list)
    parse_mode: Literal["markdown", "html", "plain"] = "plain"
    disable_notification: bool = False


class PlatformConnection:
    """Abstract base class for platform connections (Telegram, Discord, etc.)."""

    platform: Platform

    async def send(self, msg: OutgoingMessage) -> str:
        """Send a message. Returns the platform's message id."""
        raise NotImplementedError

    async def edit(self, external_message_id: str, new_text: str) -> None:
        raise NotImplementedError

    async def delete(self, external_message_id: str) -> None:
        raise NotImplementedError

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        """Answer a callback query (Telegram-specific, but generic interface)."""
        raise NotImplementedError
