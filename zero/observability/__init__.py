"""Zero v2 observability — Phase 9 + 10.

Health computation, metrics, telemetry (default OFF).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from zero.platform import Capability, CapabilityState, HealthReport, HealthStatus

__all__ = [
    "INSTALLATION_ID_KEY",
    "MetricsCounter",
    "MetricsStore",
    "TelemetryPayload",
    "build_health_report",
]


# ---------------------------------------------------------------------- metrics

@dataclass(slots=True)
class MetricsCounter:
    """A single counter with optional dimensions."""

    name: str
    value: int = 0
    dimensions: dict[str, str] = field(default_factory=dict)
    last_updated: datetime = field(default_factory=lambda: datetime.now(UTC))

    def inc(self, *, amount: int = 1) -> None:
        self.value += amount
        self.last_updated = datetime.now(UTC)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "dimensions": dict(self.dimensions),
            "last_updated": self.last_updated.isoformat(),
        }


class MetricsStore:
    """In-memory metrics store with allowlist enforcement.

    Per Hermes observability pattern: only allowlisted counters are tracked.
    Dimensions are validated.
    """

    ALLOWED_COUNTERS = frozenset({
        "router.calls.total",
        "router.calls.success",
        "router.calls.failure",
        "router.tokens.input",
        "router.tokens.output",
        "router.cost.usd",
        "agent.runs.started",
        "agent.runs.completed",
        "agent.runs.failed",
        "approval.requests.total",
        "approval.requests.approved",
        "approval.requests.rejected",
        "memory.writes.total",
        "memory.reads.total",
        "telegram.messages.received",
        "telegram.messages.sent",
        "telegram.commands.executed",
    })

    def __init__(self) -> None:
        self._counters: dict[tuple[str, str], MetricsCounter] = {}

    def inc(
        self,
        name: str,
        *,
        amount: int = 1,
        dimensions: Mapping[str, str] | None = None,
    ) -> None:
        if name not in self.ALLOWED_COUNTERS:
            return  # silently ignore non-allowlisted counters
        dim_key = self._dim_key(dimensions or {})
        key = (name, dim_key)
        if key not in self._counters:
            self._counters[key] = MetricsCounter(
                name=name, dimensions=dict(dimensions or {})
            )
        self._counters[key].inc(amount=amount)

    def snapshot(self) -> list[MetricsCounter]:
        return list(self._counters.values())

    def reset(self) -> None:
        self._counters.clear()

    @staticmethod
    def _dim_key(dimensions: Mapping[str, str]) -> str:
        return "|".join(f"{k}={v}" for k, v in sorted(dimensions.items()))


# ---------------------------------------------------------------------- telemetry

INSTALLATION_ID_KEY = "installation_id"

@dataclass(frozen=True, slots=True)
class TelemetryPayload:
    """Per T-10.2: payload has exactly 6 keys. Default OFF."""

    installation_id: str
    version: str
    mode: str  # 'personal' / 'normal' / 'dev' aggregate (most-used)
    capabilities_hash: str
    uptime_seconds: int
    error_count_24h: int

    def to_dict(self) -> dict[str, Any]:
        return {
            INSTALLATION_ID_KEY: self.installation_id,
            "version": self.version,
            "mode": self.mode,
            "capabilities_hash": self.capabilities_hash,
            "uptime_seconds": self.uptime_seconds,
            "error_count_24h": self.error_count_24h,
        }


# ---------------------------------------------------------------------- health

def build_health_report(
    *,
    version: str,
    capabilities: list[Capability],
    checks: Mapping[str, bool] | None = None,
) -> HealthReport:
    """Compute health from capabilities + ad-hoc checks.

    Per T-9.4: ``healthy|degraded|warning|critical|unknown``.
    ``offline`` is Platform-side — Zero never reports it.
    """
    cap_hash = _compute_capability_hash(capabilities)

    if not capabilities:
        return HealthReport(
            status=HealthStatus.UNKNOWN,
            capabilities_hash=cap_hash,
            version=version,
            detail={"reason": "no capabilities registered"},
        )

    states = [c.state for c in capabilities]
    has_unavailable = CapabilityState.UNAVAILABLE in states
    has_degraded = CapabilityState.DEGRADED in states
    has_unknown = CapabilityState.UNKNOWN in states
    all_available = all(s is CapabilityState.AVAILABLE for s in states)

    # Run ad-hoc checks (DB ping, Router ping, etc.)
    failed_checks = [name for name, ok in (checks or {}).items() if not ok]

    if has_unavailable or failed_checks:
        status = HealthStatus.CRITICAL
    elif has_degraded:
        status = HealthStatus.DEGRADED
    elif has_unknown:
        status = HealthStatus.WARNING
    elif all_available:
        status = HealthStatus.HEALTHY
    else:
        status = HealthStatus.UNKNOWN

    return HealthReport(
        status=status,
        capabilities_hash=cap_hash,
        version=version,
        detail={
            "failed_checks": failed_checks,
            "capability_count": len(capabilities),
            "unavailable_count": sum(1 for s in states if s is CapabilityState.UNAVAILABLE),
            "degraded_count": sum(1 for s in states if s is CapabilityState.DEGRADED),
        },
    )


def _compute_capability_hash(capabilities: list[Capability]) -> str:
    """Stable hash of capabilities (re-exported from zero.platform)."""
    from zero.platform import compute_capability_hash  # noqa: PLC0415

    return compute_capability_hash(capabilities)
