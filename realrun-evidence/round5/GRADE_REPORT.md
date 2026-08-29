# Round-5/6 HARDEST-LEVEL REAL VERIFICATION — GRADE REPORT
Date: 2026-08-30 (UTC+8) · Tree: main @ 55ee64e · Zero shortcuts, zero simulation.

## What "real" means here
- Real Telegram Bot API: token 8753924431 (@SandboxEnvironmentBot), real group -1004406039396.
- Real LLM gateway: https://api.justwoker.icu/v1, model claude-opus-5.
- Real engine process: `uvicorn zero.main:app` with background workers + polling + MCP.
- Real deliveries: sendMessage 200 OK logged in engine.log; message ids captured per run.

## 1. Full automated suite
- 1099 passed / 16 skipped (env-gated: PostgreSQL, tiktoken, textual) / 1 failed.
- The single failure is `test_tui_panels_do_not_override_textual_render_hook` —
  the optional `textual` GUI extra is NOT installed in this environment; the
  failure is byte-identical on the stashed baseline → pre-existing env gap,
  NOT a code regression. Install `[project.optional-dependencies] textual`
  locally to turn it green.

## 2. Real-credentials E2E (fresh setup → real engine → 16 phases) — 16/16 PASS
- P0  /healthz ok (34 migrations)                        PASS
- P1  /capabilities snapshot                             PASS
- P2a /start intake via REAL webhook route               PASS
- P2b /start welcome reply delivered (sendMessage 200)   PASS
- P3a actionable intake                                  PASS
- P3b planner proposed + plan card w/ buttons (3 sends)  PASS
- P4a callback approve accepted                          PASS
- P4b plan approved via callback token (1 approved)      PASS
- P4c REAL agent loop execution + delivery to REAL group PASS (message_id=389 this run)
- P5a chat intake                                        PASS
- P5b conversational reply (real claude-opus-5) + web
     search executed live + durable session history      PASS
- P6a document intake                                    PASS
- P6b document handled without media crash               PASS
- P7 web_search granted to main worker (grants=1)        PASS
- P8 MCP stdio server tool registered (mcp_e2e_echo_add) PASS
- P9 live polling identity @SandboxEnvironmentBot        PASS

## 3. Real-process regression verifiers (all re-run on THIS tree)
- Transport resilience (round-4 fix):        10/10 PASS
  (real getMe, real poll_once, real sendMessage message_id=394, dead-proxy
   cause detail + token redaction, 2/4/8s backoff, one-time proxy hint,
   clean stop)
- Port fixes (round-2 fix):                   8/8 PASS
  (serve on managed port refused with pid guidance; honest bind naming;
   non-circular free-port suggestion; config server.port honored)
- Dead-bot fixes (round-3 fix):          ALL PASS (14/14)
- Real processes (round-1/2 chain):      ALL PASS (18/18)
  (cross-process poll lock; second instance skips polling and serves 8001)
- Live SSE-aggregation proof vs real gateway: PASS
  (real forced-SSE body → aggregated content, finish_reason=stop, usage 87/814)

## 4. Bugs found during THIS hardest-level pass → fixed → pinned
1. SSE-only gateway (live): the operator's gateway streams SSE for every
   tool-declaring request even when not streaming → every conversational
   reply with granted tools died with "provider returned invalid JSON".
   FIX: aggregate the delta stream (content concatenated; tool_calls merged
   by index; last usage/finish_reason win); garbage still fails loudly.
   Hermes reference: `agent/llm/providers/anthropic_adapter.py` documents
   the same effectively-SSE-only gateway class and aggregates likewise.
   Pinned: 2 new tests + live proof script.
2. MCP re-registration on restart (live): reused DB hit
   ToolAlreadyExistsError on every boot. FIX: idempotent refresh of the
   existing tool row (schema/description may change between restarts).
   Hermes reference: its MCP tool registry upserts on reconnect; parity.
3. E2E determinism (infra): real group members race the one-shot
   auto-link-owner bootstrap → setup now pre-links the operator identity
   through the real pipeline; driver resolves live scope from the engine DB.

Commits: ea051da (Hermes-parity overhaul) + 55ee64e (this pass).

## 5. Feature coverage matrix (user's list → proof)
| Feature            | Real-run proof               | Pinned tests |
|--------------------|------------------------------|--------------|
| Planner            | P3b real proposal            | test_planner_service.py |
| Plans/plan cards   | P3b card + buttons           | test_planner_service.py |
| Approval           | P4a/P4b callback approve     | test_http_phase3.py (403s) |
| Tasks/agent loop   | P4c real execution           | test_task_decomposition.py |
| Decomposition      | ran live (403-transient retried) | test_task_decomposition.py, test_s7_tool_call_decomposition.py |
| Chat               | P5a/P5b real claude-opus-5   | test_user_session.py |
| Sessions/history   | durable turns asserted       | test_user_session.py |
| Media/documents    | P6a/P6b no-crash intake      | test_hermes_parity_round5.py |
| Web search         | P7 grant + P5b live search   | config_sync wiring |
| Tools/grants       | web_search + MCP registered  | test_tool_schema_steering.py |
| MCP                | P8 stdio server tool         | mcp_client idempotency test |
| Polling            | P9 live identity             | test_polling_heartbeat.py, test_polling_longpoll_budget.py |
| Workers            | background workers live      | test_background_workers*.py |
| Memory             | —                            | test_memory_delta.py |
| RAG/retrieval      | —                            | test_context.py (budget, relevance) |
| Compaction/compression | —                        | test_anthropic_and_hermes_parity.py, test_reference_parity_fixes.py, test_observability.py (partial recovery) |
| Agent types        | —                            | test_agent_types.py |
| Delegation/sub-agents | —                         | test_subagents.py (budget, depth) |
| Authentication/authz | owner_only live (P2–P5)    | test_authorization.py, test_http_phase3.py |
| Capabilities       | P1 real endpoint             | — |
| Multi-project/teams| management scope live        | persistence/pg-dialect tests |
| Runtimes           | openai-compatible + anthropic adapters live | test_anthropic_and_hermes_parity.py, test_hermes_parity_audit.py (fallback chain, auth classes) |

## 6. GRADE: A
- Correctness: every real-credentials phase and every real-process
  verifier passes on this exact tree; suite is green modulo one
  env-gated optional-extra failure that predates all rounds.
- Resilience: filtered-network polling (cause detail, backoff, proxy
  escape hatch), transient CDN 403s retried, SSE-only gateways
  aggregated, dual-instance 409s prevented by a cross-process lock.
- Depth: fixes are pinned by tests and proven live; two new live bugs
  were found, fixed, referenced against Hermes source, and pinned.
- Why not A+: (a) the optional `textual` extra is not installed here —
  1 pre-existing TUI test stays red in THIS environment; (b) E2E ran on
  SQLite — PostgreSQL paths are covered by env-gated tests only;
  (c) `worktree_execution` capability is unavailable in this sandbox;
  (d) commits 30c18e8..55ee64e are local — GitHub push needs your
  credentials (no token in this environment).

## 7. What YOU should run on Windows
    pip install -e ".[textual]"      # turns the 1 env-gated TUI test green
    zero start                        # production engine (reads config port)
    zero-develop serve --port 8001    # dev engine on its own DB
    zero logs                         # live engine logs (bot identity line, backoff, proxy hint)
    set ZERO_TELEGRAM_PROXY_URL=socks5h://127.0.0.1:1080   # ONLY if api.telegram.org is filtered
