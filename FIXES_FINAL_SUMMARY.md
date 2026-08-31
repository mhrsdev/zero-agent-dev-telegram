# FINAL FIX SUMMARY — Zero Agent Dev Telegram

This document consolidates every gap, regression, and live-found bug fixed in this
codebase, verified against the Hermes Agent reference architecture
(github.com/nousresearch/hermes-agent) and against a REAL live deployment
(api.justwoker.icu / claude-opus-5 / Telegram bot 8753924431 / group -1004406039396).

Full test suite at packaging time: **1235 passed / 0 failed** (deterministic) +
28/28 live e2e battery + 45/45 wave10+wave11 hardening regressions.

---

## Round 1 — Hermes-parity gaps (G1–G13) + pre-existing regressions (F1–F4)

| ID | File(s) | Problem | Fix |
|----|---------|---------|-----|
| F1 | `src/zero/app/agent_runtime.py` | Delegation tool-invoke audit import from non-existent `_now_utc_iso` in tool_service → every delegation audit write died on ImportError and was silently swallowed | Import `now_utc_iso` from `zero.app.clock` |
| F2 | `src/zero/app/api.py` | Auth middleware ran synchronous SQLite inline inside async coroutine (event-loop blocking) | Offload authenticate() + project-scope check via `starlette.concurrency.run_in_threadpool` |
| F3 | `scripts/probe_gateway_tools.py` | LIVE API key hardcoded in a tracked file (publicly leaked) | Env-read, fail-closed. **Operator note: rotate the key.** |
| F4 | `src/zero/app/scheduler_service.py` | Integration stage gated on raw `repository_id` instead of `effective_repository_id` → integration reviews/combined tests/merge proposals skipped for managed ticks | Gate on `effective_repository_id` |
| F5 | `src/zero/adapters/telegram.py` | (a) No bot-sender filter → bot loops possible; (b) answered every group message (no mention gating); (c) no burst coalescing of split messages; (d) `allowed_updates` missing channel_post/edited_channel_post | (a) `ZERO_TELEGRAM_ALLOW_BOTS` (default none); (b) `ZERO_TELEGRAM_REQUIRE_MENTION` (default true) + `ZERO_TELEGRAM_MENTION_EXEMPT_CHATS` + per-group `require_mention` override, mention/text_mention/reply-to-bot/commands handling, fail-open when bot identity unresolved; (c) in-batch coalescing (same chat+actor+topic, date-gap ≤ 120s, replay dedup, commands/media never merge); (d) extended `allowed_updates` |
| F6 | `src/zero/app/interface_transport_service.py` | Webhook verifier adapter ignored `ZERO_TELEGRAM_API_BASE` | Honors the env base |
| F7 | `src/zero/app/telegram_live.py` | No flood-strike circuit breaker → progressive edits could 429-loop | `_MAX_FLOOD_STRIKES = 3`, disables progressive edits, finalize still attempts |
| F8 | `background_workers.py`, `interface_transport_service.py`, `config_sync.py`, `manage/core/config.py` | User-session Telegram mode fully dormant (adapter existed, nothing constructed/polled it) | `TelegramCfg.mode = user_session` wired end-to-end: `BackgroundWorkerHost._user_session_loop` + `_UserSessionHost` (Telethon NewMessage handler, out-filter, per-chat serial dispatch), transport `attach_session_adapter`, token-less binding outbound, config_sync user-session bindings |
| F9 | `src/zero/app/config_sync.py` | Env-only deployments (no config.yaml) never created bindings/secrets/bootstrap → dead bot | Env bootstrap synthesizes config.yaml from `ZERO_OPENAI_API_KEY` / `ZERO_TELEGRAM_BOT_TOKEN` / `ZERO_TELEGRAM_GROUP_IDS` (ENV: sentinel refs resolved into encrypted secret rows) |
| F10/F11 | `GroupPolicy`, polling loop | No per-group require_mention override; poller polled before knowing its own identity | `GroupPolicy.require_mention`; `_build_binding_adapter(bot_username, bot_id, group_chat_id)`; getMe identity resolved BEFORE first poll |
| F12 | `approval_gate.py`, migrations 0033 (sqlite+pg), `interface_service.py`, `api.py` | ToolApprovalGate had NO Telegram notification and no inline buttons (only /approvals + HTTP) | `tool_approval_tokens` migration + domain/repo CRUD; `gate.attach_notifier` fires ONLY on fresh pending; `create_tool_approval_token` / `send_tool_approval_card` / `_process_tool_callback` (tool.manage permission, one-shot, scope+expiry checks) |
| F13 | `src/zero/manage/core/mcp_client.py` | Blocking readline with no timeout → a hung MCP server blocked boot forever; shutdown deadlock (close-vs-reader) | Reader-thread pump + bounded `_request` (10s default, 120s tool calls); terminate BEFORE close on shutdown |
| F14 | `telegram_commands.py` | `/new` (clear chat scope) dormant — `ChatHistoryRepository.clear` had no caller; `/id` missing | `/new` clears chat-scope history; `/id` added; `/help` updated |

