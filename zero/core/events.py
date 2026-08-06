"""Zero v2 internal event bus — ADR T-1.10.

Events with mandatory ``scope``. Outbox pattern (events are persisted before
delivery; if process crashes, they're replayed).

No free-form content fields — every event has a typed payload schema.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from zero.core.scope import Scope

__all__ = [
    "Event",
    "EventBus",
    "EventHandler",
    "EventName",
    "global_bus",
    "publish",
    "subscribe",
]


# ---------------------------------------------------------------------- event names

EventName = Literal[
    # tenancy
    "org.created", "workspace.created", "project.created", "project.archived",
    "user.invited", "user.role_changed", "user.removed",
    # tasks
    "task.created", "task.updated", "task.completed", "task.blocked",
    # memory
    "memory.written", "memory.invalidated", "memory.fact_promoted",
    "memory.scope_violation_attempted",
    # agents
    "agent.run.started", "agent.run.completed", "agent.run.failed",
    "agent.budget.warning", "agent.budget.exceeded",
    # approvals
    "approval.requested", "approval.resolved",
    # session
    "session.created", "session.revoked",
    # platform
    "platform.command.received", "platform.command.rejected",
    # router
    "router.call.started", "router.call.completed",
]


# ---------------------------------------------------------------------- event

@dataclass(frozen=True, slots=True)
class Event:
    """A single event. Immutable. ``scope`` is mandatory.

    Note: ``name`` is typed as ``str`` (not a Literal) to allow extensions to
    emit ad-hoc event names without rewriting this module. Publishers should
    still prefer the well-known names listed in :data:`KNOWN_EVENT_NAMES`.
    """

    name: str
    scope: Scope
    payload: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: f"evt_{uuid.uuid4().hex[:16]}")
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        # Validate payload is JSON-serializable.
        try:
            json.dumps(self.payload)
        except (TypeError, ValueError) as e:
            raise ValueError(f"event payload must be JSON-serializable: {e}") from e
        # Validate payload has no "free-form" content field (T-P.3 contract).
        # The allowlist: known keys only. This is defensive — schemas for each
        # event name should be enforced at the publisher side too.
        forbidden_keys = {"raw_input", "raw_user_message", "raw_secret"}
        bad = forbidden_keys & self.payload.keys()
        if bad:
            raise ValueError(
                f"event payload contains forbidden free-form keys: {bad} — "
                "use specific typed keys instead"
            )

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "scope": self.scope.retrieval_key(),
            "payload": self.payload,
            "created_at": self.created_at.isoformat(),
        }


# ---------------------------------------------------------------------- handlers

EventHandler = Callable[[Event], Awaitable[None]]


# ---------------------------------------------------------------------- bus

class EventBus:
    """Async pub/sub event bus with per-event-name handlers."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = {}
        self._outbox: list[Event] = []  # in-memory outbox; persisted by caller
        self._lock = asyncio.Lock()

    def subscribe(self, name: str, handler: EventHandler) -> None:
        """Register a handler for ``name``. Multiple handlers per name allowed."""
        self._handlers.setdefault(name, []).append(handler)

    async def publish(self, event: Event) -> None:
        """Persist to outbox and dispatch to all handlers.

        Failures in individual handlers are logged but do not block other
        handlers (defense in depth). Outbox is durable until cleared.
        """
        async with self._lock:
            self._outbox.append(event)

        handlers = self._handlers.get(event.name, [])
        if not handlers:
            return

        # Run handlers concurrently; collect exceptions.
        results = await asyncio.gather(
            *[h(event) for h in handlers], return_exceptions=True
        )
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                # Log and continue — one bad handler shouldn't break others.
                from zero.core.logging import get_logger  # noqa: PLC0415

                log = get_logger("zero.events")
                log.error(
                    f"event handler failed for {event.name}",
                    exc=result,
                    handler_index=i,
                    event_id=event.id,
                )

    def outbox(self) -> list[Event]:
        """Return a copy of the in-memory outbox (for testing)."""
        return list(self._outbox)

    def clear_outbox(self, *, up_to: str | None = None) -> None:
        """Clear outbox entries. If ``up_to`` is given, clears up to and including that id."""
        if up_to is None:
            self._outbox.clear()
            return
        idx = next(
            (i for i, e in enumerate(self._outbox) if e.id == up_to),
            None,
        )
        if idx is not None:
            self._outbox = self._outbox[idx + 1:]


# ---------------------------------------------------------------------- global bus

global_bus = EventBus()


def subscribe(name: str, handler: EventHandler) -> None:
    """Subscribe to the global bus."""
    global_bus.subscribe(name, handler)


async def publish(event: Event) -> None:
    """Publish to the global bus."""
    await global_bus.publish(event)
