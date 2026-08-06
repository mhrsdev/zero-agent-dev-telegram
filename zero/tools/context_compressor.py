"""Context compressor — ported from Hermes (``hermes_cli/partial_compress.py`` + ``trajectory_compressor.py``).

Per ADR T-7.2 + T-4.18:
    - Long-running conversations need compression to stay under token budget
    - User-chosen compression boundary: "fold everything before this point"
    - Keep most recent N exchanges verbatim
    - Snaps tail boundary backwards to nearest ``user`` message (preserves
      user↔assistant alternation for OpenAI API)
    - Replaces compressed region with single summary message

Also implements trajectory compression for training data generation:
    - Protects first turns (system, human, first gpt, first tool)
    - Protects last N turns
    - Compresses middle only
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ContextCompressor",
    "CompressionResult",
    "TrajectoryCompressor",
    "DEFAULT_KEEP_LAST",
    "MAX_KEEP_LAST",
]


DEFAULT_KEEP_LAST = 4  # default exchanges to keep verbatim
MAX_KEEP_LAST = 100  # clamp fat-fingered values


@dataclass(frozen=True, slots=True)
class CompressionResult:
    """Result of context compression."""

    messages: list[dict[str, Any]]
    compressed_count: int
    kept_count: int
    summary: str | None = None
    saved_tokens_est: int = 0


class ContextCompressor:
    """Compress conversation history to fit token budget.

    Usage:
        >>> compressor = ContextCompressor()
        >>> result = compressor.compress(history, keep_last=4)
        >>> new_history = result.messages
    """

    def compress(
        self,
        messages: list[dict[str, Any]],
        *,
        keep_last: int = DEFAULT_KEEP_LAST,
        max_tokens: int = 4000,
        summarizer: Any = None,  # callable(messages) -> str
    ) -> CompressionResult:
        """Compress conversation history.

        Args:
            messages: List of OpenAI-format messages.
            keep_last: Number of recent exchanges to keep verbatim.
            max_tokens: Target token budget for the compressed history.
            summarizer: Optional callable that takes old messages and returns
                a summary string. If None, a simple "[N messages compressed]"
                default summary is generated.

        Returns:
            CompressionResult with the new message list.
        """
        if not messages:
            return CompressionResult(
                messages=[], compressed_count=0, kept_count=0,
            )

        # Clamp keep_last.
        keep_last = max(0, min(keep_last, MAX_KEEP_LAST))

        # Estimate current token count (4 chars = 1 token approx).
        total_chars = sum(len(str(m.get("content", ""))) for m in messages)
        total_tokens = total_chars // 4

        if total_tokens <= max_tokens:
            # No compression needed.
            return CompressionResult(
                messages=messages,
                compressed_count=0,
                kept_count=len(messages),
                saved_tokens_est=0,
            )

        # Find the boundary: keep the last ``keep_last`` exchanges.
        # An "exchange" = user message + assistant response (+ optional tool calls).
        # Snaps backwards to nearest user message to preserve alternation.
        boundary = self._find_boundary(messages, keep_last)
        if boundary <= 0:
            # Can't compress (everything is in the "keep" window).
            return CompressionResult(
                messages=messages,
                compressed_count=0,
                kept_count=len(messages),
                saved_tokens_est=0,
            )

        # Split messages.
        old_messages = messages[:boundary]
        new_messages = messages[boundary:]

        # Generate summary.
        if summarizer is not None:
            try:
                summary = summarizer(old_messages)
            except Exception:
                summary = self._default_summary(old_messages)
        else:
            summary = self._default_summary(old_messages)

        # Build the compressed message list: summary + new messages.
        compressed_msg = {
            "role": "system",
            "content": f"[CONTEXT COMPACTION]\n{summary}",
        }
        result_messages = [compressed_msg] + new_messages

        # Estimate saved tokens.
        old_chars = sum(len(str(m.get("content", ""))) for m in old_messages)
        new_chars = len(str(compressed_msg["content"]))
        saved_tokens = max(0, (old_chars - new_chars) // 4)

        return CompressionResult(
            messages=result_messages,
            compressed_count=len(old_messages),
            kept_count=len(new_messages) + 1,  # +1 for summary
            summary=summary,
            saved_tokens_est=saved_tokens,
        )

    def _find_boundary(
        self, messages: list[dict[str, Any]], keep_last: int
    ) -> int:
        """Find the boundary index for compression.

        Walks backwards from the end, counting user messages. When we've seen
        ``keep_last`` user messages, the boundary is just before the first one.
        """
        user_count = 0
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                user_count += 1
                if user_count >= keep_last:
                    # Boundary is just after this user message's exchange.
                    # Find the start of this exchange (the user message itself).
                    return i
        # Not enough user messages — can't compress.
        return 0

    @staticmethod
    def _default_summary(messages: list[dict[str, Any]]) -> str:
        """Generate a default summary of the messages."""
        user_msgs = [m for m in messages if m.get("role") == "user"]
        assistant_msgs = [m for m in messages if m.get("role") == "assistant"]
        tool_msgs = [m for m in messages if m.get("role") == "tool"]

        parts = [
            f"Compressed {len(messages)} messages "
            f"({len(user_msgs)} user, {len(assistant_msgs)} assistant, {len(tool_msgs)} tool).",
        ]

        # Include first user message (context).
        if user_msgs:
            first_user = str(user_msgs[0].get("content", ""))[:200]
            parts.append(f"First request: {first_user}")

        # Include last assistant message (state).
        if assistant_msgs:
            last_assistant = str(assistant_msgs[-1].get("content", ""))[:200]
            parts.append(f"Last response: {last_assistant}")

        return "\n".join(parts)


class TrajectoryCompressor:
    """Compress agent trajectories for training data generation.

    Per Hermes ``trajectory_compressor.py``:
        - Protect first turns (system, human, first gpt, first tool)
        - Protect last N turns
        - Compress middle only, starting from 2nd tool response
        - Replaces compressed region with single human summary message
    """

    def __init__(
        self,
        *,
        target_max_tokens: int = 15250,
        summary_target_tokens: int = 750,
        protect_last_n_turns: int = 4,
    ) -> None:
        self._target_max = target_max_tokens
        self._summary_target = summary_target_tokens
        self._protect_last = protect_last_n_turns

    def compress(
        self,
        trajectory: list[dict[str, Any]],
        *,
        summarizer: Any = None,
    ) -> list[dict[str, Any]]:
        """Compress a trajectory to fit target token budget.

        Args:
            trajectory: List of messages (from/value format).
            summarizer: Optional callable that takes messages and returns summary.

        Returns:
            Compressed trajectory.
        """
        if len(trajectory) <= 6:
            # Too short to compress.
            return trajectory

        # Estimate tokens.
        total_chars = sum(
            len(str(m.get("value", m.get("content", ""))))
            for m in trajectory
        )
        total_tokens = total_chars // 4
        if total_tokens <= self._target_max:
            return trajectory

        # Protect head (first 4 turns) and tail (last N).
        head = trajectory[:4]
        tail_start = max(4, len(trajectory) - self._protect_last)
        tail = trajectory[tail_start:]
        middle = trajectory[4:tail_start]

        if not middle:
            return trajectory

        # Summarize the middle.
        if summarizer is not None:
            try:
                summary_text = summarizer(middle)
            except Exception:
                summary_text = self._default_summary(middle)
        else:
            summary_text = self._default_summary(middle)

        # Truncate summary if too long.
        max_summary_chars = self._summary_target * 4
        if len(summary_text) > max_summary_chars:
            summary_text = summary_text[:max_summary_chars] + "..."

        summary_msg = {
            "role": "user",
            "content": f"[COMPRESSED TRAJECTORY]\n{summary_text}",
            "from": "human",
            "value": f"[COMPRESSED TRAJECTORY]\n{summary_text}",
        }

        return head + [summary_msg] + tail

    @staticmethod
    def _default_summary(messages: list[dict[str, Any]]) -> str:
        """Generate a simple summary of the middle section."""
        tool_calls = sum(1 for m in messages if m.get("role") == "tool" or m.get("from") == "tool")
        assistant_msgs = [m for m in messages if m.get("role") == "assistant" or m.get("from") == "gpt"]

        parts = [
            f"Middle section: {len(messages)} messages "
            f"({len(assistant_msgs)} assistant, {tool_calls} tool calls).",
        ]

        # Include first assistant message.
        if assistant_msgs:
            first = str(assistant_msgs[0].get("value", assistant_msgs[0].get("content", "")))[:300]
            parts.append(f"First action: {first}")

        return "\n".join(parts)
