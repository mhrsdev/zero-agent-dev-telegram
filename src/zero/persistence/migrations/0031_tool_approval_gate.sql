-- GAP 8b/G2 (Hermes parity): per-call tool approval gate.
-- One table serves as pending request queue AND durable decision
-- record: decision IS NULL while awaiting a human verdict.
CREATE TABLE tool_approval_decisions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    execution_id TEXT,
    tool_name TEXT NOT NULL,
    args_hash TEXT NOT NULL DEFAULT '',
    grain TEXT NOT NULL DEFAULT 'once' CHECK (grain IN ('once', 'session', 'always')),
    decision TEXT CHECK (decision IN ('allow', 'deny')),
    decided_by_user_id TEXT,
    reason TEXT,
    created_at TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%fZ', 'now')),
    resolved_at TEXT
);

CREATE INDEX idx_tool_approval_project_tool ON tool_approval_decisions(project_id, tool_name);
CREATE INDEX idx_tool_approval_pending ON tool_approval_decisions(project_id, execution_id)
    WHERE decision IS NULL;
-- At most one standing always-allow per (tool, exact arguments) shape.
CREATE UNIQUE INDEX uq_tool_approval_always_allow ON tool_approval_decisions(project_id, tool_name, args_hash)
    WHERE grain = 'always' AND decision = 'allow';
