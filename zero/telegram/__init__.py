"""Zero v2 Telegram integration — Phase 4.

TelegramBot (full long-poll + webhook loop), TopicBinding, mode detection,
command framework.

Per ADR 0003 §1:
    - Mode detection is tabular and deterministic — never LLM.
    - Lookup on (group_id, topic_id) → TopicBinding.
    - No binding → GroupPolicy.default_unconfigured_topic_mode.
    - `disabled` mode = complete silence.
"""
from __future__ import annotations

from zero.telegram.bot import (
    BotRunResult,
    CallbackHandler,
    MessageHandler,
    TelegramBot,
    TelegramBotConfig,
)
from zero.telegram.topic_binding import (
    GroupPolicy,
    GroupPolicyStore,
    ModeResolutionResult,
    TopicBinding,
    TopicBindingStore,
    resolve_mode,
)
from zero.telegram.adapter import TelegramAdapter
from zero.telegram.commands import CommandContext, CommandRegistry, CommandResult
from zero.telegram.mode_isolation_tests import verify_mode_isolation

__all__ = [
    "BotRunResult",
    "CallbackHandler",
    "MessageHandler",
    "TelegramBot",
    "TelegramBotConfig",
    "TelegramAdapter",
    "TopicBinding",
    "TopicBindingStore",
    "GroupPolicy",
    "GroupPolicyStore",
    "resolve_mode",
    "ModeResolutionResult",
    "CommandRegistry",
    "CommandContext",
    "CommandResult",
    "verify_mode_isolation",
]
