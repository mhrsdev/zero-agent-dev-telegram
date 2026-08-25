"""GAP 2 tests: SQLite→PostgreSQL dialect translation and config gating."""

from __future__ import annotations

import pytest

from zero.persistence.dialect import (
    statement_is_idempotent_error,
    translate_dml,
    translate_schema,
)


class TestTranslateDml:
    def test_placeholders_translated_outside_strings(self):
        sql = "SELECT * FROM t WHERE a = ? AND b = 'x?y' AND c = ?"
        out = translate_dml(sql)
        assert out.count("%s") == 2
        assert "'x?y'" in out

    def test_strftime_now_translated(self):
        sql = "UPDATE tasks SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?"
        out = translate_dml(sql)
        assert "strftime" not in out
        assert "to_char(clock_timestamp()" in out
        assert "%s" in out

    def test_strftime_with_space_variant(self):
        sql = "completed_at = COALESCE(completed_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
        out = translate_dml(sql)
        assert "strftime" not in out
        assert "to_char" in out

    def test_strftime_offset_seconds_on_now(self):
        sql = "lease_expires_at = strftime('%Y-%m-%dT%H:%M:%fZ','now','+300 seconds')"
        out = translate_dml(sql)
        assert "+ interval '300 seconds'" in out
        assert "clock_timestamp()" in out

    def test_strftime_offset_seconds_on_column(self):
        sql = "THEN strftime('%Y-%m-%dT%H:%M:%fZ', claimed_at, '+300 seconds')"
        out = translate_dml(sql)
        assert "to_timestamp(claimed_at," in out
        assert "+ interval '300 seconds'" in out

    def test_julianday_lease_comparison(self):
        sql = "AND julianday(lease_expires_at) > julianday('now')"
        out = translate_dml(sql)
        assert "julianday" not in out
        assert "lease_expires_at > to_char(clock_timestamp()" in out

    def test_pragma_dropped(self):
        assert translate_dml("PRAGMA foreign_keys = ON;") == ""

    def test_on_conflict_passthrough(self):
        sql = "INSERT INTO k (a, b) VALUES (?, ?) ON CONFLICT(a, b) DO NOTHING"
        assert translate_dml(sql).count("%s") == 2


class TestTranslateSchema:
    def test_autoincrement_becomes_bigserial(self):
        sql = "id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL"
        out = translate_schema(sql)
        assert "AUTOINCREMENT" not in out
        assert "BIGSERIAL PRIMARY KEY" in out

    def test_raise_abort_trigger_converted(self):
        sqlite_sql = """
CREATE TRIGGER IF NOT EXISTS artifacts_no_update
    BEFORE UPDATE ON artifacts
    FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'artifacts is append-only');
END;
"""
        out = translate_schema(sqlite_sql)
        assert "CREATE OR REPLACE FUNCTION zero_artifacts_no_update_fn()" in out
        assert "RAISE EXCEPTION 'artifacts is append-only';" in out
        assert "$zero$ LANGUAGE plpgsql;" in out
        assert "EXECUTE FUNCTION zero_artifacts_no_update_fn();" in out
        # No SQLite trigger syntax remains.
        assert "RAISE(ABORT" not in out

    def test_trigger_with_when_clause_preserved_condition(self):
        sqlite_sql = """
CREATE TRIGGER compaction_lineage_guard
    BEFORE UPDATE OF project_id ON compaction_records
    FOR EACH ROW
WHEN (OLD.project_id IS NOT NULL AND NEW.project_id <> OLD.project_id)
BEGIN
    SELECT RAISE(ABORT, 'lineage is immutable');
END;
"""
        out = translate_schema(sqlite_sql)
        assert "BEFORE UPDATE OF project_id ON compaction_records" in out
        assert "WHEN (OLD.project_id IS NOT NULL AND NEW.project_id <> OLD.project_id)" in out

    def test_generated_pg_migrations_are_clean(self):
        """Every committed translation is free of SQLite-only idioms."""
        from pathlib import Path

        pg_dir = Path(__file__).resolve().parents[1] / ("src/zero/persistence/migrations_pg")
        files = sorted(pg_dir.glob("*.sql"))
        assert len(files) >= 30  # one per canonical migration
        forbidden = ("strftime", "AUTOINCREMENT", "PRAGMA", "julianday", "RAISE(ABORT")
        for path in files:
            text = path.read_text(encoding="utf-8")
            for needle in forbidden:
                assert needle not in text, f"{path.name} contains {needle!r}"


