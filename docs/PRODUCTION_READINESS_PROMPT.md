# Zero Dev Telegram — Full Production Readiness Implementation

You are working on Zero Dev Telegram, an open-source AI development agent
designed primarily for Telegram and small teams.

Repository: https://github.com/mhrsdev/zero-agent-dev-telegram
Branch: feat/management-layer (or main after merge)

## Context

Zero Dev Telegram currently has:
- A durable control-plane core: identity, plans, executions, worktrees,
  providers (OpenAI-compatible + Anthropic adapters), usage accounting,
  audit log, access policy, capability probes, backup daemon, setup wizard
  (CLI + GUI), TUI (Textual), local Web GUI (/admin)
- 598 passing deterministic tests, ruff clean, compileall clean
- A management layer (`src/zero/manage/`) with CLI, TUI, GUI sharing one
  SetupService / ConfigService / AccessPolicyService

What it does NOT have (the gaps this prompt must close):

1. **Live integration qualification** — every external call is faked;
   no real Telegram bot has ever been driven end-to-end in CI
2. **PostgreSQL persistence backend** — SQLite-only today; no async
   connection pooling for multi-worker deployments
3. **Production sandbox executor** — `host_bounded` worktree execution is
   refused in production because there is no container/chroot/namespace
   isolation backend
4. **User-session Telegram mode** — only Bot API is implemented
5. **Client-facing streaming** — SSE parsing exists internally but no
   endpoint streams tokens to clients
6. **Interactive chat endpoint** — no REST/WebSocket route accepts a user
   message and returns a model response inline (the current flow requires
   plan→approve→execute→run-ready which is batch-oriented)
7. **MCP / plugin extensibility** — no Model Context Protocol server or
   plugin registry; tool set is fixed at 5 builtins
8. **Subagent delegation** — the runtime runs tasks sequentially; no
   isolated child contexts with their own provider/model/tool scope
9. **Memory delta artifacts** — compaction reserves a field but never
   writes accepted memory deltas back to knowledge records
10. **LLM-driven task decomposition** — scheduler creates a single
    "implementation" task per plan; no planner-adapter splits into
    multi-step dependency graphs
11. **Real tokenizer** — token counting uses bytes÷4 everywhere
12. **Rate-limit-aware task retry** — tasks have attempt budgets but no
    exponential backoff or Retry-After honoring between attempts

---

## Instructions

Do NOT begin implementation immediately.

First:
1. Read the entire existing codebase (especially `docs/management-layer-plan/`,
   `docs/CURRENT_STATE_LEDGER.md`, and every module under `src/zero/manage/`)
2. Read the reference codebases at `C:\Users\SMN\Desktop\Zero\NEW\hermes-agent`
   (full source) and `C:\Users\SMN\Desktop\Zero\NEW\claude-code` (docs/plugins)
3. For each gap below, produce a design document covering: architecture,
   data model changes, API surface, security considerations, test strategy,
   migration path, rollback strategy, and acceptance criteria
4. Commit design docs before writing any implementation code
5. Implement in small, reviewable commits, one milestone at a time
6. Every milestone must end green on the full suite (~600+ tests) with
   ruff check, ruff format --check, and compileall all passing
7. Do not present planned features as completed; do not disable checks to
   make CI pass; do not store plaintext secrets

---

## GAP 1: Live Integration Qualification

### Problem
All Telegram/provider calls use deterministic fakes. The release validator
explicitly states "no live provider or Telegram behavior has been verified."

### Required implementation
1. Create `tests/integration_live/` directory with pytest markers
   `@pytest.mark.live_telegram` and `@pytest.mark.live_provider`
2. These tests read credentials from environment variables:
   - `LIVE_TELEGRAM_BOT_TOKEN` — a real bot token from BotFather
   - `LIVE_TELEGRAM_CHAT_ID` — a test group chat id
   - `LIVE_OPENAI_API_KEY` — a real OpenAI key with ≥$5 credit
   - `LIVE_ANTHROPIC_API_KEY` — a real Anthropic key
