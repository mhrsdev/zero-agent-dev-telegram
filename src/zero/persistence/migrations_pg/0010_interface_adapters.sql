-- GENERATED from 0010_interface_adapters.sql by scripts/gen_pg_migrations.py.
-- PostgreSQL dialect translation of the canonical SQLite schema.
-- Do not edit directly; re-run the generator instead.

-- Zero Develop — Milestone 13 schema: interface adapters (Telegram, Discord).
--
-- This migration adds:
--   * interface_bindings — project/channel/topic scope configuration.
--   * interface_event_log — idempotent event processing log.
--   * callback_tokens — opaque action tokens for inline keyboard callbacks.
--
-- Design invariants (per zero-interface-adapter-model and
-- TELEGRAM_FINDINGS):
--   * External IDs map to stable Zero User IDs through a verified link.
--   * Owner selects enabled project/channel/topic scopes.
--   * Telegram General and unrelated topics are not enabled by default.
--   * Normal conversation does not become execution.
--   * Approval actions use the same plan revision and authorization rules.
--   * Adapter-local storage is not authoritative project state.
--   * update_id is a transport idempotency key; domain dedup is separate.
--   * Webhook success means accepted delivery, not completed domain work.
--   * Callback payloads are compact, replayable client data; server still
--     resolves current state and permission.

-- ------------------------------------------------------------------
-- Interface bindings (project/channel/topic scope configuration)
-- ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS interface_bindings (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id),
    -- platform: telegram, discord, etc.
    platform        TEXT NOT NULL
                    CHECK (platform IN ('telegram','discord','other')),
    -- bot_token_ref: reference to the bot token secret (NOT the raw token).
    -- NULL means the bot is not yet configured.
    bot_token_ref   TEXT,
    -- chat_id: the Telegram chat ID or Discord channel ID (as text to
    -- preserve 64-bit values).
    chat_id         TEXT NOT NULL,
    -- topic_id: optional Telegram message_thread_id (forum topic).
    -- NULL means no topic (general chat or non-forum).
    topic_id        TEXT,
    -- is_enabled: whether Zero is active in this scope.
    -- Per TELEGRAM_FINDINGS: General topic is NOT enabled by default.
    is_enabled      INTEGER NOT NULL DEFAULT 0,
    -- created_by: the user who created this binding.
    created_by      TEXT NOT NULL REFERENCES users(id),
    created_at      TEXT NOT NULL
                    DEFAULT (to_char(clock_timestamp(), 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')),
    updated_at      TEXT NOT NULL
                    DEFAULT (to_char(clock_timestamp(), 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')),
    -- One binding per (platform, chat_id, topic_id) combination.
    -- NULL topic_id is treated as distinct from any non-NULL topic_id.
    UNIQUE (platform, chat_id, topic_id)
);

CREATE INDEX IF NOT EXISTS idx_interface_bindings_project
    ON interface_bindings(project_id);
CREATE INDEX IF NOT EXISTS idx_interface_bindings_enabled
    ON interface_bindings(platform, is_enabled);

-- ------------------------------------------------------------------
-- Interface event log (idempotent event processing)
-- ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS interface_event_log (
    id              TEXT PRIMARY KEY,
    project_id      TEXT REFERENCES projects(id),
    -- platform: which platform sent this event.
    platform        TEXT NOT NULL,
    -- external_event_id: transport idempotency key (e.g. Telegram
    -- update_id). UNIQUE per platform so duplicate delivery is a no-op.
    external_event_id TEXT NOT NULL,
    -- external_actor_id: the platform's user ID (as text).
    external_actor_id TEXT,
    -- resolved_user_id: the Zero User ID this event was resolved to.
    -- NULL if the user is unlinked or unknown.
    resolved_user_id TEXT REFERENCES users(id),
    -- chat_id and topic_id: the scope the event came from.
    chat_id         TEXT,
    topic_id        TEXT,
    -- event_kind: message, callback_query, command, other.
    event_kind      TEXT NOT NULL DEFAULT 'message',
    -- event_content: redacted summary of the event content.
    event_content   TEXT,
    -- processing_result: processed, ignored_unlinked, ignored_disabled,
    -- denied, error.
    processing_result TEXT NOT NULL DEFAULT 'processed'
                    CHECK (processing_result IN (
                        'processed','ignored_unlinked','ignored_disabled',
                        'denied','error'
                    )),
    -- processing_detail: optional detail about the result.
    processing_detail TEXT,
    created_at      TEXT NOT NULL
                    DEFAULT (to_char(clock_timestamp(), 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')),
    UNIQUE (platform, external_event_id)
);

CREATE INDEX IF NOT EXISTS idx_interface_event_log_project
    ON interface_event_log(project_id);
CREATE INDEX IF NOT EXISTS idx_interface_event_log_platform
    ON interface_event_log(platform, external_event_id);

-- ------------------------------------------------------------------
-- Callback tokens (opaque action tokens for inline keyboard callbacks)
-- ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS callback_tokens (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id),
    -- plan_id and revision_number: the plan revision this callback
    -- refers to. Per zero-interface-adapter-model: a callback should
    -- carry a compact opaque reference or bounded action identity.
    plan_id         TEXT NOT NULL REFERENCES plans(id),
    revision_number INTEGER NOT NULL,
    -- action: approve, reject, edit.
    action          TEXT NOT NULL
                    CHECK (action IN ('approve','reject','edit')),
    -- expires_at: when this token expires. Old tokens cannot be used.
    expires_at      TEXT NOT NULL,
    -- used_at: when this token was used. NULL means unused.
    used_at         TEXT,
    -- created_by: the user who triggered the plan proposal (the
    -- callback is sent to them for approval).
    created_by      TEXT REFERENCES users(id),
    created_at      TEXT NOT NULL
                    DEFAULT (to_char(clock_timestamp(), 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'))
);

CREATE INDEX IF NOT EXISTS idx_callback_tokens_plan
    ON callback_tokens(plan_id, revision_number);
CREATE INDEX IF NOT EXISTS idx_callback_tokens_unused
    ON callback_tokens(project_id) WHERE used_at IS NULL;