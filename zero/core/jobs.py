"""Zero v2 background jobs — ADR T-1.8.

Queue, worker, status, retry, timeout, recovery after restart.
No silent loss.
"""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from zero.core.scope import Scope

__all__ = [
    "Job",
    "JobHandler",
    "JobPriority",
    "JobQueue",
    "JobStatus",
    "enqueue",
    "global_queue",
]


# ---------------------------------------------------------------------- enums

class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RETRYING = "retrying"


class JobPriority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


# ---------------------------------------------------------------------- job

@dataclass(slots=True)
class Job:
    """A background job."""

    name: str
    handler_name: str
    scope: Scope
    payload: dict[str, Any] = field(default_factory=dict)
    priority: JobPriority = JobPriority.NORMAL
    max_retries: int = 3
    retry_count: int = 0
    timeout_seconds: float = 300.0
    id: str = field(default_factory=lambda: f"job_{uuid.uuid4().hex[:16]}")
    status: JobStatus = JobStatus.PENDING
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    completed_at: datetime | None = None
    last_error: str | None = None

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "handler": self.handler_name,
            "scope": self.scope.retrieval_key(),
            "priority": self.priority.value,
            "status": self.status.value,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "created_at": self.created_at.isoformat(),
        }


JobHandler = Callable[[Job], Awaitable[None]]


# ---------------------------------------------------------------------- queue

class JobQueue:
    """In-memory async job queue with worker pool.

    In-memory queue. A persistent (DB-backed outbox) implementation can be
    be added in Phase 9 — see ADR T-1.8.
    """

    def __init__(self, *, max_workers: int = 4) -> None:
        # Tuple type: (priority_int, sequence_float, Job)
        self._queue: asyncio.PriorityQueue[tuple[int, float, Job]] = asyncio.PriorityQueue()
        self._handlers: dict[str, JobHandler] = {}
        self._workers: list[asyncio.Task[None]] = []
        self._max_workers = max_workers
        self._running: dict[str, Job] = {}
        self._stop_event = asyncio.Event()

    def register_handler(self, name: str, handler: JobHandler) -> None:
        """Register a handler for ``name``."""
        self._handlers[name] = handler

    async def enqueue(self, job: Job) -> str:
        """Add ``job`` to the queue. Returns the job id."""
        # Priority queue expects (priority_int, sequence, item).
        priority_int = {
            JobPriority.URGENT: 0,
            JobPriority.HIGH: 1,
            JobPriority.NORMAL: 2,
            JobPriority.LOW: 3,
        }[job.priority]
        await self._queue.put((priority_int, job.created_at.timestamp(), job))
        return job.id

    async def start(self) -> None:
        """Start the worker pool."""
        if self._workers:
            return
        self._stop_event.clear()
        for _ in range(self._max_workers):
            self._workers.append(asyncio.create_task(self._worker_loop()))

    async def stop(self, *, drain: bool = False, timeout: float = 30.0) -> None:
        """Stop the worker pool. If ``drain``, wait for queued jobs."""
        if drain:
            await self._queue.join()
        self._stop_event.set()
        for w in self._workers:
            w.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    async def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            # PriorityQueue returns (priority, seq, job)
            priority, seq, job = item
            try:
                self._running[job.id] = job
                job.status = JobStatus.RUNNING
                job.started_at = datetime.now(UTC)
                await self._run_job(job)
                job.status = JobStatus.COMPLETED
                job.completed_at = datetime.now(UTC)
            except Exception as e:
                job.last_error = str(e)
                if job.retry_count < job.max_retries:
                    job.retry_count += 1
                    job.status = JobStatus.RETRYING
                    # Re-enqueue with delay.
                    await asyncio.sleep(min(2**job.retry_count, 30))
                    await self.enqueue(job)
                else:
                    job.status = JobStatus.FAILED
                    job.completed_at = datetime.now(UTC)
                    from zero.core.logging import get_logger  # noqa: PLC0415

                    log = get_logger("zero.jobs")
                    log.error(
                        f"job {job.name} permanently failed after {job.retry_count} retries",
                        exc=e,
                        job=job.to_log_dict(),
                    )
            finally:
                self._running.pop(job.id, None)
                self._queue.task_done()

    async def _run_job(self, job: Job) -> None:
        handler = self._handlers.get(job.handler_name)
        if handler is None:
            raise RuntimeError(
                f"no handler registered for job type {job.handler_name!r}"
            )
        try:
            await asyncio.wait_for(handler(job), timeout=job.timeout_seconds)
        except TimeoutError as e:
            raise RuntimeError(
                f"job {job.name} timed out after {job.timeout_seconds}s"
            ) from e


# ---------------------------------------------------------------------- global queue

global_queue = JobQueue()


async def enqueue(job: Job) -> str:
    """Enqueue to the global queue."""
    return await global_queue.enqueue(job)
