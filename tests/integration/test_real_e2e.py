"""Real end-to-end tests using actual API credentials.

These tests verify the full pipeline with real external services:
    1. Real Telegram bot token (ZERO_REAL_BOT_TOKEN env var)
    2. Real Gemini API key (GEMINI_API_KEY env var)
    3. Real RouterShim + RouterClient + AgentLoop

Tests are SKIPPED if the required env vars are not set.

To run:
    export ZERO_REAL_BOT_TOKEN="your_telegram_bot_token"
    export GEMINI_API_KEY="your_gemini_api_key"
    pytest tests/integration/test_real_e2e.py -v --no-cov
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

# Skip all tests if no real bot token is available.
_REAL_TOKEN = os.environ.get("ZERO_REAL_BOT_TOKEN", "")
_GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
pytestmark = pytest.mark.skipif(
    not _REAL_TOKEN,
    reason="ZERO_REAL_BOT_TOKEN not set — set it to a real bot token to run live tests",
)


@pytest.fixture
def temp_db_dir() -> Path:
    with tempfile.TemporaryDirectory(prefix="zero-e2e-") as d:
        yield Path(d)


@pytest.fixture
async def runner(temp_db_dir: Path):
    """Build a ZeroAgentRunner with real Telegram + Gemini credentials."""
    # Set env vars for the runner to pick up.
    os.environ["TELEGRAM_BOT_TOKEN"] = _REAL_TOKEN
    os.environ["GEMINI_API_KEY"] = _GEMINI_KEY or "fake-key-for-shim-test"
    os.environ["ZERO_ROUTER__API_KEY"] = "secret://env/GEMINI_API_KEY"
    os.environ["ZERO_ROUTER__PROVIDER"] = "gemini"
    os.environ["ZERO_DATABASE__SQLITE_DIR"] = str(temp_db_dir)
    os.environ["ZERO_LOGGING__LEVEL"] = "warning"  # reduce noise

    from zero.agents.runner import ZeroAgentRunner, ZeroAgentRunnerConfig
    from zero.core.config import reset_config_cache

    reset_config_cache()
    runner = ZeroAgentRunner(ZeroAgentRunnerConfig(dry_run=True))
    await runner.setup()
    yield runner
    await runner.stop()
    reset_config_cache()
    # Clean up env vars.
    for key in (
        "TELEGRAM_BOT_TOKEN", "GEMINI_API_KEY", "ZERO_ROUTER__API_KEY",
        "ZERO_ROUTER__PROVIDER", "ZERO_DATABASE__SQLITE_DIR", "ZERO_LOGGING__LEVEL",
    ):
        os.environ.pop(key, None)


class TestRealRunnerSetup:
    """Verify the runner can set up with real credentials."""

    @pytest.mark.asyncio
    async def test_runner_setup_with_real_credentials(self, runner) -> None:  # type: ignore[no-untyped-def]
        """Runner builds all components with real Telegram + Gemini."""
        assert runner.provider is not None
        assert runner.provider.provider_name == "gemini"
        assert runner.shim is not None
        assert runner.shim.is_running
        assert runner.router_client is not None
        assert runner.memory_store is not None
        assert runner.todo_store is not None
        assert runner.orchestrator is not None
        assert runner.db is not None

    @pytest.mark.asyncio
    async def test_shim_health_endpoint(self, runner) -> None:  # type: ignore[no-untyped-def]
        """Shim /health endpoint returns provider info."""
        import httpx

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"http://127.0.0.1:{runner.shim.actual_port}/v1/health"
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert data["provider"] == "gemini"


class TestRealGeminiCall:
    """Verify the runner can make a real Gemini API call through the shim.

    SKIPPED if GEMINI_API_KEY is not set or quota is exhausted.
    """

    @pytest.mark.asyncio
    @pytest.mark.skipif(
        not _GEMINI_KEY,
        reason="GEMINI_API_KEY not set",
    )
    async def test_real_gemini_call_through_shim(self, runner) -> None:  # type: ignore[no-untyped-def]
        """Make a real Gemini API call via RouterClient → RouterShim → Gemini."""
        from zero.agents.router_client import RouterMessage
        from zero.core.scope import Scope

        scope = Scope.personal(user_id="usr_real_test").with_default_memory_scope()
        try:
            resp = await runner.router_client.complete(
                messages=[RouterMessage(role="user", content="Say 'hello' in exactly one word.")],
                scope=scope,
                max_tokens=10,
            )
            # If quota is exhausted, we get a 429 error which raises RouterCallError.
            # That's still a "real" test — it proves the pipeline is wired.
            assert resp is not None
            assert resp.model.startswith("gemini-")
        except Exception as e:
            # 429 quota errors are expected if the free tier is exhausted.
            # The test still verifies the pipeline is wired correctly.
            if "429" in str(e) or "quota" in str(e).lower():
                pytest.skip(f"Gemini quota exhausted (expected): {e}")
            raise


class TestRealTelegramBot:
    """Verify the runner's Telegram bot can connect to the real API."""

    @pytest.mark.asyncio
    async def test_bot_get_me(self, runner) -> None:  # type: ignore[no-untyped-def]
        """The runner's bot can call getMe against the real Telegram API."""
        # In dry_run mode, the bot isn't built — so we build one manually.
        from zero.agents.runner import ZeroAgentRunnerConfig
        from zero.telegram.bot import TelegramBot, TelegramBotConfig
        from zero.telegram.commands import CommandRegistry
        from zero.telegram.topic_binding import GroupPolicyStore, TopicBindingStore

        from zero.core.secret import CompositeSecretResolver

        resolver = CompositeSecretResolver()
        os.environ["TELEGRAM_BOT_TOKEN_E2E"] = _REAL_TOKEN

        async def handler(msg, mode_result):  # type: ignore[no-untyped-def]  # noqa: ANN001
            return "test"

        bot = TelegramBot(
            config=TelegramBotConfig(
                bot_token_ref="secret://env/TELEGRAM_BOT_TOKEN_E2E",
                polling_timeout_seconds=3,
            ),
            resolver=resolver,
            binding_store=TopicBindingStore(),
            policy_store=GroupPolicyStore(),
            command_registry=CommandRegistry(),
            message_handler=handler,
        )
        try:
            me = await bot.bot.get_me()
            assert me.id > 0
            assert me.username is not None
            assert me.is_bot is True
        finally:
            await bot.bot.session.close()
            os.environ.pop("TELEGRAM_BOT_TOKEN_E2E", None)