3. Tests must be skipped unless env vars are present AND
   `ZERO_ENABLE_LIVE_TESTS=1` is explicitly set
4. Write these live tests:
   - `test_live_telegram_get_me.py`: call getMe via adapter, assert
     non-empty username and bot flag
   - `test_live_telegram_send_message.py`: send a test message to the
     configured chat, assert message_id returned
   - `test_live_telegram_poll.py`: run one poll_once cycle, assert either
     empty result or valid update structure
   - `test_live_openai_completion.py`: send minimal completion, assert
     non-empty content and usage tokens > 0
   - `test_live_anthropic_completion.py`: same for Anthropic adapter
   - `test_live_provider_streaming.py`: verify SSE events arrive incrementally
5. Create `.github/workflows/live-tests.yml` triggered by
   `workflow_dispatch` only (never on push/PR); secrets injected from
   GitHub repository secrets
6. Document required secrets and how to obtain them in
   `docs/LIVE_TESTING.md`

### Acceptance criteria
- All live tests pass when run manually with real credentials
- CI workflow exists but does not run automatically without dispatch
- Zero deterministic tests are affected

---

## GAP 2: PostgreSQL Persistence Backend

### Problem
SQLite works for single-process development but cannot handle concurrent
writers, network access, or horizontal scaling needed for production teams.

### Required implementation
1. Add `asyncpg` or `psycopg[async]` to dependencies under `[pg]` extra
2. Create `src/zero/persistence/pg_connection.py` mirroring the
    Database interface (connect, transaction, ping)
3. Create `src/zero/persistence/pg_migrations.py` using a numbered
   migration approach compatible with existing SQL (translate dialect)
4. Create `src/zero/persistence/pg_repositories/` implementing the same
   protocol as each SQLite repository
5. Config: accept `ZERO_DATABASE_URL=postgresql://...` when the `pg`
   extra is installed; fail closed if not installed
6. Docker Compose: add optional postgres service with healthcheck
7. Migration runner: dual-dialect support — detect URL scheme and
   delegate to appropriate backend
8. Connection pooling: min 2, max 20, configurable via ZERO_PG_POOL_*

### Acceptance criteria
- All existing tests pass against SQLite (unchanged)
- New integration tests pass against a disposable Postgres container
- Repository protocol is identical; services don't know the backend
- Fail-closed: production with pg:// URL and missing extra → clear error

---

## GAP 3: Production Sandbox Executor

### Problem
Worktree command execution (`host_bounded` mode) runs commands directly
on the host with scrubbed env. This is adequate for trusted agents but
not a hostile-code sandbox. Production refuses it entirely.

### Required implementation
Implement a pluggable executor protocol:

```python
class CommandExecutor(Protocol):
    def execute(self, argv, cwd, timeout, output_limit) -> ExecResult: ...
```

Three implementations:

1. **HostBoundedExecutor** (current behavior, dev/test only)
2. **DockerExecutor**: runs commands inside a pinned Docker container
   - Image: configurable via `ZERO_SANDBOX_IMAGE` (default `python:3.12-slim`)
   - Resource limits: CPU quota, memory limit, pids limit, no network
   - Volume mounts: worktree dir mounted read-write; nothing else
   - Security opts: `no-new-privileges:true`, drop all caps except CHOWN/SETUID
   - Non-root user inside container
   - Timeout enforced by docker CLI `--time` + SIGKILL fallback
3. **FirejailExecutor** (Linux-only): wraps commands in firejail with
   private tmp, no network (--net=none), read-only system dirs

Configuration:
- `ZERO_SANDBOX_EXECUTOR = none | docker | firejail` (default: none)
- When `none`, production refuses command execution (current behavior)
- When `docker`, validate Docker socket availability at startup
- When `firejail`, validate firejail binary presence at startup

Wire into WorktreeService._validate_command / _run_bounded_process via
the executor protocol so callers don't know the backend.

