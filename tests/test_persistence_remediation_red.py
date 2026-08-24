"""RED tests for SQLite lifecycle, migration integrity, and idempotency."""

from __future__ import annotations

import sqlite3

import pytest

import zero.persistence.migrations as migration_module
from zero.config import Settings
from zero.persistence.connection import Database
from zero.persistence.migrations import MigrationError, apply_migrations


def test_file_sqlite_connections_enable_wal_busy_timeout_and_close_cleanly(
    test_settings: Settings, tmp_db_path
):
    settings = test_settings.model_copy(update={"database_url": f"sqlite:///{tmp_db_path}"})
    database = Database(settings)

    connection = database.connect()
    journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
    assert str(journal_mode).lower() == "wal"
    assert int(busy_timeout) >= 1000

    database.close()
    assert database.ping() is True
    database.close()


def test_applied_migrations_store_and_validate_sha256_checksums(
    test_settings: Settings, tmp_path, monkeypatch
):
    migration_file = tmp_path / "9000_checksum_probe.sql"
    migration_file.write_text("CREATE TABLE checksum_probe (id INTEGER);\n")
    database = Database(test_settings)
    monkeypatch.setattr(migration_module, "_migration_files", lambda: [migration_file])

    assert apply_migrations(database) == 1
    row = (
        database.connect()
        .execute(
            "SELECT checksum FROM schema_migrations WHERE id = ?",
            (migration_file.stem,),
        )
        .fetchone()
    )
    assert row[0]

    migration_file.write_text("CREATE TABLE checksum_probe (id INTEGER, v TEXT);\n")
    with pytest.raises(MigrationError, match="checksum"):
        apply_migrations(database)


def test_failed_migration_rolls_back_all_ddl(test_settings: Settings, tmp_path, monkeypatch):
    migration_file = tmp_path / "9001_atomic_probe.sql"
    migration_file.write_text(
        "CREATE TABLE atomic_probe (id INTEGER);\nCREATE TABLE broken_probe (id INTEGER,);\n"
    )
    database = Database(test_settings)
    monkeypatch.setattr(migration_module, "_migration_files", lambda: [migration_file])

    with pytest.raises(MigrationError):
        apply_migrations(database)
    assert (
        database.connect()
        .execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='atomic_probe'")
        .fetchone()
        is None
    )
    assert database.connect().execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 0


def test_idempotent_error_classifier_is_precise():
    assert (
        migration_module._is_idempotent_error(
            sqlite3.OperationalError("table users already exists")
        )
        is True
    )
    assert (
        migration_module._is_idempotent_error(sqlite3.OperationalError("view users already exists"))
        is False
    )
    assert (
        migration_module._is_idempotent_error(
            sqlite3.OperationalError("duplicate column name: status")
        )
        is True
    )
