-- Zero Develop — initial schema (Phase 1, Milestone 1).
--
-- This file is intentionally minimal. Identity/membership (M2),
-- permissions/tools/audit (M3), plans (M4), executions (M5), tasks (M6),
-- agent types (M7), artifacts and memory (M8) all arrive in later
-- migrations.
--
-- What this file establishes:
--   1. The schema_migrations bookkeeping table (created by the runner,
--      but we create it here too so a fresh database is consistent
--      regardless of entry point).
--   2. The projects table — the foreign-key target for every
--      project-scoped table added in later milestones. Created now so
--      that the discipline "every project-scoped table has a
--      project_id FK" can start from day one.
--   3. A runtime_markers table — a tiny durable marker written by the
--      smoke test to prove the database is wired and migrations ran.
--      Dropped in a later migration once real tables exercise the
--      same path.

CREATE TABLE IF NOT EXISTS schema_migrations (
    id           TEXT PRIMARY KEY,
    applied_at   TEXT NOT NULL
                 DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS projects (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    created_at   TEXT NOT NULL
                 DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS runtime_markers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    value        TEXT NOT NULL,
    created_at   TEXT NOT NULL
                 DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