---

## Round 2 — Live-found bugs (H1–H3)

| ID | File(s) | Problem | Fix |
|----|---------|---------|-----|
| H1 | `tool_service.py`, `config_sync.py` | Internet-search handler never REBOUND on restart → "No handler registered" 500s after every restart | `ToolService.rebind_server_handler` + called on existing-row branch |
| H2 | `tools_websearch.py` | Websearch flapped on transient DDG ConnectTimeout | Bounded 2-attempt retry for transient errors only |
| H3 | `tests/integration_live/*` | Fixtures drifted (adapter built without mandatory transport → live tests unpassable) | Fixture fixed + gateway edge-403 retry helper + buffered-SSE honest assertion |

---

## Round 3 — Bugs from the real live execution log (B1–B8)

| ID | File(s) | Problem (seen live) | Fix |
|----|---------|---------------------|-----|
| B1 | `persistence/repositories/agent_type_repository.py` | `agent type concurrency limit reached: ... already has 1 running instance(s); max_concurrent_instances=1` → tasks marked failed, graph blocked (`Blocker: task failed; graph cannot proceed`) — caused by STALE instance rows leaked from interrupted executions | `release_stale_running_instances()`: an instance lease is valid ONLY while its task is `running`; boot sweep + per-execution recovery release the rest |
| B1b | `src/zero/app/background_workers.py` | No recovery at boot for executions interrupted by a restart | `_startup_recovery()`: global stale-instance sweep + `recover_after_restart` for EVERY non-terminal execution + worktree boot sweep |
| B1c/B2 | `src/zero/app/worktree_service.py`, `persistence/repositories/worktree_repository.py` | `UNIQUE constraint failed: worktrees.task_id` → `workspace/context setup failed: IntegrityError` on task re-attempt (2 real poisoned rows confirmed in live DB) | `_abandon_worktree()` (legal transition out of allocated/active/interrupted + best-effort git cleanup) called from `create_worktree` on re-attempt AND `abandon_stale_worktrees()` boot sweep; repo gained `list_worktrees_in_states` |
| B3 | `src/zero/app/agent_runtime.py` | `evidence/postcondition failed: RuntimeEvidenceError` with ZERO cause recorded | `_failure_detail()`: failure wrappers record redacted exception Class+message (3 wrapper sites) |
| B4 | `provider_service.py`, `telegram_live.py`, `telegram_chat.py` | Garbled tool-call rendering (`🔧 ?(and")`, `🔧 ?(: "ls")`) — one garbled line per streaming fragment | `_tap_stream`: per-call_id accumulation (`pending_name` buffering, `replace=True` on later fragments); `TelegramLiveStream.on_tool_call` + `TelegramExecutionProgress.on_stream_event` REPLACE the pending line |
| B5 | approval card path | `🔧 Tool approval needed / Execution: -` repeated with empty execution reference | Card always shows `Approval: <id>`; execution line shows `(ad-hoc / chat)` instead of bare `-` |
| B6 | `src/zero/app/worker_service.py` | At-capacity agent types caused claim+terminal-fail instead of waiting | `agent_type_at_capacity()` pre-check in `run_ready_tasks`: at-capacity tasks are DEFERRED (stay `ready`) instead of claimed+failed |
| B7 | `background_workers.py` | `'_ChatSerialDispatcher' object is not callable` → bot processed ZERO polled messages | `__call__` alias: adapter calls `background_dispatch(_run)` |
| B8 | `src/zero/app/worker_service.py`, scheduler wiring | Boot-only recovery left dead-lease tasks blocking their graph forever when the lease was still live at boot | `WorkerService.reconcile_expired_leases()` + scheduler-tick wiring: expired-lease running tasks recovered EVERY tick (and never steals live work) |

