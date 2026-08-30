"""Live Telegram streaming renderer (Hermes stream-consumer parity, gap A+B).

The gap this closes, in one sentence: Hermes renders the agent's answer
INTO a Telegram message that is opened once and progressively edited as
tokens and tool calls arrive ("edit-in-place" streaming), while Zero's
historical chat path stayed silent for the whole generation and then
dropped one final message — so a 60-second answer looked dead and every
tool call was invisible.

Parity mechanisms ported from the reference gateway:

- **Throttled edit-in-place**: frames are flushed at most every
  ``min_edit_interval`` seconds (reference: ~1.2s between edits) so a
  fast token stream does not trip Bot API flood control (~1 edit/0.8s
  sustained can earn 200s penalties);
- **Saturation dedup**: past the 4096 UTF-16 preview cap every
  progressive frame truncates to the same text — re-sending is a visual
  no-op that still burns flood budget (reference ``_last_overflow_preview``),
  so identical saturated frames are skipped silently;
- **Tool progress lines (Strategy B)**: tool status lines render BELOW
  the accumulated text separated by a horizontal rule; real text
  overwrites them on the next delta; finalize sends ONLY the text
  (tool chrome is presentation, never persisted content);
- **Finalize with overflow split**: the final edit carries the full
  rendered content; when it exceeds one message, the first chunk edits
  the preview IN PLACE and the remaining chunks go out as follow-up
  messages (no duplicate preview, reference ``_edit_overflow_split``);
- **Never raises**: streaming is observability. Every failure is logged
  and swallowed — a broken preview must never fail the turn itself.

This module is transport-honest: it uses the SAME adapter/credential
boundary every other outbound path uses (no token ever persists here).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from zero.adapters.telegram_render import chunk_telegram_text, utf16_len

logger = logging.getLogger(__name__)

#: Minimum seconds between progressive edits (flood-control guard).
_MIN_EDIT_INTERVAL = 2.0
#: Live preview cap: edits truncate to this many UTF-16 units; the full
#: content is delivered at finalize via the overflow split.
_PREVIEW_CAP = 3800
#: Tool progress line cap (characters) — one bounded line per call.
_TOOL_LINE_LIMIT = 160
#: Maximum tool lines retained for the compose frame.
_MAX_TOOL_LINES = 6
#: Tool summary footer kept on finalize when tools ran.
_TOOL_SUMMARY_LIMIT = 6


def _truncate_preview(text: str) -> str:
    """UTF-16-bounded preview text with an explicit continuation marker."""
    if utf16_len(text) <= _PREVIEW_CAP:
        return text
    # Binary-search a safe prefix in UTF-16 units (chunking by codepoints
    # can overshoot the unit budget with astral characters).
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if utf16_len(text[:mid]) <= _PREVIEW_CAP - 20:
            low = mid
        else:
            high = mid - 1
    return text[:low].rstrip() + "\n\n…"


def _tool_line(name: str, arguments: Any) -> str:
    """One compact tool-call status line (Hermes ToolCallChunk parity)."""
    preview = ""
    if isinstance(arguments, dict) and arguments:
        parts: list[str] = []
        for key, value in list(arguments.items())[:3]:
            rendered = str(value).replace("\n", " ")
            if len(rendered) > 60:
                rendered = rendered[:57] + "…"
            parts.append(f"{key}={rendered}")
        preview = " ".join(parts)
    elif isinstance(arguments, str) and arguments.strip():
        preview = arguments.strip().replace("\n", " ")[:80]
    line = f"🔧 {name}({preview})" if preview else f"🔧 {name}()"
    return line[:_TOOL_LINE_LIMIT]


class TelegramLiveStream:
    """One live Telegram bubble: opened once, edited as content arrives.

    The renderer owns NO content authority: the final answer is always
    delivered by the caller's durable send path (chunked HTML); the live
    bubble is presentation state that converges to the final content.
    """

    def __init__(
        self,
        *,
        adapter: Any,
        chat_id: str,
        topic_id: str | None = None,
        header: str = "✍️ …",
        min_edit_interval: float = _MIN_EDIT_INTERVAL,
        sleeper=time.monotonic,
    ) -> None:
        self._adapter = adapter
        self._chat_id = str(chat_id)
        self._topic_id = topic_id
        self._header = header or "✍️ …"
        self._min_edit_interval = max(0.2, float(min_edit_interval))
        self._sleeper = sleeper
        self._lock = threading.Lock()
        self._message_id: str | None = None
        self._text = ""
        self._tool_lines: list[str] = []
        self._last_edit_at = 0.0
        self._last_frame: str | None = None
        self._finalized = False

    # ------------------------------------------------------------------
    # Event handlers (called from the streaming callbacks)
    # ------------------------------------------------------------------

    def on_text_delta(self, delta: str) -> None:
        """Append one text delta and schedule a throttled frame."""
        if not delta:
            return
        with self._lock:
            self._text += delta
            self._tool_lines.clear()
            self._flush_locked(force=False)

    def on_text_reset(self) -> None:
        """Drop accumulated text (text-protocol tool-call noise)."""
        with self._lock:
            self._text = ""
            self._flush_locked(force=False)

    def on_tool_call(self, name: str, arguments: Any) -> None:
        """Show one tool invocation as a progress line."""
        with self._lock:
            self._tool_lines.append(_tool_line(name, arguments))
            del self._tool_lines[:-_MAX_TOOL_LINES]
            self._flush_locked(force=True)

    def on_tool_result(self, name: str, ok: bool, detail: str = "") -> None:
        """Replace the newest pending tool line with its settled state."""
        mark = "✅" if ok else "⚠️"
        suffix = f" — {detail[:80]}" if detail else ""
        with self._lock:
            for index in range(len(self._tool_lines) - 1, -1, -1):
                if self._tool_lines[index].startswith(f"🔧 {name}("):
                    self._tool_lines[index] = (
                        f"{mark} {name} done{suffix}"[:_TOOL_LINE_LIMIT]
                    )
                    break
            self._flush_locked(force=False)

    # ------------------------------------------------------------------
    # Frame composition + delivery
    # ------------------------------------------------------------------

    def _compose_frame_locked(self) -> str:
        """Strategy B: accumulated text, then tool lines under a rule."""
        body = self._text or self._header
        if self._tool_lines:
            return body + "\n\n---\n" + "\n".join(self._tool_lines)
        return body

    def _flush_locked(self, *, force: bool) -> None:
        if self._finalized:
            return
        now = self._sleeper()
        if not force and (now - self._last_edit_at) < self._min_edit_interval:
            return
        frame = _truncate_preview(self._compose_frame_locked())
        if frame == self._last_frame:
            return
        self._open_locked()
        if self._message_id is None:
            return
        try:
            self._adapter.edit_message(
                chat_id=self._chat_id,
                message_id=self._message_id,
                text=frame,
                topic_id=self._topic_id,
            )
        except Exception as exc:  # noqa: BLE001 - preview must never fail the turn
            logger.debug("live stream edit skipped: %s", type(exc).__name__)
            return
        self._last_edit_at = now
        self._last_frame = frame

    def _open_locked(self) -> None:
        """Send the preview message once; subsequent frames edit it."""
        if self._message_id is not None:
            return
        try:
            response = self._adapter.send_message(
                chat_id=self._chat_id,
                text=self._header,
                topic_id=self._topic_id,
            )
            payload: Any = None
            try:
                payload = response.json() if callable(getattr(response, "json", None)) else None
            except Exception:  # noqa: BLE001
                payload = None
            if isinstance(payload, dict):
                result = payload.get("result")
                if isinstance(result, dict) and result.get("message_id") is not None:
                    self._message_id = str(result["message_id"])
                    logger.info(
                        "chat live stream opened (chat=%s message_id=%s) — "
                        "edits will stream the answer in place",
                        self._chat_id,
                        self._message_id,
                    )
        except Exception as exc:  # noqa: BLE001
            logger.info("live stream preview send failed: %s", type(exc).__name__)

    # ------------------------------------------------------------------
    # Finalize
    # ------------------------------------------------------------------

    def finalize(self, final_text: str, *, tool_names: list[str] | None = None) -> bool:
        """Converge the bubble to the final answer (Hermes overflow split).

        The preview message is edited IN PLACE to the first chunk of the
        final answer; when the answer spans multiple messages the
        remaining chunks go out as follow-up messages (exactly one
        preview, no duplicate first message). When the preview bubble
        never opened (send failed earlier), finalize does nothing and
        returns ``False`` so the caller can fall back to its own durable
        send path.
        """
        with self._lock:
            self._finalized = True
            message_id = self._message_id
        if message_id is None:
            return False
        footer = ""
        if tool_names:
            unique: list[str] = []
            for name in tool_names:
                if name not in unique:
                    unique.append(name)
            shown = ", ".join(f"{name}" for name in unique[:_TOOL_SUMMARY_LIMIT])
            more = (
                f" +{len(unique) - _TOOL_SUMMARY_LIMIT}"
                if len(unique) > _TOOL_SUMMARY_LIMIT
                else ""
            )
            footer = f"\n\n🛠 tools used: {shown}{more}"
        body = (final_text or "").strip()
        if not body:
            body = "(the model returned an empty answer)"
        chunks = chunk_telegram_text(body + footer, with_indicators=True)
        if not chunks:
            chunks = ["(the model returned an empty answer)"]
        try:
            self._adapter.edit_message(
                chat_id=self._chat_id,
                message_id=message_id,
                text=chunks[0],
                topic_id=self._topic_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("live stream finalize edit failed: %s", type(exc).__name__)
            return False
        for chunk in chunks[1:]:
            try:
                self._adapter.send_message(
                    chat_id=self._chat_id,
                    text=chunk,
                    topic_id=self._topic_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("live stream finalize continuation failed: %s", type(exc).__name__)
        return True

    @property
    def message_id(self) -> str | None:
        with self._lock:
            return self._message_id


class TelegramExecutionProgress:
    """Live per-execution progress bubble for the task graph (gap C).

    One message per execution: a header with the execution id, one line
    per task (⏳ pending → 🔧 running → ✅/⚠️ terminal), and a bounded
    live tail of the CURRENT task's streamed text/tool activity. The
    final durable execution summary is delivered by the result-delivery
    queue exactly as before — this bubble is the live view BETWEEN ticks.

    Created lazily: the message is sent on the FIRST event, so quiet
    ticks never spam the chat. Never raises. Thread-safe.
    """

    def __init__(
        self,
        *,
        adapter: Any,
        chat_id: str,
        topic_id: str | None = None,
        min_edit_interval: float = _MIN_EDIT_INTERVAL,
        sleeper=time.monotonic,
    ) -> None:
        self._chat_id = str(chat_id)
        self._topic_id = topic_id
        self._min_edit_interval = max(0.2, float(min_edit_interval))
        self._sleeper = sleeper
        self._lock = threading.Lock()
        self._adapter = adapter
        self._message_id: str | None = None
        self._tasks: dict[str, dict[str, str]] = {}
        self._task_order: list[str] = []
        self._current_tail = ""
        self._current_tool: list[str] = []
        self._last_edit_at = 0.0
        self._last_frame: str | None = None
        self._closed = False

    # ------------------------------------------------------------------
    # Task lifecycle events (from AgentRuntime task_event_callback)
    # ------------------------------------------------------------------

    def on_task_started(self, task_id: str, objective: str) -> None:
        with self._lock:
            self._tasks[task_id] = {"state": "running", "objective": objective}
            if task_id not in self._task_order:
                self._task_order.append(task_id)
            self._current_tail = ""
            self._current_tool = []
            self._flush_locked(force=True)

    def on_task_finished(self, task_id: str, state: str, detail: str = "") -> None:
        mark = {"completed": "✅", "failed": "⚠️", "cancelled": "🚫"}.get(state, "·")
        with self._lock:
            entry = self._tasks.setdefault(task_id, {"objective": ""})
            entry["state"] = f"{mark} {state}"
            if detail:
                entry["detail"] = detail[:200]
            if task_id in self._task_order:
                self._task_order.remove(task_id)
            self._task_order.append(task_id)
            self._flush_locked(force=True)

    # ------------------------------------------------------------------
    # Stream events (from the provider stream_observer of the CURRENT task)
    # ------------------------------------------------------------------

    def on_stream_event(self, payload: dict) -> None:
        if not isinstance(payload, dict):
            return
        kind = payload.get("type")
        with self._lock:
            if kind == "text_delta":
                text = str(payload.get("text") or "")
                if text:
                    self._current_tail = (self._current_tail + text)[-1200:]
                    self._current_tool.clear()
                    self._flush_locked(force=False)
            elif kind == "tool_call":
                line = _tool_line(str(payload.get("name") or "?"), payload.get("arguments"))
                self._current_tool.append(line)
                del self._current_tool[:-_MAX_TOOL_LINES]
                self._flush_locked(force=True)

    # ------------------------------------------------------------------
    # Frame composition + delivery
    # ------------------------------------------------------------------

    def _compose_frame_locked(self) -> str:
        lines = ["⚙️ **Execution progress**"]
        for task_id in self._task_order[-8:]:
            entry = self._tasks.get(task_id, {})
            objective = str(entry.get("objective") or "").strip().replace("\n", " ")
            if len(objective) > 110:
                objective = objective[:107] + "…"
            state = entry.get("state", "⏳ pending")
            line = f"{state}: {objective}" if objective else f"{state} ({task_id[:12]})"
            detail = entry.get("detail")
            if detail:
                line += f"\n    ↳ {detail}"
            lines.append(line)
        if self._task_order:
            lines.append("")
        if self._current_tool:
            lines.extend(self._current_tool[-2:])
        elif self._current_tail:
            tail = self._current_tail.strip()
            if tail:
                lines.append(f"…{tail[-400:]}")
        return "\n".join(lines)

    def _flush_locked(self, *, force: bool) -> None:
        if self._closed:
            return
        now = self._sleeper()
        if not force and (now - self._last_edit_at) < self._min_edit_interval:
            return
        frame = _truncate_preview(self._compose_frame_locked())
        if frame == self._last_frame:
            return
        if self._message_id is None:
            try:
                response = self._adapter.send_message(
                    chat_id=self._chat_id,
                    text=frame,
                    topic_id=self._topic_id,
                )
                payload: Any = None
                try:
                    payload = (
                        response.json() if callable(getattr(response, "json", None)) else None
                    )
                except Exception:  # noqa: BLE001
                    payload = None
                if isinstance(payload, dict):
                    result = payload.get("result")
                    if isinstance(result, dict) and result.get("message_id") is not None:
                        self._message_id = str(result["message_id"])
                        logger.info(
                            "execution progress bubble opened (chat=%s "
                            "message_id=%s) — task events will stream here",
                            self._chat_id,
                            self._message_id,
                        )
            except Exception as exc:  # noqa: BLE001
                logger.info("execution progress open failed: %s", type(exc).__name__)
                return
        else:
            try:
                self._adapter.edit_message(
                    chat_id=self._chat_id,
                    message_id=self._message_id,
                    text=frame,
                    topic_id=self._topic_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("execution progress edit skipped: %s", type(exc).__name__)
                return
        self._last_edit_at = now
        self._last_frame = frame

    def close(self) -> None:
        with self._lock:
            self._closed = True


__all__ = ["TelegramLiveStream", "TelegramExecutionProgress"]
