"""Health and readiness domain types.

Phase 1 only needs a tiny health/readiness contract. The real depth
(database connectivity, dependency reachability, migration state) grows
in later milestones. The shape is stable from day one so callers and
tests can rely on it.

Per ``zero-modular-bootstrap`` §"One executable path is a design asset":
the smoke test starts the same application entry point intended for
later deployment, using isolated configuration and persistence. The
health endpoint is therefore not a mock — it exercises the real
persistence layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

HealthStatus = Literal["ok", "degraded", "down"]


@dataclass(frozen=True)
class HealthReport:
    """Stable health/readiness report.

    Attributes:
        status: overall status. ``ok`` means the process can serve
            requests. ``degraded`` means it can serve but some
            dependency is unhealthy. ``down`` means it cannot serve.
        version: the running application version, for diagnostics.
        environment: the runtime environment (development, test,
            production). Useful for operators to confirm they are
            looking at the right deployment.
        database: status of the database connection. ``ok`` if a
            trivial query succeeded; ``down`` otherwise.
        migration_count: number of applied schema migrations. ``None``
            if the count could not be determined.
    """

    status: HealthStatus
    version: str
    environment: str
    database: HealthStatus
    migration_count: int | None

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "version": self.version,
            "environment": self.environment,
            "database": self.database,
            "migration_count": self.migration_count,
        }


def aggregate_status(*parts: HealthStatus) -> HealthStatus:
    """Combine multiple component statuses into one overall status.

    Rules:
    - If any part is ``down``, the result is ``down``.
    - Else if any part is ``degraded``, the result is ``degraded``.
    - Else ``ok``.
    """
    if not parts:
        return "ok"
    if any(part == "down" for part in parts):
        return "down"
    if any(part == "degraded" for part in parts):
        return "degraded"
    return "ok"
