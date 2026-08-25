-- GENERATED from 0013_provider_usage_nullable_dedup.sql by scripts/gen_pg_migrations.py.
-- PostgreSQL dialect translation of the canonical SQLite schema.
-- Do not edit directly; re-run the generator instead.

-- Provider request-level usage has no provider message ID.
-- SQLite UNIQUE constraints treat NULL values as distinct, so the
-- table-level composite UNIQUE cannot enforce one NULL-message row.
CREATE UNIQUE INDEX IF NOT EXISTS uq_usage_request_without_message
    ON usage_records(provider_request_id)
    WHERE provider_message_id IS NULL;