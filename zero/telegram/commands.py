"""Zero v2 Telegram command framework — ADR T-4.17.

Registry, parsing, typed parameters, help, error messages.
Each command declares permission + scope.
"""
from __future__ import annotations

import abc
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from zero.core.permissions import PermissionContext
from zero.core.scope import Scope

__all__ = [
    "Command",
    "CommandContext",
    "CommandError",
    "CommandRegistry",
    "CommandResult",
    "global_registry",
    "register_command",
]


class CommandError(Exception):
    """Raised when a command cannot be parsed or executed."""


@dataclass(frozen=True, slots=True)
class CommandContext:
    """Runtime context for a command invocation."""

    scope: Scope
    actor_id: str
    permission_ctx: PermissionContext
    args: list[str]
    raw_text: str
    chat_id: str
    topic_id: int | None = None


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Result of a command execution."""

    text: str
    success: bool = True
    reply_to_message_id: str | None = None
    parse_mode: Literal["markdown", "html", "plain"] = "plain"


class Command(abc.ABC):
    """Abstract base class for commands.

    Subclasses implement :meth:`execute` and provide :attr:`name`,
    :attr:`description`, :attr:`required_permission`.
    """

    name: str
    description: str
    required_permission: str
    usage: str = ""

    @abc.abstractmethod
    async def execute(self, ctx: CommandContext) -> CommandResult:
        ...


class CommandRegistry:
    """Registry of commands with parser."""

    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}

    def register(self, command: Command) -> None:
        if command.name in self._commands:
            raise ValueError(f"command {command.name!r} already registered")
        self._commands[command.name] = command

    def list_commands(self) -> list[Command]:
        return sorted(self._commands.values(), key=lambda c: c.name)

    def get_help(self) -> str:
        lines = ["Available commands:"]
        for cmd in self.list_commands():
            lines.append(f"/{cmd.name} — {cmd.description}")
        return "\n".join(lines)

    async def dispatch(self, text: str, ctx_factory: Callable[[list[str]], CommandContext]) -> CommandResult:
        """Parse ``text`` and dispatch the matching command.

        ``ctx_factory`` receives the parsed arg list and returns a CommandContext.
        """
        if not text.startswith("/"):
            raise CommandError(f"not a command: {text!r}")
        # Strip leading "/" and split on whitespace.
        body = text[1:]
        try:
            parts = shlex.split(body)
        except ValueError as e:
            raise CommandError(f"invalid command syntax: {e}") from e
        if not parts:
            raise CommandError("empty command")

        name = parts[0].lower()
        # Strip @botname suffix (Telegram adds this in groups).
        name = re.sub(r"@.*$", "", name)
        args = parts[1:]

        cmd = self._commands.get(name)
        if cmd is None:
            return CommandResult(
                text=f"Unknown command: /{name}\n\n{self.get_help()}",
                success=False,
            )

        ctx = ctx_factory(args)
        # Permission check.
        from zero.core.permissions import has_permission  # noqa: PLC0415

        if not has_permission(cmd.required_permission, ctx.permission_ctx):
            return CommandResult(
                text=f"Permission denied: /{cmd.name} requires {cmd.required_permission!r}",
                success=False,
            )

        try:
            return await cmd.execute(ctx)
        except CommandError as e:
            return CommandResult(text=f"Error: {e}", success=False)
        except Exception as e:
            return CommandResult(text=f"Internal error: {e}", success=False)


# ---------------------------------------------------------------------- global registry

global_registry = CommandRegistry()


def register_command(command: Command) -> None:
    global_registry.register(command)
