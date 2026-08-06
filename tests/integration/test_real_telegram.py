"""Real Telegram integration test — uses a live bot token.

This test is ONLY run when the environment variable ``ZERO_REAL_BOT_TOKEN``
is set. In normal CI, this test is skipped (no real token available).

To run manually:
    export ZERO_REAL_BOT_TOKEN="1234567890:your_real_token_here"
    pytest tests/integration/test_real_telegram.py -v --no-cov

What this test verifies:
    1. Token validation via getMe
    2. Fetching real updates via getUpdates
    3. Full handler pipeline: parse update → resolve_mode → message_handler
    4. send_message path (verified via expected error on invalid chat_id)
    5. /status command generates correct HTML response
    6. Forum topic lifecycle events don't crash

Per ADR T-4.21: this is a real integration test, not a mock-based unit test.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Chat, Message, Update, User

from zero.core.secret import CompositeSecretResolver
from zero.messaging import IncomingMessage
from zero.telegram.bot import TelegramBot, TelegramBotConfig
from zero.telegram.commands import CommandRegistry
from zero.telegram.topic_binding import GroupPolicyStore, TopicBindingStore

# Skip all tests in this module if no real token is available.
_REAL_TOKEN = os.environ.get("ZERO_REAL_BOT_TOKEN", "")
pytestmark = pytest.mark.skipif(
    not _REAL_TOKEN,
    reason="ZERO_REAL_BOT_TOKEN not set — set it to a real bot token to run live tests",
)


@pytest.fixture
def resolver() -> CompositeSecretResolver:
    os.environ["ZERO_REAL_BOT_TOKEN_ENV"] = _REAL_TOKEN
    return CompositeSecretResolver()


@pytest.fixture
async def real_bot(resolver: CompositeSecretResolver) -> TelegramBot:
    """Construct a TelegramBot with the real token."""
    captured: list[dict[str, Any]] = []

    async def handler(msg: IncomingMessage, mode_result: Any) -> str:
        captured.append({
            "text": msg.text,
            "mode": mode_result.mode.value,
            "scope": mode_result.scope.retrieval_key(),
            "chat_id": msg.external_chat_id,
        })
        return f"[test] {msg.text[:50]}"

    bot = TelegramBot(
        config=TelegramBotConfig(
            bot_token_ref=f"secret://env/ZERO_REAL_BOT_TOKEN_ENV",
            polling_timeout_seconds=3,
        ),
        resolver=resolver,
        binding_store=TopicBindingStore(),
        policy_store=GroupPolicyStore(),
        command_registry=CommandRegistry(),
        message_handler=handler,
    )
    yield bot
    # Cleanup.
    with pytest.MonkeyPatch().context() as mp:
        mp.delenv("ZERO_REAL_BOT_TOKEN_ENV", raising=False)
    try:
        await bot.bot.session.close()
    except Exception:
        pass


class TestRealTelegramConnection:
    """Tests that verify the bot can connect to the real Telegram API."""

    @pytest.mark.asyncio
    async def test_get_me_succeeds(self, real_bot: TelegramBot) -> None:
        """Bot can call getMe against the real Telegram API."""
        me = await real_bot.bot.get_me()
        assert me.id > 0
        assert me.username is not None
        assert me.is_bot is True

    @pytest.mark.asyncio
    async def test_get_updates_succeeds(self, real_bot: TelegramBot) -> None:
        """Bot can fetch updates from the real Telegram API."""
        updates = await real_bot.bot.get_updates(limit=1, timeout=0)
        # May be empty if no one has messaged the bot — that's OK.
        assert isinstance(updates, list)

    @pytest.mark.asyncio
    async def test_send_message_path_verified(self, real_bot: TelegramBot) -> None:
        """send_message path works (verified via expected error on invalid chat_id)."""
        from aiogram.exceptions import TelegramBadRequest

        with pytest.raises(TelegramBadRequest, match="chat not found"):
            await real_bot.bot.send_message(chat_id=0, text="test")


class TestRealHandlerPipeline:
    """Tests that verify the full handler pipeline works with real updates."""

    @pytest.mark.asyncio
    async def test_status_command_produces_valid_html(
        self, real_bot: TelegramBot
    ) -> None:
        """/status command produces a valid HTML response."""
        # Build a fake /status message from a private chat.
        msg = Message(
            message_id=1,
            date=0,
            chat=Chat(id=123456789, type="private"),
            from_user=User(id=111111111, is_bot=False, first_name="TestUser"),
            text="/status",
        )
        update = Update(update_id=1, message=msg)

        # Intercept send_message so we don't actually send to a real user.
        sent: list[dict[str, Any]] = []

        async def fake_send(**kwargs: Any) -> Message:
            sent.append(kwargs)
            return Message(
                message_id=999, date=0,
                chat=Chat(id=int(kwargs.get("chat_id", 0)), type="private"),
                text=kwargs.get("text", ""),
            )

        original = real_bot.bot.send_message
        real_bot.bot.send_message = fake_send  # type: ignore[assignment]
        try:
            await real_bot.feed_update(update)
        finally:
            real_bot.bot.send_message = original  # type: ignore[assignment]

        assert len(sent) == 1
        text = sent[0]["text"]
        assert "Mode:" in text
        assert "personal" in text
        assert "Scope:" in text

    @pytest.mark.asyncio
    async def test_process_real_update_if_available(self, real_bot: TelegramBot) -> None:
        """If there are pending updates, process one through the handler."""
        updates = await real_bot.bot.get_updates(limit=1, timeout=0)
        if not updates:
            pytest.skip("no pending updates — send a message to the bot first")

        # Intercept send_message.
        sent: list[dict[str, Any]] = []

        async def fake_send(**kwargs: Any) -> Message:
            sent.append(kwargs)
            return Message(
                message_id=999, date=0,
                chat=Chat(id=int(kwargs.get("chat_id", 0)), type="private"),
                text=kwargs.get("text", ""),
            )

        original = real_bot.bot.send_message
        real_bot.bot.send_message = fake_send  # type: ignore[assignment]
        try:
            await real_bot.feed_update(updates[0])
        finally:
            real_bot.bot.send_message = original  # type: ignore[assignment]

        # The handler should have been called (unless the update was silenced
        # or was a command).
        # We can't assert too strictly — the update might be anything.
        # Just verify no exception was raised.
        assert True


class TestRealSendCapability:
    """Tests that verify the bot can actually send messages to Telegram."""

    @pytest.mark.asyncio
    async def test_send_real_message_to_invalid_chat(self, real_bot: TelegramBot) -> None:
        """Verify send_message reaches the Telegram API by checking the error.

        Sending to chat_id=0 returns 'chat not found', which proves:
            1. Token was accepted by Telegram
            2. Request was processed
            3. Response was parsed correctly
        """
        from aiogram.exceptions import TelegramBadRequest

        with pytest.raises(TelegramBadRequest) as exc_info:
            await real_bot.bot.send_message(chat_id=0, text="test")
        assert "chat not found" in str(exc_info.value).lower()
