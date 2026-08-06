"""Zero v2 agent loop — ADR T-7.3 / Phase 7.

The main conversation loop:
    1. Call Router with messages + tools
    2. If Router returned tool_calls → dispatch via tool registry
    3. Append tool results to messages
    4. Repeat until Router returns finish_reason='stop' or max_turns reached

Iteration budget + grace-call pattern (ported from Hermes conversation_loop).
Interrupt flag checked at top of every loop iteration.

Context compression (ported from Hermes trajectory_compressor.py):
    - When the conversation history exceeds the token budget, the oldest
      messages are compressed into a single summary message.
    - The most recent N exchanges are kept verbatim.
    - Compression runs BEFORE each Router call (not after) so the Router
      always sees a context that fits within its token budget.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from zero.agents.budget import BudgetTracker
from zero.agents.definition import AgentDefinition
from zero.agents.router_client import RouterClient, RouterMessage, RouterResponse, RouterToolCall
from zero.core.scope import Scope

__all__ = [
    "AgentLoop",
    "AgentLoopResult",
    "LoopInterrupted",
    "ToolDispatcher",
    "DEFAULT_MAX_CONTEXT_TOKENS",
    "DEFAULT_KEEP_LAST_EXCHANGES",
]


class LoopInterrupted(Exception):
    """Raised when the loop is interrupted (cancel signal)."""


# Tool dispatcher protocol — implemented by the tools/registry.py module.
ToolDispatcher = Callable[[RouterToolCall, Scope], Awaitable[str]]

# Context compression defaults.
DEFAULT_MAX_CONTEXT_TOKENS = 16_000  # compress if history exceeds this
DEFAULT_KEEP_LAST_EXCHANGES = 6  # keep last 6 user↔assistant exchanges verbatim


@dataclass(frozen=True, slots=True)
class AgentLoopResult:
    """Final result of an agent loop execution."""

    output_text: str
    total_cost_usd: float
    turns: int
    finish_reason: str
    cancelled: bool = False

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "output_chars": len(self.output_text),
            "total_cost_usd": round(self.total_cost_usd, 6),
            "turns": self.turns,
            "finish_reason": self.finish_reason,
            "cancelled": self.cancelled,
        }


# ---------------------------------------------------------------------- loop

class AgentLoop:
    """Single-agent conversation loop.

    Construction:
        >>> loop = AgentLoop(
        ...     router=router_client,
        ...     agent_def=coding_agent,
        ...     budget_tracker=tracker,
        ...     tool_dispatcher=dispatcher,
        ... )

    Usage:
        >>> result = await loop.run(
        ...     user_message="implement feature X",
        ...     launched_by="usr_01H...",
        ... )
    """

    def __init__(
        self,
        *,
        router: RouterClient,
        agent_def: AgentDefinition,
        budget_tracker: BudgetTracker,
        tool_dispatcher: ToolDispatcher,
        max_context_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS,
        keep_last_exchanges: int = DEFAULT_KEEP_LAST_EXCHANGES,
    ) -> None:
        self._router = router
        self._agent_def = agent_def
        self._budget = budget_tracker
        self._tool_dispatcher = tool_dispatcher
        self._interrupted = False
        self._max_context_tokens = max_context_tokens
        self._keep_last_exchanges = keep_last_exchanges

    def interrupt(self) -> None:
        """Request graceful interrupt at next loop iteration."""
        self._interrupted = True

    async def run(
        self,
        *,
        user_message: str,
        launched_by: str,
        history: list[RouterMessage] | None = None,
    ) -> AgentLoopResult:
        """Run the agent loop until completion or interrupt.

        Returns the final output text + total cost.
        """
        scope = self._agent_def.scope
        messages: list[RouterMessage] = []

        # System prompt first.
        messages.append(RouterMessage(role="system", content=self._agent_def.system_prompt))

        # Then history (if any — for sub-agents, history is empty by design).
        if history:
            messages.extend(history)

        # Then current user message.
        messages.append(RouterMessage(role="user", content=user_message))

        # Build tool definitions (deferred tool loading — only name + short
        # description sent initially; full schema loaded on first call).
        tools = self._build_tool_definitions()

        turns = 0
        total_cost = 0.0
        last_response: RouterResponse | None = None

        while turns < self._agent_def.max_turns:
            # Interrupt check at top of every iteration.
            if self._interrupted:
                return AgentLoopResult(
                    output_text=last_response.content if last_response else "",
                    total_cost_usd=total_cost,
                    turns=turns,
                    finish_reason="interrupted",
                    cancelled=True,
                )

            # Context compression: if history is too long, compress it.
            messages = self._maybe_compress(messages)

            # Budget check BEFORE call.
            self._budget.check(
                scope=scope,
                agent_def_id=self._agent_def.id,
            )

            # Call Router.
            response = await self._router.complete(
                messages=messages,
                tools=tools if tools else None,
                scope=scope,
                effort_tier=self._agent_def.effort_tier,
            )
            total_cost += response.cost_usd
            self._budget.record(
                amount_usd=response.cost_usd,
                scope=scope,
                agent_def_id=self._agent_def.id,
            )

            turns += 1
            last_response = response

            # Append assistant message.
            messages.append(RouterMessage(
                role="assistant",
                content=response.content,
                tool_calls=_serialize_tool_calls(response.tool_calls) if response.tool_calls else None,
            ))

            # If no tool calls, we're done.
            if not response.tool_calls:
                return AgentLoopResult(
                    output_text=response.content,
                    total_cost_usd=total_cost,
                    turns=turns,
                    finish_reason=response.finish_reason,
                )

            # Dispatch tool calls (concurrently if multiple).
            tool_results = await asyncio.gather(
                *[
                    self._dispatch_tool(tc, scope)
                    for tc in response.tool_calls
                ],
                return_exceptions=True,
            )

            # Append each tool result as a 'tool' message.
            for tc, result in zip(response.tool_calls, tool_results, strict=True):
                if isinstance(result, BaseException):
                    tool_output: str = f"[TOOL_ERROR] {tc.name}: {result}"
                else:
                    tool_output = result
                messages.append(RouterMessage(
                    role="tool",
                    content=tool_output,
                    tool_call_id=tc.id,
                    name=tc.name,
                ))

        # Max turns reached.
        return AgentLoopResult(
            output_text=last_response.content if last_response else "",
            total_cost_usd=total_cost,
            turns=turns,
            finish_reason="max_turns_reached",
        )

    def _maybe_compress(self, messages: list[RouterMessage]) -> list[RouterMessage]:
        """Compress the message history if it exceeds the token budget.

        Uses :class:`zero.tools.context_compressor.ContextCompressor` to
        fold old messages into a single summary, keeping the most recent
        N exchanges verbatim.
        """
        # Estimate total tokens (4 chars = 1 token).
        total_chars = sum(len(m.content or "") for m in messages)
        total_tokens = total_chars // 4
        if total_tokens <= self._max_context_tokens:
            return messages  # no compression needed

        # Convert to dict format for the compressor.
        from zero.tools.context_compressor import ContextCompressor  # noqa: PLC0415

        compressor = ContextCompressor()
        msg_dicts = [_router_msg_to_dict(m) for m in messages]
        result = compressor.compress(
            msg_dicts,
            keep_last=self._keep_last_exchanges,
            max_tokens=self._max_context_tokens,
        )
        # Convert back to RouterMessage list.
        return [
            RouterMessage(
                role=m["role"],  # type: ignore[arg-type]
                content=m.get("content"),
                tool_calls=m.get("tool_calls"),
                tool_call_id=m.get("tool_call_id"),
                name=m.get("name"),
            )
            for m in result.messages
        ]

    def _build_tool_definitions(self) -> list[dict[str, Any]]:
        """Build OpenAI-format tool definitions.

        Implements deferred tool loading (T-7.5 acceptance criterion):
            - Tier 0: ≤4 tools → send full schemas (cheap, no overhead).
            - Tier 1: >4 tools and total schema size ≤4000 chars → send full schemas.
            - Tier 2: >4 tools and total schema size >4000 chars → send only
              name + short description; full schema loaded on first call via
              tool_describe/tool_call bridge tools.

        This pattern is ported from Hermes ``tools/tool_search.py``.
        """
        # Lazy import to avoid circular dependency.
        from zero.tools.registry import registry as tool_registry  # noqa: PLC0415

        # Get full definitions for the allowed tools.
        full_defs = tool_registry.get_definitions(
            allowed=self._agent_def.tool_allowlist,
            include_schema=True,
        )

        # Tier 0: ≤4 tools → send full.
        if len(full_defs) <= 4:
            return full_defs

        # Compute total schema size.
        import json  # noqa: PLC0415

        total_size = sum(len(json.dumps(d)) for d in full_defs)

        # Tier 1: total schema ≤4000 chars → send full.
        if total_size <= 4000:
            return full_defs

        # Tier 2: too big — send only name + description.
        # Add bridge tools: tool_describe, tool_call.
        deferred_defs = tool_registry.get_definitions(
            allowed=self._agent_def.tool_allowlist,
            include_schema=False,  # name + description only
        )
        # Add the bridge tools.
        deferred_defs.append({
            "type": "function",
            "function": {
                "name": "tool_describe",
                "description": "Get the full JSON Schema for a tool before calling it. "
                               "Use when you need to know the exact parameters of a tool.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "The tool name to describe",
                        },
                    },
                    "required": ["name"],
                },
            },
        })
        deferred_defs.append({
            "type": "function",
            "function": {
                "name": "tool_call",
                "description": "Call a tool by name with the given arguments. "
                               "Use after tool_describe if you need the schema.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "arguments": {"type": "object"},
                    },
                    "required": ["name", "arguments"],
                },
            },
        })
        return deferred_defs

    async def _dispatch_tool(self, tc: RouterToolCall, scope: Scope) -> str:
        """Dispatch a single tool call via the tool registry.

        First checks the global tool registry. If the tool is a bridge tool
        (tool_describe, tool_call), handles it specially. Otherwise dispatches
        via the registered handler.
        """
        from zero.tools.registry import registry as tool_registry  # noqa: PLC0415
        from zero.tools.base import ToolContext  # noqa: PLC0415

        # Handle bridge tools (for deferred loading Tier 2).
        if tc.name == "tool_describe":
            tool_name = str(tc.arguments.get("name", ""))
            entry = tool_registry.get(tool_name)
            if entry is None:
                return f"[TOOL_ERROR] tool {tool_name!r} not found"
            return json.dumps(entry.to_definition(include_schema=True)["function"])
        if tc.name == "tool_call":
            tool_name = str(tc.arguments.get("name", ""))
            tool_args = tc.arguments.get("arguments", {})
            if not isinstance(tool_args, dict):
                return f"[TOOL_ERROR] arguments must be a dict, got {type(tool_args).__name__}"
            # Coerce args to schema types.
            tool_args = tool_registry.coerce_args(tool_name, tool_args)
            ctx = ToolContext(
                scope=scope,
                actor_id="agent_loop",
                tool_call_id=tc.id,
            )
            result = await tool_registry.dispatch(tool_name, tool_args, ctx)
            return result.output

        # Regular tool: use the tool dispatcher (which wraps the registry).
        return await self._tool_dispatcher(tc, scope)


def _serialize_tool_calls(tcs: list[RouterToolCall]) -> list[dict[str, Any]]:
    """Convert RouterToolCall list to OpenAI-format dict list."""
    import json  # noqa: PLC0415

    return [
        {
            "id": tc.id,
            "type": "function",
            "function": {
                "name": tc.name,
                "arguments": json.dumps(tc.arguments),
            },
        }
        for tc in tcs
    ]


def _router_msg_to_dict(m: RouterMessage) -> dict[str, Any]:
    """Convert RouterMessage to OpenAI-format dict (for context compressor)."""
    out: dict[str, Any] = {"role": m.role}
    if m.content is not None:
        out["content"] = m.content
    if m.tool_calls is not None:
        out["tool_calls"] = m.tool_calls
    if m.tool_call_id is not None:
        out["tool_call_id"] = m.tool_call_id
    if m.name is not None:
        out["name"] = m.name
    return out
