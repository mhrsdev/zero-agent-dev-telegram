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
import time
import weakref
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


def _close_connections(connections: set[sqlite3.Connection]) -> None:
    """Close and forget every connection in ``connections``.

    Registered as a :func:`weakref.finalize` callback so an abandoned
    :class:`Database` still releases its handles. The callback holds the
    only other strong reference to the set, which keeps the connections
    alive until it runs — relying on ``__del__`` instead is not enough,
    because a ``Database`` and its connections are usually collected in
    the same cyclic-GC batch and the connection's own finalizer may run
    first, emitting ``ResourceWarning: unclosed database`` (an error
    under this repo's warnings-as-errors policy).
    """
    for conn in tuple(connections):
        try:
            conn.close()
        except sqlite3.Error:
            pass
    connections.clear()


class Database:
    """Thin wrapper around a SQLite connection.

    The wrapper is intentionally small. We are not building an ORM or a
    connection pool; SQLite handles concurrency at the file level. The
    wrapper's job is:

    - give us one place to enable ``PRAGMA foreign_keys = ON``;
    - cache in-memory databases per-process so tests work;
    - own the lifecycle of every connection it hands out;
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
        self._connections: set[sqlite3.Connection] = set()
        self._lock = threading.RLock()
        self._local = threading.local()
        self._finalizer = weakref.finalize(self, _close_connections, self._connections)

    @property
    def is_in_memory(self) -> bool:
        return self._path == ":memory:"

    def connect(self) -> sqlite3.Connection | _TransactionConnection:
        """Return a connection and reassert foreign-key enforcement."""
        transaction_conn = getattr(self._local, "transaction_conn", None)
        if transaction_conn is not None:
            return transaction_conn
        if self.is_in_memory:
            conn = self._connect_memory()
        else:
            conn = self._connect_file()
        self._ensure_foreign_keys(conn)
        return conn

    def _ensure_foreign_keys(self, conn: sqlite3.Connection) -> None:
        """Ensure a reusable connection cannot silently disable SQLite FKs."""
        if not conn.in_transaction:
            conn.execute("PRAGMA foreign_keys = ON")
        enabled = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        if enabled != 1:
            raise DatabaseError("SQLite foreign-key enforcement is disabled")

    @contextmanager
    def transaction(
        self,
        *,
        enforce_foreign_keys: bool = True,
    ) -> Iterator[sqlite3.Connection | _TransactionConnection]:
        """Share one connection across a business transaction.

        Repositories call :meth:`connect` independently. Binding the
        transaction connection to the current thread keeps those calls
        atomic on both in-memory and file-backed SQLite databases.

        ``enforce_foreign_keys=False`` is reserved for an explicitly marked
        SQLite table-rebuild migration.  Normal application transactions
        remain fail-closed with foreign-key enforcement enabled.
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
            if enforce_foreign_keys:
                self._ensure_foreign_keys(conn)
            else:
                if conn.in_transaction:
                    raise DatabaseError(
                        "foreign-key-disabled transactions must begin before any write"
                    )
                conn.execute("PRAGMA foreign_keys = OFF")
                if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 0:
                    raise DatabaseError("SQLite foreign-key enforcement could not be disabled")
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
                if not enforce_foreign_keys:
                    # PRAGMA foreign_keys is connection-scoped and may only
                    # change outside a transaction.
                    conn.execute("PRAGMA foreign_keys = ON")
                    self._ensure_foreign_keys(conn)
                del self._local.transaction_conn
                del self._local.transaction_depth
                if not self.is_in_memory:
                    conn.close()
                    self._connections.discard(conn)
                    if getattr(self._local, "file_conn", None) is conn:
                        self._local.file_conn = None

    def _configure_connection(self, conn: sqlite3.Connection, *, wal: bool) -> sqlite3.Connection:
        conn.row_factory = sqlite3.Row
        # A busy timeout prevents transient writer contention from becoming
        # an opaque ``database is locked`` failure in request handlers.
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA foreign_keys = ON")
        if wal:
            for attempt in range(6):
                try:
                    conn.execute("PRAGMA journal_mode = WAL")
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower() or attempt == 5:
                        raise
                    time.sleep(0.05 * (2**attempt))
            conn.execute("PRAGMA synchronous = NORMAL")
        self._connections.add(conn)
        return conn

    def _connect_memory(self) -> sqlite3.Connection:
        with self._lock:
            if self._memory_conn is None:
                conn = sqlite3.connect(":memory:", check_same_thread=False)
                self._memory_conn = self._configure_connection(conn, wal=False)
            return self._memory_conn

    def _open_file_connection(self) -> sqlite3.Connection:
        # Ensure the parent directory exists for file databases.
        if self._path not in ("", ":memory:"):
            parent = Path(self._path).parent
            if str(parent) not in ("", "."):
                parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            self._path,
            check_same_thread=False,
            timeout=5.0,
        )
        return self._configure_connection(conn, wal=True)

    def _connect_file(self) -> sqlite3.Connection:
        """Return one lifecycle-managed connection per worker thread."""
        conn = getattr(self._local, "file_conn", None)
        if conn is not None:
            try:
                conn.execute("SELECT 1")
                return conn
            except sqlite3.Error:
                try:
                    conn.close()
                finally:
                    self._connections.discard(conn)
                self._local.file_conn = None
        conn = self._open_file_connection()
        self._local.file_conn = conn
        return conn

    # ------------------------------------------------------------------
    # Convenience helpers used by application code
    # ------------------------------------------------------------------

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
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
        """Commit the current worker connection."""
        conn = self.connect()
        conn.commit()

    def close(self) -> None:
        """Close all lifecycle-managed connections owned by this database."""
        with self._lock:
            _close_connections(self._connections)
            self._memory_conn = None
            if hasattr(self._local, "file_conn"):
                self._local.file_conn = None

    def __del__(self) -> None:
        """Release connections deterministically when the wrapper dies.

        The ``weakref.finalize`` registered in ``__init__`` is the
        guaranteed path; this keeps the thread-local bookkeeping tidy
        when the object is dropped normally.
        """
        try:
            self.close()
        except Exception:  # noqa: BLE001 - interpreter teardown must not raise
            pass

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


def open_database(settings: Settings):
    """Open the backend matching the configured URL scheme (GAP 2).

    SQLite URLs return the classic :class:`Database`; PostgreSQL URLs
    return :class:`~zero.persistence.pg_connection.PostgresDatabase`.
    Scheme validation happened at configuration load; unknown schemes
    fail closed there.
    """
    scheme = settings.database_url.split(":", 1)[0].strip().lower()
    if scheme in ("postgresql", "postgres"):
        from zero.persistence.pg_connection import PostgresDatabase

        return PostgresDatabase(settings)
    return Database(settings)
