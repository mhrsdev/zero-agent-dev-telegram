-- GENERATED from 0029_management.sql by scripts/gen_pg_migrations.py.
-- PostgreSQL dialect translation of the canonical SQLite schema.
-- Do not edit directly; re-run the generator instead.

-- Management layer tables (Zero Dev Telegram).
-- Additive only; no changes to existing engine tables.

CREATE TABLE IF NOT EXISTS group_policies (
    chat_id        TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title          TEXT NOT NULL DEFAULT '',
    kind           TEXT NOT NULL DEFAULT 'supergroup',
    topic_id       TEXT,
    enabled        INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
    default_agent  TEXT NOT NULL DEFAULT 'main_worker',
    allowed_features TEXT NOT NULL DEFAULT '["chat"]',
    rate_limit_per_min INTEGER NOT NULL DEFAULT 10 CHECK (rate_limit_per_min >= 1),
    daily_token_budget INTEGER NOT NULL DEFAULT 200000 CHECK (daily_token_budget >= 0),
    added_by       TEXT,
    added_at       TEXT NOT NULL DEFAULT (to_char(clock_timestamp(), 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'))
);

CREATE TABLE IF NOT EXISTS admin_users (
    username       TEXT PRIMARY KEY,
    password_hash  TEXT NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (to_char(clock_timestamp(), 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'))
);

CREATE TABLE IF NOT EXISTS setup_tokens (
    token_hash     TEXT PRIMARY KEY,
    expires_at     TEXT NOT NULL,
    used_at        TEXT
);

CREATE TABLE IF NOT EXISTS provider_health (
    provider_id    TEXT NOT NULL,
    model          TEXT NOT NULL DEFAULT '',
    state          TEXT NOT NULL DEFAULT 'closed' CHECK (state IN ('closed','open','half_open')),
    failures       INTEGER NOT NULL DEFAULT 0,
    last_failure_at TEXT,
    opened_until   TEXT,
    PRIMARY KEY (provider_id, model)
);

CREATE TABLE IF NOT EXISTS usage_counters (
    day            TEXT NOT NULL,
    project_id     TEXT NOT NULL,
    chat_id        TEXT,
    provider       TEXT NOT NULL,
    model          TEXT NOT NULL,
    requests       INTEGER NOT NULL DEFAULT 0,
    input_tokens   INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    failed         INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd TEXT NOT NULL DEFAULT '0',
    PRIMARY KEY (day, project_id, chat_id, provider, model)
);