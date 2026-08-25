"""GAP 2 integration tests against a disposable PostgreSQL container.

These tests require a reachable PostgreSQL instance. They are skipped
unless ``ZERO_TEST_PG_URL`` is set, e.g.::

    docker compose --profile pg up -d postgres
    ZERO_TEST_PG_URL=postgresql://zero:zero@127.0.0.1:5432/zero \
        pytest -m pg_integration tests/test_pg_integration.py
"""

from __future__ import annotations

import os

import pytest

pytestmark = [
    pytest.mark.pg_integration,
    pytest.mark.skipif(
        not os.environ.get("ZERO_TEST_PG_URL"),
        reason="set ZERO_TEST_PG_URL to a disposable PostgreSQL instance",
    ),
]


@pytest.fixture(scope="module")
def pg_settings():
    from zero.config import Settings

    url = os.environ["ZERO_TEST_PG_URL"]
    return Settings.load_for_test(database_url=url)


def test_migrations_apply_and_count(pg_settings):
    from zero.persistence.connection import open_database
    from zero.persistence.migrations import apply_migrations, count_applied_migrations

    database = open_database(pg_settings)
    first = apply_migrations(database)
    second = apply_migrations(database)
    assert first > 0
    assert second == 0  # idempotent rerun
    assert count_applied_migrations(database) == first


def test_repository_roundtrip_through_pg(pg_settings):
    from zero.persistence.connection import open_database
    from zero.persistence.migrations import apply_migrations

    database = open_database(pg_settings)
    apply_migrations(database)

    owner = database.execute(
        "INSERT INTO users (id, display_name) VALUES (?, ?) RETURNING id",
        ("zu_pgtest000000000000000001", "PG smoke"),
    ).fetchone()
    assert owner["id"] == "zu_pgtest000000000000000001"


def test_ping(pg_settings):
    from zero.persistence.connection import open_database

    assert open_database(pg_settings).ping() is True


def test_transaction_rollback_on_error(pg_settings):
    from zero.persistence.connection import open_database

    database = open_database(pg_settings)
    try:
        with database.transaction() as tx:
            tx.execute(
                "INSERT INTO users (id, display_name) VALUES (?, ?)",
                ("zu_pgtestrollback00000000001", "will roll back"),
            )
            raise RuntimeError("force rollback")
    except RuntimeError:
        pass
    row = database.execute(
        "SELECT COUNT(*) AS c FROM users WHERE id = ?",
        ("zu_pgtestrollback00000000001",),
    ).fetchone()
    assert row["c"] == 0