Tests: unit tests with mocked subprocess/docker; integration tests with
real Docker (skipif no Docker socket).

### Acceptance criteria
- Production can enable host_bounded execution when a sandbox executor
  is configured
- Commands cannot escape the worktree or access host filesystem/network
- Capability report honestly reflects which executor is active

---

## GAP 4: User-Session Telegram Mode

### Problem
Only Bot API is supported. Some users want the agent to act as their
personal Telegram account (reading/writing as themselves).

### Required implementation
1. Add Telethon or Pyrogram as an optional dependency under `[session]` extra
2. Create `src/zero/adapters/user_session.py` implementing the same
   NormalizedEvent intake protocol as telegram.py
3. Configuration schema additions:
   ```yaml
   telegram:
     mode: bot_api | user_session
     session:
       api_id: int
       api_hash_ref: sec_…    # stored encrypted
       phone_ref: sec_…        # stored encrypted
       session_string_ref: sec_…  # encrypted session blob
   ```
4. Setup wizard step 4 gains a "User Session" branch that:
   - Explains ToS implications and ban risk clearly
   - Collects api_id/api_hash via masked input
   - Initiates phone login → OTP prompt → optional 2FA password
   - Stores session string encrypted; NEVER persists OTP codes
   - Does NOT enable by default; explicit opt-in required
5. Security requirements:
   - Session string encrypted at rest (same Fernet profile as other secrets)
   - OTP codes held in memory only, never written to disk or logs
   - Rate limiting: max 30 messages/min outbound (anti-spam)
   - Explicit disclaimer shown during setup
6. Access policy applies identically (owner_only default)

### Acceptance criteria
- User-session mode disabled unless [session] extra installed AND
  explicitly enabled in config
- OTP/session material never appears in logs, audit, or diagnostics
- Same access-policy gate as Bot API messages

---

## GAP 5: Client-Facing Streaming

### Problem
SSE parsing exists internally (provider_adapter) but no HTTP endpoint
streams tokens to clients in real time.

### Required implementation
1. Add `GET /admin/executions/{eid}/stream` (SSE) endpoint:
   - Content-Type: text/event-stream
   - Emits `data: {"type":"text_delta","text":"…"}` per token
   - Emits `data: {"type":"tool_call","name":"…","arguments":{…}}`
   - Emits `data: {"type":"done","finish_reason":"stop"}` at end
   - Heartbeat every 15s to keep connections alive through proxies
2. Add `POST /admin/chat/{project_id}` (non-streaming alternative):
   - Body: `{"message": "...", "agent_scope": "main_worker"}`
   - Runs a single-turn completion through the runtime (no plan gate)
   - Returns full response JSON
3. Wire into AgentRuntime: expose a generator/callback interface that
   yields CanonicalStreamEvents as they arrive (currently collected
   internally by _collect_stream)
4. GUI: add a simple chat panel on the dashboard using fetch() +
   ReadableStream to render deltas progressively
5. TUI: add a "Chat" screen that connects to the SSE endpoint and
   renders tokens in a scrollable pane
6. Security: both endpoints require admin auth; rate-limited; no raw
   prompts/responses logged

### Acceptance criteria
- curl -N http://127.0.0.1:8000/admin/executions/X/stream shows incremental text
- GUI chat panel renders tokens progressively (manual verification)
- Existing non-streaming endpoints unchanged

---

## GAP 6: Interactive Chat Endpoint

### Problem
No way to send a message and get a model response without going through
the full plan/approve/execute pipeline.

### Required implementation
Add `POST /admin/chat` endpoint that:
1. Creates a ephemeral conversation context (no persistent plan)
2. Builds a single-turn request with the system prompt + user message
3. Dispatches through the provider chain (with fallback)
4. Optionally executes tool calls up to max_tool_rounds (default 3)
5. Returns JSON: `{"content": "...", "tool_calls_executed": [...],
   "usage": {…}, "provider_request_id": "…"}`
