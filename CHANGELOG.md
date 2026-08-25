# Changelog

All notable changes to Zero Develop are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/); versions
are milestone-based rather than semver until 1.0.

## [Unreleased] — production-readiness gap closure (GAPs 1–12)

Design documents for every gap: `docs/gap-designs/` (committed before
any implementation).

### Added

- **GAP 11 — real token counting** (`zero/manage/core/tokenizer.py`,
  `[tokenizer]` extra): tiktoken exact counts for known GPT families
  (`o200k_base`, `cl100k_base`) with module-level encoding cache;
  bytes÷4 fallback preserved everywhere else. Threaded through
  `estimate_tokens`, the compaction fit ladder, context-builder
  budgets, retrieval scoring, and a new pre-flight
  `estimate_request_tokens`.
- **GAP 12 — rate-limit-aware task retry**
  (`zero/app/retry_backoff.py`, migration `0030`): exponential backoff
  (60 s × 2ⁿ capped at 1 h) with jitter; provider Retry-After honored;
  `tasks.next_retry_at` column + scheduler gating + API exposure on
  `GET /executions/{id}/tasks`.
- **GAP 5 — client-facing SSE streaming**: `ExecutionStreamHub`
  fan-out; provider-level `stream_observer` tap
  (`text_delta`/`tool_call`/`done`) that leaves durable bookkeeping
  unchanged; `AgentRuntime.run_task(..., stream_callback=...)`;
  `GET /admin/executions/{id}/stream` with 15 s keepalive heartbeats;
  GUI live-stream panel (fetch + ReadableStream) and TUI Chat screen.
- **GAP 6 — interactive chat endpoint**: `ChatService` ephemeral
  single-turn completions through the normal provider chain with
  optional granted-tool rounds, token-bucket rate limiting
  (`ZERO_CHAT_RATE_LIMIT_PER_MIN`, default 10), usage recorded via the
  standard path; `POST /admin/chat/{project_id}` JSON endpoint plus an
  admin chat panel.
- **GAP 9 — memory delta artifacts**
  (`zero/app/memory_delta.py`): "Accepted decisions" / "Blockers or
  failures" sections of LLM compaction summaries become durable
  KnowledgeRecords linked by a memory-delta artifact and recorded in
  the reserved `memory_delta_artifact_id`; opt-in per agent type via
  `model_policy["memory_delta_enabled"]`; deterministic fallback
  summaries never extract.
- **GAP 10 — LLM task decomposition**
  (`zero/app/task_decomposition.py`): validated JSON task graphs
  (≤256 nodes, ≤1024 edges, acyclic) cached per plan revision;
  scheduler falls back to the historical single task when disabled
  (default) or on any failure.
- **GAP 3 — production sandbox executors**
  (`zero/app/executors/`): `CommandExecutor` protocol with
  HostBounded/Docker/Firejail backends; Docker runs use no-network,
  pid/mem/cpu caps, no-new-privileges, cap-drop ALL (+CHOWN/SETUID),
  non-root uid, single worktree bind mount, watchdog kill on timeout;
  fail-closed availability probing at composition;
  `ZERO_SANDBOX_EXECUTOR=none|docker|firejail`; production permits
  host_bounded mode only with a genuine sandbox selected; capability
  report names the active executor.
- **GAP 2 — PostgreSQL backend**
  (`zero/persistence/pg_connection.py`, `dialect.py`,
  `migrations_pg/`, `[pg]` extra): pooled psycopg backend mirroring
  the SQLite facade (SAVEPOINT nesting, dict rows with positional
  access, sqlite3 exception mapping so all repositories stay
  backend-agnostic); bounded SQLite→PG SQL translation incl. all 105
  RAISE-guard triggers → plpgsql functions; generated committed
  `migrations_pg/*.sql` via `scripts/gen_pg_migrations.py`;
  dual-dialect migration runner with advisory-lock fencing;
  `postgresql://` URLs accepted only when psycopg is importable
  (fail-closed otherwise); `ZERO_PG_POOL_MIN/MAX` (2/20); optional
  compose `postgres` service; container tests marked `pg_integration`.
- **GAP 8 — subagent delegation** (`zero/app/delegation.py`):
  runtime-owned `delegate` tool; isolated in-process child contexts
  with fresh conversations and intersection-only tool narrowing
  (workspace tools excluded by default); depth cap 3 via ContextVar;
  child provider requests tagged `sub_agent_type` so
  `is_whole_tree=False` keeps whole-tree aggregation correct;
  structured error payloads never crash the parent.
- **GAP 7 — MCP client + plugin registry**
  (`zero/manage/core/mcp_client.py`,
  `zero/manage/plugins/registry.py`, `[mcp]` extra): MCP stdio
  JSON-RPC transport (initialize/tools list/call); tools registered as
  `mcp_<server>_<tool>` through the standard grant/redaction/audit
  pipeline; plugin discovery from `$ZERO_HOME/plugins` +
  `/opt/zero/plugins` with system→user alphabetical load order,
  `register(manage_context)` contract, and per-plugin failure
  isolation; sample plugin `examples/plugins/echo_upper.py`.
- **GAP 4 — user-session Telegram mode**
  (`zero/adapters/user_session.py`, `[session]` extra): Telethon-backed
  adapter gated on explicit `ZERO_TELEGRAM_MODE=user_session` AND
  importability; same NormalizedEvent intake/access-policy gate as Bot
  API; outbound 30/min token bucket (cap 60);
  `run_session_login` disclaimer → phone → OTP → 2FA flow held entirely
  in memory, returning the session string for encrypted storage.
- **GAP 1 — live integration qualification**
  (`tests/integration_live/`, `.github/workflows/live-tests.yml`,
  `docs/LIVE_TESTING.md`): six double-gated live tests (getMe /
  sendMessage / poll / OpenAI / Anthropic / incremental streaming)
  against the real production adapters; dispatch-only CI workflow.

### Changed

- Environment-compatibility hardening required by current toolchains:
  bodyless-204 routes drop PEP563 `-> None` annotations (fastapi),
  secret request payloads use `SecretStr` (pydantic ≥2.10 warning
  compliance), provider HTTP contract test parses JSON bodies instead
  of asserting transport-specific separators.

### Security

- Docker sandbox: no network namespace, dropped capabilities,
  non-root uid, worktree-only bind mount.
- User-session: OTP/2FA material never persisted or logged; session
  strings stored only as Fernet-encrypted secrets.
- Chat/stream endpoints require admin auth; prompts/responses are not
  logged; tool outputs pass existing redaction.
