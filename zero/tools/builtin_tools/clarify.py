"""ClarifyTool — ask the user for clarification (real async wait).

This tool blocks the agent loop until the user responds or the timeout
expires. It uses a callback mechanism so the actual UI (Telegram inline
keyboard, CLI prompt, web UI) is injected by the runner.

Production behavior (Telegram):
    1. Create a future keyed by ``clarify_id``.
    2. Call the installed clarify callback (sends a Telegram message with
       inline keyboard buttons — one per choice + "Other").
    3. Wait for the future. When the user taps a button, the Telegram
       callback handler calls :func:`submit_clarification` to resolve
       the future.

Test behavior (no callback installed):
    1. Create a future keyed by ``clarify_id``.
    2. Wait for the future. The test calls
       :func:`submit_clarification` directly to resolve it.

Per ADR T-7.3: this tool is BLOCKED for sub-agents (only the top-level
agent can ask the user for clarification). This is enforced by the
Orchestrator's ``DELEGATE_BLOCKED_TOOLS`` set.
"""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from zero.tools.base import Tool, ToolContext, ToolSpec

__all__ = [
    "ClarifyTool",
    "ClarifyCallback",
    "CLARIFY_SCHEMA",
    "set_clarify_callback",
    "submit_clarification",
    "register",
]


CLARIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "question": {"type": "string"},
        "choices": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Up to 4 choices; a 5th 'Other' is always appended by UI",
            "maxItems": 4,
        },
        "multi_select": {"type": "boolean", "default": False},
        "timeout_seconds": {"type": "integer", "default": 120},
    },
    "required": ["question", "choices"],
}

# Pending clarify requests keyed by clarify_id.
_pending_clarify: dict[str, asyncio.Future[str]] = {}

# Callback registry: when the agent loop calls ClarifyTool, the runner installs
# a callback that sends the actual Telegram message with inline keyboard
# buttons. The callback receives (clarify_id, question, choices, multi_select,
# ToolContext) and is expected to display the question to the user.
ClarifyCallback = Callable[
    [str, str, list[str], bool, ToolContext],
    Awaitable[None],
]
_clarify_callback: ClarifyCallback | None = None


def set_clarify_callback(callback: ClarifyCallback | None) -> None:
    """Inject the clarify UI callback (called by the runner).

    The callback is responsible for actually displaying the question +
    choices to the user (e.g. sending a Telegram message with inline
    keyboard buttons). When the user taps a button, the Telegram callback
    handler calls :func:`submit_clarification` to resolve the future.
    """
    global _clarify_callback
    _clarify_callback = callback


def submit_clarification(clarify_id: str, response: str) -> bool:
    """Submit a user's response to a pending clarify request.

    Called by the Telegram callback handler when user taps a button (or
    by tests to programmatically resolve a clarify request).

    Returns ``True`` if the clarification was pending and has been resolved,
    ``False`` if it was already resolved or unknown.
    """
    future = _pending_clarify.pop(clarify_id, None)
    if future is None or future.done():
        return False
    future.set_result(response)
    return True


class ClarifyTool(Tool):
    """Ask the user for clarification (real async wait for response).

    Single-select (radio) or multi-select (checkbox) up to 4 choices.
    A 5th "Other" option is always appended by the UI.

    Blocks the agent loop until the user responds or timeout expires.
    """

    spec = ToolSpec(
        name="clarify",
        description="Ask user for clarification (single or multi-select). Blocks until response.",
        parameters_schema=CLARIFY_SCHEMA,
        required_permissions=frozenset(),
        approval_level="none",
        untrusted_output=False,  # user's selection is data, not untrusted
    )

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> str:
        question = str(args["question"])
        choices = list(args.get("choices", []))
        multi = bool(args.get("multi_select", False))
        timeout = int(args.get("timeout_seconds", 120))

        if not choices:
            return "[TOOL_ERROR] at least one choice required"
        if len(choices) > 4:
            return f"[TOOL_ERROR] max 4 choices, got {len(choices)}"

        clarify_id = f"clr_{uuid.uuid4().hex[:12]}"

        # Create a future that will be resolved by submit_clarification().
        loop = asyncio.get_event_loop()
        future: asyncio.Future[str] = loop.create_future()
        _pending_clarify[clarify_id] = future

        # Invoke the installed callback (sends Telegram message with inline
        # keyboard buttons). If no callback is installed (tests), skip —
        # the test is expected to call submit_clarification directly.
        if _clarify_callback is not None:
            try:
                await _clarify_callback(clarify_id, question, choices, multi, ctx)
            except Exception as e:
                _pending_clarify.pop(clarify_id, None)
                return f"[TOOL_ERROR] failed to send clarify message: {e}"

        try:
            response = await asyncio.wait_for(future, timeout=timeout)
            return response
        except TimeoutError:
            _pending_clarify.pop(clarify_id, None)
            return "[CLARIFY_TIMEOUT] user did not respond in time"
        finally:
            _pending_clarify.pop(clarify_id, None)


def register() -> None:
    """Register the ClarifyTool with the global tool registry."""
    from zero.tools.builtin_tools._helpers import register_tool  # noqa: PLC0415

    register_tool(ClarifyTool())
