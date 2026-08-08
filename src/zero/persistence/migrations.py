"""Restart-safe, idempotent migration runner.

Design (see ADR 0005):

- Migrations are plain ``.sql`` files under ``migrations/``.
- Files are applied in lexical order.
- Each migration is split into individual statements; each statement
  is executed separately so partial-failure recovery is granular.
- Statements that fail with "duplicate column name" or "already
  exists" are treated as already-applied (idempotent) and skipped.
  This makes migrations safe to re-run after a partial failure.
- Successful application of a migration is recorded in
  ``schema_migrations``.
- On restart, already-applied migrations are skipped entirely.
- A failed migration that cannot be made idempotent raises
  :class:`MigrationError` and leaves ``schema_migrations`` unchanged.

Statement splitting respects SQLite trigger syntax (``CREATE TRIGGER
... BEGIN ... END;``) where semicolons may appear inside the trigger
body.
"""

from __future__ import annotations

import re
import sqlite3
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


def _ensure_schema_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            id           TEXT PRIMARY KEY,
            applied_at   TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        )
        """
    )
    conn.commit()


def _applied_migrations(conn: sqlite3.Connection) -> set[str]:
    cursor = conn.execute("SELECT id FROM schema_migrations")
    return {row[0] for row in cursor.fetchall()}


# Errors that indicate a statement is already applied (idempotent skip).
# SQLite error messages we treat as "already done":
#   - "duplicate column name: X"  (ALTER TABLE ADD COLUMN)
#   - "table X already exists"     (CREATE TABLE without IF NOT EXISTS)
#   - "index X already exists"     (CREATE INDEX without IF NOT EXISTS)
#   - "trigger X already exists"   (CREATE TRIGGER without IF NOT EXISTS)
_IDEMPOTENT_ERROR_PATTERNS = (
    "duplicate column name",
    "already exists",
)


def _is_idempotent_error(exc: sqlite3.Error) -> bool:
    msg = str(exc).lower()
    return any(pattern in msg for pattern in _IDEMPOTENT_ERROR_PATTERNS)


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
    """Apply all pending migrations.

    Returns the number of newly applied migrations.
    """
    files = _migration_files()
    if not files:
        return 0

    conn = database.connect()
    _ensure_schema_migrations_table(conn)
    already = _applied_migrations(conn)

    applied = 0
    for path in files:
        migration_id = path.stem
        if migration_id in already:
            continue
        sql = path.read_text(encoding="utf-8")
        statements = _split_statements(sql)
        try:
            for stmt in statements:
                stmt_text = stmt.strip()
                if not stmt_text or stmt_text.startswith("--"):
                    continue
                try:
                    conn.execute(stmt_text)
                except sqlite3.Error as exc:
                    if _is_idempotent_error(exc):
                        # Already applied; safe to continue.
                        continue
                    raise
            conn.execute(
                "INSERT INTO schema_migrations (id) VALUES (?)",
                (migration_id,),
            )
            conn.commit()
            applied += 1
        except sqlite3.Error as exc:
            conn.rollback()
            raise MigrationError(
                f"Migration {migration_id} failed: {exc}"
            ) from exc

    return applied


def count_applied_migrations(database: Database) -> int:
    """Return the number of migrations recorded as applied."""
    conn = database.connect()
    _ensure_schema_migrations_table(conn)
    cursor = conn.execute("SELECT COUNT(*) FROM schema_migrations")
    return int(cursor.fetchone()[0])
