# Changelog

All notable changes to Zero Develop are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/); versions
are milestone-based rather than semver until 1.0.

## [Unreleased] — independent audit fixes (Phase 3–17 findings)

Every fix has a reproduction + regression test under `tests/test_audit_*`
plus targeted suites; full evidence in the audit report.

### Fixed — Critical / High

- **Telegram intake crashed whenever managed config set
  `access.owner_project_id`** (`_CfgView` walrus/del leftover raised
  UnboundLocalError on every event). Groups now flow as plain dicts and
  `policy.build_gate` normalizes both dict- and object-shaped groups.
- **Owners were never recognized by the access-policy gate**: the owner
  lookup called a nonexistent repository method and the broad except
  swallowed it; now calls `list_external_identities_for_user` and logs
  lookup failures.
- **`POST /admin/providers/{id}/test` required no session or CSRF**,
  letting anyone who could reach the loopback port trigger paid provider
  probes. Now guarded like every other mutating admin route.
- **Engine bearer middleware made `/admin` unreachable in production**
  (two auth systems collided). `/admin/*` is exempt from the bearer
  gate; the GUI keeps its own scrypt-password + CSRF scheme.
- **`zero setup` could never finish**: no secret store was wired, so
  commit always refused with "secrets not stored". The CLI now persists
  secrets through the encrypted engine store, bootstraps
  `ZERO_SECRET_KEY` into `$ZERO_HOME/secret.key` + `.env` (0600), routes
  non-interactive steps through validation, and reports commit failures
  as clean exit-code-2 messages instead of tracebacks.

### Fixed — Medium

- `zero doctor` crashed with a raw YAML parser error on corrupted
  config.yaml; it now reports a failing `config` check.
- Three CLI commands (`capabilities`, `backup-daemon`, `backup-status`)
  had parsers but were unreachable from dispatch.
- Wizard silently dropped collected values: fallback-models CSV,
  agents default agent for groups, updates auto-apply, group discovery
  token field name. The unwired compaction-threshold field was removed
  rather than pretending to persist it.
- TUI hardcoded admin port 8787 while every server start used 8000;
  both now honor `ZERO_PANEL_PORT`.
- Dashboard linked to nonexistent `/web/projects/new`.
- Password change did not invalidate existing admin sessions; sessions
  are now purged on rotation. Login brute-force lockout added
  (5 failures / 10 min per client IP).
- `_ensure_setup_code` crashed first-run bootstrap when `$ZERO_HOME`
  did not exist.
- CLI engine bridge leaked one real HTTP client per invocation in dev
  mode; transports are closed after wizard secret operations.

### Changed

- Doctor `--fix` no longer claims automated fixes were applied.
- GUI usage loader logs query failures instead of rendering empty
  tables silently.
- Plugins receive real managed config and a name-scoped secret facade
  (management project only) at composition time.
- `probes.telegram_get_me` honors `ZERO_TELEGRAM_API_BASE` (self-hosted
  Bot API gateways / tests).
- README configuration table documents all new environment variables
  and optional extras.

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
