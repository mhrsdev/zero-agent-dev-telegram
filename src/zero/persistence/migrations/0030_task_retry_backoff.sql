-- GAP 12: rate-limit-aware task retry scheduling.
-- Stores the earliest instant a failed task may be requeued. NULL means
-- the task is immediately eligible (historical behavior).
ALTER TABLE tasks ADD COLUMN next_retry_at TEXT;