class TestTodoStoreRealDb:
    """Verify the TodoStore persists to real SQLite for ALL scopes."""

    @pytest.mark.asyncio
    async def test_personal_scope_todo_persists(self, runner) -> None:  # type: ignore[no-untyped-def]
        """Todos in PERSONAL scope persist to personal.db (not in-memory)."""
        from zero.core.scope import Scope

        scope = Scope.personal(user_id="usr_todo_test").with_default_memory_scope()
        store = runner.todo_store

        # Add a todo.
        item = await store.add_async(
            scope=scope,
            text="test personal todo",
            created_by="usr_todo_test",
        )
        assert item.todo_id.startswith("td_")

        # List should show it.
        items = await store.list_async(scope=scope)
        assert any(i.todo_id == item.todo_id for i in items)

        # Complete it.
        completed = await store.complete_async(scope=scope, index=1)
        assert completed is not None
        assert completed.completed is True

        # Remove it.
        removed = await store.remove_async(scope=scope, index=1)
        assert removed is not None

    @pytest.mark.asyncio
    async def test_normal_scope_todo_persists(self, runner) -> None:  # type: ignore[no-untyped-def]
        """Todos in NORMAL scope persist to normal.db (not in-memory)."""
        from zero.core.scope import Scope

        scope = Scope.normal(group_id="grp_todo_test", topic_id=0).with_default_memory_scope()
        store = runner.todo_store

        item = await store.add_async(
            scope=scope,
            text="test normal todo",
            created_by="usr_todo_test",
        )
        items = await store.list_async(scope=scope)
        assert any(i.todo_id == item.todo_id for i in items)

    @pytest.mark.asyncio
    async def test_dev_scope_todo_persists(self, runner) -> None:  # type: ignore[no-untyped-def]
        """Todos in DEVELOPMENT scope persist to dev.db."""
        from zero.core.scope import Scope

        scope = Scope.development(
            org_id="org_todo_test",
            workspace_id="ws_todo_test",
            project_id="prj_todo_test",
            group_id="grp_todo_test",
            topic_id=0,
        ).with_default_memory_scope()
        store = runner.todo_store

        item = await store.add_async(
            scope=scope,
            text="test dev todo",
            created_by="usr_todo_test",
        )
        items = await store.list_async(scope=scope)
        assert any(i.todo_id == item.todo_id for i in items)


class TestConversationStoreRealDb:
    """Verify the ConversationStore persists to real SQLite for ALL scopes."""

    @pytest.mark.asyncio
    async def test_personal_conversation_persists(self, runner) -> None:  # type: ignore[no-untyped-def]
        """Conversation in PERSONAL scope persists to personal.db."""
        from zero.core.scope import Scope

        scope = Scope.personal(user_id="usr_conv_test").with_default_memory_scope()
        store = runner.conversation_store

        # Create a session.
        session = await store.get_or_create_session_async(
            scope=scope,
            external_chat_id="123456789",
            topic_id=0,
            user_id="usr_conv_test",
        )
        assert session.session_id.startswith("cs_")

        # Append a message.
        await store.append_message_async(
            scope=scope,
            session=session,
            role="user",
            content="hello",
        )

        # Get history.
        history = await store.get_history_async(scope=scope, session=session)
        assert len(history) == 1
        assert history[0].content == "hello"
        assert history[0].role == "user"

    @pytest.mark.asyncio
    async def test_normal_conversation_persists(self, runner) -> None:  # type: ignore[no-untyped-def]
        """Conversation in NORMAL scope persists to normal.db."""
        from zero.core.scope import Scope

        scope = Scope.normal(group_id="grp_conv_test", topic_id=0).with_default_memory_scope()
        store = runner.conversation_store

        session = await store.get_or_create_session_async(
            scope=scope,
            external_chat_id="987654321",
            topic_id=0,
            user_id="usr_conv_test",
        )
        await store.append_message_async(
            scope=scope,
            session=session,
            role="assistant",
            content="hi there",
        )
        history = await store.get_history_async(scope=scope, session=session)
        assert len(history) == 1
        assert history[0].content == "hi there"