6. Usage recorded normally (accounting, cost estimation)
7. Rate limited: configurable requests/minute (default 10)
8. Requires admin auth (GUI session or bearer token)

This enables interactive experimentation without polluting project state.

---

## GAP 7: MCP Server + Plugin Registry

### Problem
Tool set is fixed at 5 builtins. No way to extend without modifying source.

### Required implementation
Part A — MCP Client:
1. Add `mcp` package as optional dependency under `[mcp]` extra
2. Create `src/zero/manage/core/mcp_client.py`: connect to an MCP server
   process (stdio), list available tools, invoke them, translate
   results to/from ToolCallResult format
3. Config schema addition:
   ```yaml
   mcp_servers:
     - name: filesystem
       command: ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
       enabled: true
   ```
4. On startup, connect to enabled MCP servers, discover tools, register
   them in ToolService with `mcp_<server>_<tool>` naming convention
5. Tool grants apply as usual (capability-based authorization)

Part B — Plugin Registry:
1. Create `src/zero/manage/plugins/` with discovery from two paths:
   - `~/.zero/plugins/*.py` (user plugins)
   - `/opt/zero/plugins/*.py` (system plugins)
2. Each plugin file exports `register(manage_context)` where
   `manage_context` provides: config, secret_store, tool_registry
3. Load order: alphabetical within each dir; user overrides system
4. Plugin loading failures logged but never crash the app

### Acceptance criteria
- An MCP filesystem server's tools appear alongside builtin tools
- A sample plugin can add a custom tool callable by agents
- Tools respect existing capability-grant authorization

---

## GAP 8: Subagent Delegation

### Problem
AgentRuntime processes tasks sequentially with no isolation between them.

### Required implementation
1. Add a `delegate` tool that agents can call mid-execution:
   ```
   delegate(objective, agent_type?, tools?, model?, context_budget?)
   ```
