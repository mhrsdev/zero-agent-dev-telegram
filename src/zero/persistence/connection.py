"""SQLite connection management.

Design notes (see ADR 0005):

- ``:memory:`` databases are cached per-process so that tests sharing a
  config see the same in-memory database. Without this, each
  ``sqlite3.connect(":memory:")`` call returns a brand-new empty
  database, which would break any test that runs migrations then
  queries.
- For file databases, we open a new connection per use. SQLite handles
  file-level locking; FastAPI's async event loop calls us through a
  thread pool (``run_in_threadpool``), so we set
  ``check_same_thread=False`` to allow the connection to be used from
  the worker thread.
- Foreign keys are enabled on every connection. This is the smallest
  durable enforcement of project-scoped lineage (per
  ``zero-project-isolation-evidence`` §"Canonical constraints and
  policy complement each other").
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from zero.config import ConfigError, Settings


class DatabaseError(RuntimeError):
    """Typed persistence failure (per ``zero-control-plane-trust`` §"Failure
    shapes teach the boundary")."""


def _resolve_sqlite_path(database_url: str) -> str:
    """Convert a normalized ``sqlite:///...`` URL into a sqlite3 path.

    - ``sqlite::memory:`` -> ``:memory:``
    - ``sqlite:///./foo.db`` -> ``./foo.db``
    - ``sqlite:///foo.db`` -> ``foo.db``
    - ``sqlite:///absolute/path.db`` -> ``/absolute/path.db``
    """
    if database_url == "sqlite::memory:":
        return ":memory:"
    if database_url.startswith("sqlite:///"):
        return database_url[len("sqlite:///") :]
    raise ConfigError(f"Unsupported database URL: {database_url!r}")


class _TransactionConnection:
    """Prevent repository commits from escaping an outer transaction."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.raw = connection

    def __getattr__(self, name: str) -> Any:
        return getattr(self.raw, name)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


class Database:
    """Thin wrapper around a SQLite connection.

    The wrapper is intentionally small. We are not building an ORM or a
    connection pool; SQLite handles concurrency at the file level. The
    wrapper's job is:

    - give us one place to enable ``PRAGMA foreign_keys = ON``;
    - cache in-memory databases per-process so tests work;
    - give us one place to add tracing/metrics later (Milestone 14).
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._path = _resolve_sqlite_path(settings.database_url)
        # For in-memory databases we cache the connection so that
        # multiple calls within the same process see the same schema
        # and data. For file databases we open a fresh connection each
        # time, which lets SQLite handle file-level locking.
        self._memory_conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._local = threading.local()

    @property
    def is_in_memory(self) -> bool:
        return self._path == ":memory:"

    def connect(self) -> sqlite3.Connection | _TransactionConnection:
        """Return a connection. See class docstring for caching rules."""
        transaction_conn = getattr(self._local, "transaction_conn", None)
        if transaction_conn is not None:
            return transaction_conn
        if self.is_in_memory:
            return self._connect_memory()
        return self._connect_file()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection | _TransactionConnection]:
        """Share one connection across a business transaction.

        Repositories call :meth:`connect` independently. Binding the
        transaction connection to the current thread keeps those calls
        atomic on both in-memory and file-backed SQLite databases.
        """
        existing = getattr(self._local, "transaction_conn", None)
        if existing is not None:
            depth = getattr(self._local, "transaction_depth", 0) + 1
            savepoint = f"zero_nested_{depth}"
            self._local.transaction_depth = depth
            existing.raw.execute(f"SAVEPOINT {savepoint}")
            try:
                yield existing
                existing.raw.execute(f"RELEASE SAVEPOINT {savepoint}")
            except BaseException:
                existing.raw.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                existing.raw.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
            finally:
                self._local.transaction_depth = depth - 1
            return

        with self._lock:
            conn = self._connect_memory() if self.is_in_memory else self._connect_file()
            transaction_conn = _TransactionConnection(conn)
            self._local.transaction_conn = transaction_conn
            self._local.transaction_depth = 0
            try:
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                yield transaction_conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                del self._local.transaction_conn
                del self._local.transaction_depth
                if not self.is_in_memory:
                    conn.close()

    def _connect_memory(self) -> sqlite3.Connection:
        with self._lock:
            if self._memory_conn is None:
                conn = sqlite3.connect(":memory:", check_same_thread=False)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA foreign_keys = ON")
                self._memory_conn = conn
            return self._memory_conn

    def _connect_file(self) -> sqlite3.Connection:
        # Ensure the parent directory exists for file databases.
        if self._path not in ("", ":memory:"):
            parent = Path(self._path).parent
            if str(parent) not in ("", "."):
                parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    # ------------------------------------------------------------------
    # Convenience helpers used by application code
    # ------------------------------------------------------------------

    def execute(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> sqlite3.Cursor:
        """Execute a single statement and return the cursor.

        Caller is responsible for committing write transactions. We do
        not auto-commit because business facts often span multiple
        statements (per ``zero-control-plane-trust`` §"Atomicity follows
        the business fact").
        """
        conn = self.connect()
        try:
            return conn.execute(sql, params)
        finally:
            if not self.is_in_memory:
                # For file databases we open a fresh connection each
                # time; the caller must ``conn.commit()`` (returned by
                # this method via cursor.connection) before closing.
                # We do NOT close here because the caller may need to
                # fetch rows after this method returns.
                pass

    def commit(self) -> None:
        """Commit the current in-memory connection (if any).

        For file databases, callers should commit on the connection
        they obtained from ``connect()``.
        """
        if self.is_in_memory and self._memory_conn is not None:
            self._memory_conn.commit()

    def close(self) -> None:
        """Close the cached in-memory connection, if any.

        Used by tests to reset state between test functions. For file
        databases, each caller closes its own connection.
        """
        with self._lock:
            if self._memory_conn is not None:
                self._memory_conn.close()
                self._memory_conn = None

    # ------------------------------------------------------------------
    # Health probe
    # ------------------------------------------------------------------

    def ping(self) -> bool:
        """Return True if a trivial query succeeds."""
        try:
            conn = self.connect()
            conn.execute("SELECT 1")
            return True
        except sqlite3.Error:
            return False