Runtime config for bounded retry: `ZERO_TASK_MAX_ATTEMPTS=8` (retry w/ backoff),
`ZERO_EVIDENCE_TEST_COMMAND="python3 -m pytest -q"` (run_full_test_suite evidence
tasks previously failed with no command configured).

---

## Verification at packaging time

- Deterministic suite: **1235 passed / 0 failed** (includes 25 wave10 + 20 wave11 new regression tests)
- Live e2e battery: **28/28** (health, capabilities, metrics, Telegram getMe/send/commands/single-poller-lock, provider completion+streaming+native tool-calls, identity link+verify, plan+revision+provenance, agent types+knowledge, isolated runner, websearch, REAL MCP stdio roundtrip, tool-approval pending→card→resolve, chat bridge live-streamed to the real group, real LLM planner proposal, REAL 7-task decomposition graph, scheduler tick, agent runtime, RAG ingest+approve+FTS+retrieval+ledger, REAL LLM compaction, usage ledger, auth bootstrap, interface bindings/deliveries, plan-card callback tokens)
- Live DB invariants after fixes: **0 stale agent instances, 0 stale worktrees**
- Known external (NOT a code bug): api.justwoker.icu origin occasionally flaps Cloudflare 522s — pipeline retries automatically; failures are honest and diagnosable in durable task errors.

## Regression test files added

- `tests/test_hermes_parity_hardening_wave10.py` (25 tests)
- `tests/test_live_hardening_wave11.py` (20 tests)
- Migration: `src/zero/persistence/migrations/0033_tool_approval_tokens.sql` (+ `_pg` variant)

## Operator notes

1. **Rotate the leaked API key** (it was committed upstream in `scripts/probe_gateway_tools.py` before this fix; the file is now env-read/fail-closed).
2. Boot recovery + tick reconciliation are automatic; no manual DB surgery is required after restarts/crashes.
3. See `docs/LIVE_RUN_REPORT.md` for the live run narrative and `realrun-evidence/` for artifacts.

---

## Round 4 — Resumed live run with the API alive (2026-08-31, B10–B14)

The engine was resumed against the real gateway (claude-opus-5 via
api.justwoker.icu) and real Telegram group. The greeting goal was re-run
end-to-end. Four more real bugs were found and fixed; the goal now
COMPLETES the full pipeline (planner → decomposition → scheduler → agent
runtime with worktree + tools → evidence pytest exit=0 → result delivery
to the group, message_id=805).

