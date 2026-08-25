-- GENERATED from 0014_interface_binding_scoped_claims.sql by scripts/gen_pg_migrations.py.
-- PostgreSQL dialect translation of the canonical SQLite schema.
-- Do not edit directly; re-run the generator instead.

-- Interface event identity and claim leases.
--
-- Provider event IDs are scoped to an adapter/bot account, not globally to
-- the provider name.  Existing rows are retained in the empty legacy scope;
-- new delivery paths provide the stable interface binding scope.

DROP INDEX IF EXISTS idx_interface_event_claims_retry;
DROP INDEX IF EXISTS idx_interface_event_log_platform;

CREATE TABLE interface_event_claims_v2 (
    platform            TEXT NOT NULL
                        CHECK (platform IN ('telegram','discord','other')),
    binding_scope       TEXT NOT NULL DEFAULT '',
    binding_id          TEXT REFERENCES interface_bindings(id),
    external_event_id   TEXT NOT NULL,
    state               TEXT NOT NULL DEFAULT 'processing'
                        CHECK (state IN ('processing','succeeded','failed')),
    attempt_count       INTEGER NOT NULL DEFAULT 1
                        CHECK (attempt_count > 0),
    claimed_at          TEXT NOT NULL
                        DEFAULT (to_char(clock_timestamp(), 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')),
    lease_expires_at    TEXT NOT NULL
                        DEFAULT (to_char(clock_timestamp() + interval '300 seconds', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')),
    completed_at        TEXT,
    PRIMARY KEY (platform, binding_scope, external_event_id)
);

INSERT INTO interface_event_claims_v2
    (platform, binding_scope, external_event_id, state, attempt_count,
     claimed_at, lease_expires_at, completed_at)
SELECT platform, '', external_event_id, state, attempt_count,
       claimed_at,
       CASE WHEN state = 'processing'
            THEN to_char(to_timestamp(claimed_at, 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') + interval '300 seconds', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
            ELSE claimed_at END,
       completed_at
FROM interface_event_claims;

DROP TABLE interface_event_claims;
ALTER TABLE interface_event_claims_v2 RENAME TO interface_event_claims;

CREATE INDEX idx_interface_event_claims_retry
    ON interface_event_claims(platform, binding_scope, state, lease_expires_at);

CREATE TABLE interface_event_log_v2 (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT REFERENCES projects(id),
    platform            TEXT NOT NULL
                        CHECK (platform IN ('telegram','discord','other')),
    binding_scope       TEXT NOT NULL DEFAULT '',
    binding_id          TEXT REFERENCES interface_bindings(id),
    external_event_id   TEXT NOT NULL,
    external_actor_id   TEXT,
    resolved_user_id    TEXT REFERENCES users(id),
    chat_id             TEXT,
    topic_id            TEXT,
    event_kind          TEXT NOT NULL DEFAULT 'message',
    event_content      TEXT,
    processing_result   TEXT NOT NULL DEFAULT 'processed'
                        CHECK (processing_result IN (
                            'processed','ignored_unlinked','ignored_disabled',
                            'denied','error'
                        )),
    processing_detail   TEXT,
    created_at          TEXT NOT NULL
                        DEFAULT (to_char(clock_timestamp(), 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'))
);

INSERT INTO interface_event_log_v2
    (id, project_id, platform, binding_scope, external_event_id,
     external_actor_id, resolved_user_id, chat_id, topic_id, event_kind,
     event_content, processing_result, processing_detail, created_at)
SELECT id, project_id, platform, '', external_event_id,
       external_actor_id, resolved_user_id, chat_id, topic_id, event_kind,
       event_content, processing_result, processing_detail, created_at
FROM interface_event_log;

DROP TABLE interface_event_log;
ALTER TABLE interface_event_log_v2 RENAME TO interface_event_log;

CREATE UNIQUE INDEX uq_interface_event_log_scope
    ON interface_event_log(platform, binding_scope, external_event_id);
CREATE INDEX idx_interface_event_log_platform
    ON interface_event_log(platform, binding_scope, external_event_id);