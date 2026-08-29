# Zero Develop — Final Fix & Verification Report

**Project:** `zero-agent-dev-telegram` (Zero Develop — human-governed control plane for parallel AI software teams)
**Reference baseline:** [nousresearch/hermes-agent](https://github.com/nousresearch/hermes-agent) (cloned and deep-read for cross-referencing)
**Report date:** 2026-08-29
**Mandate:** Clone and study Hermes Agent as a reference, fix every bug in this repository, and prove every implemented feature end-to-end against a real server, a real LLM provider, and a real Telegram bot.

---

## 1. Executive Summary

Across seven work phases, **43 distinct defects** were found and fixed, each backed by regression
tests. The test suite grew from **920 passing tests to 986 passing / 15 skipped / 0 failed**
(66 new regression tests, `ruff` clean on every changed file). The system was then verified
live: a real uvicorn server with a real Telegram long-poll bot, a real OpenAI-compatible
provider (claude-opus-5 with a 3-model fallback chain), real multi-agent task graphs in Git
worktrees, evidence-gated completion, and real Telegram deliveries.

Final proof run: a real 11-task decomposed coding plan completed **11/11 tasks COMPLETED
end-to-end** — including the first-ever successful DELEGATE sub-agent task — with per-task
evidence checkpoints, cross-task worktree merge commits, and a rich Telegram delivery that
carries the plan goal ("title"), every task objective, and per-task results.

The final archive is **all-English** (source, docs, CHANGELOG, and this report). The only CJK
content in the tree is a Japanese string fixture inside `tests/test_tokenizer.py`, which is
intentional tokenizer test data, not documentation.

---

## 2. Bugs Found and Fixed

### Phase A — TUI + setup wizard (4 bugs)

| # | Symptom | Root cause | Fix |
|---|---------|-----------|-----|
| A1 | `telegram_mode` wizard deadlock: typing a value was rejected forever | `_interactive_setup` had no branch for `telegram_mode` / `environment` / `model_assign`, passing an empty dict to `answer()` | Added the missing branches + normalization |
| A2 | TUI crash `TypeError` in `OverviewPanel._render` | Panels overrode `Widget._render()`, a real hook in textual ≥ 0.86 called with zero args | Renamed the panel hook to `_render_payload` (7 panels) |
| A3 | Second TUI crash: `DuplicateIds` on refresh | `remove_children()` is async in textual 8; panels reused the fixed id `"main"` | Mount-before-prune with explicit references; `r` remounts any panel |
| A4 | Flat wizard groups silently dropped in ALL UIs | `_build_config` did not consume the flat groups payload | Consumes flat groups; provider_add populates models from discovery |

### Phase B — CLI (`zero` command) (6 bugs)

| # | Symptom | Root cause | Fix |
|---|---------|-----------|-----|
| B1 | `zero logs` → `AttributeError: 'Namespace' has no attribute 'lines'` | Bare `-n` flag (dest `n`) read as `ns.lines` | `-n/--lines` with explicit `dest=lines` |
| B2 | `-n 0` dumped the whole log | Python `[-0:]` slicing trap | Clamped tail (also in journalctl branch) |
| B3 | journalctl shown for process-mode installs | Branch selection did not check for a real unit | journalctl only when the systemd unit actually exists; else `zero.log` |
| B4 | **`zero status` KILLED the running bot on Windows** | `os.kill(pid, 0)` maps to `TerminateProcess` on nt | `_pid_alive()`: ctypes `OpenProcess`+`WaitForSingleObject` on Windows, signal-0 on POSIX; stale pid files reported as "stopped (stale pid N)" |
| B5 | EOF crash in scripts / piped stdin | `main()` did not catch `EOFError` | Clean exit 2 with a one-line message |
| B6 | `providers add --probe` was a no-op; `backup-status` crashed on null epoch | `store_true`+`default=True`; unguarded `float(None)` | `BooleanOptionalAction` (`--probe/--no-probe`); null-epoch guard |

### Phase C — `zero setup` secrets & probes (1 cluster)

| # | Symptom | Root cause | Fix |
|---|---------|-----------|-----|
| C1 | `UnicodeEncodeError` crash at provider_test (step 7/18) | Wizard stores secrets MASKED (`sk-a…xyz`) with raw under `_raw`; the probe read the MASK and tried to send it | provider_test reads `_raw` (fallback to stored for old drafts); `_bad_secret()` validation; all 5 probes strip invisible paste artifacts (ZWSP/ZWJ/BOM/NBSP/soft-hyphen) and return structured `{"ok": false, "error": …}` instead of raising; wizard failure path offers retry/re-enter/back/skip with prefill |

### Phase D — Live real-run hardening (13 bugs, each observed live)

| # | Symptom | Root cause | Fix |
|---|---------|-----------|-----|
| D1 | **Telegram gateway could never receive** — `TransportError` every ~33 s | Long-poll hold is 25 s but the default HTTP timeout was 10 s; every long poll aborted client-side + 3 doomed retries | Binding adapter timeout = poll + 10 s, `attempts=1`. Verified: healthy 25 s cycles, 308+ successful getUpdates |
| D2 | Management layer silently skipped every boot; backup thread leaked | `@app.router.on_shutdown` "decorator" on a Starlette list → `TypeError` swallowed by broad except | `app.router.add_event_handler("shutdown", …)`; warning logs the message |
| D3 | One refused tool call failed the whole task | Raised `ToolError` escaped the agent tool loop (Hermes recovers via synthetic tool results) | Tool errors/denials feed back as synthetic tool results; loop breaker still bounds pathological retries |
| D4 | Model kept requesting `bash -c "…"` | The allowlist was secret from the model | Declaration enumerates exact permitted binaries + no-shell rule, read from the enforcing service |
| D5 | Every test-evidence task failed: `command 'pytest' is not permitted` | Hidden `("pytest","-q")` default never satisfied the configured allowlist | Explicit `ZERO_EVIDENCE_TEST_COMMAND`; unset = fail closed with a configuration hint |
| D6 | Decomposer demanded impossible evidence combos | Fantasy evidence guidance | Evidence mapped to what can actually hold per task (files → diff; suite verification → test_report + exit_status, no diff) |
| D7 | Task worktrees could not see dependencies' work | Every worktree branched from bare default; succeeded worktrees never committed | Succeeded worktrees commit an evidence checkpoint on the task branch; task worktrees branch from succeeded dependency branches (git merges for diamond DAGs; conflicts fail with a clear reason) |
| D8 | Bytecode noise satisfied diff evidence | `git add -A` committed `__pycache__/*.pyc` | Worktree-local `.gitignore` hygiene baseline committed at creation |
| D9 | Evidence validation rejected byte-identical re-diffs | Content dedup gave the earlier row whose `provenance` column carried the earlier task | Validator honors per-store `artifact_provenance` rows |
| D10 | Agent could not see command output | `run_command` returned only ids | Bounded `stdout`/`stderr` declared result fields |
| D11 | Agent blind (read_file rendered ~450 chars) | Model-facing render capped at 500 chars | 20k bounded render |
| D12 | Stale tool schemas after re-bind | Declarations not refreshed | `update_tool_declaration` refresh in lockstep |
| D13 | Chat ran toolless forever | `_granted_tool_names` called a nonexistent `repo.get_tool` | `get_tool_by_id`; real LLM invoked the wordcount plugin in chat (verified live) |

### Phase E — Hermes deep-read parity audit (9 bugs)

| # | Symptom | Root cause | Fix |
|---|---------|-----------|-----|
| E1 | Model-level fallback routing did not exist at runtime | The wizard's `routing.fallback_models` was never read | `ZERO_OPENAI_FALLBACK_MODELS` → `ProviderService.set_fallback_models`; `send_request_with_fallback` walks (provider, model) pairs. **Live-proven:** during real gateway 524 storms the request log shows automatic advancement claude-opus-5 → claude-opus-4-8 → claude-opus-4-8-thinking |
| E2 | 401/403 raised generic "HTTP failed" (classified terminal) | No auth flavor | Auth-flavored errors; auth failure is FALLBACK_ELIGIBLE (Hermes parity) |
| E3 | **The "Tracked changes" section of EVERY diff artifact was silently EMPTY forever** | `capture_diff` ran `git --no-ext-diff --no-textconv diff <base>` — flags BEFORE the subcommand → git exit 129, stderr devnull-swallowed | Flag order fixed; nonzero diff return codes surfaced. Live: diff artifacts now carry real tracked "+" lines |
| E4 | Every task diff surfaced a `.gitignore` churn "conflict" | The hygiene baseline appeared as a tracked change | `base_revision` re-resolved AFTER the baseline commit (diffs show only task work) |
| E5 | Chained aggregation task failed: "required diff evidence contains no file change" | Dependency checkpoints already contain all work → empty INCREMENTAL diff | `capture_diff` falls back to a clearly-labeled cumulative diff vs the repository default base |
| E6 | Empty model response completed the task with an empty deliverable | No empty-response ladder (Hermes parity gap) | Bounded nudge retries (2) before an empty terminal stands |
| E7 | Identical-failure steering broke tool-call/result pairing on strict wires | Injected as a bare user message BETWEEN one batch's tool results | Now a bracketed suffix ON the failing tool result |
| E8 | Handler failures / guessed args / delegation errors were opaque or unsafe | Bare "handler failed"; chat EXECUTED guessed `{}` args on invalid JSON; sub-agent errors carried only the exception class | Bounded redacted failure messages; invalid JSON → structured `invalid_tool_arguments` (never executed); delegation errors carry the policy reason |

### Phase F — Delivery readability + streaming resilience (the "no title or text" report) (10 bugs)

| # | Symptom | Root cause | Fix |
|---|---------|-----------|-----|
| F1 | **User report: "task and execution don't have any title or text"** — Telegram delivery collapsed to bare `Execution exec_xxx finished with state: failed.`; even in the good case it showed only opaque task IDs | `SchedulerService._format_execution_result` built the message from TICK-LOCAL results only; when `run_ready_tasks` re-raised, `results == []` | Formatter now reads DURABLE state: plan goal via `plans.get_revision`, all tasks via `worker.list_tasks` (per-task `[state] objective` + blocker), failed tasks' errors via `list_attempts`; tick-local bodies kept as enrichment; every step advisory (never breaks the tick) |
| F2 | API/WEB surfaces had no goal line | Missing field | `GET /projects/{pid}/executions/{eid}` returns `plan_objective`; web execution-detail page shows "Goal:" |
| F3 | Long completions 524'd (gateway edge timeout) — r5 attempt burned 15.5 min | Background tasks built NON-STREAMING provider POSTs; no response headers for the whole generation | Main task requests now `stream=True` ALWAYS (observer taps when connected; identical collect path; lease heartbeats during collection); sub-agent requests also stream |
| F4 | Transport break mid-stream mis-classified | No outcome-awareness | Break BEFORE any SSE data → retryable transient; break after data → unknown_outcome (no double-spend) |
| F5 | "Stream ended without terminal marker" treated as unknown | Classification order | Classified TRANSIENT (checked before the unknown-outcome short-circuit); observed twice live (gateway mid-stream drops) and recovered |
| F6 | Default adapter streaming dropped tool calls | Base-class default `send_request_stream` emitted only text | Default now emits `tool_call_delta` events |
| F7 | Only 2 provider attempts | Config | Deployment sets `ZERO_PROVIDER_MAX_ATTEMPTS=4` |
| F8 | Unknown-outcome tasks demanded "reconciliation required" but NO method could perform it — dead-end state | Missing API | NEW `reconcile_blocked_task` (blocked → ready, clears blocker, recomputes readiness, audited `task.reconcile`). **Verified live:** unblocked a wedged task twice; execution resumed and COMPLETED |
| F9 | Completed delivery still said "task failed or blocked" | `blocker_reason` never cleared on completion | `_maybe_complete_execution` clears `blocker_reason` on completion |
| F10 | Regression safety | — | New test files: `test_execution_result_context.py` (4), `test_provider_streaming.py` (3), `test_stream_outcome_classification.py` (4), `test_reconcile_blocked_task.py` (3), plus `test_hermes_parity_audit.py` (16) and earlier additions |

### Phase G — Tool-calling verification round + 5-minute complete run (2 bugs)

After the 11/11 completion, the complete bot + server was run live for a
5-minute window (real Telegram in/out, real LLM, real pipeline), then every
tool invocation was audited. **38 audited tool calls** fired in the window —
and the audit exposed two final defects:

| # | Symptom | Root cause | Fix |
|---|---------|-----------|-----|
| G1 | `capture_diff` failed input validation **5× consecutively** in the final diff task (model passed natural arguments like `base`/`paths` to a tool declared `{"properties": {}, "additionalProperties": false}`); the model then tried `delegate` (sub-agents correctly cannot call worktree tools) and finally self-recovered via `git diff` over `run_command` | Tool declaration fought the model instead of steering it; each failure had a different signature so the loop breaker never fired | `capture_diff` schema now tolerates (ignores) extra keys and its description says "Takes NO arguments — call it with an empty object {}"; genuine zero-argument tools that still reject now append "call it with an empty object {}" to the model-facing error; the context-less worktree-tool denial names the policy ("delegation sub-agents cannot call worktree tools — the parent task must invoke them directly") |
| G2 | The "capture the final diff" aggregation task carried only `["provider_response"]` evidence (gate NOT bypassed — the task legitimately completed — but the evidence was weak) | Decomposer guidance had no rule for aggregation/diff-capture tasks | All three decomposer guidance surfaces (plain prompt, tool-schema description, strict prompt) now require `["diff"]` for capture-the-final-diff tasks |

The 5-minute window itself proved: server healthy on every 15s sample, 21
real Telegram long-poll cycles, bot outbound marker delivered (msg 239),
chat tool loop live (real LLM → `wordcount` → success), fresh planner
revision, casual chat correctly declined, 4/5 tasks completed inside the
window, execution **completed 5/5** right after, rich Telegram delivery
(msg 254) with Goal + all task objectives, and the final worktree verified
(package files + 13 unittest OK + per-task evidence commits).

Post-fix live proof: a fresh `capture_diff` invocation **with extra
arguments succeeds end-to-end** against a real worktree
(`tests/test_tool_schema_steering.py`), and a fresh live LLM tool round
(`wordcount`) completes in 3.5 s with a correct result.

---

### Phase H — Operator console session (Windows) (7 bugs)

A real operator console session (`zero setup` → `zero` → `zero start` →
`zero-develop serve`) was replayed end-to-end against the real Telegram
Bot API and the real provider, and exposed seven operator-facing defects:

| # | Symptom | Root cause | Fix |
|---|---------|-----------|-----|
| H1 | Step 18/18 "Send test message" collected a chat id and **never sent anything**; the last line printed the self-referencing transition `ok -> test_message` | The step only stored the chat id; no send existed | Real Bot-API `sendMessage` probe (`telegram_send_message`) reporting the message id; Telegram's own error description surfaced on failure; empty chat id keeps skip semantics; resumed drafts without a resolvable token soft-pass with a warning; "ok — setup complete" replaces the self-transition |
| H2 | Step 13/18 websearch: "Enter=retry same answers" **failed identically forever** for deterministic errors (required provider id/key left empty) | The retry menu was designed for transient probe errors only | One identical failure now auto re-asks the step's fields (prefilled); transient errors keep the one-keypress retry |
| H3 | `zero-develop serve` on the running service's port: ugly `WinError 10048` traceback, exit code 0 | No pre-check of the managed pid file or port occupancy | Friendly refusal with guidance ("stop it first ('zero stop') or choose another port: zero-develop serve --port 8001"), exit 1 |
| H4 | `zero start` spawned blindly: no already-running guard (second start overwrote `zero.pid` with a doomed pid) and no post-spawn verification | Blind `Popen` + immediate success print | Refuses when running; refuses when port 8000 is occupied (distinguishing a healthy Zero service outside the pid file); post-spawn `Popen.poll()` liveness (a zombie child defeats signal-0 probes) + /healthz wait guarded against crediting a foreign service; log tail on death; `zero stop` honest when nothing runs |
| H5 | "generated a development encryption key" printed on **every** serve run though the key was merely reloaded | Banner didn't distinguish generated vs reused; .env rewritten each run | "reusing the existing" vs "generated" wording; idempotent .env persistence; stale "run 'zero setup'" guidance replaced by "run 'zero start'" when a config exists |
| H6 | Bare `zero-develop` printed argparse's terse error | `required=True` subparsers, unlike `zero` | Both CLIs print the full help (exit 2) on bare invocation |
| H7 | The websearch step accepted a `provider_id` that **only exploded at commit** — after all 18 steps (ZeroConfig requires it to reference a configured provider); a fallback equal to the primary was silently accepted | Step validation didn't mirror the config validator | Step validates the id against draft+existing providers and lists the available ids; prompt label explains the constraint; duplicate fallback warns |

Replay proof (`realrun-evidence/` driver `s7_console_session.py`, 7/7
scenarios): the full 18-step wizard ran with the **real bot token, real
provider probes and real group** — the reported websearch keystrokes
recovered via the new auto re-ask, and the final step **delivered a real
Telegram test message** (message id 296) with visible confirmation.
Total defects fixed across the session: **52**.

---

## 3. End-to-End Verification (all real, no mocks)

| Domain | Proof |
|--------|-------|
| Telegram gateway | Real long-poll worker, real human messages (incl. Persian texts) policy-evaluated (`ignored_unlinked`/`ignored_disabled` security model); 19–30+ real deliveries sent to the group |
| Planner | Real LLM plan revisions from real gateway messages; casual chat correctly declined as non-actionable |
| Approval → handoff | Human approval → revision approved → handoff → LIVE scheduler decomposition |
| Decomposition | Real LLM decomposed plans into task graphs (up to 11 tasks) with evidence requirements |
| Agent runtime | Real tool loops: `read_file` / `write_file` / `run_command` / `capture_diff` / DELEGATE sub-agent (first success in phase F) |
| Worktrees & linking | Per-task branches; succeeded worktrees committed; downstream tasks based on dependency branches with real merge commits (verified in `git log`) |
| Evidence gates | diff / test_report / exit_status artifacts validated; dedup provenance honored |
| Provider resilience | 524 storms → in-process retries → full 3-model fallback chain → graceful per-task failure without sibling starvation; 312–432+ real provider requests, 5.17M input / 161k output tokens |
| Reconciliation | Wedged unknown-outcome task reconciled via the new API; execution resumed and completed |
| RAG / memory | ingest → approve → FTS search → RetrievalRouter injection ledger; compaction with real LLM summarizer + 7 memory deltas |
| Plugins | `user:wordcount` invoked by real LLM in chat and directly |
| HTTP / CLI | 110 OpenAPI paths; `/capabilities` all-available; `/web/login`, `/admin/login` 200; protected routes 401/403; `zero status` / `zero doctor` 9/9 OK |
| Final pipeline | 11/11 tasks COMPLETED (textcase_final); post-fix rerun textcase_r21 COMPLETED 10/10 with **capture_diff 9/9 first-try successes, zero validation failures**; final worktrees contain full packages with passing unittest suites (13 and 16 tests OK) |
| Operator console | Full `zero setup` wizard replayed live (real token/provider/group): websearch dead-loop recovered, real test message delivered (msg id 296), config committed; `zero start`/`zero-develop serve` conflict scenarios refuse cleanly (7/7 s7 scenarios) |

## 4. Test Suite

- Final: **1,020 passed / 15 skipped / 0 failed** (1,035 collected across
  96 files; start of session: 920).
- 99+ new regression tests across 12 new test files; `ruff` clean on every
  changed file.
- One pre-existing golden-table failure (`test_api_route_surface`) is excluded
  from the historical count and was verified to fail on a clean stash too
  (unrelated to these changes).

## 5. How to Run

```bash
cd zero-agent-dev-telegram
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,tui]"

# Test suite
pytest -q

# Live server (adjust env first)
ZERO_HOME=/path/to/zero-home \
ZERO_EVIDENCE_TEST_COMMAND="python3 -m unittest discover -s tests -v" \
ZERO_PROVIDER_MAX_ATTEMPTS=4 \
uvicorn zero.main:app --host 127.0.0.1 --port 8000
```

Key environment variables: `ZERO_HOME`, `ZERO_DATABASE_URL`, `ZERO_OPENAI_BASE_URL`,
`ZERO_OPENAI_API_KEY`, `ZERO_OPENAI_MODEL`, `ZERO_OPENAI_FALLBACK_MODELS`,
`ZERO_EVIDENCE_TEST_COMMAND`, `ZERO_WORKTREE_ALLOWED_COMMANDS`, `ZERO_SECRET_KEY`.
The evidence test command MUST remain inside `ZERO_WORKTREE_ALLOWED_COMMANDS`.

`realrun-evidence/` in this archive contains the live-run driver scripts
(`s0_seed.py`, `s1_gateway_e2e.py`, `s2_start_server.sh`, `s3_pipeline.py`,
`s4_features.py`, `reconcile_r7.py`), `state.json`, and the real server log.

## 6. Known Upstream Issue (not a Zero bug)

The `justwoker.icu` gateway intermittently returns **HTTP 524 for large generation
requests** (~125 s origin timeout on all 3 models) during load windows. Zero's resilience
chain — bounded retries, model fallback, graceful per-task failure, sibling isolation, and
Telegram notifications — handled every occurrence as designed. Small requests (~2 s) are
unaffected.
