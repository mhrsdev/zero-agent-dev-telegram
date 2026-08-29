-- Durable per-scope chat history for conversational messaging
-- (Hermes session parity, round 5).
--
-- Hermes keys conversations as platform:chat_type:chat_id[:thread]
-- and persists every turn in state.db so sessions survive restarts.
-- Zero's Telegram path historically had NO session memory: every
-- message was an isolated planner event, so follow-up questions lost
-- all context. This table stores the conversational fallback
-- transcript per (platform, chat, topic) scope with a bounded,
-- oldest-first read window. It is intentionally NOT plan/execution
-- state: it is presentation-layer memory only.
CREATE TABLE chat_messages (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    topic_id TEXT,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX idx_chat_messages_scope_time ON chat_messages(platform, chat_id, topic_id, created_at);
CREATE INDEX idx_chat_messages_project_time ON chat_messages(project_id, created_at);
