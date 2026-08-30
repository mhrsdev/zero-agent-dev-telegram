# LIVE RUN REPORT — 2026-08-31

Hermes-parity hardening wave 10 + live verification with real credentials.

## Result at a glance

| Layer | Result |
|---|---|
| Deterministic test suite | **0 failed** (includes 28 new regression tests) |
| Live feature battery (`scripts/live_e2e_zero.py`) | **28/28 PASS** |
| Repo's own live-gated integration tests | **5 passed** / 1 skipped (no direct Anthropic key) |
| Engine | `uvicorn zero.main:app` on 127.0.0.1:8011, polling worker online |

## What was fixed (17 items)

**Pre-existing test regressions (4)**

1. `agent_runtime._audit_delegation` imported a non-existent `_now_utc_iso` → EVERY delegation audit write died silently (round-9 GAP-L regressions red).
2. Auth middleware ran sync SQLite reads inline in the async coroutine → offloaded to the request threadpool.
3. A LIVE API key was hardcoded in `scripts/probe_gateway_tools.py` (leaked to the public repo — **rotate it**); now env-read, fail-closed.
4. Scheduler integration stage gated on the raw `repository_id` instead of the resolver-resolved `effective_repository_id` → reviews/combined-tests/merge-proposals silently skipped on managed ticks.

**Hermes-parity gaps (13)**

5. Bot-sender filter on the Telegram transport (`ZERO_TELEGRAM_ALLOW_BOTS`, default `none`) — no bot-to-bot loops.
6. Group mention gating (`ZERO_TELEGRAM_REQUIRE_MENTION`, default on; commands/@mentions/reply-to-bot pass; per-chat exempt list + per-group `require_mention` override; fail-open when bot identity unresolved).
7. In-batch text burst coalescing (same chat+actor+topic, ≤120 s gap, same-batch replay dedup, commands/media never merge).
8. `allowed_updates` now requests `channel_post`/`edited_channel_post`.
9. Live-stream flood-strike circuit breaker (3 strikes → stop progressive edits; finalize still delivers).
10. User-session Telegram mode fully wired (was validated but dormant): `TelegramCfg.mode`, worker loop with Telethon handler, token-less bindings, outbound via the session adapter.
11. Env-only deployment bootstrap: config.yaml synthesized from `ZERO_OPENAI_API_KEY` / `ZERO_TELEGRAM_BOT_TOKEN` / `ZERO_TELEGRAM_GROUP_IDS`; `ENV:` sentinels resolved into encrypted secret rows.
12. Bot identity resolved via getMe BEFORE first poll and fed into mention gating.
13. `/new` command (clears durable chat scope; `ChatHistoryRepository.clear` had been waiting unused) + `/id` scope introspection; `/help` updated.
14. Tool-approval inline buttons: migration 0033 `tool_approval_tokens`, domain/repo/service surface, gate notifier fired on fresh pending requests, card with Allow once / Always / Deny pushed to every enabled Telegram binding, one-shot consume + `tool.manage` authorization on press.
15. MCP requests bounded (reader-thread pump, 10 s default / 120 s tool calls) + shutdown deadlock fix (terminate BEFORE closing stdout — close-vs-reader deadlock).
16. Web-search handler rebind on every boot (live-found: restarts left `internet_search` with no handler → 500s).
17. Transient-retry for the DDG backend (live-found flap: ConnectTimeout ↔ real results within minutes).

## Live evidence highlights

- Telegram: getMe, setMyCommands, sendMessage (message_id 606/611/614/615/621/625/630/636/652 in the real group), live-streamed chat turn (edit-in-place), execution progress bubbles, plan-approval cards and tool-approval cards with real inline keyboards.
- Provider: claude-opus-5 via `api.justwoker.icu/v1` — completion, streaming, native tool-calls; 37+ provider requests in the durable ledger.
- Real 7-task dependency-graph decomposition from the live LLM (`survey_repo_layout` → … → `capture_final_diff`).
- Agent-type instance-lease concurrency enforcement observed live (`max_concurrent_instances=1` respected).
- Fail-closed honesty observed live: tasks demanding `diff`/`test_report` evidence are blocked with a precise reason instead of faked (docker/firejail unavailable in this sandbox → host_bounded mode still allows real worktree file tools + allowlisted commands).
- Single-poller guarantee: a second getUpdates consumer is refused the poll lock while the engine owns it.
- RAG: ingest → approve → FTS hit → retrieval candidate + injection ledger. Compaction: real LLM summary, record `activated`. Memory: agent-type knowledge written and listed.

## Operator notes

- The engine boots fully from env now:
  ```bash
  export ZERO_ENV=development
  export ZERO_OPENAI_API_KEY=...        # api.justwoker.icu key
  export ZERO_OPENAI_BASE_URL=https://api.justwoker.icu/v1
  export ZERO_OPENAI_MODEL=claude-opus-5
  export ZERO_TELEGRAM_BOT_TOKEN=...    # bot token
  export ZERO_TELEGRAM_GROUP_IDS=-1004406039396
  uvicorn zero.main:app --host 127.0.0.1 --port 8011
  ```
- In groups the bot answers commands, @mentions, and replies to its own messages (privacy mode + mention gating). Set `ZERO_TELEGRAM_REQUIRE_MENTION=false` to answer everything.
- **Rotate the leaked API key** — it was committed to the public repo before this run and remains in git history.
- New env knobs: `ZERO_TELEGRAM_REQUIRE_MENTION`, `ZERO_TELEGRAM_ALLOW_BOTS`, `ZERO_TELEGRAM_MENTION_EXEMPT_CHATS`, `ZERO_TELEGRAM_GROUP_IDS`, `ZERO_TOOL_APPROVAL_MODE=manual` (enables the approval buttons).
