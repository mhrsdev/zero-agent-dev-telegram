"""Zero v2 budget enforcement — ADR T-7.4.

Per-Project and per-Agent caps. Checked **before** each call (not after).
Over-cap halts execution. Kill switch. Cost read from Router response header
``x-zero-cost-usd`` (NOT computed locally — ADR 0004 §5 structural test).

80% threshold event published for warning.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from zero.core.errors import ErrorCode, ZeroError
from zero.core.events import Event, publish
from zero.core.scope import Scope

__all__ = [
    "Budget",
    "BudgetExceededError",
    "BudgetTracker",
    "BudgetWarning",
]


class BudgetExceededError(ZeroError):
    def __init__(self, message: str, *, internal: str | None = None) -> None:
        super().__init__(code=ErrorCode.BUDGET_EXCEEDED, message=message, internal=internal)


@dataclass
class BudgetWarning:
    """Warning emitted when budget crosses 80% threshold."""

    scope: Scope
    spent: float
    cap: float
    threshold: float


@dataclass(slots=True)
class Budget:
    """A single budget cap with current spend tracking."""

    cap_usd: float
    spent_usd: float = 0.0
    warning_threshold: float = 0.8  # 80%
    _warning_emitted: bool = field(default=False, init=False, repr=False)

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.cap_usd - self.spent_usd)

    @property
    def utilization(self) -> float:
        """0.0 to 1.0+ — fraction of cap consumed."""
        if self.cap_usd <= 0:
            return 1.0
        return self.spent_usd / self.cap_usd

    @property
    def is_exceeded(self) -> bool:
        return self.spent_usd >= self.cap_usd

    @property
    def is_warning(self) -> bool:
        return self.utilization >= self.warning_threshold and not self.is_exceeded

    def check_before_call(self, *, estimated_cost_usd: float = 0.0) -> None:
        """Raise :class:`BudgetExceededError` if budget would be exceeded.

        Called BEFORE every Router call. The ``estimated_cost_usd`` is the
        worst-case we expect this call to cost (usually 0 — we don't trust
        estimates and prefer post-hoc reconciliation).
        """
        if self.is_exceeded:
            raise BudgetExceededError(
                f"budget already exceeded: spent ${self.spent_usd:.4f} / cap ${self.cap_usd:.4f}"
            )
        # Don't pre-charge — just check the cap. The actual cost is recorded
        # from the Router response header.

    def record_spend(self, amount_usd: float, *, scope: Scope) -> None:
        """Record actual cost from Router response header.

        Per ADR T-7.4: cost is read from Router, never computed locally.
        """
        self.spent_usd += amount_usd

        # Emit warning event if crossed threshold (only once per budget).
        if self.is_warning and not self._warning_emitted:
            self._warning_emitted = True
            _emit_budget_event(
                name="agent.budget.warning",
                scope=scope,
                payload={
                    "spent": round(self.spent_usd, 4),
                    "cap": round(self.cap_usd, 4),
                    "utilization": round(self.utilization, 3),
                },
            )

        # Emit exceeded event if crossed cap.
        if self.is_exceeded:
            _emit_budget_event(
                name="agent.budget.exceeded",
                scope=scope,
                payload={
                    "spent": round(self.spent_usd, 4),
                    "cap": round(self.cap_usd, 4),
                },
            )


def _emit_budget_event(*, name: str, scope: Scope, payload: dict[str, Any]) -> None:
    """Emit an event safely — silently no-op if no event loop is running.

    We never want budget tracking to crash an agent because the event bus
    couldn't be reached.
    """
    try:
        import asyncio  # noqa: PLC0415

        loop = asyncio.get_running_loop()
        event = Event(name=name, scope=scope, payload=payload)
        asyncio.ensure_future(publish(event))
    except RuntimeError:
        # No running event loop — skip event publishing.
        # The budget threshold state is still recorded on the Budget object,
        # which is the source of truth for enforcement.
        pass


class BudgetTracker:
    """Tracks per-Project and per-Agent budgets."""

    def __init__(self) -> None:
        self._project_budgets: dict[str, Budget] = {}  # project_id -> Budget
        self._agent_budgets: dict[str, Budget] = {}    # agent_def_id -> Budget
        self._kill_switch: bool = False

    def set_project_budget(self, project_id: str, cap_usd: float) -> Budget:
        b = Budget(cap_usd=cap_usd)
        self._project_budgets[project_id] = b
        return b

    def set_agent_budget(self, agent_def_id: str, cap_usd: float) -> Budget:
        b = Budget(cap_usd=cap_usd)
        self._agent_budgets[agent_def_id] = b
        return b

    def get_project_budget(self, project_id: str) -> Budget | None:
        return self._project_budgets.get(project_id)

    def get_agent_budget(self, agent_def_id: str) -> Budget | None:
        return self._agent_budgets.get(agent_def_id)

    def check(
        self,
        *,
        scope: Scope,
        agent_def_id: str | None = None,
    ) -> None:
        """Check both project and agent budgets before a call.

        Raises :class:`BudgetExceededError` if either is exceeded.
        """
        if self._kill_switch:
            raise BudgetExceededError("kill switch active — all Router calls blocked")

        if scope.is_development() and scope.project_id is not None:
            pb = self._project_budgets.get(scope.project_id)
            if pb is not None:
                pb.check_before_call()

        if agent_def_id is not None:
            ab = self._agent_budgets.get(agent_def_id)
            if ab is not None:
                ab.check_before_call()

    def record(
        self,
        *,
        amount_usd: float,
        scope: Scope,
        agent_def_id: str | None = None,
    ) -> None:
        """Record actual spend from Router response header."""
        if scope.is_development() and scope.project_id is not None:
            pb = self._project_budgets.get(scope.project_id)
            if pb is not None:
                pb.record_spend(amount_usd, scope=scope)
        if agent_def_id is not None:
            ab = self._agent_budgets.get(agent_def_id)
            if ab is not None:
                ab.record_spend(amount_usd, scope=scope)

    def activate_kill_switch(self) -> None:
        """Block all Router calls immediately. Use for incidents."""
        self._kill_switch = True

    def deactivate_kill_switch(self) -> None:
        self._kill_switch = False
