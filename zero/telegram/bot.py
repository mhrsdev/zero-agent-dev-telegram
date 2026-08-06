"""Zero v2 Telegram bot — full long-poll + webhook loop with aiogram 3.x.

Per ADR 0001: aiogram 3.13+ for mature Forum/Topic support.
Per ADR T-4.2: Bot API only (no Telethon/MTProto).
Per ADR T-4.18: Session management — conversation context with window + expiry.
Per ADR T-4.20: Input security — user input always sent as **data** to LLM,
never as instruction.

Architecture:
    1. ``TelegramBot`` owns the aiogram ``Bot`` + ``Dispatcher``.
    2. Message handler resolves Scope via ``resolve_mode()`` (deterministic,
       never LLM).
    3. ``silenced=True`` (mode=disabled) → bot stays completely silent.
    4. ``mode=personal`` → routes to Personal agent.
    5. ``mode=normal`` → routes to Group agent (Group Memory).
    6. ``mode=development`` → routes to Project agent (full dev features).
    7. Callback queries (button presses) feed the approval queue.
    8. Forum topic lifecycle events update TopicBindings.

Run with:
    zero serve
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ContentType
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatMemberAdministrator,
    ChatMemberOwner,
    ForumTopicClosed,
    ForumTopicCreated,
    ForumTopicEdited,
    ForumTopicReopened,
    GeneralForumTopicHidden,
    GeneralForumTopicUnhidden,
    Message,
    Update,
    User,
)

from zero.core.audit import ActorType, AuditEntry, AuditResult, audit
from zero.core.errors import ZeroError
from zero.core.logging import get_logger, set_request_context
from zero.core.scope import Mode, Scope
from zero.core.secret import CompositeSecretResolver, SecretResolver
from zero.messaging import (
    IncomingMessage,
    OutgoingMessage,
    Participant,
    Platform,
)
from zero.security.approval import ApprovalChoice, ApprovalResolver
from zero.telegram.commands import CommandContext, CommandRegistry, CommandResult
from zero.telegram.topic_binding import (
    BindingStatus,
    GroupPolicy,
    GroupPolicyStore,
    ModeResolutionResult,
    TopicBinding,
    TopicBindingStore,
    resolve_mode,
)

if TYPE_CHECKING:
    from zero.agents.budget import BudgetTracker
    from zero.agents.router_client import RouterClient

__all__ = [
    "TelegramBot",
    "TelegramBotConfig",
    "MessageHandler",
    "CallbackHandler",
    "BotRunResult",
    "BOT_STARTUP_GRACE_SECONDS",
    "POLLING_TIMEOUT_SECONDS",
    "WEBHOOK_PATH",
]

_log = get_logger("zero.telegram.bot")

# Defaults
BOT_STARTUP_GRACE_SECONDS = 5.0  # warmup before serving traffic
POLLING_TIMEOUT_SECONDS = 30
WEBHOOK_PATH = "/telegram/webhook"


# ---------------------------------------------------------------------- types

@dataclass(slots=True)
class TelegramBotConfig:
    """Configuration for the Telegram bot runner."""

    bot_token_ref: str  # secret://env/TELEGRAM_BOT_TOKEN
    bot_username: str | None = None
    webhook_url: str | None = None  # if None → long-polling
    webhook_secret_ref: str | None = None
    webhook_listen_host: str = "127.0.0.1"  # never 0.0.0.0
    webhook_listen_port: int = 8080
    allowed_updates: list[str] = field(
        default_factory=lambda: ["message", "edited_message", "callback_query"]
    )
    drop_pending_updates: bool = False
    polling_timeout_seconds: int = POLLING_TIMEOUT_SECONDS


@dataclass(slots=True)
class BotRunResult:
    """Result of running the bot to completion."""

    started_at: float
    stopped_at: float | None = None
    updates_processed: int = 0
    errors: int = 0
    last_error: str | None = None


# Handler protocol: takes the platform-neutral IncomingMessage + resolved
# ModeResolutionResult, returns the text to send back (or None for silence).
MessageHandler = Callable[[IncomingMessage, ModeResolutionResult], Awaitable[str | None]]
CallbackHandler = Callable[[CallbackQuery, str], Awaitable[None]]


# ---------------------------------------------------------------------- bot

class TelegramBot:
    """Full Telegram bot with long-poll and webhook support.

    Construction:
        >>> bot = TelegramBot(
        ...     config=cfg,
        ...     resolver=CompositeSecretResolver(),
        ...     binding_store=TopicBindingStore(),
        ...     policy_store=GroupPolicyStore(),
        ...     command_registry=CommandRegistry(),
        ...     message_handler=my_handler,
        ... )

    Run long-polling:
        >>> await bot.start_polling()  # blocks until cancelled

    Run webhook:
        >>> await bot.start_webhook()  # blocks until cancelled
    """

    def __init__(
        self,
        *,
        config: TelegramBotConfig,
        resolver: SecretResolver,
        binding_store: TopicBindingStore,
        policy_store: GroupPolicyStore,
        command_registry: CommandRegistry,
        message_handler: MessageHandler,
        callback_handler: CallbackHandler | None = None,
        approval_resolver: ApprovalResolver | None = None,
        router_client: RouterClient | None = None,  # noqa: ARG002  # reserved for handler closures
        budget_tracker: BudgetTracker | None = None,  # noqa: ARG002  # reserved
        role_store: Any = None,
        conversation_store: Any = None,
    ) -> None:
        self._config = config
        self._resolver = resolver
        self._binding_store = binding_store
        self._policy_store = policy_store
        self._command_registry = command_registry
        self._message_handler = message_handler
        self._callback_handler = callback_handler or _default_callback_handler
        self._approval_resolver = approval_resolver
        self._role_store = role_store
        self._conversation_store = conversation_store

        # Resolve token from secret:// reference (at construction time is OK
        # because we need it to construct the Bot — but we still don't store
        # the raw value as a class attribute).
        secret = self._resolver.resolve(self._config.bot_token_ref)
        self._bot = Bot(
            token=secret.reveal(),
            default=DefaultBotProperties(parse_mode="HTML"),
        )
        self._dp = Dispatcher()
        self._running = False
        self._stop_event = asyncio.Event()

        # Register handlers.
        self._register_handlers()

    # ------------------------------------------------------------------ properties

    @property
    def bot(self) -> Bot:
        return self._bot

    @property
    def dispatcher(self) -> Dispatcher:
        return self._dp

    @property
    def is_running(self) -> bool:
        return self._running

    # ------------------------------------------------------------------ handler registration

    def _register_handlers(self) -> None:
        """Wire up aiogram message/callback/topic handlers."""

        # /start, /help
        self._dp.message.register(
            self._handle_help,
            Command("start", "help", ignore_case=True),
        )

        # /status — show current mode
        self._dp.message.register(
            self._handle_status_command,
            Command("status", ignore_case=True),
        )

        # /bind <mode> [project_id] — admin only
        self._dp.message.register(
            self._handle_bind_command,
            Command("bind", ignore_case=True),
        )

        # /unbind — admin only
        self._dp.message.register(
            self._handle_unbind_command,
            Command("unbind", ignore_case=True),
        )

        # /policy <default_unconfigured_topic_mode> — admin only
        self._dp.message.register(
            self._handle_policy_command,
            Command("policy", ignore_case=True),
        )

        # Forum topic lifecycle events
        self._dp.message.register(
            self._handle_topic_created,
            F.forum_topic_created,
        )
        self._dp.message.register(
            self._handle_topic_edited,
            F.forum_topic_edited,
        )
        self._dp.message.register(
            self._handle_topic_closed,
            F.forum_topic_closed,
        )
        self._dp.message.register(
            self._handle_topic_reopened,
            F.forum_topic_reopened,
        )
        self._dp.message.register(
            self._handle_general_hidden,
            F.general_forum_topic_hidden,
        )
        self._dp.message.register(
            self._handle_general_unhidden,
            F.general_forum_topic_unhidden,
        )

        # Callback query (button press — for approval workflow)
        self._dp.callback_query.register(self._handle_callback, F.data)

        # Catch-all: regular messages → route through message_handler
        self._dp.message.register(self._handle_message)

    # ------------------------------------------------------------------ message handler (catch-all)

    async def _handle_message(self, message: Message) -> None:
        """Catch-all for regular messages (text, voice, photo, etc.).

        Steps:
            1. Resolve Scope via deterministic ``resolve_mode()``.
            2. If silenced → return immediately (no response).
            3. If command (starts with /) → dispatch via command_registry.
            4. Otherwise → call user-provided message_handler.
            5. Send response (if any) as reply.
        """
        # Skip messages without text AND without voice/photo — aiogram should
        # have already routed those to specific handlers, but just in case.
        if message.text is None and message.voice is None and message.photo is None:
            return

        # Skip bot's own messages (prevents infinite loop).
        if message.from_user is not None and message.from_user.is_bot:
            return

        # Determine if this is a private chat or group.
        is_private = message.chat.type == ChatType.PRIVATE
        group_id = (
            f"grp_{_hash_chat_id(message.chat.id)}" if not is_private else None
        )
        topic_id = message.message_thread_id or 0
        user_id = (
            f"usr_{_hash_user_id(message.from_user.id)}"
            if message.from_user is not None
            else "usr_unknown"
        )

        # Resolve mode.
        result = resolve_mode(
            is_private=is_private,
            user_id=user_id,
            group_id=group_id,
            topic_id=topic_id,
            binding_store=self._binding_store,
            policy_store=self._policy_store,
        )

        # T-4.6: silenced mode = complete silence.
        if result.silenced:
            return

        # Set request context for logging.
        tokens = set_request_context(
            request_id=f"tg_{message.message_id}_{message.chat.id}",
            scope=result.scope,
            actor=user_id,
        )

        try:
            # Build platform-neutral IncomingMessage.
            incoming = _build_incoming_message(
                message=message,
                scope=result.scope,
                user_id=user_id,
                group_id=group_id,
                topic_id=topic_id,
            )

            # Command routing: if text starts with /, try command registry.
            if message.text and message.text.startswith("/"):
                cmd_result = await self._dispatch_command(message, result.scope, user_id)
                if cmd_result is not None:
                    if cmd_result.text:
                        await self._send_message(
                            chat_id=message.chat.id,
                            text=cmd_result.text,
                            topic_id=topic_id,
                            reply_to=message.message_id,
                            parse_mode=cmd_result.parse_mode,
                        )
                    return

            # Normal message → call user-provided handler.
            try:
                response_text = await self._message_handler(incoming, result)
            except Exception as e:
                _log.error(f"message handler raised: {e}", exc=e)
                response_text = (
                    "⚠️ Internal error processing your message. "
                    "The error has been logged."
                )

            if response_text is None:
                # Handler chose to stay silent.
                return

            await self._send_message(
                chat_id=message.chat.id,
                text=response_text,
                topic_id=topic_id,
                reply_to=message.message_id,
            )
        finally:
            # Reset request context.
            from zero.core.logging import reset_request_context  # noqa: PLC0415

            reset_request_context(tokens)

    # ------------------------------------------------------------------ command dispatch

    async def _dispatch_command(
        self,
        message: Message,
        scope: Scope,
        user_id: str,
    ) -> CommandResult | None:
        """Dispatch a slash command via the command registry.

        Returns None if the command wasn't recognized by the registry
        (caller may then treat as regular message).
        """
        if not message.text:
            return None
        from zero.core.permissions import (  # noqa: PLC0415
            PermissionContext,
            Role,
            global_registry,
        )

        # Determine role: use DB-backed role store if available,
        # otherwise default roles (PERSONAL_USER for personal, DEVELOPER for groups).
        role: Role
        if self._role_store is not None:
            role = await self._role_store.get_role_for_scope_async(
                user_id=user_id, scope=scope,
            )
        else:
            # Default roles when no role store is configured.
            role = (
                Role.PERSONAL_USER if scope.is_personal() else Role.DEVELOPER
            )

        def ctx_factory(args: list[str]) -> CommandContext:
            return CommandContext(
                scope=scope,
                actor_id=user_id,
                permission_ctx=PermissionContext(
                    actor_id=user_id,
                    actor_type=ActorType.HUMAN,
                    scope=scope,
                    role=role,
                ),
                args=args,
                raw_text=message.text or "",
                chat_id=str(message.chat.id),
                topic_id=message.message_thread_id,
            )

        try:
            return await self._command_registry.dispatch(message.text, ctx_factory)
        except Exception as e:
            _log.error(f"command dispatch failed: {e}", exc=e)
            return CommandResult(
                text=f"⚠️ Command failed: {e}",
                success=False,
            )

    # ------------------------------------------------------------------ /start, /help

    async def _handle_help(self, message: Message, command: CommandObject) -> None:
        """Handle /start and /help."""
        help_text = self._command_registry.get_help()
        # Prepend intro.
        intro = (
            "🤖 <b>Zero v2</b> — Telegram-based AI collaboration platform\n\n"
            "I operate in one of three modes per chat:\n"
            "  • <b>Personal</b> — your private 1:1 chat with me\n"
            "  • <b>Normal</b> — group chat with shared Group Memory\n"
            "  • <b>Development</b> — Topic bound to a Project (full dev features)\n\n"
        )
        await self._send_message(
            chat_id=message.chat.id,
            text=intro + help_text,
            topic_id=message.message_thread_id or 0,
            reply_to=message.message_id,
            parse_mode="html",
        )

    # ------------------------------------------------------------------ /status

    async def _handle_status_command(self, message: Message, command: CommandObject) -> None:
        """Show the current mode + scope for this chat."""
        is_private = message.chat.type == ChatType.PRIVATE
        group_id = (
            f"grp_{_hash_chat_id(message.chat.id)}" if not is_private else None
        )
        topic_id = message.message_thread_id or 0
        user_id = (
            f"usr_{_hash_user_id(message.from_user.id)}"
            if message.from_user is not None
            else "usr_unknown"
        )

        result = resolve_mode(
            is_private=is_private,
            user_id=user_id,
            group_id=group_id,
            topic_id=topic_id,
            binding_store=self._binding_store,
            policy_store=self._policy_store,
        )

        lines = [
            "<b>Zero v2 — Status</b>",
            "",
            f"<b>Mode:</b> <code>{result.mode.value}</code>",
            f"<b>Scope:</b> <code>{result.scope.retrieval_key()}</code>",
            f"<b>Memory scope:</b> <code>{result.scope.memory_scope_id or '(unset)'}</code>",
        ]
        if result.binding is not None:
            lines.extend([
                "",
                "<b>TopicBinding</b>",
                f"  • Binding ID: <code>{result.binding.id}</code>",
                f"  • Mode: <code>{result.binding.mode}</code>",
                f"  • Configured by: <code>{result.binding.configured_by}</code>",
            ])
            if result.binding.project_id:
                lines.append(f"  • Project: <code>{result.binding.project_id}</code>")
        else:
            lines.extend([
                "",
                "<i>No explicit TopicBinding — using GroupPolicy default.</i>",
            ])
            if result.policy_applied:
                lines.append("<i>(policy_applied=True)</i>")
        if result.silenced:
            lines.append("\n🚫 <b>SILENCED</b> — Zero will not respond.")

        await self._send_message(
            chat_id=message.chat.id,
            text="\n".join(lines),
            topic_id=topic_id,
            reply_to=message.message_id,
            parse_mode="html",
        )

    # ------------------------------------------------------------------ /bind

    async def _handle_bind_command(self, message: Message, command: CommandObject) -> None:
        """Bind a Topic to a mode: /bind normal | /bind dev <project_id> | /bind disabled.

        Admin only.
        """
        # Permission check — must be group admin (or owner in private).
        if not await self._is_admin(message.chat.id, message.from_user.id if message.from_user else 0):
            await self._send_message(
                chat_id=message.chat.id,
                text="⛔ Only group admins can change TopicBindings.",
                topic_id=message.message_thread_id or 0,
                reply_to=message.message_id,
            )
            return

        args = (command.args or "").strip().split()
        if not args:
            await self._send_message(
                chat_id=message.chat.id,
                text=(
                    "Usage: <code>/bind normal</code> | <code>/bind dev &lt;project_id&gt;</code> | "
                    "<code>/bind disabled</code>"
                ),
                topic_id=message.message_thread_id or 0,
                reply_to=message.message_id,
                parse_mode="html",
            )
            return

        mode_arg = args[0].lower()
        if mode_arg not in ("normal", "dev", "disabled"):
            await self._send_message(
                chat_id=message.chat.id,
                text=f"⛔ Invalid mode <code>{mode_arg}</code>. Use normal, dev, or disabled.",
                topic_id=message.message_thread_id or 0,
                reply_to=message.message_id,
                parse_mode="html",
            )
            return

        project_id: str | None = None
        if mode_arg == "dev":
            if len(args) < 2:
                await self._send_message(
                    chat_id=message.chat.id,
                    text="⛔ <code>/bind dev</code> requires a project_id argument.",
                    topic_id=message.message_thread_id or 0,
                    reply_to=message.message_id,
                    parse_mode="html",
                )
                return
            project_id = args[1]
            if not project_id.startswith("prj_"):
                await self._send_message(
                    chat_id=message.chat.id,
                    text=f"⛔ project_id must start with <code>prj_</code>, got <code>{project_id}</code>",
                    topic_id=message.message_thread_id or 0,
                    reply_to=message.message_id,
                    parse_mode="html",
                )
                return

        group_id = f"grp_{_hash_chat_id(message.chat.id)}"
        topic_id = message.message_thread_id or 0
        user_id = (
            f"usr_{_hash_user_id(message.from_user.id)}"
            if message.from_user is not None
            else "usr_unknown"
        )

        # Build memory_scope_id (independent of mode).
        if mode_arg == "dev":
            memory_scope_id = f"mem:prj:{project_id}"
        else:
            memory_scope_id = f"mem:grp:{group_id}:{topic_id}"

        try:
            binding = TopicBinding(
                group_id=group_id,
                topic_id=topic_id,
                mode=mode_arg,  # type: ignore[arg-type]
                memory_scope_id=memory_scope_id,
                configured_by=user_id,
                project_id=project_id,
            )
        except ValueError as e:
            await self._send_message(
                chat_id=message.chat.id,
                text=f"⛔ Invalid binding: {e}",
                topic_id=topic_id,
                reply_to=message.message_id,
            )
            return

        self._binding_store.upsert(binding)

        # Audit.
        with suppress(Exception):
            await audit().log(AuditEntry(
                actor_type=ActorType.HUMAN,
                actor_id=user_id,
                action="memory.write",  # binding is a metadata write
                scope=binding.memory_scope_id and _scope_from_binding(binding) or _scope_from_group(group_id, topic_id),
                result=AuditResult.SUCCESS,
                target_type="topic_binding",
                target_id=binding.id,
                after={"mode": binding.mode, "project_id": binding.project_id},
            ))

        await self._send_message(
            chat_id=message.chat.id,
            text=(
                f"✅ Topic bound to mode <b>{binding.mode}</b>.\n"
                f"  • Binding ID: <code>{binding.id}</code>\n"
                f"  • Memory scope: <code>{binding.memory_scope_id}</code>"
                + (f"\n  • Project: <code>{binding.project_id}</code>" if binding.project_id else "")
            ),
            topic_id=topic_id,
            reply_to=message.message_id,
            parse_mode="html",
        )

    # ------------------------------------------------------------------ /unbind

    async def _handle_unbind_command(self, message: Message, command: CommandObject) -> None:
        """Remove the TopicBinding (reverts to GroupPolicy default)."""
        if not await self._is_admin(message.chat.id, message.from_user.id if message.from_user else 0):
            await self._send_message(
                chat_id=message.chat.id,
                text="⛔ Only group admins can change TopicBindings.",
                topic_id=message.message_thread_id or 0,
                reply_to=message.message_id,
            )
            return

        group_id = f"grp_{_hash_chat_id(message.chat.id)}"
        topic_id = message.message_thread_id or 0
        archived = self._binding_store.archive(group_id, topic_id)
        if not archived:
            await self._send_message(
                chat_id=message.chat.id,
                text="ℹ️ No active binding for this Topic. Nothing to unbind.",
                topic_id=topic_id,
                reply_to=message.message_id,
            )
            return

        await self._send_message(
            chat_id=message.chat.id,
            text="✅ TopicBinding archived. Topic now uses GroupPolicy default.",
            topic_id=topic_id,
            reply_to=message.message_id,
        )

    # ------------------------------------------------------------------ /policy

    async def _handle_policy_command(self, message: Message, command: CommandObject) -> None:
        """Set the GroupPolicy: /policy normal | /policy disabled."""
        if not await self._is_admin(message.chat.id, message.from_user.id if message.from_user else 0):
            await self._send_message(
                chat_id=message.chat.id,
                text="⛔ Only group admins can change GroupPolicy.",
                topic_id=message.message_thread_id or 0,
                reply_to=message.message_id,
            )
            return

        args = (command.args or "").strip().split()
        if not args:
            group_id = f"grp_{_hash_chat_id(message.chat.id)}"
            policy = self._policy_store.get_or_default(group_id)
            await self._send_message(
                chat_id=message.chat.id,
                text=(
                    f"Current GroupPolicy:\n"
                    f"  • default_unconfigured_topic_mode: <code>{policy.default_unconfigured_topic_mode}</code>"
                ),
                topic_id=message.message_thread_id or 0,
                reply_to=message.message_id,
                parse_mode="html",
            )
            return

        mode_arg = args[0].lower()
        if mode_arg not in ("normal", "disabled"):
            await self._send_message(
                chat_id=message.chat.id,
                text="⛔ policy must be <code>normal</code> or <code>disabled</code>.",
                topic_id=message.message_thread_id or 0,
                reply_to=message.message_id,
                parse_mode="html",
            )
            return

        group_id = f"grp_{_hash_chat_id(message.chat.id)}"
        self._policy_store.set(_make_policy(group_id, mode_arg))
        await self._send_message(
            chat_id=message.chat.id,
            text=f"✅ GroupPolicy updated. Default unconfigured topic mode: <code>{mode_arg}</code>",
            topic_id=message.message_thread_id or 0,
            reply_to=message.message_id,
            parse_mode="html",
        )

    # ------------------------------------------------------------------ topic lifecycle

    async def _handle_topic_created(self, message: Message) -> None:
        """When a new Forum topic is created, log it for awareness.

        Per ADR T-4.4: no auto-binding. User must explicitly /bind.
        """
        event: ForumTopicCreated | None = message.forum_topic_created
        if event is None or message.chat is None:
            return
        group_id = f"grp_{_hash_chat_id(message.chat.id)}"
        topic_id = message.message_thread_id or 0
        _log.info(
            f"forum topic created: group={group_id} topic={topic_id} name={event.name!r}"
        )

    async def _handle_topic_edited(self, message: Message) -> None:
        event: ForumTopicEdited | None = message.forum_topic_edited
        if event is None:
            return
        _log.info(f"forum topic edited: {event.name!r}")

    async def _handle_topic_closed(self, message: Message) -> None:
        event: ForumTopicClosed | None = message.forum_topic_closed
        if event is None:
            return
        # Closing a topic never deletes data (memory_scope_id is bound to
        # Project, not Telegram topic_id).
        _log.info(f"forum topic closed: chat={message.chat.id} thread={message.message_thread_id}")

    async def _handle_topic_reopened(self, message: Message) -> None:
        event: ForumTopicReopened | None = message.forum_topic_reopened
        if event is None:
            return
        _log.info(f"forum topic reopened: chat={message.chat.id} thread={message.message_thread_id}")

    async def _handle_general_hidden(self, message: Message) -> None:
        event: GeneralForumTopicHidden | None = message.general_forum_topic_hidden
        if event is None:
            return
        _log.info(f"general forum topic hidden: chat={message.chat.id}")

    async def _handle_general_unhidden(self, message: Message) -> None:
        event: GeneralForumTopicUnhidden | None = message.general_forum_topic_unhidden
        if event is None:
            return
        _log.info(f"general forum topic unhidden: chat={message.chat.id}")

    # ------------------------------------------------------------------ callback query

    async def _handle_callback(self, query: CallbackQuery) -> None:
        """Handle button presses (approval workflow).

        Callback data format: ``<action>:<id>`` e.g. ``approve:apv_01HABC``.
        """
        if query.data is None:
            return
        # Always answer the callback to remove the loading state.
        with suppress(TelegramBadRequest, TelegramNetworkError):
            await query.answer()

        # Delegate to user-provided callback handler.
        try:
            await self._callback_handler(query, query.data)
        except Exception as e:
            _log.error(f"callback handler raised: {e}", exc=e)

    # ------------------------------------------------------------------ send helpers

    async def _send_message(
        self,
        *,
        chat_id: int,
        text: str,
        topic_id: int = 0,
        reply_to: int | None = None,
        parse_mode: str = "html",
        disable_notification: bool = False,
    ) -> Message | None:
        """Send a message, respecting topic_id for Forum groups."""
        kwargs: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_notification": disable_notification,
        }
        if topic_id and topic_id > 0:
            kwargs["message_thread_id"] = topic_id
        if reply_to is not None:
            kwargs["reply_to_message_id"] = reply_to

        try:
            return await self._bot.send_message(**kwargs)
        except TelegramBadRequest as e:
            # Common: "message thread not found" if topic was deleted.
            _log.warning(f"telegram bad request: {e}")
            # Retry without topic_id.
            kwargs.pop("message_thread_id", None)
            try:
                return await self._bot.send_message(**kwargs)
            except (TelegramBadRequest, TelegramNetworkError):
                return None
        except TelegramNetworkError as e:
            _log.error(f"telegram network error: {e}", exc=e)
            return None

    # ------------------------------------------------------------------ admin check

    async def _is_admin(self, chat_id: int, user_id: int) -> bool:
        """Check if ``user_id`` is an admin of ``chat_id``.

        Private chat: always True (user is the owner of their own chat).
        Group: query Telegram API.
        """
        if user_id == 0:
            return False
        # For private chat, the user is always the owner.
        try:
            chat = await self._bot.get_chat(chat_id)
        except (TelegramBadRequest, TelegramNetworkError):
            return False
        if chat.type == ChatType.PRIVATE:
            return True
        # Group: check membership.
        try:
            member = await self._bot.get_chat_member(chat_id, user_id)
        except (TelegramBadRequest, TelegramNetworkError):
            return False
        return isinstance(member, (ChatMemberAdministrator, ChatMemberOwner))

    # ------------------------------------------------------------------ run loops

    async def start_polling(self) -> BotRunResult:
        """Start long-polling. Blocks until cancelled or fatal error."""
        import time  # noqa: PLC0415

        result = BotRunResult(started_at=time.monotonic())
        self._running = True
        self._stop_event.clear()

        # Drop pending updates if configured.
        if self._config.drop_pending_updates:
            with suppress(TelegramBadRequest, TelegramNetworkError):
                await self._bot.delete_webhook(drop_pending_updates=True)

        # Validate bot token by calling get_me().
        try:
            me = await self._bot.get_me()
            _log.info(
                f"telegram bot connected: @{me.username} (id={me.id})",
            )
        except (TelegramBadRequest, TelegramNetworkError) as e:
            _log.error(f"failed to connect to telegram: {e}", exc=e)
            self._running = False
            result.last_error = str(e)
            return result

        # Startup grace period.
        await asyncio.sleep(BOT_STARTUP_GRACE_SECONDS)

        # Start polling. aiogram handles update dispatch internally.
        _log.info(
            f"starting long-polling (timeout={self._config.polling_timeout_seconds}s, "
            f"allowed_updates={self._config.allowed_updates})"
        )
        try:
            await self._dp.start_polling(
                self._bot,
                allowed_updates=self._config.allowed_updates,
                polling_timeout=self._config.polling_timeout_seconds,
                handle_as_tasks=True,
                close_bot_session=True,
            )
        except asyncio.CancelledError:
            _log.info("polling cancelled by caller")
        except Exception as e:
            _log.error(f"polling crashed: {e}", exc=e)
            result.errors += 1
            result.last_error = str(e)
        finally:
            self._running = False
            result.stopped_at = time.monotonic()
            with suppress(Exception):
                await self._bot.session.close()

        return result

    async def start_webhook(self) -> BotRunResult:
        """Start webhook server. Blocks until cancelled.

        Per T-9.2: webhook must listen on 127.0.0.1 (never 0.0.0.0) unless
        explicitly configured otherwise — and even then, must be behind a
        reverse proxy with TLS termination.
        """
        import time  # noqa: PLC0415

        from aiogram.webhook.aiohttp_server import (  # noqa: PLC0415
            SimpleRequestHandler,
            setup_application,
        )
        from aiohttp import web  # noqa: PLC0415

        result = BotRunResult(started_at=time.monotonic())
        self._running = True
        self._stop_event.clear()

        if not self._config.webhook_url:
            raise ValueError("webhook_url is required for webhook mode")

        # Set the webhook.
        webhook_secret = ""
        if self._config.webhook_secret_ref:
            with suppress(Exception):
                webhook_secret = self._resolver.resolve(
                    self._config.webhook_secret_ref
                ).reveal()

        try:
            await self._bot.set_webhook(
                url=self._config.webhook_url,
                secret_token=webhook_secret or None,
                allowed_updates=self._config.allowed_updates,
                drop_pending_updates=self._config.drop_pending_updates,
            )
        except (TelegramBadRequest, TelegramNetworkError) as e:
            _log.error(f"failed to set webhook: {e}", exc=e)
            self._running = False
            result.last_error = str(e)
            return result

        # Validate.
        me = await self._bot.get_me()
        _log.info(f"telegram bot connected (webhook): @{me.username}")

        # Build aiohttp app.
        app = web.Application()
        handler = SimpleRequestHandler(
            dispatcher=self._dp,
            bot=self._bot,
            secret_token=webhook_secret or None,
        )
        handler.register(app, path=WEBHOOK_PATH)
        setup_application(app, self._dp, bot=self._bot)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(
            runner,
            host=self._config.webhook_listen_host,
            port=self._config.webhook_listen_port,
        )
        await site.start()
        _log.info(
            f"webhook listening on http://{self._config.webhook_listen_host}:"
            f"{self._config.webhook_listen_port}{WEBHOOK_PATH}"
        )

        try:
            await self._stop_event.wait()
        except asyncio.CancelledError:
            _log.info("webhook cancelled by caller")
        finally:
            await runner.cleanup()
            with suppress(TelegramBadRequest, TelegramNetworkError):
                await self._bot.delete_webhook()
            with suppress(Exception):
                await self._bot.session.close()
            self._running = False
            result.stopped_at = time.monotonic()

        return result

    async def stop(self) -> None:
        """Signal the bot to stop (for webhook mode)."""
        self._stop_event.set()
        if self._dp._running:  # type: ignore[attr-defined]  # aiogram internal flag
            await self._dp.stop_polling()

    # ------------------------------------------------------------------ manual update feed (for tests)

    async def feed_update(self, update: Update) -> Any:
        """Feed a single Update to the dispatcher (for tests / webhook simulation)."""
        return await self._dp.feed_update(bot=self._bot, update=update)


# ---------------------------------------------------------------------- helpers

def _hash_chat_id(chat_id: int) -> str:
    """Derive a stable grp_<ulid> from a Telegram chat_id.

    Telegram chat IDs are int64. We hash to a fixed-width hex string to
    create a stable ULID-style identifier. The prefix ``grp_`` is per ADR 0002.
    """
    import hashlib  # noqa: PLC0415

    h = hashlib.sha256(f"chat:{chat_id}".encode("utf-8")).hexdigest()[:26]
    return h


def _hash_user_id(user_id: int) -> str:
    """Derive a stable usr_<ulid> from a Telegram user_id."""
    import hashlib  # noqa: PLC0415

    h = hashlib.sha256(f"user:{user_id}".encode("utf-8")).hexdigest()[:26]
    return h


def _build_incoming_message(
    *,
    message: Message,
    scope: Scope,
    user_id: str,
    group_id: str | None,
    topic_id: int,
) -> IncomingMessage:
    """Convert aiogram Message to platform-neutral IncomingMessage."""
    sender = Participant(
        external_id=str(message.from_user.id) if message.from_user else "0",
        display_name=(
            message.from_user.full_name
            if message.from_user
            else "Unknown"
        ),
        is_bot=message.from_user.is_bot if message.from_user else False,
        username=message.from_user.username if message.from_user else None,
    )
    text = message.text or message.caption or ""
    return IncomingMessage(
        platform=Platform.TELEGRAM,
        external_chat_id=str(message.chat.id),
        external_message_id=str(message.message_id),
        topic_id=topic_id,
        sender=sender,
        text=text,
        scope=scope,
        is_edit=message.edit_date is not None,
        raw_metadata={
            "chat_type": message.chat.type,
            "is_forum": message.chat.is_forum,
            "user_id": user_id,
            "group_id": group_id,
        },
    )


def _scope_from_binding(binding: TopicBinding) -> Scope:
    """Build a Scope from a TopicBinding (for audit)."""
    if binding.mode == "dev" and binding.project_id:
        return Scope.development(
            org_id=f"org_for_{binding.project_id}",
            workspace_id=f"ws_for_{binding.project_id}",
            project_id=binding.project_id,
            group_id=binding.group_id,
            topic_id=binding.topic_id,
        ).with_default_memory_scope()
    return Scope.normal(
        group_id=binding.group_id,
        topic_id=binding.topic_id,
    ).with_default_memory_scope()


def _scope_from_group(group_id: str, topic_id: int) -> Scope:
    return Scope.normal(group_id=group_id, topic_id=topic_id).with_default_memory_scope()


def _make_policy(group_id: str, mode: str) -> GroupPolicy:
    """Build a GroupPolicy with the given default_unconfigured_topic_mode."""
    return GroupPolicy(
        group_id=group_id,
        default_unconfigured_topic_mode=mode,  # type: ignore[arg-type]  # mypy can't narrow Literal from str
    )


async def _default_callback_handler(query: CallbackQuery, data: str) -> None:
    """Default callback handler — just logs. Real handler provided by caller."""
    _log.info(f"callback received: data={data!r} from={query.from_user.id}")