2. Implementation: spawns a new AgentRuntime.run_task() call with:
   - A new synthetic task (child execution linked to parent)
   - Its own agent type (defaults to caller's type)
   - Isolated conversation history (fresh messages)
   - Optional narrower tool set and smaller context budget
   - Result returned to the parent as a tool result
3. Depth limit: max nesting level 3 (parent → child → grandchild)
4. Concurrency: delegated tasks count toward the agent type's
   max_concurrent_instances limit
5. Usage accounting: delegated requests tagged `is_whole_tree=False`;
   aggregation sums whole-tree correctly
6. Timeout: delegated task inherits parent's lease duration

### Acceptance criteria
- Parent agent delegates a subtask and receives the result inline
- Delegated usage is tracked separately and summed correctly
- Nesting depth limit prevents runaway recursion
- Concurrent delegation respects instance limits

---

## GAP 9: Memory Delta Artifacts

### Problem
Compaction reserves `memory_delta_artifact_id` but never writes it.

### Required implementation
After successful compaction:
1. Extract key decisions/facts from the compacted summary (the LLM
   summarizer already produces structured sections)
2. Parse sections into structured memory records:
   - "Accepted decisions" → KnowledgeRecord(kind="decision")
   - "Blockers or failures" → KnowledgeRecord(kind="failure")
3. Store each record via AgentTypeService.add_knowledge()
4. Set `memory_delta_artifact_id` on the CompactionRecord
5. Make this opt-in per agent type: `memory_delta_enabled: bool`

### Acceptance criteria
- After compaction with LLM summarizer, knowledge records exist
- Without LLM summarizer (fallback template), no records created
- Field properly populated and queryable

---

## GAP 10: LLM-Driven Task Decomposition

### Problem
Scheduler always creates one "implementation" task regardless of plan complexity.

### Required implementation
1. Add a decomposition step in SchedulerService before creating execution:
   - Send the approved plan revision content to the LLM with a prompt
     asking for a JSON array of tasks with dependencies
   - Schema: `[{"key": "auth", "objective": "...", "scope": [...],
     "depends_on": []}, …]`
   - Validate: ≤256 tasks, ≤1024 edges, acyclic, non-empty objectives
   - Fall back to single-task on parse/validation failure
2. Make this opt-in via config: `decomposition.enabled: bool`
3. Cache decomposition results keyed by plan_revision_id (idempotent)
4. Log the decomposition prompt/response pair as evidence artifacts

### Acceptance criteria
- Simple plans still produce single-task graphs (backward compatible)
- Complex plans produce multi-node dependency graphs when enabled
- Decomposition failure falls back gracefully to single-task

---

## GAP 11: Real Token Counting

### Problem
Token counting uses bytes÷4 heuristic everywhere.

### Required implementation
1. Add `tiktoken` as optional dependency under `[tokenizer]` extra
2. Create `src/zero/manage/core/tokenizer.py`:
   ```python
   def count_tokens(text: str, model: str) -> int:
       """Use tiktoken when available for known models; fall back to bytes÷4."""
   ```
3. Model→encoding mapping for common models (cl100k_base, o200k_base)
4. Thread through to estimate_tokens(), compaction threshold checks,
   context builder budget calculations, and usage cost estimation
5. Graceful degradation: tiktoken not installed → bytes÷4 (current behavior)
6. Cache encoding objects at module level (tiktoken is expensive to init)

### Acceptance criteria
- With tiktoken installed: accurate counts for GPT/Claude models
- Without: current bytes÷4 heuristic (documented as approximate)
- No behavioral change in tests that don't install tiktoken

---

## GAP 12: Rate-Limit-Aware Task Retry

### Problem
Tasks have attempt budgets but retries happen immediately with no delay.

### Required implementation
1. In the scheduler's requeue logic, track last_failure_at timestamp
2. Compute delay before requeueing:
   - Base delay: 60s * 2^(attempt_number - 1), capped at 3600s
   - Jitter: uniform random [0, 0.5 * base_delay]
   - If provider error contained Retry-After: honor it (capped 3600s)
3. Store next_retry_at in task metadata (blocker_reason or new column)
4. Scheduler skips requeueing until next_retry_at has passed
5. Expose next_retry_at in GET /executions/{id}/tasks response

### Acceptance criteria
- Failed tasks wait exponentially longer between retries
- Retry-After from provider rate limits is honored
- Tasks stuck in permanent failure eventually exhaust budget and block

---

## ORDER OF IMPLEMENTATION

Phased to minimize risk and maximize value:

| Phase | Gaps | Effort | Dependencies |
|-------|------|--------|-------------|
| 1 | GAP 11 (tokenizer) + GAP 12 (retry backoff) | Small | None |
| 2 | GAP 5 (streaming) + GAP 6 (chat endpoint) | Medium | None |
| 3 | GAP 9 (memory deltas) + GAP 10 (decomposition) | Medium | Phase 1 |
| 4 | GAP 3 (sandbox executor) | Medium-large | None |
| 5 | GAP 2 (PostgreSQL) | Large | None |
| 6 | GAP 8 (subagents) | Medium-large | Phase 2 |
| 7 | GAP 7 (MCP + plugins) | Medium | Phase 2 |
| 8 | GAP 4 (user-session Telegram) | Medium | Phase 2 |
| 9 | GAP 1 (live qualification) | Small | All above |

## QUALITY REQUIREMENTS

For EVERY gap:
- Unit tests covering happy path + edge cases + error handling
- Integration tests exercising the real component (not mocks)
- No TODO/FIXME/stub left in shipped code
- Backward compatibility maintained (existing tests still pass)
- Documentation updated in docs/
- Security review for any new attack surface

## VERIFICATION CHECKLIST

Before declaring done:
1. Full test suite passes (>650 tests expected)
2. ruff check + format --check + compileall clean
3. Each gap's specific acceptance criteria met
4. Manual smoke test of user-facing features (wizard, TUI, GUI)
5. Live integration tests pass with real credentials (GAP 1)
6. No secrets in logs, configs, or diagnostic outputs
7. CHANGELOG updated with new features
