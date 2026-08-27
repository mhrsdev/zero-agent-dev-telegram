# Changelog

All notable changes to Zero Develop are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/); versions
are milestone-based rather than semver until 1.0.

## [Unreleased] — Hermes-parity hardening (G1 + G2)

### Added

- Per-call tool approval gate (GAP 8b/G2, Hermes parity): opt-in via
  ``ZERO_TOOL_APPROVAL_MODE=manual``. Durable ``tool_approval_decisions``
  table (migration 0031) is both pending queue and decision record;
  service semantics port Hermes' layered approvals — hardline floor
  denies catastrophic arguments under any allowlist, deny rules outrank
  allows (``grain=always`` denial escalates to a TOOL-WIDE wildcard),
  standing always-allows key on canonical argument hash (tool-wide row
  supported), session grants scope in-process per execution, and the
  pending window carries a TTL so the runtime never blocks forever
  (model receives ``approval_pending``/``approval_denied`` as
  structured tool errors instead of dying). REST surface:
  ``GET /projects/{id}/tool-approvals`` (project.view) and
  ``POST .../tool-approvals/{request_id}/resolve`` (tool.manage);
  disabled deployments answer 409. Golden route tables updated.

### Changed

- AgentRuntime execution loop resilience (GAP 8b/G1, Hermes parity):
  malformed/unparseable or non-object tool arguments and undeclared
  tool names no longer raise-and-kill the attempt — the model receives
  structured error payloads (with declared-tool surface revealed) and
  can self-correct; guessed arguments are never executed.
  ``finish_reason=length`` together with unparseable arguments discards
  the broken assistant turn and re-asks with doubled ``max_tokens``
  (bounded 2 boosts, cap 32768) before anything executes. An
  identical-failure breaker counts repeated identical tool failures,
  injects an explicit change-approach steering note at the third
  failure and falls through to the summary nudge at five; terminal
  errors distinguish breaker-trip from round-budget exhaustion.

## [Unreleased] — Forced tool-call path (S7) and production env preset

### Added

- Canonical ``tool_choice`` support end to end:
  ``normalize_tool_choice`` folds modes (`auto`/`none`/`required`) and
  forced-function shorthands into one canonical shape;
  OpenAI-compatible and Anthropic adapters render it per protocol
  (nested `{type:function,function:{name}}` vs `{type:auto|any|tool}`);
  payloads stay byte-identical when unset, request hashes change only
  when set, provider-fallback reconstruction preserves it
  (task forcing survives a failover), and Anthropic's missing `none`
  mode fails loudly instead of silently doing nothing.
- Decomposition is now wired into the production composition root
  (services builder): the scheduler receives a real `TaskDecomposer`
  over the shared provider service, so `ZERO_DECOMPOSITION_ENABLED=1`
  takes effect on live servers (previously the flag existed but no
  decomposer was ever constructed — flipping it was a silent no-op).
- S7 recovery analytics (`zero.app.decomposition_analytics`): every
  decompose() outcome is recorded with attempts used, rescued
  near-miss dependency repairs (raw typo + Jaccard score), escalation,
  legacy degradation and single-task fallback facts, aggregated PER
  MODEL. Headline metric `typo_rate_per_graph` tracks per-model slug
  discipline over time; JSONL evidence sink via
  `ZERO_DECOMPOSITION_ANALYTICS_PATH` keeps durable audit lines from
  live servers without schema changes. Repair planning is factored
  into pure `plan_dependency_repairs` / `apply_dependency_repairs`
  while `repair_dangling_dependencies` keeps its exact contract.
- Decomposition ladder deepening (GAP 10 hardening):
  strict system prompt plus a single forced `emit_task_graph` tool
  call replaces reliance on free-text JSON; an escalated re-ask with
  even stricter phrasing covers one bad reply; deterministic recovery
  of structured output repairs near-miss dependency keys (unique-best
  snake_case Jaccard >= 0.5, duplicate edges collapsed) and
  normalizes dependency order before any re-ask is spent; providers
  without native tools (capability gate or gateway 4xx on tools)
  degrade once to the legacy text contract; transient failures stop
  the ladder immediately; idempotency keys are attempt-scoped.
