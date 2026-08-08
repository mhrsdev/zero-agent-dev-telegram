"""Persistence layer tests.

Verifies:
- migrations are restart-safe (already-applied migrations are skipped);
- the schema_migrations bookkeeping table is created;
- foreign keys are enforced (per ADR 0005 §"Persistence starts with
  invariants");
- a file database survives a process restart;
- an in-memory database is shared within one process.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from zero.config import Settings
from zero.persistence.connection import Database
from zero.persistence.migrations import (
    apply_migrations,
    count_applied_migrations,
)


def test_migrations_create_schema(test_settings: Settings) -> None:
    database = Database(test_settings)
    applied = apply_migrations(database)
    assert applied >= 1
    # The schema_migrations table exists.
    conn = database.connect()
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = {row[0] for row in cursor.fetchall()}
    assert "schema_migrations" in tables
    assert "projects" in tables
    assert "runtime_markers" in tables


def test_migrations_are_restart_safe(test_settings: Settings) -> None:
    """Re-running apply_migrations must skip already-applied migrations
    and return 0 newly applied."""
    database = Database(test_settings)
    first = apply_migrations(database)
    assert first >= 1
    second = apply_migrations(database)
    assert second == 0
    assert count_applied_migrations(database) >= 1


def test_count_applied_migrations_returns_correct_number(
    test_settings: Settings,
) -> None:
    database = Database(test_settings)
    apply_migrations(database)
    count = count_applied_migrations(database)
    # The exact count grows as migrations are added; what matters is
    # that every migration file present was recorded.
    files = sorted(Path("src/zero/persistence/migrations").glob("*.sql"))
    assert count == len(files)
    assert count >= 1


def test_foreign_keys_are_enforced(test_settings: Settings) -> None:
    """Per ADR 0005: constraints are the smallest durable enforcement
    of ownership and lineage. Foreign keys must be ON."""
    database = Database(test_settings)
    apply_migrations(database)
    conn = database.connect()
    cursor = conn.execute("PRAGMA foreign_keys")
    assert cursor.fetchone()[0] == 1


def test_in_memory_database_is_shared_within_process(
    test_settings: Settings,
) -> None:
    """Two :meth:`Database.connect` calls on the same in-memory database
    must return the same connection (so tests that run migrations then
    query see the same data)."""
    database = Database(test_settings)
    conn1 = database.connect()
    conn2 = database.connect()
    assert conn1 is conn2


def test_file_database_survives_restart(
    tmp_db_path: Path,
) -> None:
    """A file database must persist data across a process restart.

    We simulate a restart by closing the :class:`Database` wrapper and
    constructing a fresh one pointing at the same file.
    """
    settings = Settings.load_for_test(database_url=f"sqlite:///{tmp_db_path}")
    db1 = Database(settings)
    apply_migrations(db1)
    conn = db1.connect()
    conn.execute(
        "INSERT INTO runtime_markers (name, value) VALUES (?, ?)",
        ("restart_test", "survived"),
    )
    conn.commit()
    # Construct a fresh Database pointing at the same file.
    db2 = Database(settings)
    conn2 = db2.connect()
    cursor = conn2.execute(
        "SELECT value FROM runtime_markers WHERE name = ?",
        ("restart_test",),
    )
    row = cursor.fetchone()
    assert row is not None
    assert row[0] == "survived"


def test_ping_returns_true_on_healthy_database(
    test_settings: Settings,
) -> None:
    database = Database(test_settings)
    apply_migrations(database)
    assert database.ping() is True


def test_runtime_markers_unique_constraint_enforced(
    test_settings: Settings,
) -> None:
    """The ``runtime_markers.name UNIQUE`` constraint must prevent
    duplicate inserts (per ADR 0005 §"Persistence starts with
    invariants")."""
    database = Database(test_settings)
    apply_migrations(database)
    conn = database.connect()
    conn.execute(
        "INSERT INTO runtime_markers (name, value) VALUES (?, ?)",
        ("unique_test", "first"),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO runtime_markers (name, value) VALUES (?, ?)",
            ("unique_test", "second"),
        )
