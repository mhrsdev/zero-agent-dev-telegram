"""Health service — application operation that builds a HealthReport.

This is intentionally a small, pure-Python class. It depends on the
persistence interface (the :class:`Database` from
:mod:`zero.persistence.connection`) and on a callable that returns
the count of applied migrations. Both are injected so tests can
substitute fakes if needed (though Phase 1 tests use the real
database).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from zero.domain.health import HealthReport, HealthStatus, aggregate_status
from zero.persistence.connection import Database


class HealthService:
    """Builds :class:`HealthReport` instances on demand.

    Args:
        version: application version string.
        environment: runtime environment name (development, test,
            production).
        database: the :class:`Database` to ping.
        migration_counter: callable that returns the number of applied
            migrations. Injected so this class does not import the
            migration runner directly.
    """

    def __init__(
        self,
        *,
        version: str,
        environment: str,
        database: Database,
        migration_counter: Callable[[Database], int],
    ) -> None:
        self._version = version
        self._environment = environment
        self._database = database
        self._migration_counter = migration_counter

    def report(self) -> HealthReport:
        db_status = self._ping_database()
        try:
            migration_count = self._migration_counter(self._database)
        except (OSError, RuntimeError, sqlite3.Error):
            migration_count = None
        overall = aggregate_status(db_status)
        if migration_count is None:
            overall = aggregate_status(overall, "degraded")
        return HealthReport(
            status=overall,
            version=self._version,
            environment=self._environment,
            database=db_status,
            migration_count=migration_count,
        )

    def _ping_database(self) -> HealthStatus:
        try:
            ok = self._database.ping()
        except (OSError, RuntimeError, sqlite3.Error):
            return "down"
        return "ok" if ok else "down"
