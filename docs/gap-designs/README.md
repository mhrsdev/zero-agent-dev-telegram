# Gap Design Documents

Designs for the 12 production-readiness gaps in
`PRODUCTION_READINESS_PROMPT.md`, written before any implementation
code per that prompt's instructions.

Reference implementations consulted:

- **hermes-agent** (`C:\Users\SMN\Desktop\Zero\NEW\hermes-agent`):
  jittered backoff + Retry-After parsing (`agent/retry_utils.py`),
  pluggable execution environments incl. hardened Docker
  (`tools/environments/docker.py`), MCP client lifecycle/naming
  (`tools/mcp_tool.py`), delegation depth/tool-narrowing model
  (`tools/delegate_tool.py`), SSE frame conventions
  (`gateway/platforms/api_server.py`).
- **claude-code** docs/plugins: plugin manifest/discovery contract,
  `mcp__server__tool` naming precedent, subagent depth limit 3 and
  tool-scoping rules, settings precedence philosophy.

| Doc | Gap | Phase |
|---|---|---|
| GAP-01-live-integration.md | Live qualification | 9 |
| GAP-02-postgresql.md | PostgreSQL backend | 5 |
| GAP-03-sandbox-executor.md | Sandbox executors | 4 |
| GAP-04-user-session.md | User-session Telegram | 8 |
| GAP-05-client-streaming.md | SSE streaming | 2 |
| GAP-06-chat-endpoint.md | Interactive chat | 2 |
| GAP-07-mcp-plugins.md | MCP + plugins | 7 |
| GAP-08-subagents.md | Delegation | 6 |
| GAP-09-memory-deltas.md | Memory deltas | 3 |
| GAP-10-task-decomposition.md | LLM decomposition | 3 |
| GAP-11-real-token-counting.md | tiktoken counting | 1 |
| GAP-12-retry-backoff.md | Rate-limit-aware retry | 1 |

Every implementation milestone must end green: full deterministic
suite, `ruff check`, `ruff format --check`, `python -m compileall`.
Baseline at design time: 614 tests passing (16 POSIX-only skips on
Windows).
