"""In-process fan-out hub for execution stream events (GAP 5).

Streams are observability, not storage: events published while no
subscriber is connected are dropped, and every subscriber owns an
unbounded queue drained by its own SSE generator. A per-execution
subscriber cap prevents a runaway dashboard from exhausting memory.
"""

from __future__ import annotations

import queue
import threading
from typing import Any


class ExecutionStreamHub:
    """Fan out JSON-safe event payloads to SSE subscribers."""

    def __init__(self, *, max_subscribers_per_execution: int = 4) -> None:
        if max_subscribers_per_execution < 1:
            raise ValueError("max_subscribers_per_execution must be positive")
        self._max = max_subscribers_per_execution
        self._lock = threading.Lock()
        self._queues: dict[str, list[queue.SimpleQueue[dict[str, Any]]]] = {}

    def subscribe(self, execution_id: str) -> queue.SimpleQueue[dict[str, Any]]:
        """Register a subscriber queue; raises LookupError over the cap."""
        q: queue.SimpleQueue[dict[str, Any]] = queue.SimpleQueue()
        with self._lock:
            subscribers = self._queues.setdefault(execution_id, [])
            if len(subscribers) >= self._max:
                raise LookupError(
                    f"execution {execution_id} already has {self._max} stream subscribers"
                )
            subscribers.append(q)
            return q

    def unsubscribe(self, execution_id: str, q: queue.SimpleQueue[dict[str, Any]]) -> None:
        with self._lock:
            subscribers = self._queues.get(execution_id)
            if subscribers is None:
                return
            try:
                subscribers.remove(q)
            except ValueError:
                return
            if not subscribers:
                self._queues.pop(execution_id, None)

    def publish(self, execution_id: str, payload: dict[str, Any]) -> int:
        """Deliver one payload to every current subscriber.

        Returns the number of subscribers that received it. Payloads are
        shared by reference; subscribers must treat them as read-only.
        """
        with self._lock:
            subscribers = list(self._queues.get(execution_id, ()))
        for q in subscribers:
            q.put(payload)
        return len(subscribers)

    def subscriber_count(self, execution_id: str) -> int:
        with self._lock:
            return len(self._queues.get(execution_id, ()))


__all__ = ["ExecutionStreamHub"]
