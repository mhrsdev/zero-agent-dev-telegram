"""Restart-safe, idempotent migration runner.

Design (see ADR 0005):

- Migrations are plain ``.sql`` files under ``migrations/``.
- Files are applied in lexical order.
- Each migration is executed as one transaction, including all DDL and the
  ``schema_migrations`` ledger insert. A failure rolls back the complete
  migration so no partial schema can be recorded as successful.
- Statements that fail with "duplicate column name" or "already
  exists" are treated as already-applied (idempotent) and skipped only
  within the current migration transaction.
- Successful application of a migration is recorded in
  ``schema_migrations``.
- On restart, already-applied migrations are skipped entirely.
- A failed migration that cannot be made idempotent raises
  :class:`MigrationError` and rolls back both schema changes and its ledger row.

Statement splitting respects SQLite trigger syntax (``CREATE TRIGGER
... BEGIN ... END;``) where semicolons may appear inside the trigger
body.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
from pathlib import Path

from zero.persistence.connection import Database


class MigrationError(RuntimeError):
    """Raised when a migration cannot be applied."""


def _migration_files() -> list[Path]:
    """Return the list of ``.sql`` files under ``migrations/``, sorted."""
    migrations_dir = Path(__file__).parent / "migrations"
    if not migrations_dir.is_dir():
        return []
    return sorted(migrations_dir.glob("*.sql"))


_MIGRATION_LOCK = threading.RLock()


def _ensure_schema_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            id           TEXT PRIMARY KEY,
            applied_at   TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            checksum     TEXT
        )
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(schema_migrations)")}
    if "checksum" not in columns:
        # Existing installations predate checksums.  The nullable column is
        # added without rewriting historical migration rows; apply_migrations
        # backfills only from the exact current migration file once.
        conn.execute("ALTER TABLE schema_migrations ADD COLUMN checksum TEXT")
    conn.commit()


def _migration_checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def _applied_migrations(conn: sqlite3.Connection) -> dict[str, str | None]:
    cursor = conn.execute("SELECT id, checksum FROM schema_migrations")
    return {str(row[0]): row[1] for row in cursor.fetchall()}


# Errors that indicate a statement is already applied (idempotent skip).
# Deliberately do not match every ``already exists`` message: a view or
# unexpected object collision can change behavior and must fail loudly.
_IDEMPOTENT_ERROR_PATTERNS = (
    re.compile(r"^duplicate column name:\s*.+$"),
    re.compile(r"^(table|index|trigger)\s+\S+\s+already exists$"),
)


def _is_idempotent_error(exc: sqlite3.Error) -> bool:
    msg = str(exc).strip().lower()
    return any(pattern.match(msg) for pattern in _IDEMPOTENT_ERROR_PATTERNS)


def _split_statements(sql: str) -> list[str]:
    """Split a migration script into individual statements.

    Respects SQLite trigger syntax where a semicolon may appear inside
    a ``BEGIN ... END`` block. Comments (lines starting with ``--``)
    are preserved attached to the following statement so the SQL
    remains readable in logs.
    """
    # Strip block comments /* ... */ first.
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    statements: list[str] = []
    buf: list[str] = []
    in_trigger = False
    for line in sql.splitlines():
        stripped = line.strip()
        # Skip full-line comments only when outside a statement.
        if not buf and stripped.startswith("--"):
            continue
        if not in_trigger and stripped == "":
            continue
        buf.append(line)
        # Detect trigger body boundaries.
        upper = stripped.upper()
        if "BEGIN" in upper and "TRIGGER" in " ".join(buf).upper():
            in_trigger = True
        if in_trigger and upper.endswith("END;"):
            in_trigger = False
            statements.append("\n".join(buf))
            buf = []
            continue
        if not in_trigger and stripped.endswith(";"):
            statements.append("\n".join(buf))
            buf = []
    if buf:
        # Trailing content without a semicolon — append if non-trivial.
        text = "\n".join(buf).strip()
        if text and not text.startswith("--"):
            statements.append(text)
    return statements


def apply_migrations(database: Database) -> int:
    """Apply all pending migrations atomically and verify checksums."""
    files = _migration_files()
    if not files:
        return 0

    with _MIGRATION_LOCK:
        conn = database.connect()
        _ensure_schema_migrations_table(conn)
        applied_checksums = _applied_migrations(conn)
        file_map = {path.stem: path for path in files}

        # A migration file is immutable once applied.  Historical databases
        # without checksums receive a one-time exact-content backfill; any
        # recorded checksum mismatch fails closed before new DDL runs.
        for migration_id, recorded in applied_checksums.items():
            path = file_map.get(migration_id)
            if path is None:
                raise MigrationError(f"Applied migration {migration_id} has no migration file")
            checksum = _migration_checksum(path.read_text(encoding="utf-8"))
            if recorded is None:
                conn.execute(
                    "UPDATE schema_migrations SET checksum = ? WHERE id = ?",
                    (checksum, migration_id),
                )
                conn.commit()
            elif str(recorded) != checksum:
                raise MigrationError(
                    f"Migration {migration_id} checksum mismatch: "
                    f"recorded {recorded}, current {checksum}"
                )

        newly_applied = 0
        for path in files:
            migration_id = path.stem
            if migration_id in applied_checksums:
                continue
            sql = path.read_text(encoding="utf-8")
            checksum = _migration_checksum(sql)
            statements = _split_statements(sql)
            try:
                # ``BEGIN IMMEDIATE`` in Database.transaction serializes
                # writers in-process and across SQLite processes.  All DDL,
                # bookkeeping, and rollback happen in the same transaction.
                # A marked SQLite table-rebuild migration may temporarily
                # disable foreign keys; ordinary migrations never may.
                rebuild_migration = "ZERO_MIGRATION_FOREIGN_KEYS_OFF" in sql
                with database.transaction(enforce_foreign_keys=not rebuild_migration) as tx:
                    # Re-read after BEGIN IMMEDIATE. Another process may have
                    # applied this migration while this worker waited for the
                    # SQLite writer lock; executing its DDL again would cause
                    # duplicate bookkeeping or destructive races.
                    already_applied = tx.execute(
                        "SELECT checksum FROM schema_migrations WHERE id = ?",
                        (migration_id,),
                    ).fetchone()
                    if already_applied is not None:
                        recorded_checksum = already_applied[0]
                        if recorded_checksum is not None and str(recorded_checksum) != checksum:
                            raise MigrationError(
                                f"Migration {migration_id} checksum mismatch after lock acquisition"
                            )
                        applied_checksums[migration_id] = checksum
                        continue
                    for stmt in statements:
                        stmt_text = stmt.strip()
                        if not stmt_text or stmt_text.startswith("--"):
                            continue
                        try:
                            tx.execute(stmt_text)
                        except sqlite3.Error as exc:
                            if _is_idempotent_error(exc):
                                continue
                            raise
                    tx.execute(
                        "INSERT INTO schema_migrations (id, checksum) VALUES (?, ?)",
                        (migration_id, checksum),
                    )
            except sqlite3.Error as exc:
                raise MigrationError(f"Migration {migration_id} failed: {exc}") from exc
            newly_applied += 1
            applied_checksums[migration_id] = checksum
        return newly_applied


def count_applied_migrations(database: Database) -> int:
    """Return the number of migrations recorded as applied."""
    conn = database.connect()
    _ensure_schema_migrations_table(conn)
    cursor = conn.execute("SELECT COUNT(*) FROM schema_migrations")
    return int(cursor.fetchone()[0])
