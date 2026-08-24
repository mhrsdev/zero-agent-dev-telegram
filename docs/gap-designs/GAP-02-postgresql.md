# GAP 2 Design — PostgreSQL Persistence Backend

Status: design accepted · Phase 5

## Problem

SQLite is single-writer and file-local; multi-worker deployments and
network-attached databases need PostgreSQL. Today
`SUPPORTED_DATABASE_SCHEME = "sqlite"` refuses everything else at the
config boundary (fail-closed by design).

## Architecture

Dual-backend persistence behind the existing `Database` shape. The
repositories are SQL-string based over a `sqlite3.Connection`-like
object; we keep that property by making the Postgres backend speak the
same *synchronous* DB-API surface (psycopg3), executed off the event
loop exactly like SQLite today (`run_in_threadpool` / worker threads).
This avoids rewriting 13 repositories for async while still providing
a real server-side pool.

```
Settings.database_url scheme
   sqlite://…  → SqliteDatabase (unchanged)
   postgresql://… or postgres://… (+ [pg] extra installed)
               → PostgresDatabase (pooled psycopg AsyncConnection used
                 synchronously via connection pool; min 2 / max 20,
                 ZERO_PG_POOL_MIN / ZERO_PG_POOL_MAX)
   otherwise   → ConfigError (fail closed, unchanged)
```

Components under `src/zero/persistence/`:

1. **`pg_connection.py`** — `PostgresDatabase` mirroring `Database`:
   `connect()`, `transaction()` (BEGIN/COMMIT + SAVEPOINT nesting),
   `ping()`, `close()`. Pool: `psycopg_pool.ConnectionPool`
   (`min_size`, `max_size`, open at startup, health-checked).
2. **`pg_migrations.py`** — reads the same numbered `.sql` files but
   from `src/zero/persistence/migrations_pg/*.sql`: dialect-translated
   copies (AUTOINCREMENT→IDENTITY/BIGSERIAL,
   `strftime(...)`→`to_char(clock_timestamp(), ...)`,
   `PRAGMA foreign_keys` dropped in favor of real FKs). Same
   `schema_migrations` ledger with checksums; identical apply order and
   idempotent-error tolerance.
3. **`pg_repositories/`** — one module per SQLite repository
   implementing the identical protocol (same method names/signatures).
   Divergent SQL isolated per method. Services receive repositories via
   composition root (`build_application_services`) so they never learn
   the backend.
4. **Migration runner dual-dialect**: `apply_migrations(database)`
   dispatches on `database.dialect`; each backend keeps its own
   directory and ledger rows (ledger table name shared).

### Translation policy

Rather than a generic translator (fragile), PG migrations are
hand-maintained translations validated by integration tests that run
the full migration set against a disposable container and then execute
a repository smoke-suite per aggregate. A CI job does this per PR
touching persistence.

## Data model changes

No logical schema changes: same tables/columns/constraints, translated
dialect only. SQLite remains the canonical reference schema.

## API surface

- Config: `ZERO_DATABASE_URL=postgresql://user:pass@host:5432/zero`.
- Settings gains `pg_pool_min` (default 2), `pg_pool_max` (default 20)
  via `ZERO_PG_POOL_MIN/MAX`.
- Missing `[pg]` extra with a pg URL → `ConfigError` naming the extra
  to install (fail closed at load time).

## Security considerations

- Database credentials stay in the URL/env; never logged (existing
  redaction of database_url stays).
- SSL: `sslmode` honored from URL params; production guidance requires
  `sslmode=require` or better (documented).
- Least privilege: documented role needs only its own schema DML+DDL
  during migrate.

## Test strategy

- Unit tests run on SQLite unchanged (CI default) — zero behavioral drift.
- New `tests/integration_pg/` marked `@pytest.mark.pg_integration`;
  skipped unless a Postgres URL env (`ZERO_TEST_PG_URL`) is reachable;
  CI workflow spins `postgres:16` service container with healthcheck.
- Repository parity harness: for every repository, a shared test module
  exercises CRUD against whichever backend is active, ensuring protocol
  equality.
- Fail-closed tests: pg URL without extra → clear ConfigError message;
  bad scheme refused as today.

## Migration path

1. Land backend + extras; SQLite default everywhere.
2. Operators opt in via `ZERO_DATABASE_URL`.
3. Docker Compose gains optional `postgres` service (profile `pg`)
   with healthcheck.

## Rollback strategy

Point `ZERO_DATABASE_URL` back at SQLite; both schemas are kept in sync
by mirrored migrations until deprecation (not before Phase 9 sign-off).

## Acceptance criteria

- All existing tests pass against SQLite unchanged.
- Integration tests pass against disposable Postgres container.
- Services remain backend-agnostic (protocol identical).
- Production with pg URL + missing extra fails closed with actionable error.
