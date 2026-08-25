-- GENERATED from 0030_task_retry_backoff.sql by scripts/gen_pg_migrations.py.
-- PostgreSQL dialect translation of the canonical SQLite schema.
-- Do not edit directly; re-run the generator instead.

-- GAP 12: rate-limit-aware task retry scheduling.
-- Stores the earliest instant a failed task may be requeued. NULL means
-- the task is immediately eligible (historical behavior).
ALTER TABLE tasks ADD COLUMN next_retry_at TEXT;