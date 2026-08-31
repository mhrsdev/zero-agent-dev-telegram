# CURRENT STATE LEDGER — zero-agent-dev-telegram

**Project:** Zero Develop (`zero-agent-dev-telegram`) — human-governed control plane for parallel AI software teams
**Reference baseline deep-read:** [nousresearch/hermes-agent](https://github.com/nousresearch/hermes-agent) (gateway, stream consumer, stream events, Telegram platform adapter, workers, sessions)
**Ledger date:** 2026-09-01 (UTC+8)
**Tree state:** `main` @ upstream tag `v0.9.5` + wave-14/15 fixes (fixes 13–23; all changes listed in §5)
**Mandate:** Make this repo behave like Hermes Agent on Telegram — stream and report everything, live — and prove every integrated feature against a real server, real LLM gateway, and real Telegram bot. Zero simulation.

---

## 1. Current state snapshot

| Dimension | State |
|---|---|
| Full automated suite | **1305 passed / 14 skipped / 0 failed** (skips are env-gated: PostgreSQL, live-credential, tiktoken extras) |
| Live engine | Boots and runs: `zero-develop serve` with background workers, long-poll bot, MCP manager |
| Real credentials exercised | Gateway `https://api.justwoker.icu/v1` · model `claude-opus-5` · bot token `8753924431:AAHc…` (@SandboxEnvironmentBot) · group `-1004406039396` |
| Real deliveries | Durable `result_deliveries` with real Telegram message ids (latest: message_id 920 in the real group, multi-team drill) |
| Multi-team drill final grade | **13/13 checks PASS** — 3 teams, 23/23 tasks completed, all knowledge tokens grounded (`download/live-evidence.jsonl`, `drill.*` rows) |
| Streaming parity with Hermes | Telegram chat receives edit-in-place live preview (throttled), tool-call progress lines, per-task execution progress bubbles, finalize with overflow split |

### Suite history ledger

| Milestone | Passed | Skipped | Failed |
|---|---|---|---|
| Upstream baseline (pre-fix) | 920 | — | 0 |
| After phases A–D + parity waves (FINAL_REPORT.md) | 986 | 15 | 0 |
| Round-5/6/7 live-hardening baseline entering round 8 | 1124 | 14 | 0 |
| After round 8 (Hermes live-streaming parity, GAP A–G) | 1143 | 13 | 0 |
| After round 9 (live feature proof, GAP H–L) | 1155 | 13 | 0 |
| After mega-scale wave 13 (12 fixes, live feature battery) | 1270 | 13 | 0 |
| **After wave 14/15 (fixes 13–23, multi-team drill) — current** | **1305** | **14** | **0** |

---

## 2. Defect & gap ledger (complete)

All items below are FIXED, each pinned by named regression tests.

### Phase A–D legacy waves (43 distinct defects; full table in FINAL_REPORT.md)

- **Phase A — TUI + setup wizard (4):** wizard deadlock on `telegram_mode`; two Textual ≥0.86 crash classes (`_render` override, `DuplicateIds`); flat wizard groups dropped.
- **Phase B — CLI (6):** `zero logs` crash, `-n 0` dump, journalctl misbranch, **`zero status` killing the bot on Windows**, EOF crash, `--probe` no-op + null-epoch crash.
- **Phase C — secrets & probes:** masked-secret probe crash (`UnicodeEncodeError`) + all 5 probes hardened against invisible paste artifacts.
- **Phase D — live real-run hardening (13, each observed live):** polling conflicts, callback acks, model routing alignment, transient 403 degradation, media handling, plan cards, webhook press answering, stale-query-id polling kill, scheduler tick routing, decomposer transport retries, approval boundary matrix, and more.
- **Parity waves (20):** MCP client + plugin registry, user-session Telegram mode, live integration qualification, forced tool-call path, HTTP API decomposition, TUI/logging consolidation.

### Rounds 8–9 — Hermes live-streaming parity gaps (GAP A–L)

| Gap | Sev | Defect (as found) | Fix (as shipped) |
|---|---|---|---|
| A | P0 | No live streaming — Telegram chat got ONE final message | `telegram_live.py` `TelegramLiveStream`: throttled edit-in-place, UTF-16 saturation dedup, finalize overflow split, never-raises contract; bridge degrades to durable send |
| B | P0 | Tool calls invisible in chat | Strategy-B tool progress lines (`🔧 tool(args)… ✓/✗`) injected into the stream bubble via `text_delta`/`tool_call`/`tool_result` events |
| C | P0 | Task executions silent until final summary | `run_ready_tasks` emits `task_started/completed/failed`; scheduler accepts `stream_callback`+`task_event_callback`; per-execution `TelegramExecutionProgress` bubbles fanned out per Telegram binding |
| D | P1 | Edits: no markdown render, no "message is not modified" tolerance, no RetryAfter handling | `edit_message` renders `render_telegram_html`, idempotent-edit tolerance, bounded flood waits; `messaging.py` redacted error-body snippets |
| E | P1 | Long LLM turn blocked the whole polling loop | `poll_once(background_dispatch=)` off-thread intake + `_ChatSerialDispatcher` per-chat FIFO lanes (8 lanes / 16 queued) |
| F | P2 | Only `/start` `/help` | `telegram_commands.py` command book: `/status` `/tasks` `/model` `/approvals` from durable state |
| G | P2 | Pending manual tool approvals not surfaced | `/approvals` command reads `approval_gate.list_pending()` |
| H | P0 | Compaction summarizer resolved `settings.openai_model` (last unaligned routing consumer) → every summarizer call failed on the operator gateway → **memory deltas never extracted** | `CompactionService.summarizer_routing` + config_sync pin to `routing.primary_model`; 4 pinned tests; live-proven (real claude-opus-5 summary, 9 durable memory deltas) |
| I | P1 | `GET /projects/{pid}/rag` → 500 (missing `actor_id` kwarg) | Fixed + pinned |
| J | P1 | `GET /projects/{pid}/agent-types/{tid}/knowledge` → misleading 404 (same class) | Fixed + full router scan (no remaining call sites) + pinned |
| K | P2 | Duplicate RAG ingest → raw `IntegrityError` 500 | Honest `409 Conflict` (`RagDocumentAlreadyExistsError`); pinned |
| L | P1 | Delegate tool bypassed tool registry → no durable `tool.invoke` audit for delegation | `AgentRuntime(audit_repo=)` writes redacted `tool.invoke` (`target_id='delegate'`) rows on every exit path; 2 pinned tests + live audit-row proof |

### Waves 14–15 — multi-team decomposition drill (fixes 13–23; 2026-08-31 → 2026-09-01)

All fixes found by the live multi-team drill (3 concurrent teams over the real gateway + real Telegram)
and by re-auditing Hermes' provider/approval/config surfaces. Each pinned in
`tests/test_hermes_parity_wave14.py` / `test_hermes_parity_wave15_reconcile.py`.

| Fix | Sev | Defect (as found) | Fix (as shipped) |
|---|---|---|---|
| 13 | P0 | Fallback chain built from an ALPHABETICALLY SORTED registry view — `fallback_priority` and file order never decided real failover; two OpenAI-compatible entries collapsed onto one protocol-level name (2nd silently skipped forever) | Adapters register under their config entry id; chain ordered by `fallback_priority` with the primary-model instance leading; `_sync_planner`/scheduler/compaction pins resolve INSTANCE ids; idempotent re-registration replaces rotated credentials |
| 13b | P0 | `routing.breaker` was parsed-and-ignored config fiction; a dead gateway kept eating the whole fallback window | Provider breaker: terminal failures arm time-boxed cooldowns (rate-limit exponential 60s→1h cap, auth ≥5 min, streak threshold from config); cooled fallbacks skipped, fail-open when the chain would collapse to one candidate; success clears state; `max_attempts_per_provider` now drives the live service |
| 14 | P1 | Approval posture frozen at boot — `approvals.mode`/`pending_ttl` in config.yaml were dead letters; operators had to rebuild to flip manual→auto | `ApprovalsCfg` + `ToolApprovalGate.set_mode()/set_pending_ttl()` synced at boot and on every config sync (Hermes re-read-on-every-check parity) |
| 15 | P1 | Per-project tool floors granted for the MANAGEMENT project only — operator-created projects had no workspace tools | Config sync grants floors per project (see wave-13 note) |
| 16 | P1 | Scheduler knobs (decomposition on/off, task retry budget, tick parallelism) env-only — unreachable for config.yaml-only operators | `FeaturesCfg` + `_sync_features` drives the live scheduler from config |
| 17 | P0 | The real LLM planner had NO HTTP route — `/projects/{pid}/planner/propose` did not exist (teams B/C in the drill could not plan at all over the API surface) | Route + `PlannerProposeRequest` registered; honest 4xx on unknown events; OpenAPI-pinned |
| 19 | P1 | Internal long-generation calls (planner proposal, task decomposition ×2, compaction summarizer) sent silent non-streaming bodies → gateway edge timeouts on long completions | All internal call sites `stream=True`; `ProviderService.send_request` routes `stream=True` through `send_request_stream`+`_collect_stream` with lease heartbeats |
| 21 | P0 | Blocked-task reconciliation (`WorkerService.reconcile_blocked_task`) unreachable over HTTP — blocked tasks piled up with no operator surface | `POST /projects/{pid}/executions/{eid}/tasks/{tid}/reconcile` + module-level `ReconcileTaskRequest` (nested model would degrade to a query param under `from __future__ import annotations`) |
| 22 | P1 | All messaging transport failures conflated: HTTP rejections (429, provably-not-landed) treated like ambiguous network failures | Typed `TransportRejectedError` split; maps to RETRYABLE `InterfaceTransportError`; true network failures stay `InterfaceTransportUnknownOutcome` (no silent double-send) |
| 23 | P2 | Duplicate secret name re-store → misleading 500 "configure ZERO_SECRET_KEY" (`SecretAlreadyExistsError` is a `SecretError` subclass caught by the 500 branch) | Honest `409 Conflict` with accurate detail; pinned by ASGI-level test |

**Drill proof (2026-08-31/09-01, evidence `drill.*` rows in `/home/z/my-project/download/live-evidence.jsonl`):**
Team A via real Telegram webhook → claude-opus-5 planner → callback approve → 6/6 tasks; Teams B/C via
API planner/propose (fix 17+19) → approve → handoff → 10/10 and 7/7 tasks; planted knowledge tokens
(`escalation`, `HERON TEAL`, `99.95`) all grounded in durable artifacts; RAG injection ledger 54 rows;
0 pending approvals (auto mode, fix 14); usage 143→317 records; 7 delegate `tool.invoke` audit rows;
real Telegram delivery (message_id 920). **13/13 observe checks PASS.**

---

## 3. Feature surface status matrix

Every feature integrated into Zero, with verification status and evidence pointer.

| Feature | Status | Verification & evidence |
|---|---|---|
| Chat / live streaming (Hermes parity) | ✅ LIVE-PROVEN | Real bot edits a live preview in the real group; round-8 bridge streaming e2e (18 pinned tests) |
| Tool call reporting in chat | ✅ LIVE-PROVEN | Strategy-B tool lines observed in real stream bubbles |
| Agent loop (planner → plan → execute) | ✅ LIVE-PROVEN | Real claude-opus-5 planner proposed a real 12-task dependency graph; plan card + real button press (round-9 rag profile X1a–c) |
| Planner | ✅ LIVE-PROVEN | Aligned to `routing.primary_model`; drives real decompositions |
| Decomposition | ✅ LIVE-PROVEN | Round-7 decomposition profile 13/13; transport-retry budget pinned |
| Tasks / task graphs / runtimes | ✅ LIVE-PROVEN | 11/12 tasks completed live (1 failure = documented isolation-backend boundary, fails closed by design) |
| Workers / scheduler / background | ✅ LIVE-PROVEN | Round-9 heartbeat from the real bot; per-chat dispatch lanes pinned for ordering |
| Teams / multi-project | ✅ LIVE-PROVEN | Second project created via real teams surface; strict agent-type + RAG scope isolation (topo T1–T3b) |
| Agent Types | ✅ LIVE-PROVEN | `falcon-research` / `memory-keeper` created via real management API, listed back (M1–M3) |
| Knowledge / Memory | ✅ LIVE-PROVEN | Knowledge records accepted + listed back; 9 memory deltas durable after real compaction (GAP-H fix proven) |
| RAG | ✅ LIVE-PROVEN | Injection ledger shows the 3 real `knowledge_record` ids retrieved; planted facts (BLUE HERON, 7.3) in 7/12 task transcripts; routes fixed (GAP I/J/K) |
| Compaction / Compression | ✅ LIVE-PROVEN | Round-9 compaction verifier 13/13; real LLM summary with all 6 sections; context version activated at 1322 tokens |
| Delegation / Sub-agents | ✅ LIVE-PROVEN | Round-9 deleg profile 7/7; sub-agent usage tagged whole-tree=0; SUBAGENT-OK in parent answer; durable audit row (GAP L) |
| Plans / Plan cards / Approvals | ✅ LIVE-PROVEN | Real token mint + real callback press accepted; approval boundary matrix (18 service pins); round-7 approval profile 21/21 |
| Approval (manual tool approvals) | ✅ TEST-PINNED + surfaced | `approval_gate.list_pending()` surfaced via `/approvals` |
| Authentication / identity gate | ✅ LIVE-PROVEN | Stranger message blocked at the identity gate (`ignored_unlinked`, topo A1); admin-auth on chat/stream endpoints |
| Polling | ✅ LIVE-PROVEN | Long-poll heartbeat; stale-query-id resilience pinned; off-thread dispatch |
| WebSearch | ✅ TEST-PINNED | DDG-lite tool through grant/redaction/audit pipeline |
| MCP | ✅ TEST-PINNED | stdio JSON-RPC transport; tools registered as `mcp_<server>_<tool>` through standard grants/audit |
| Chat commands | ✅ TEST-PINNED | `/status` `/tasks` `/model` `/approvals` from durable state; no LLM cost |
| Worktree / file-editing execution | ⚠️ OPERATOR BOUNDARY | Requires a real isolation backend (docker/firejail); sandbox host cannot exercise it — tasks fail closed, never silently |

---

## 4. How to run and verify

```bash
cd zero-agent-dev-telegram
python -m venv .venv && .venv/bin/pip install -e ".[dev,tui,mcp,tokenizer]"
.venv/bin/python -m pytest -q          # expect: 1305 passed, 14 skipped

# Live boot (operator credentials already seeded in the e2e env):
bash scripts/round9_engine.sh boot     # engine + workers + polling + MCP
bash scripts/round9_engine.sh topo     # teams/agent-types/knowledge/identity profile
bash scripts/round9_engine.sh rag      # planner → 12-task graph → approve → RAG-grounded run
bash scripts/round9_engine.sh deleg    # delegation / sub-agent drill
.venv/bin/python scripts/e2e_round9_compaction.py   # real compaction + memory deltas
bash scripts/round9_engine.sh stop
```

Round-8 equivalents: `scripts/round8_engine.sh`, `scripts/run_round8_e2e.py` (live-streaming parity drive). Round-5 harness: `scripts/e2e_round5_setup.py` + `run_round5_e2e.py`.

---

## 5. Changed-file inventory

### Rounds 8–9 working tree

**Modified (17):**
`src/zero/adapters/messaging.py` · `src/zero/adapters/telegram.py` · `src/zero/app/agent_runtime.py` · `src/zero/app/api.py` · `src/zero/app/background_workers.py` · `src/zero/app/chat_service.py` · `src/zero/app/compaction_service.py` · `src/zero/app/config_sync.py` · `src/zero/app/interface_service.py` · `src/zero/app/planner_service.py` · `src/zero/app/provider_service.py` · `src/zero/app/routers/artifact.py` · `src/zero/app/routers/topology.py` · `src/zero/app/scheduler_service.py` · `src/zero/app/services.py` · `src/zero/app/telegram_chat.py` · `src/zero/persistence/repositories/provider_repository.py`

**New source (3):**
`src/zero/app/telegram_live.py` · `src/zero/app/telegram_commands.py` · `src/zero/app/text_tool_protocol.py`

**New tests (4):**
`tests/test_hermes_parity_round8_live.py` · `tests/test_hermes_parity_round9_compaction.py` · `tests/test_hermes_parity_round9_delegation.py` · `tests/test_hermes_parity_round9_routes.py`

**New tooling (6):**
`scripts/e2e_round8_drive.py` · `scripts/run_round8_e2e.py` · `scripts/round8_engine.sh` · `scripts/e2e_round9_drive.py` · `scripts/e2e_round9_compaction.py` · `scripts/round9_engine.sh` (+ `scripts/probe_gateway_tools.py`)

**Docs / evidence:**
`CHANGELOG.md` (this wave) · `CURRENT_STATE_LEDGER.md` (this file) · `realrun-evidence/round8/` · `realrun-evidence/round9/` (39/39 evidence.json + GRADE_REPORT.md)

### Waves 14–15 (fixes 13–23)

**Modified (21 src + 5 test pins):**
`src/zero/adapters/messaging.py` · `src/zero/adapters/telegram.py` · `src/zero/adapters/user_session.py` · `src/zero/app/api.py` · `src/zero/app/approval_gate.py` · `src/zero/app/background_workers.py` · `src/zero/app/config_sync.py` · `src/zero/app/interface_service.py` · `src/zero/app/interface_transport_service.py` · `src/zero/app/planner_service.py` · `src/zero/app/provider_adapter.py` · `src/zero/app/provider_service.py` · `src/zero/app/routers/execution.py` · `src/zero/app/routers/plan.py` · `src/zero/app/routers/secret.py` · `src/zero/app/routers/tool_approvals.py` · `src/zero/app/scheduler_service.py` · `src/zero/app/services.py` · `src/zero/app/task_decomposition.py` · `src/zero/app/telegram_chat.py` · `src/zero/config.py` · `src/zero/domain/interfaces.py` · `src/zero/manage/core/config.py` · `src/zero/manage/core/mcp_client.py`; pin updates: `tests/test_dead_bot_regressions.py` · `tests/test_hermes_parity_round9_compaction.py` · `tests/test_api_route_surface.py` · `tests/test_mega_scale_wave13.py` · `tests/test_tui_wizard_regressions.py`

**New source (1):**
`src/zero/persistence/migrations/0034_tool_approval_session_action.sql` (+ `_pg` twin) — widen tool-approval token vocabulary with `allow_session`

**New tests (2):**
`tests/test_hermes_parity_wave14.py` (726 lines: fallback priority, breaker, approvals retune, feature flags, session-grain tokens) · `tests/test_hermes_parity_wave15_reconcile.py` (fix 17/19/21/22/23 surfaces incl. golden-route reconciliation + duplicate-secret 409)

---

## 6. Known boundaries (documented, not defects)

1. **Worktree / file-editing tasks** need a real isolation backend (docker / firejail). Without one, those tasks fail closed with an explicit reason; retrieval/analysis/planning/delegation paths are unaffected. Unchanged and documented since round 5.
2. **Skipped tests (14)** are environment-gated (PostgreSQL, live-credential, tiktoken extras) — they run in CI with those services present.
3. The gateway's multi-minute CDN-edge flaps are absorbed by bounded transport retries (decomposer 5/15/30/60s; auth failures stay fail-fast).
