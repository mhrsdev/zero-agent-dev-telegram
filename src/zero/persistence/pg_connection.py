"""PostgreSQL connection management behind the SQLite ``Database`` shape (GAP 2).

The repositories are SQL-string based against a ``sqlite3.Connection``
-like surface. :class:`PostgresDatabase` keeps that contract:

- pooled connections (``psycopg_pool``, min/max from settings);
- ``connect()`` / ``transaction()`` / ``ping()`` / ``close()`` mirror
  the SQLite facade, including thread-local transaction binding and
  SAVEPOINT nesting;
- statements pass through :mod:`zero.persistence.dialect`
  translation; results are dict rows (``row["col"]`` like sqlite3.Row);
- psycopg failures are re-raised as the matching ``sqlite3`` exception
  types so repository error handling is backend-agnostic.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from zero.config import Settings

logger = logging.getLogger(__name__)


class DatabaseError(RuntimeError):
    """Typed persistence failure (same shape as the SQLite module)."""


def _translate(sql: str) -> str:
    from zero.persistence.dialect import translate_dml

    return translate_dml(sql)


class _PGRow(dict):
    """Dict row that also supports positional access like sqlite3.Row."""

    _order: tuple[str, ...] = ()

    def __getitem__(self, key):
        if isinstance(key, int):
            return dict.__getitem__(self, self._order[key])
        return dict.__getitem__(self, key)

    def keys(self):
        return self._order


def _wrap_row(row: Any) -> Any:
    if not isinstance(row, dict):
        return row
    wrapped = _PGRow(row)
    wrapped._order = tuple(row.keys())
    return wrapped


class _PGCursorAdapter:
    """Cursor-like result handle exposing fetchone/fetchall/lastrowid."""

    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor

    def fetchone(self) -> Any:
        row = self._cursor.fetchone()
        return _wrap_row(row) if row is not None else None

    def fetchall(self) -> list[Any]:
        rows = self._cursor.fetchall() or []
        return [_wrap_row(row) for row in rows]

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    @property
    def lastrowid(self) -> int | None:
        # PostgreSQL has no lastrowid; callers use RETURNING instead.
        return None


class _PGConnectionAdapter:
    """A pooled connection speaking the sqlite3.Connection-ish dialect."""

    dialect = "postgresql"

    def __init__(self, pool: Any, conn: Any, database: PostgresDatabase) -> None:
        self._pool = pool
        self._conn = conn
        self._database = database

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> _PGCursorAdapter:
        translated = _translate(sql)
        cursor = self._conn.cursor()
        try:
            if params:
                cursor.execute(translated, tuple(params))
            else:
                cursor.execute(translated)
        except Exception as exc:
            raise self._database.map_exception(exc) from exc
        return _PGCursorAdapter(cursor)

    def commit(self) -> None:
        try:
            self._conn.commit()
        except Exception as exc:
            raise self._database.map_exception(exc) from exc

    def rollback(self) -> None:
        try:
            self._conn.rollback()
        except Exception as exc:
            raise self._database.map_exception(exc) from exc

    @property
    def in_transaction(self) -> bool:
        return getattr(self._conn, "closed", False) is False and (
            self._conn.info.transaction_status_name != "IDLE"
        )

    def close(self) -> None:
        try:
            self._pool.putconn(self._conn)
        except Exception as exc:  # noqa: BLE001 - pool teardown must not mask outcomes
            logger.debug("putconn failed: %s", type(exc).__name__)


class PostgresDatabase:
    """Pooled PostgreSQL backend mirroring the SQLite Database API."""

    dialect = "postgresql"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pool: Any = None
        self._lock = threading.RLock()
        self._local = threading.local()

    # ------------------------------------------------------------------
    # Pool lifecycle
    # ------------------------------------------------------------------

    def _get_pool(self) -> Any:
        with self._lock:
            if self._pool is None:
                try:
                    from psycopg.rows import dict_row
                    from psycopg_pool import ConnectionPool
                except ImportError as exc:  # pragma: no cover - guarded by config
                    raise ConfigMissingExtraError(
                        "PostgreSQL support requires the [pg] extra "
                        "(pip install 'zero-develop[pg]')"
                    ) from exc
                self._pool = ConnectionPool(
                    conninfo=self._settings.database_url,
                    min_size=self._settings.pg_pool_min,
                    max_size=self._settings.pg_pool_max,
                    open=True,
                    name=f"zero-{uuid.uuid4().hex[:8]}",
                    # Timestamps are stored as UTC ISO-8601 text; every
                    # session pins UTC so to_char/to_timestamp agree.
                    kwargs={"row_factory": dict_row, "options": "-c timezone=UTC"},
                )
            return self._pool

    def map_exception(self, exc: Exception) -> Exception:
        """Map psycopg exceptions onto the sqlite3 hierarchy repos catch."""
        sqlstate = getattr(exc, "sqlstate", "")
        message = str(exc)
        if sqlstate.startswith("23"):  # integrity violations
            return sqlite3.IntegrityError(message)
        if sqlstate in {"40001", "40P01"}:  # serialization/deadlock
            return sqlite3.OperationalError(message)
        if sqlstate.startswith("08") or sqlstate == "57P01":
            return sqlite3.OperationalError(message)
        return sqlite3.DatabaseError(message)

    # ------------------------------------------------------------------
    # Facade used by repositories
    # ------------------------------------------------------------------

    def connect(self) -> _PGConnectionAdapter:
        transaction_conn = getattr(self._local, "transaction_conn", None)
        if transaction_conn is not None:
            return transaction_conn
        pool = self._get_pool()
        conn = pool.getconn()
        adapter = _PGConnectionAdapter(pool, conn, self)
        return adapter

    @contextmanager
    def transaction(
        self,
        *,
        enforce_foreign_keys: bool = True,
    ) -> Iterator[_PGConnectionAdapter]:
        del enforce_foreign_keys  # PostgreSQL enforces FKs natively.
        existing = getattr(self._local, "transaction_conn", None)
        if existing is not None:
            depth = getattr(self._local, "transaction_depth", 0) + 1
            savepoint = f"zero_nested_{depth}_{uuid.uuid4().hex[:8]}"
            existing.execute(f"SAVEPOINT {savepoint}")
            self._local.transaction_depth = depth
            try:
                yield existing
                existing.execute(f"RELEASE SAVEPOINT {savepoint}")
            except BaseException:
                existing.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                existing.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
            finally:
                self._local.transaction_depth = depth - 1
            return

        pool = self._get_pool()
        conn = pool.getconn()
        adapter = _PGConnectionAdapter(pool, conn, self)
        self._local.transaction_conn = adapter
        self._local.transaction_depth = 0
        try:
            adapter.execute("BEGIN")
            yield adapter
            adapter.commit()
        except BaseException:
            try:
                adapter.rollback()
            finally:
                pass
            raise
        finally:
            del self._local.transaction_conn
            del self._local.transaction_depth
            adapter.close()

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> _PGCursorAdapter:
        conn = self.connect()
        return conn.execute(sql, params)

    def commit(self) -> None:
        conn = self.connect()
        conn.commit()

    def close(self) -> None:
        with self._lock:
            if self._pool is not None:
                try:
                    self._pool.close()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("pool close failed: %s", type(exc).__name__)
                self._pool = None

    def ping(self) -> bool:
        try:
            conn = self.connect()
            conn.execute("SELECT 1")
            conn.commit()
            return True
        except Exception:  # noqa: BLE001 - probe must never raise
            return False


class ConfigMissingExtraError(RuntimeError):
    """Raised when psycopg is required but the [pg] extra is absent."""


__all__ = [
    "ConfigMissingExtraError",
    "DatabaseError",
    "PostgresDatabase",
]