| ID | File(s) | Problem (seen live) | Fix |
|----|---------|---------------------|-----|
| B10 | `worktree_service.py`, `agent_runtime.py` | Every worktree carries the server-managed hygiene `.gitignore` (committed at creation), so the cumulative diff was NEVER empty and the "required diff evidence" gate passed even when the agent produced nothing. Additionally, the cumulative FALLBACK (meant for aggregation tasks) let generative tasks pass on their dependencies' work alone (the live "create the test module" task completed without creating any file). | Hygiene paths excluded from incremental/cumulative diffs and the status section; a genuinely change-less attempt yields EMPTY diff content (rejected by the existing gate). PLUS: a generative objective (create/write/fix/…) whose diff evidence is only the no-change fallback marker now raises `RuntimeEvidenceError`. |
| B11 | `executors/sandbox.py`, `worktree_service.py` | The scrubbed child env resolved bare `python3`/`pytest` to the SYSTEM interpreter — "No module named pytest" on every evidence run. | `scrubbed_env` exposes the engine venv (`VIRTUAL_ENV` + venv PATH) when venv-mounted; `host_interpreter_argv` rewrites bare `python`/`python3`/`pytest` to the engine's own interpreter on the HOST path only (never container backends). |
| B12a | `approval_gate.py`, `config.py`, `services.py` | `ZERO_TOOL_APPROVAL_MODE` had no unattended option (`off`/`manual` only); manual gated EVERY call including `ls`/`git status` — the live agent burned whole attempts on pending approvals and gave up. | New `auto` mode: hardline floor + operator deny rules stay enforced, everything else flows without a human click. Hardline matching hardened for argv-shaped JSON (strip keys/separators before matching — `"rm", "-rf", "/"` now matches). |
| B12b | `approval_gate.py` | Manual mode had no read-only triage. | Provably read-only calls (read_file, capture_diff, `ls`/`grep`, read-only git subcommands, `git worktree list`) auto-allow with cause `safe_readonly`; deny rules still outrank. |
| B13 | `agent_runtime.py` | Retries ran with the IDENTICAL prompt (blind), and the evidence error said only `exit=5` — the agent could not know "no tests ran" means CREATE the tests. | `_task_prompt_with_retry` appends the last failed attempt's redacted error + pattern-specific actionable guidance (e.g. "no tests ran → create the test files yourself with write_file"); the evidence error now carries a bounded command-output tail. |
| B14 | `config_sync.py` | **THE BIG ONE**: the tool-capability model requires a grant per (project, tool, agent_scope); only `internet_search` was ever granted. EVERY task agent's workspace tool call (`read_file`/`write_file`/`run_command`/`capture_diff`) was denied with "No grant for tool … in scope …" for the whole deployment — agents could do nothing, while the runtime's own evidence commands (bypassing ToolService) kept working and masked the gap. | `_ensure_workspace_tool_grants` at config sync: grants all four workspace tools to `main_worker` + `sub_agent_type`, idempotently, at every boot. |
| CORRUPTION | `tool_service.py` | The source contained a syntax-breaking corruption at 2 sites (`self._handlers[handler_key]` → `self._handlersandler_key`) — masked by stale bytecode caches; any fresh install would die with SyntaxError. (The corruption was baked into the previous archive/commit.) | Repaired both sites; full `compileall` clean; all bytecode caches flushed. |
| cosmetic | `worker_service.py` | Executions showed a stale "awaiting automatic task retry" blocker while running. | Claiming work on a paused execution clears the blocker. |

### Resumed-run verification

- Fresh greeting goal `exec_5q6dolo3fid5d0tszoufbji6`: **completed** — all 4 tasks green, evidence pytest exit=0 (agent-created `tests/test_greeting.py` asserting `greet("World") == "Hello, World!"`), result delivered to the real group (message_id=805).
- DB invariants after the run: 0 stale agent instances, 0 stale worktrees.
- Full deterministic suite: EXIT=0 (includes 25 new wave12 regression tests).
- Live battery 28/28 re-verified earlier in this session (see Round 2 evidence).

---

## Round 5 — Mega-scale live run: super-massive project/task/team management (2026-08-31, M15)

The hardest real run: 3 real projects, 3 real teams (10 agent types with
staggered concurrency limits), 3 large goals decomposed by the REAL LLM
planner into 44 tasks with dependency graphs, all worked concurrently
against the real gateway and real Telegram group.

### Live-found bug

| ID | File(s) | Problem (seen live) | Fix |
|----|---------|---------------------|-----|
| M15 | `background_workers.py`, `config.py` | **Head-of-line blocking across projects**: the managed worker iterated projects SEQUENTIALLY. One project's long tick (the 15-task textkit graph grinding its frontier for ~9 minutes) starved every other project's scheduling — the freshly approved Nettools/Dataviz plans sat UNCLAIMED for 12+ minutes while P1 monopolized the loop. `tick_parallel_executions` only parallelizes executions WITHIN one project's tick, so nothing helped across projects. | New `ZERO_TICK_PROJECT_PARALLELISM` (1..8, default 1 = historical serial). N>1 ticks up to N projects concurrently via a bounded ThreadPoolExecutor. Per-project error isolation preserved (`scheduler:{id}` / `reconcile:{id}` / `scheduler-parallel:{id}` error records); claims/leases remain exactly-once — the same concurrency model the intra-tick execution pool already exercises. Restart safety was already in place: boot recovery reconciled the interrupted P1 frontier task automatically (0 stale instances, 0 stale worktrees). |

