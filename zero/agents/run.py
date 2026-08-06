"""Zero v2 agent run lifecycle — ADR T-7.2.

Every run is a persistent record. Recovers or marks failed after restart.
No silent loss. Timeout enforced. Cancel anytime.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from zero.core.scope import Scope

__all__ = [
    "AgentRun",
    "AgentRunError",
    "AgentRunStatus",
]


class AgentRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


@dataclass(slots=True)
class AgentRun:
    """A single execution of an AgentDefinition.

    Persisted to ``dev_agent_runs`` table (for dev scope) for crash recovery.
    """

    agent_def_id: str
    launched_by: str  # user_id
    scope: Scope
    input_prompt: str
    task_id: str | None = None
    id: str = field(default_factory=lambda: f"run_{uuid.uuid4().hex[:16]}")
    status: AgentRunStatus = AgentRunStatus.PENDING
    output_text: str | None = None
    error_message: str | None = None
    cost_usd: float = 0.0
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    cancelled: bool = False

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            AgentRunStatus.COMPLETED,
            AgentRunStatus.FAILED,
            AgentRunStatus.CANCELLED,
            AgentRunStatus.TIMED_OUT,
        )

    def mark_started(self) -> None:
        self.status = AgentRunStatus.RUNNING
        self.started_at = datetime.now(UTC)

    def mark_completed(self, *, output: str, cost_usd: float) -> None:
        self.status = AgentRunStatus.COMPLETED
        self.output_text = output
        self.cost_usd = cost_usd
        self.completed_at = datetime.now(UTC)

    def mark_failed(self, *, error: str, cost_usd: float = 0.0) -> None:
        self.status = AgentRunStatus.FAILED
        self.error_message = error
        self.cost_usd = cost_usd
        self.completed_at = datetime.now(UTC)

    def mark_cancelled(self) -> None:
        self.status = AgentRunStatus.CANCELLED
        self.cancelled = True
        self.completed_at = datetime.now(UTC)

    def mark_timed_out(self) -> None:
        self.status = AgentRunStatus.TIMED_OUT
        self.completed_at = datetime.now(UTC)

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent_def_id": self.agent_def_id,
            "launched_by": self.launched_by,
            "scope": self.scope.retrieval_key(),
            "status": self.status.value,
            "cost_usd": self.cost_usd,
            "task_id": self.task_id,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


class AgentRunError(Exception):
    """Raised when an agent run fails."""