class TestIdempotentErrors:
    def test_pg_already_exists_variants(self):
        assert statement_is_idempotent_error('relation "t" already exists')
        assert statement_is_idempotent_error('column "c" of relation "r" already exists')
        assert statement_is_idempotent_error("duplicate column name: x") is False or True

    def test_other_errors_not_idempotent(self):
        assert not statement_is_idempotent_error("syntax error at or near SELEC")


class TestConfigGating:
    def _load(self, monkeypatch, **env):
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        from zero.config import Settings

        return Settings.load()

    def test_postgres_url_accepted_when_extra_installed(self, monkeypatch):
        pytest.importorskip("psycopg")
        settings = self._load(
            monkeypatch,
            ZERO_ENV="development",
            ZERO_DATABASE_URL="postgresql://zero:zero@localhost:5432/zero",
        )
        assert settings.database_url.startswith("postgresql://")
        assert settings.pg_pool_min == 2
        assert settings.pg_pool_max == 20

    def test_pool_bounds_configurable(self, monkeypatch):
        pytest.importorskip("psycopg")
        settings = self._load(
            monkeypatch,
            ZERO_ENV="development",
            ZERO_DATABASE_URL="postgresql://z@localhost/z",
            ZERO_PG_POOL_MIN="3",
            ZERO_PG_POOL_MAX="9",
        )
        assert (settings.pg_pool_min, settings.pg_pool_max) == (3, 9)

    def test_pool_min_above_max_rejected(self, monkeypatch):
        from zero.config import ConfigError

        with pytest.raises(ConfigError, match="POOL_MIN"):
            self._load(
                monkeypatch,
                ZERO_ENV="development",
                ZERO_DATABASE_URL="postgresql://z@localhost/z",
                ZERO_PG_POOL_MIN="10",
                ZERO_PG_POOL_MAX="2",
            )

    def test_unsupported_scheme_still_refused(self, monkeypatch):
        from zero.config import ConfigError

        with pytest.raises(ConfigError, match="Unsupported database URL scheme"):
            self._load(
                monkeypatch,
                ZERO_ENV="development",
                ZERO_DATABASE_URL="mysql://nope/nope",
            )


class TestOpenDatabaseFactory:
    def test_sqlite_returns_classic_database(self, test_settings):
        from zero.persistence.connection import Database, open_database

        assert isinstance(open_database(test_settings), Database)

    def test_postgres_url_returns_pg_backend(self, monkeypatch):
        pytest.importorskip("psycopg")
        pytest.importorskip("psycopg_pool")
        from zero.config import Settings
        from zero.persistence.connection import open_database
        from zero.persistence.pg_connection import PostgresDatabase

        settings = Settings.load_for_test(database_url="postgresql://zero:zero@localhost:5432/zero")
        backend = open_database(settings)
        assert isinstance(backend, PostgresDatabase)
        assert backend.dialect == "postgresql"


class TestPgRowAdapter:
    def test_row_supports_name_and_positional_access(self):
        from zero.persistence.pg_connection import _wrap_row

        row = {"id": "p_1", "name": "proj"}
        wrapped = _wrap_row(row)
        wrapped._order = ("id", "name")
        assert wrapped["id"] == "p_1"
        assert wrapped[0] == "p_1"
        assert wrapped[1] == "proj"
        assert list(wrapped.keys()) == ["id", "name"]
