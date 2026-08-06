"""Tests for the Telegram bot — Phase 4 T-4.21 acceptance.

Uses aiogram's testing utilities + FakeBot pattern.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType
from aiogram.types import (
    Chat,
    Message,
    Update,
    User,
)

from zero.core.scope import Mode
from zero.messaging import IncomingMessage, OutgoingMessage, Platform
from zero.security.approval import ApprovalResolver, ApprovalStore
from zero.telegram.bot import TelegramBot, TelegramBotConfig
from zero.telegram.commands import CommandRegistry
from zero.telegram.topic_binding import (
    GroupPolicyStore,
    TopicBinding,
    TopicBindingStore,
)


# ---------------------------------------------------------------------- fixtures

class FakeBot:
    """Minimal aiogram Bot stand-in for tests.

    Stores all sent messages for assertion.
    """

    def __init__(self, token: str = "1234567890:TESTTokenForPytestOnlyXXXXXXXXXXXXXXXXXXX") -> None:
        self.token = token
        self.id = 1234567890  # bot user id, used by aiogram Dispatcher for logging
        self.sent_messages: list[dict[str, Any]] = []
        self._chat_members: dict[tuple[int, int], str] = {}  # (chat_id, user_id) -> status
        self._me = User(id=1234567890, is_bot=True, first_name="ZeroTestBot", username="zero_test_bot")

    async def send_message(self, **kwargs: Any) -> Message:
        self.sent_messages.append({"method": "send_message", **kwargs})
        return Message(
            message_id=len(self.sent_messages),
            date=0,
            chat=Chat(id=int(kwargs.get("chat_id", 0)), type="private"),
            text=kwargs.get("text", ""),
        )

    async def send_voice(self, **kwargs: Any) -> Message:
        self.sent_messages.append({"method": "send_voice", **kwargs})
        return Message(
            message_id=len(self.sent_messages),
            date=0,
            chat=Chat(id=int(kwargs.get("chat_id", 0)), type="private"),
        )

    async def get_me(self) -> User:
        return self._me

    async def get_chat(self, chat_id: int) -> Chat:
        return Chat(id=chat_id, type="private")

    async def get_chat_member(self, chat_id: int, user_id: int) -> Any:
        status = self._chat_members.get((chat_id, user_id), "member")
        # Return a simple object with .status attribute.
        class FakeMember:
            def __init__(self, status: str) -> None:
                self.status = status
        return FakeMember(status)

    async def delete_webhook(self, **kwargs: Any) -> Any:
        return True

    class _Session:
        async def close(self) -> None:
            pass

    @property
    def session(self) -> Any:
        return self._Session()


@pytest.fixture
def fake_bot() -> FakeBot:
    return FakeBot()


@pytest.fixture
def binding_store() -> TopicBindingStore:
    return TopicBindingStore()


@pytest.fixture
def policy_store() -> GroupPolicyStore:
    return GroupPolicyStore()


@pytest.fixture
def command_registry() -> CommandRegistry:
    return CommandRegistry()


@pytest.fixture
def captured_messages() -> list[str]:
    """List of response texts from the message handler."""
    return []


@pytest.fixture
def message_handler(captured_messages: list[str]):
    """Default message handler — echoes the text back uppercase."""
    async def handler(msg: IncomingMessage, mode_result: Any) -> str | None:
        captured_messages.append(msg.text)
        return f"Echo: {msg.text}"
    return handler


@pytest.fixture
def resolver(monkeypatch: pytest.MonkeyPatch):
    from zero.core.secret import CompositeSecretResolver

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1234567890:TESTTokenForPytestOnlyXXXXXXXXXXXXXXXXXXX")
    return CompositeSecretResolver()


@pytest.fixture
async def bot(
    fake_bot: FakeBot,
    binding_store: TopicBindingStore,
    policy_store: GroupPolicyStore,
    command_registry: CommandRegistry,
    message_handler,
    resolver,
) -> TelegramBot:
    """Construct a TelegramBot with a patched Bot instance."""
    cfg = TelegramBotConfig(
        bot_token_ref="secret://env/TELEGRAM_BOT_TOKEN",
    )
    # Construct without starting polling.
    bot = TelegramBot(
        config=cfg,
        resolver=resolver,
        binding_store=binding_store,
        policy_store=policy_store,
        command_registry=command_registry,
        message_handler=message_handler,
    )
    # Replace the real Bot with our fake.
    bot._bot = fake_bot  # type: ignore[assignment]
    return bot


# ---------------------------------------------------------------------- helpers

def _make_message(
    *,
    text: str = "hello",
    chat_id: int = 1001,
    chat_type: str = "private",
    user_id: int = 42,
    username: str = "alice",
    message_thread_id: int | None = None,
    is_forum: bool = False,
    message_id: int = 1,
    is_bot: bool = False,
    first_name: str = "Alice",
    forum_topic_created: Any = None,
) -> Message:
    """Build a minimal aiogram Message for testing."""
    chat = Chat(
        id=chat_id,
        type=chat_type,  # type: ignore[arg-type]
        is_forum=is_forum if chat_type != "private" else None,
    )
    user = User(
        id=user_id,
        is_bot=is_bot,
        first_name=first_name,
        username=username,
    )
    msg = Message(
        message_id=message_id,
        date=0,
        chat=chat,
        from_user=user,
        text=text,
        message_thread_id=message_thread_id,
    )
    if forum_topic_created is not None:
        # Use model_copy to add forum_topic_created (Message is frozen).
        msg = msg.model_copy(update={"forum_topic_created": forum_topic_created})
    return msg


def _make_update(*, message: Message) -> Update:
    return Update(update_id=1, message=message)


# ---------------------------------------------------------------------- tests

class TestBotConstruction:
    def test_bot_constructs(
        self, bot: TelegramBot, fake_bot: FakeBot
    ) -> None:
        assert bot.is_running is False
        assert bot.bot is fake_bot


class TestMessageHandling:
    @pytest.mark.asyncio
    async def test_private_message_routes_to_personal(
        self,
        bot: TelegramBot,
        fake_bot: FakeBot,
        captured_messages: list[str],
    ) -> None:
        """Private chat → PERSONAL mode, handler is called."""
        msg = _make_message(text="hello", chat_type="private")
        update = _make_update(message=msg)

        # Feed the update to the dispatcher.
        await bot.feed_update(update)

        # Handler should have been called.
        assert len(captured_messages) == 1
        assert captured_messages[0] == "hello"
        # Bot should have sent a reply.
        assert len(fake_bot.sent_messages) == 1
        assert "Echo: hello" in fake_bot.sent_messages[0]["text"]

    @pytest.mark.asyncio
    async def test_silenced_mode_no_response(
        self,
        bot: TelegramBot,
        fake_bot: FakeBot,
        binding_store: TopicBindingStore,
        captured_messages: list[str],
    ) -> None:
        """mode=disabled → complete silence, no handler call."""
        # Use the same hash function the bot uses to derive group_id.
        from zero.telegram.bot import _hash_chat_id

        chat_id = -1001234567890
        group_id = f"grp_{_hash_chat_id(chat_id)}"
        binding_store.upsert(TopicBinding(
            group_id=group_id,
            topic_id=100,
            mode="disabled",
            memory_scope_id=f"mem:grp:{group_id}:100",
            configured_by="usr_test",
        ))

        msg = _make_message(
            text="hello",
            chat_id=chat_id,
            chat_type="supergroup",
            is_forum=True,
            message_thread_id=100,
        )
        update = _make_update(message=msg)

        await bot.feed_update(update)

        # Handler NOT called.
        assert len(captured_messages) == 0
        # No reply sent.
        assert len(fake_bot.sent_messages) == 0

    @pytest.mark.asyncio
    async def test_group_message_uses_policy_default(
        self,
        bot: TelegramBot,
        fake_bot: FakeBot,
        captured_messages: list[str],
    ) -> None:
        """Group with no binding → GroupPolicy.default_unconfigured_topic_mode=normal."""
        msg = _make_message(
            text="hello group",
            chat_id=-1001234567890,
            chat_type="supergroup",
            is_forum=True,
            message_thread_id=200,
        )
        update = _make_update(message=msg)

        await bot.feed_update(update)

        # Handler should have been called (normal mode → not silenced).
        assert len(captured_messages) == 1
        assert captured_messages[0] == "hello group"

    @pytest.mark.asyncio
    async def test_bot_ignores_own_messages(
        self,
        bot: TelegramBot,
        fake_bot: FakeBot,
        captured_messages: list[str],
    ) -> None:
        """Bot must not respond to its own messages (prevents infinite loop)."""
        msg = _make_message(
            text="bot's own message",
            chat_type="private",
            is_bot=True,
            first_name="ZeroBot",
            user_id=999,
        )
        update = _make_update(message=msg)

        await bot.feed_update(update)

        assert len(captured_messages) == 0
        assert len(fake_bot.sent_messages) == 0


class TestCommands:
    @pytest.mark.asyncio
    async def test_help_command(
        self,
        bot: TelegramBot,
        fake_bot: FakeBot,
    ) -> None:
        msg = _make_message(text="/start", chat_type="private")
        update = _make_update(message=msg)

        await bot.feed_update(update)

        assert len(fake_bot.sent_messages) == 1
        assert "Zero v2" in fake_bot.sent_messages[0]["text"]

    @pytest.mark.asyncio
    async def test_status_command_personal(
        self,
        bot: TelegramBot,
        fake_bot: FakeBot,
    ) -> None:
        msg = _make_message(text="/status", chat_type="private")
        update = _make_update(message=msg)

        await bot.feed_update(update)

        assert len(fake_bot.sent_messages) == 1
        text = fake_bot.sent_messages[0]["text"]
        assert "personal" in text.lower()
        assert "Mode:" in text

    @pytest.mark.asyncio
    async def test_status_command_group_with_binding(
        self,
        bot: TelegramBot,
        fake_bot: FakeBot,
        binding_store: TopicBindingStore,
    ) -> None:
        group_id = "grp_" + "b" * 26
        binding_store.upsert(TopicBinding(
            group_id=group_id,
            topic_id=100,
            mode="normal",
            memory_scope_id=f"mem:grp:{group_id}:100",
            configured_by="usr_test",
        ))

        msg = _make_message(
            text="/status",
            chat_id=-1001234567890,
            chat_type="supergroup",
            is_forum=True,
            message_thread_id=100,
        )
        update = _make_update(message=msg)

        await bot.feed_update(update)

        assert len(fake_bot.sent_messages) == 1
        text = fake_bot.sent_messages[0]["text"]
        assert "normal" in text.lower()
        assert "TopicBinding" in text


class TestTopicLifecycle:
    @pytest.mark.asyncio
    async def test_topic_created_does_not_crash(
        self,
        bot: TelegramBot,
        fake_bot: FakeBot,
    ) -> None:
        """Forum topic creation event should be handled without error."""
        from aiogram.types import ForumTopicCreated

        topic = ForumTopicCreated(name="New Topic", icon_color=0x6FB9F0, icon_custom_emoji_id=None)
        msg = _make_message(
            text="",
            chat_id=-1001234567890,
            chat_type="supergroup",
            is_forum=True,
            message_thread_id=100,
            forum_topic_created=topic,
        )
        update = _make_update(message=msg)

        # Should not raise.
        await bot.feed_update(update)

        # No reply should be sent (lifecycle events are silent).
        assert len(fake_bot.sent_messages) == 0
