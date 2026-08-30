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
