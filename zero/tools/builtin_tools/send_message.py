"""SendMessageTool — send a message to a Telegram chat (cross-chat messaging).

Per ADR T-7.3: this tool is BLOCKED for sub-agents (only the top-level
agent can send messages to other chats). This is enforced by the
Orchestrator's ``DELEGATE_BLOCKED_TOOLS`` set.

The actual send is performed via a callback injected by the runner
(:func:`set_send_message_callback`). This decoupling lets the tool work
in production (sends via ``TelegramBot.bot.send_message``) and in tests
(mock callback that records the call).
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from zero.tools.base import Tool, ToolContext, ToolSpec

__all__ = [
    "SendMessageTool",
    "SendMessageCallback",
    "SEND_MESSAGE_SCHEMA",
    "set_send_message_callback",
    "register",
]


SEND_MESSAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "chat_id": {"type": "string", "description": "Target chat ID"},
        "text": {"type": "string", "description": "Message text"},
        "topic_id": {"type": "integer", "description": "Forum topic ID (optional)"},
        "parse_mode": {
            "type": "string",
            "enum": ["html", "markdown", "plain"],
            "description": "Parse mode (default: plain)",
        },
    },
    "required": ["chat_id", "text"],
}

# Global send_message callback (injected by runner — calls TelegramBot.send_message).
SendMessageCallback = Callable[[str, str, int | None, str], Awaitable[bool]]
_send_message_callback: SendMessageCallback | None = None


def set_send_message_callback(cb: SendMessageCallback | None) -> None:
    """Inject the send_message callback (called by runner).

    The callback receives (chat_id, text, topic_id, parse_mode) and returns
    True on success, False on failure.
    """
    global _send_message_callback
    _send_message_callback = cb


class SendMessageTool(Tool):
    """Send a message to a Telegram chat (cross-chat messaging)."""

    spec = ToolSpec(
        name="send_message",
        description="Send a message to a Telegram chat (cross-chat messaging).",
        parameters_schema=SEND_MESSAGE_SCHEMA,
        required_permissions=frozenset({"message.send"}),
        approval_level="standard",
    )

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> str:
        if _send_message_callback is None:
            return "[TOOL_ERROR] send_message callback not configured"
        chat_id = str(args["chat_id"])
        text = str(args["text"])
        topic_id = args.get("topic_id")
        if topic_id is not None:
            topic_id = int(topic_id)
        parse_mode = str(args.get("parse_mode", "plain"))

        try:
            success = await _send_message_callback(chat_id, text, topic_id, parse_mode)
        except Exception as e:
            return f"[TOOL_ERROR] send_message failed: {e}"
        return "✅ sent" if success else "❌ failed to send"


def register() -> None:
    """Register the SendMessageTool with the global tool registry."""
    from zero.tools.builtin_tools._helpers import register_tool  # noqa: PLC0415

    register_tool(SendMessageTool())
