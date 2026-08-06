"""Zero Agent Runner — wires TelegramBot + AgentLoop + Router + LLM provider.

This is the production entrypoint that replaces the placeholder echo handler
in the CLI's ``serve`` command. It:

    1. Loads ZeroConfig + SecretResolver
    2. Opens the Database (three SQLite files)
    3. Builds the LLM provider (Gemini / OpenAI / OpenRouter / custom)
    4. Starts the RouterShim (if provider != "custom") — exposes the provider
       at http://127.0.0.1:<port>/v1 so RouterClient can talk to it
    5. Builds RouterClient pointed at the shim (or external Router URL)
    6. Builds BudgetTracker + DbMemoryStore + DbTodoStore + RoleStore +
       ConversationStore + ApprovalResolver
    7. Builds AgentDefinition for personal/normal/dev modes
    8. Builds the tool dispatcher (closure over tool registry)
    9. Builds the Orchestrator (wired with router + dispatcher)
    10. Builds TelegramBot with a real message_handler that:
        a. Resolves Scope (already done by bot)
        b. Looks up or creates a conversation session
        c. Loads memory context
        d. Builds AgentLoop with the right agent_def for the mode
        e. Runs the loop
        f. Persists the assistant reply to conversation + memory
        g. Returns the text reply
    11. Starts the Telegram polling loop

Voice messages: routed through VoiceMessageRouter (download → transcribe →
handler → TTS → sendVoice).

This is the glue that makes Zero a real working product, not a stub.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from zero.agents.budget import BudgetTracker
from zero.agents.definition import AgentDefinition, AgentType
from zero.agents.llm_provider import (
    GeminiProvider,
    GenericOpenAIProvider,
    LLMProvider,
    OpenAIProvider,
    OpenRouterProvider,
    RouterShim,
    RouterShimConfig,
    build_provider_from_config,
)
from zero.agents.loop import AgentLoop, AgentLoopResult
from zero.agents.orchestrator import Orchestrator
from zero.agents.router_client import RouterClient, RouterMessage, RouterToolCall
from zero.core.config import ZeroConfig, get_config
from zero.core.logging import configure_logging, get_logger
from zero.core.scope import Mode, Scope
from zero.core.secret import CompositeSecretResolver, SecretResolver
from zero.db import Database
from zero.memory.db_store import DbMemoryStore
from zero.security.approval import ApprovalResolver
from zero.stores.approval_store import DbApprovalResolver, DbApprovalStore
from zero.stores.conversation_store import DbConversationStore
from zero.stores.role_store import DbRoleStore
from zero.stores.todo_store import DbTodoStore
from zero.telegram.bot import TelegramBot, TelegramBotConfig
from zero.telegram.commands import CommandRegistry
from zero.telegram.topic_binding import (
    GroupPolicyStore,
    ModeResolutionResult,
    TopicBinding,
    TopicBindingStore,
)
from zero.telegram.voice_handler import VoiceMessageRouter
from zero.tools.builtin_tools import (
    set_approval_request_deps,
    set_clarify_callback,
    set_delegate_orchestrator,
    set_memory_store,
    set_send_message_callback,
    set_todo_store,
)
from zero.tools.registry import registry as tool_registry

if TYPE_CHECKING:
    from zero.messaging import IncomingMessage
    from zero.telegram.topic_binding import ModeResolutionResult

__all__ = [
    "AgentRunContext",
    "ZeroAgentRunner",
    "ZeroAgentRunnerConfig",
]

_log = get_logger("zero.runner")


# ---------------------------------------------------------------------- config

@dataclass(slots=True)
class ZeroAgentRunnerConfig:
    """Configuration for the ZeroAgentRunner.

    Defaults are pulled from ZeroConfig at runner construction time, but
    callers can override individual fields for testing.
    """

    config: ZeroConfig | None = None
    resolver: SecretResolver | None = None
    db: Database | None = None
    # If True, don't actually start the Telegram bot — just set up the runner.
    dry_run: bool = False
    # If True, the runner will use stub voice transcriber + TTS (for tests).
    use_stub_voice: bool = False
    # Override the LLM provider (for tests).
    provider_override: LLMProvider | None = None
    # Override the router shim port (0 = auto-pick).
    shim_port: int = 0


# ---------------------------------------------------------------------- run context

@dataclass(slots=True)
class AgentRunContext:
    """Per-message run context — built fresh for each incoming message."""

    scope: Scope
    mode_result: ModeResolutionResult
    user_id: str
    group_id: str | None
    topic_id: int
    conversation_session_id: str | None = None
    history: list[RouterMessage] = field(default_factory=list)


# ---------------------------------------------------------------------- runner

class ZeroAgentRunner:
    """The production runner — wires every component together.

    Lifecycle:
        >>> runner = ZeroAgentRunner()
        >>> await runner.setup()       # build everything
        >>> await runner.start()       # start shim + telegram bot
        >>> # ... runs forever ...
        >>> await runner.stop()        # graceful shutdown

    For tests:
        >>> runner = ZeroAgentRunner(config=ZeroAgentRunnerConfig(dry_run=True))
        >>> await runner.setup()
        >>> # Manually call runner.handle_message(...) with a fake IncomingMessage
    """

    def __init__(self, runner_cfg: ZeroAgentRunnerConfig | None = None) -> None:
        self._runner_cfg = runner_cfg or ZeroAgentRunnerConfig()
        self._config: ZeroConfig = self._runner_cfg.config or get_config()
        self._resolver: SecretResolver = (
            self._runner_cfg.resolver or CompositeSecretResolver()
        )
        self._db: Database | None = self._runner_cfg.db
        self._owns_db = self._db is None  # we'll create it

        # Components (built in setup()).
        self._provider: LLMProvider | None = None
        self._shim: RouterShim | None = None
        self._router_client: RouterClient | None = None
        self._budget: BudgetTracker | None = None
        self._memory_store: DbMemoryStore | None = None
        self._todo_store: DbTodoStore | None = None
        self._role_store: DbRoleStore | None = None
        self._conversation_store: DbConversationStore | None = None
        self._approval_store: DbApprovalStore | None = None
        self._approval_resolver: DbApprovalResolver | None = None
        self._orchestrator: Orchestrator | None = None
        self._voice_router: VoiceMessageRouter | None = None
        self._bot: TelegramBot | None = None
        self._binding_store: TopicBindingStore | None = None
        self._policy_store: GroupPolicyStore | None = None
        self._command_registry: CommandRegistry | None = None

        # Agent definitions per mode (cached).
        self._agent_defs: dict[Mode, AgentDefinition] = {}

        # Setup state.
        self._setup_done = False
        self._shim_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ setup

    async def setup(self) -> None:
        """Build all components. Idempotent."""
        if self._setup_done:
            return

        # Configure logging.
        configure_logging(
            level=self._config.logging.level,
            format_=self._config.logging.format,
            redact=self._config.logging.redact_secrets,
            log_user_content=self._config.logging.log_user_message_content,
        )
        _log.info(
            "ZeroAgentRunner setup starting",
            extra={
                "provider": self._config.router.provider if self._config.router else None,
                "db_backend": self._config.database.backend,
            },
        )

        # Open the database.
        if self._db is None:
            self._db = await self._open_database()

        # Build stores.
        self._memory_store = DbMemoryStore(self._db)
        self._todo_store = DbTodoStore(self._db)
        self._role_store = DbRoleStore(self._db)
        self._conversation_store = DbConversationStore(self._db)
        self._approval_store = DbApprovalStore(self._db)
        self._approval_resolver = DbApprovalResolver(self._approval_store)

        # Inject stores into tools (global injection — used by builtin tools).
        set_memory_store(self._memory_store)
        set_todo_store(self._todo_store)

        # Inject the orchestrator (for delegate_task tool).
        # Done after orchestrator is built below.

        # Inject the clarify callback (sends Telegram inline keyboard).
        set_clarify_callback(self._make_clarify_callback())

        # Inject the send_message callback (for send_message tool).
        set_send_message_callback(self._make_send_message_callback())

        # Inject the approval_request deps (store + send_keyboard callback).
        set_approval_request_deps(
            store=self._approval_store,
            send_keyboard=self._make_approval_keyboard_callback(),
        )

        # Build the LLM provider (or use override).
        if self._runner_cfg.provider_override is not None:
            self._provider = self._runner_cfg.provider_override
        elif self._config.router is not None:
            self._provider = build_provider_from_config(
                router_cfg=self._config.router,
                resolver=self._resolver,
            )
            # Start the RouterShim if provider is not "custom".
            if self._config.router.provider != "custom":
                await self._start_shim()

        # Build RouterClient.
        if self._config.router is not None:
            base_url = self._shim.base_url if self._shim else self._config.router.base_url
            self._router_client = RouterClient(
                base_url=base_url,
                api_key_ref=self._config.router.api_key,
                resolver=self._resolver,
                timeout_seconds=self._config.router.timeout_seconds,
                max_retries=self._config.router.max_retries,
                default_model=self._config.router.default_model,
            )

        # Build budget tracker.
        self._budget = BudgetTracker()

        # Build orchestrator (wired with router + dispatcher).
        self._orchestrator = Orchestrator(
            budget_tracker=self._budget,
            max_concurrent_children=self._config.agent.max_concurrent_runs,
            max_depth=1,
            router=self._router_client,
            tool_dispatcher=self._make_tool_dispatcher(),
        )

        # Inject the orchestrator into the delegate_task tool (now that it's built).
        set_delegate_orchestrator(self._orchestrator)

        # Build agent definitions per mode.
        self._agent_defs = {
            Mode.PERSONAL: self._build_agent_def(Mode.PERSONAL),
            Mode.NORMAL: self._build_agent_def(Mode.NORMAL),
            Mode.DEVELOPMENT: self._build_agent_def(Mode.DEVELOPMENT),
        }

        # Build binding/policy stores.
        self._binding_store = TopicBindingStore()
        self._policy_store = GroupPolicyStore()

        # Build command registry.
        self._command_registry = self._build_command_registry()

        # Build Telegram bot (unless dry_run). Voice router is built after
        # the bot because it needs the bot instance.
        if not self._runner_cfg.dry_run:
            self._bot = await self._build_telegram_bot()
            self._voice_router = self._build_voice_router()
        else:
            self._voice_router = None

        self._setup_done = True
        _log.info("ZeroAgentRunner setup complete")

    async def _open_database(self) -> Database:
        """Open the SQLite database (three files)."""
        from zero.db.sqlite_backend import SqliteBackend  # noqa: PLC0415

        if self._config.database.backend != "sqlite":
            raise RuntimeError(
                f"database backend {self._config.database.backend!r} not supported by runner; "
                "use sqlite for now"
            )
        sqlite_dir = self._config.database.sqlite_dir
        if sqlite_dir is None:
            raise RuntimeError("database.sqlite_dir must be set")
        sqlite_dir = Path(sqlite_dir)
        sqlite_dir.mkdir(parents=True, exist_ok=True)
        backend = SqliteBackend(sqlite_dir)
        db = Database(backend=backend)
        await db.start()
        _log.info(f"database opened at {sqlite_dir}")
        return db

    async def _start_shim(self) -> None:
        """Start the RouterShim that wraps the LLM provider."""
        assert self._provider is not None
        assert self._config.router is not None
        cfg = RouterShimConfig(
            host=self._config.router.shim_host,
            port=self._runner_cfg.shim_port or self._config.router.shim_port,
            api_key_ref=self._config.router.api_key,
        )
        self._shim = RouterShim(provider=self._provider, config=cfg)
        await self._shim.start()
        _log.info(f"RouterShim started at {self._shim.base_url}")

    def _build_agent_def(self, mode: Mode) -> AgentDefinition:
        """Build an AgentDefinition for the given mode.

        Per ADR T-7.1: agent belongs to one scope. We build a "template"
        AgentDefinition whose scope is a placeholder; the actual scope is
        set per-message by replacing the scope field.
        """
        from dataclasses import replace  # noqa: PLC0415

        # Use a placeholder scope — replaced at run time.
        placeholder_scope = Scope.personal(user_id="usr_placeholder").with_default_memory_scope()

        if mode is Mode.PERSONAL:
            agent_type = AgentType.TRIAGE
            system_prompt = (
                "You are Zero, a personal AI assistant for an individual developer.\n"
                "You help with reminders, notes, research, and quick tasks.\n"
                "You have access to memory_search (your long-term notes), todo, "
                "and clarify tools.\n"
                "Keep responses concise (3-5 sentences) unless asked for detail.\n"
                "When the user mentions a fact they want remembered, suggest they "
                "use the memory_save tool (which requires approval).\n"
            )
            tool_allowlist = frozenset({
                "memory_search", "todo", "clarify", "web_fetch", "read_file",
            })
            effort_tier = "zero/fast"
        elif mode is Mode.NORMAL:
            agent_type = AgentType.TRIAGE
            system_prompt = (
                "You are Zero, a team assistant for a Telegram group chat.\n"
                "You help the team with shared notes, decisions, and quick lookups.\n"
                "NORMAL mode does NOT allow facts or decisions — those require "
                "DEVELOPMENT mode with a project binding.\n"
                "Keep responses concise and respectful of group context.\n"
            )
            tool_allowlist = frozenset({
                "memory_search", "todo", "clarify", "web_fetch",
            })
            effort_tier = "zero/fast"
        else:  # DEVELOPMENT
            agent_type = AgentType.CODING
            system_prompt = (
                "You are Zero, a development assistant bound to a project.\n"
                "You have full access to file operations, shell execution (in a "
                "sandbox), memory (facts + decisions), git, and the orchestrator "
                "for spawning sub-agents.\n"
                "Per ADR T-6.5: personal memory is NEVER retrieved in DEVELOPMENT mode.\n"
                "Always confirm risky operations (file writes, shell commands) via the "
                "approval workflow before executing.\n"
                "Use the todo tool to track multi-step tasks.\n"
                "When stuck, use clarify to ask the user — don't guess.\n"
            )
            tool_allowlist = frozenset({
                "read_file", "write_file", "patch_file", "list_files", "search_files",
                "bash_exec", "web_fetch", "todo", "clarify", "git_status",
                "memory_search", "delegate_task", "send_message", "approval_request",
            })
            effort_tier = "zero/coding"

        return AgentDefinition(
            name=f"zero-{mode.value}",
            agent_type=agent_type,
            scope=placeholder_scope,
            system_prompt=system_prompt,
            effort_tier=effort_tier,
            tool_allowlist=tool_allowlist,
            max_turns=self._config.agent.max_turns,
            budget_usd=self._config.agent.budget_default_usd,
            description=f"Zero agent for {mode.value} mode",
        )

    def _make_tool_dispatcher(self):
        """Build the tool dispatcher closure used by AgentLoop.

        The dispatcher receives a RouterToolCall + Scope, looks up the tool
        in the registry, and invokes it with a fresh ToolContext.
        """
        from zero.tools.base import ToolContext  # noqa: PLC0415

        async def dispatcher(tc: RouterToolCall, scope: Scope) -> str:
            # Coerce args to schema types (best effort).
            args = tool_registry.coerce_args(tc.name, tc.arguments)
            ctx = ToolContext(
                scope=scope,
                actor_id="agent_loop",
                tool_call_id=tc.id,
            )
            result = await tool_registry.dispatch(tc.name, args, ctx)
            return result.output

        return dispatcher

    def _make_clarify_callback(self):
        """Build the clarify callback that sends a Telegram inline keyboard.

        The callback sends a message with buttons for each choice + "Other".
        When the user taps a button, the Telegram callback handler calls
        ``submit_clarification(clarify_id, response)`` to resolve the future.
        """
        async def callback(
            clarify_id: str,
            question: str,
            choices: list[str],
            multi: bool,
            ctx: Any,
        ) -> None:
            if self._bot is None:
                # No bot — silently skip (test mode).
                return
            from aiogram.types import (  # noqa: PLC0415
                InlineKeyboardButton,
                InlineKeyboardMarkup,
            )

            # Build inline keyboard — one button per choice.
            buttons = []
            for choice in choices:
                # Truncate choice to 60 chars (Telegram callback_data limit is 64).
                choice_short = choice[:50]
                # Format: clarify:<clarify_id>:<choice_text>
                cb_data = f"clarify:{clarify_id}:{choice_short}"[:64]
                buttons.append([InlineKeyboardButton(text=choice, callback_data=cb_data)])
            # Always append "Other" button.
            buttons.append([InlineKeyboardButton(
                text="Other (type your reply)",
                callback_data=f"clarify:{clarify_id}:__other__",
            )])
            keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

            # Send the message to the chat the agent is running in.
            # We use the conversation_store to find the external_chat_id for
            # this scope. If not available, fall back to the bot's first chat.
            # This is a best-effort approach — in a multi-chat deployment,
            # clarify only works if we can identify the originating chat.
            chat_id = await self._find_chat_id_for_scope(ctx.scope)
            if chat_id is None:
                _log.warning("clarify: could not find chat_id for scope — skipping keyboard")
                return

            try:
                await self._bot.bot.send_message(
                    chat_id=chat_id,
                    text=f"❓ {question}",
                    reply_markup=keyboard,
                )
            except Exception as e:
                _log.warning(f"clarify: failed to send inline keyboard: {e}")

        return callback

    def _make_send_message_callback(self):
        """Build the send_message callback for the SendMessageTool."""
        async def callback(
            chat_id: str,
            text: str,
            topic_id: int | None,
            parse_mode: str,
        ) -> bool:
            if self._bot is None:
                return False
            try:
                kwargs: dict[str, Any] = {
                    "chat_id": int(chat_id),
                    "text": text,
                }
                if topic_id and topic_id > 0:
                    kwargs["message_thread_id"] = topic_id
                if parse_mode == "html":
                    kwargs["parse_mode"] = "HTML"
                elif parse_mode == "markdown":
                    kwargs["parse_mode"] = "MarkdownV2"
                await self._bot.bot.send_message(**kwargs)
                return True
            except Exception as e:
                _log.error(f"send_message failed: {e}", exc=e)
                return False

        return callback

    def _make_approval_keyboard_callback(self):
        """Build the callback that sends an approval inline keyboard."""
        async def callback(approval_id: str, description: str, ctx: Any) -> None:
            if self._bot is None:
                return
            from aiogram.types import (  # noqa: PLC0415
                InlineKeyboardButton,
                InlineKeyboardMarkup,
            )

            buttons = [
                [
                    InlineKeyboardButton(text="✅ Approve", callback_data=f"approve:{approval_id}"),
                    InlineKeyboardButton(text="❌ Reject", callback_data=f"reject:{approval_id}"),
                ],
                [
                    InlineKeyboardButton(text="✏️ Edit", callback_data=f"edit:{approval_id}"),
                    InlineKeyboardButton(text="📝 Request Changes", callback_data=f"changes:{approval_id}"),
                ],
            ]
            keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

            chat_id = await self._find_chat_id_for_scope(ctx.scope)
            if chat_id is None:
                _log.warning("approval: could not find chat_id for scope — skipping keyboard")
                return

            try:
                await self._bot.bot.send_message(
                    chat_id=chat_id,
                    text=f"🔒 <b>Approval required</b>\n\n{description}",
                    reply_markup=keyboard,
                    parse_mode="HTML",
                )
            except Exception as e:
                _log.warning(f"approval: failed to send inline keyboard: {e}")

        return callback

    async def _find_chat_id_for_scope(self, scope: Scope) -> int | None:
        """Find the most recent Telegram chat_id for a given scope.

        Used by the clarify/approval callbacks to send inline keyboards to
        the right chat. We look up active conversation sessions for this
        scope and return the most recently active one's external_chat_id.
        """
        if self._conversation_store is None:
            return None
        try:
            sessions = await self._conversation_store.list_active_sessions_async(
                scope=scope,
            )
            if not sessions:
                return None
            # Most recently active session.
            latest = max(sessions, key=lambda s: s.last_activity_at)
            try:
                return int(latest.external_chat_id)
            except (ValueError, TypeError):
                return None
        except Exception as e:
            _log.warning(f"failed to find chat_id for scope: {e}")
            return None

    def _build_command_registry(self) -> CommandRegistry:
        """Build the command registry with default commands."""
        from zero.telegram.commands import (  # noqa: PLC0415
            Command,
            CommandContext,
            CommandResult,
        )

        reg = CommandRegistry()
        # Capture self for closures.
        runner = self

        # /clear — clear conversation history.
        class ClearCommand(Command):
            name = "clear"
            description = "Clear conversation history for this chat."
            required_permission = "message.send"
            usage = "/clear"

            async def execute(self, ctx: CommandContext) -> CommandResult:
                if runner._conversation_store is not None and ctx.scope is not None:
                    sessions = await runner._conversation_store.list_active_sessions_async(
                        scope=ctx.scope,
                    )
                    for s in sessions:
                        await runner._conversation_store.end_session_async(s.session_id)
                return CommandResult(text="✅ Conversation cleared.", success=True)

        reg.register(ClearCommand())

        # /memory — search memory.
        class MemoryCommand(Command):
            name = "memory"
            description = "Search your long-term memory."
            required_permission = "message.send"
            usage = "/memory <query>"

            async def execute(self, ctx: CommandContext) -> CommandResult:
                if runner._memory_store is None:
                    return CommandResult(text="Memory store not available.", success=False)
                if not ctx.args:
                    return CommandResult(text="Usage: /memory <query>", success=False)
                query = " ".join(ctx.args)
                results_raw = runner._memory_store.retrieve(ctx.scope, query, limit=5)
                # Handle async retrieve (DbMemoryStore).
                import asyncio as _asyncio  # noqa: PLC0415
                if _asyncio.iscoroutine(results_raw):
                    results = await results_raw
                else:
                    results = results_raw
                if not results:
                    return CommandResult(text="No matching memories found.", success=True)
                lines = [f"Found {len(results)} memories:"]
                for r in results:
                    kind = r.entry.kind.value
                    content = r.entry.content[:100]
                    lines.append(f"  [{kind}] {content}")
                return CommandResult(text="\n".join(lines), success=True)

        reg.register(MemoryCommand())

        # /todos — list todos.
        class TodosCommand(Command):
            name = "todos"
            description = "List your todos."
            required_permission = "message.send"
            usage = "/todos"

            async def execute(self, ctx: CommandContext) -> CommandResult:
                if runner._todo_store is None:
                    return CommandResult(text="Todo store not available.", success=False)
                items = await runner._todo_store.list_async(scope=ctx.scope)
                if not items:
                    return CommandResult(text="No todos.", success=True)
                lines = ["Todos:"]
                for i, t in enumerate(items, 1):
                    mark = "[x]" if t.completed else "[ ]"
                    lines.append(f"  {i}. {mark} {t.item_text}")
                return CommandResult(text="\n".join(lines), success=True)

        reg.register(TodosCommand())

        return reg

    def _build_voice_router(self) -> VoiceMessageRouter | None:
        """Build the voice message router.

        Returns None if voice processing is disabled or the bot isn't built yet
        (in dry_run mode, voice router is not needed).
        """
        if self._bot is None or self._config.router is None:
            return None

        from zero.telegram.voice_handler import VoiceRouterConfig  # noqa: PLC0415
        from zero.voice.tts import StubTTSClient, TTSClient  # noqa: PLC0415
        from zero.voice.transcriber import (  # noqa: PLC0415
            RouterVoiceTranscriber,
            StubVoiceTranscriber,
            VoiceTranscriber,
        )

        # Build transcriber.
        if self._runner_cfg.use_stub_voice:
            transcriber: VoiceTranscriber = StubVoiceTranscriber()
            tts_client: TTSClient = StubTTSClient()
        else:
            # Real transcription via the RouterShim.
            base_url = self._shim.base_url if self._shim else self._config.router.base_url
            transcriber = RouterVoiceTranscriber(
                base_url=base_url,
                api_key_ref=self._config.router.api_key,
                resolver=self._resolver,
            )
            # Use Edge TTS for free default TTS.
            try:
                from zero.voice.tts import EdgeTTSClient  # noqa: PLC0415
                tts_client = EdgeTTSClient()
            except ImportError:
                tts_client = StubTTSClient()

        # The message_handler closure (defined in _build_telegram_bot).
        # We need to define it here too so the voice router can call it.
        async def voice_message_handler(msg, mode_result):  # type: ignore[no-untyped-def]  # noqa: ANN001
            return await self.handle_message(msg, mode_result)

        return VoiceMessageRouter(
            bot=self._bot.bot,
            transcriber=transcriber,
            tts_client=tts_client,
            message_handler=voice_message_handler,
            config=VoiceRouterConfig(
                enable_transcription=True,
                enable_tts_response=True,
            ),
        )

    async def _build_telegram_bot(self) -> TelegramBot:
        """Build the Telegram bot with a real message handler."""
        if self._config.telegram is None:
            raise RuntimeError("telegram config is missing — run 'zero init' first")

        bot_cfg = TelegramBotConfig(
            bot_token_ref=self._config.telegram.bot_token,
            bot_username=self._config.telegram.bot_username,
            webhook_url=self._config.telegram.webhook_url,
            webhook_secret_ref=self._config.telegram.webhook_secret,
            drop_pending_updates=self._config.telegram.drop_pending_updates,
            allowed_updates=self._config.telegram.allowed_updates,
        )

        async def message_handler(
            msg: IncomingMessage,
            mode_result: ModeResolutionResult,
        ) -> str | None:
            """Real message handler — runs the agent loop."""
            return await self.handle_message(msg, mode_result)

        async def callback_handler(query: Any, data: str) -> None:
            """Handle callback queries (approval + clarify buttons)."""
            await self.handle_callback(query, data)

        return TelegramBot(
            config=bot_cfg,
            resolver=self._resolver,
            binding_store=self._binding_store or TopicBindingStore(),
            policy_store=self._policy_store or GroupPolicyStore(),
            command_registry=self._command_registry or CommandRegistry(),
            message_handler=message_handler,
            callback_handler=callback_handler,
            approval_resolver=self._approval_resolver,
            router_client=self._router_client,
            budget_tracker=self._budget,
            role_store=self._role_store,
            conversation_store=self._conversation_store,
        )

    # ------------------------------------------------------------------ message handler

    async def handle_message(
        self,
        msg: IncomingMessage,
        mode_result: ModeResolutionResult,
    ) -> str | None:
        """Handle an incoming message — run the agent loop.

        This is the heart of the runner. Steps:
            1. Build the AgentRunContext (scope, user, history).
            2. Look up the right AgentDefinition for the mode.
            3. Build an AgentLoop with the right scope.
            4. Load conversation history (from DbConversationStore).
            5. Run the loop.
            6. Persist the user message + assistant reply to the conversation.
            7. Return the assistant text.
        """
        if self._router_client is None:
            return "⚠️ Router not configured — cannot process messages."
        if self._budget is None:
            return "⚠️ Budget tracker not initialized."

        scope = mode_result.scope
        mode = scope.mode
        user_id = msg.raw_metadata.get("user_id", "usr_unknown")
        group_id = msg.raw_metadata.get("group_id")
        topic_id = msg.topic_id or 0

        # Look up the agent def for this mode.
        base_agent_def = self._agent_defs.get(mode)
        if base_agent_def is None:
            return f"⚠️ No agent configured for mode {mode.value!r}."

        # Replace the placeholder scope with the actual scope.
        from dataclasses import replace  # noqa: PLC0415

        agent_def = replace(base_agent_def, scope=scope)

        # Load conversation history.
        history: list[RouterMessage] = []
        session_id: str | None = None
        if self._conversation_store is not None:
            # Get or create a conversation session.
            session = await self._conversation_store.get_or_create_session_async(
                scope=scope,
                external_chat_id=msg.external_chat_id,
                topic_id=topic_id,
                user_id=user_id,
                ttl_seconds=self._config.security.session_ttl_seconds,
            )
            session_id = session.session_id
            messages = await self._conversation_store.get_history_async(
                scope=scope,
                session=session,
                limit=20,
            )
            for m in messages:
                history.append(RouterMessage(
                    role=m.role,  # type: ignore[arg-type]
                    content=m.content,
                ))

        # Retrieve relevant memory.
        memory_context = ""
        if self._memory_store is not None and msg.text:
            try:
                results = await self._memory_store.retrieve(
                    scope=scope,
                    query=msg.text,
                    limit=5,
                    max_tokens=1000,
                )
                if results:
                    memory_lines = [
                        f"[{r.entry.kind.value}] {r.entry.content[:200]}"
                        for r in results
                    ]
                    memory_context = "\n\nRelevant memory:\n" + "\n".join(memory_lines)
            except Exception as e:
                _log.warning(f"memory retrieval failed: {e}")

        # Build the user message (with memory context if any).
        user_content = msg.text or ""
        if memory_context:
            user_content = f"{user_content}{memory_context}"

        # Build the AgentLoop.
        loop = AgentLoop(
            router=self._router_client,
            agent_def=agent_def,
            budget_tracker=self._budget,
            tool_dispatcher=self._make_tool_dispatcher(),
        )

        # Run the loop.
        try:
            result = await loop.run(
                user_message=user_content,
                launched_by=user_id,
                history=history,
            )
        except Exception as e:
            _log.error(f"agent loop failed: {e}", exc=e)
            return f"⚠️ Agent loop failed: {e}"

        # Persist the user message + assistant reply to the conversation.
        if self._conversation_store is not None and session_id is not None:
            try:
                await self._conversation_store.add_message_by_session_id_async(
                    scope=scope,
                    session_id=session_id,
                    role="user",
                    content=msg.text or "",
                )
                await self._conversation_store.add_message_by_session_id_async(
                    scope=scope,
                    session_id=session_id,
                    role="assistant",
                    content=result.output_text,
                )
            except Exception as e:
                _log.warning(f"failed to persist conversation: {e}")

        return result.output_text

    async def handle_callback(self, query: Any, data: str) -> None:
        """Handle a Telegram callback query (button press).

        Routes to:
            - Approval resolver if data starts with "approve:"/"reject:"/"edit:"/"changes:"
            - Clarify resolver if data starts with "clarify:"
        """
        from zero.tools.builtin_tools.clarify import submit_clarification  # noqa: PLC0415

        # Clarify callback.
        if data.startswith("clarify:"):
            parts = data.split(":", 2)
            if len(parts) < 3:
                return
            clarify_id = parts[1]
            response = parts[2]
            submit_clarification(clarify_id, response)
            try:
                await query.message.edit_reply_markup()  # type: ignore[attr-defined]
            except Exception:
                pass
            return

        # Approval callback.
        if data.startswith(("approve:", "reject:", "edit:", "changes:")):
            parts = data.split(":", 1)
            if len(parts) < 2:
                return
            choice_str = parts[0]
            approval_id = parts[1]
            from zero.security.approval import ApprovalChoice  # noqa: PLC0415

            choice_map = {
                "approve": ApprovalChoice.APPROVE,
                "reject": ApprovalChoice.REJECT,
                "edit": ApprovalChoice.EDIT,
                "changes": ApprovalChoice.REQUEST_CHANGES,
            }
            choice = choice_map.get(choice_str)
            if choice is None or self._approval_resolver is None:
                return
            try:
                await self._approval_resolver.resolve_async(
                    approval_id,
                    approver_id=str(query.from_user.id),
                    choice=choice,
                )
            except Exception as e:
                _log.warning(f"approval resolve failed: {e}")
                return
            try:
                await query.message.edit_text(  # type: ignore[attr-defined]
                    f"✅ Approval {choice.value}: {approval_id}"
                )
            except Exception:
                pass

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Start the runner — Telegram polling loop.

        Blocks until cancelled or fatal error.
        """
        if not self._setup_done:
            await self.setup()
        if self._bot is None:
            raise RuntimeError("bot not built — call setup() first")

        _log.info("ZeroAgentRunner starting — entering polling loop")
        result = await self._bot.start_polling()
        _log.info(
            "ZeroAgentRunner polling stopped",
            extra={
                "updates_processed": result.updates_processed,
                "errors": result.errors,
                "last_error": result.last_error,
            },
        )

    async def stop(self) -> None:
        """Graceful shutdown."""
        _log.info("ZeroAgentRunner stopping")
        if self._bot is not None:
            await self._bot.stop()
        if self._shim is not None:
            await self._shim.stop()
        if self._db is not None and self._owns_db:
            await self._db.stop()
        _log.info("ZeroAgentRunner stopped")

    # ------------------------------------------------------------------ accessors

    @property
    def bot(self) -> TelegramBot | None:
        return self._bot

    @property
    def router_client(self) -> RouterClient | None:
        return self._router_client

    @property
    def provider(self) -> LLMProvider | None:
        return self._provider

    @property
    def shim(self) -> RouterShim | None:
        return self._shim

    @property
    def memory_store(self) -> DbMemoryStore | None:
        return self._memory_store

    @property
    def todo_store(self) -> DbTodoStore | None:
        return self._todo_store

    @property
    def role_store(self) -> DbRoleStore | None:
        return self._role_store

    @property
    def conversation_store(self) -> DbConversationStore | None:
        return self._conversation_store

    @property
    def approval_store(self) -> DbApprovalStore | None:
        return self._approval_store

    @property
    def orchestrator(self) -> Orchestrator | None:
        return self._orchestrator

    @property
    def db(self) -> Database | None:
        return self._db