### Live-verified engine invariants (observed, no code change needed)

- **One-chat-one-project governance**: `create_binding` refuses to bind the
  same Telegram group to a second project ("interface binding already
  belongs to another project") — a real scope-isolation invariant, pinned
  here as documented behavior. Mega setup therefore gave P2/P3 their own
  encrypted bot-token secrets, and progress fan-out stays on P1's binding.
- **At-capacity deferral (B6) under real load**: `live-keeper` raised to
  max_concurrent_instances=2 runs 2 tasks in parallel worktrees while
  further ready tasks stay `ready` (never claimed+failed).
- **Automatic retry with backoff (GAP 12)**: a P3 task interrupted by the
  engine restart re-entered the queue with `next_retry_at` set and the
  execution honestly `paused` with "awaiting automatic task retry".

| M15b | `background_workers.py` | The pool-per-cycle variant still joined the SLOWEST project's tick: fast projects ticked only once per P1-cycle (P1's long graph drain gates every cycle). | Independent per-project scheduler loops (Hermes-style per-scope loops): each project ticks at its own cadence; a coordinator discovers projects DYNAMICALLY (projects created after boot join without an engine restart; dead loops are respawned); `ZERO_TICK_PROJECT_PARALLELISM` bounds concurrent ticks via a semaphore (provider load cap). Serial default (1) untouched. |
| M16 | `config_sync.py` | **Per-project tool floors were management-project only**: operator-created projects had ZERO tool grants, so every task agent's `read_file`/`write_file`/`run_command`/`capture_diff` was denied ("No grant for tool ... in scope ...") and coding tasks failed HONESTLY with `required diff evidence contains no file change` — agents literally could not write. | `_ensure_per_project_tool_floors`: the `internet_search` + four workspace tool floors are per-project boot invariants, granted idempotently for EVERY project with per-project isolation. Verified live: grants flow to the new projects at restart. |
| M16b | operator config (no code change) | New projects had no repository, so coding tasks fail-closed: `a coding task requires repository_id when the project does not have exactly one repository`. | Correct engine behavior; the operator registers the project's repository (`register_repository`). Done live for both new projects (shared live-repo path; isolated per-task worktrees remain). |

| M17 | `agent_runtime.py` | **The base task prompt sabotaged coding agents**: its final line — "Return a concise completion report..." — was obeyed literally by read-heavy agents (objectives referencing dependency documents). They returned text-only reports without ever calling `write_file`, failing the diff gate attempt after attempt (`task objective expects file changes but the attempt recorded none of its own`). | Diff-evidence tasks now end with an explicit hands-on imperative (use workspace tools, ACTUALLY implement in the workspace, text-only answers FAIL the gate), keeping the honesty clause; non-diff tasks keep the historical report contract. |
| M18 | `agent_runtime.py` | **Dependency text outputs were inaccessible**: a dependency task with `provider_response` evidence (the nettools API contract) produced a text artifact that lives ONLY in the database. Downstream objectives referenced "the documented rules" — the agent searched the workspace, found nothing, and honestly reported it could not proceed (text-only → failed diff gate). | Completed dependencies' `provider_response` texts are injected into downstream task prompts (`_dependency_output_context`, bounded: 1800 chars/dependency, 6000 total, silent fallback on errors). Found by the wave13 test itself: the no-failed-attempt early return initially skipped the context — first attempts (the most important ones) now always receive it. |

### Mega-run verification

- 3 goals → REAL planner revisions approved → REAL decomposer graphs:
  textkit 15 tasks / 23 edges, nettools 15 tasks, dataviz-ascii 14 tasks.
- Full deterministic suite after M15: **1265 passed / 0 failed** (10
  env-gated skips), including 6 new `tests/test_mega_scale_wave13.py`
  regression tests (concurrent wall-clock overlap, multi-thread spread,
  serial-order backcompat, per-project isolation, 8-project clamp,
  env validation, default=1).