- Live GLM evidence (real bridge to GLM-class model): all three probe
  plans produced validated multi-task DAGs on the first forced ask
  (10–22 tasks, 11–23 edges, 9.8s–23.8s), ahead of legacy free-text
  duration; two previously-failing raw responses recover exactly via
  key repair (`build_vendor_dashboard` -> declared key). Evidence:
  `scripts/logs/s7/{summary.md,probe_results.jsonl,raw/,bridge.log}`,
  harness `scripts/s7_decomposition_probe.py`, transport
  `scripts/ai_bridge/server.mjs`.
- `.env.example` restructured (core/workers/providers/execution/
  interfaces) with a go-live **production preset**; every knob name
  cross-checked against source, retry/backoff semantics documented,
  previously-undocumented operational knobs surfaced
  (`ZERO_DECOMPOSITION_ENABLED`, sandbox executor, PG pool bounds).

### Tests

- New `tests/test_s7_tool_call_decomposition.py`: 30+ cases covering
  canonicalization, both wire formats (incl. stream path omission),
  hash sensitivity/fallback propagation through a real service stack,
  extraction preference order, the full ladder matrix, repair rules,
  and degradation semantics.

## [Unreleased] — HTTP API decomposition

### Changed

- `src/zero/app/api.py` (2,801 lines) split into the
  `zero.app.routers` package: sixteen per-domain router modules
  (`auth`, `identity`, `authorization`, `secret`, `tool`, `audit`,
  `plan`, `execution`, `artifact`, `topology`, `provider`,
  `worktree`, `integration`, `interface`, `webhook`, `health`) plus
  shared `deps.py` (`authorized_actor`/`request_project_actor`;
  45 renamed call sites) and `models.py` (the `_StrictRequest`
  request-model family). `api.py` retains application assembly only
  (304 lines), with the sixteen `register_*` call sites kept in
  their original registration order.
- Public HTTP behavior was pinned before any motion by
  `tests/test_api_route_surface.py`: all 89 JSON routes, a
  duplicate-registration guard, and an OpenAPI contract that also
  covers the 19 management-web operations which reach the schema
  through the include wrapper. Green before, during, and after the
  split.
- Test-surface follow-up: the anti-spoofing test resolves
  `StoreArtifactRequest` from its canonical module
  (`zero.app.routers.models`) instead of the old `app.api`
  attribute surface; the assertions themselves are unchanged.

## [Unreleased] — engineering pass (analysis, fixes, consolidation)

### Fixed

- **Wizard group discovery crashed with `NameError`**:
  `probes.telegram_recent_chats` referenced an undefined module
  global; it now uses the shared `_telegram_base()` helper. Covered
  by a regression test driving a local HTTP stub through
  `ZERO_TELEGRAM_API_BASE`.
- **Pool misconfiguration masked by an install hint**: pool bounds
  are validated before the `[pg]` capability gate, so
  `ZERO_PG_POOL_MIN > ZERO_PG_POOL_MAX` reports the real error even
  on hosts without psycopg.
- **Release gate rejected every build**: the hand-maintained expected
  migration list stopped at 0028 while the tree ships 32 migrations;
  the set is now derived from the migrations directory, and a
  regression test guards against both list drift and a silently
  empty gate.
- **CI release smoke asserted the wrong CLI contract**: bare
  `zero-develop` exits in argparse now that subcommands are required;
  the fail-closed probe and server start both invoke
  `zero-develop serve` explicitly.
- **Load-flaky provider retry test**: the wall-clock budget
  (`elapsed < 2.0s`) measured loopback latency under load; the test
  now records `time.sleep` inputs and asserts Retry-After: 0
  produces exactly two zero-second sleeps.

### Changed

- Dev dependencies now include `httpx2>=2.0.0`: starlette >=1.0
  prefers it for `TestClient`, and its fallback warning tripped the
  repo's fail-closed warnings policy on fresh installs.
- `$ZERO_HOME` resolution consolidated into
  `zero.manage.core.config.zero_home`; seven duplicated readers were
  removed with unchanged behavior.

### Removed

- Accidentally committed SQLite sidecar files (`zero_develop.db-shm`,
  `zero_develop.db-wal`); ignore patterns extended to `*.db-shm` /
  `*.db-wal`. A byte-level scan found no secrets in the blobs.

### Documentation

- README corrected (Python floor is 3.11+, clone URL, `serve`
  subcommand, LICENSE reference); current-state ledger repopulated.

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
