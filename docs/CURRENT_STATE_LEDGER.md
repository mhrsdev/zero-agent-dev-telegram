# Current State Ledger — SUPERSEDED

> **This ledger is superseded by [`/CURRENT_STATE_LEDGER.md`](../CURRENT_STATE_LEDGER.md)
> (repo root), which tracks the effective state of the working tree after the
> rounds-8/9 Hermes live-streaming parity wave (suite: 1155 passed / 13 skipped /
> 0 failed; round-9 live grade: 39/39 phases, 0 failures).**
>
> The content below is the historical snapshot from the August 2026 engineering
> pass (794-test era) and is retained for claim-level context only. Per-phase
> closeout evidence remains in `docs/PHASE_*_CLOSEOUT.md`.

---

## Historical snapshot (August 2026 engineering pass)

Living summary of what is verified in this repository and what remains
open. Claim-level evidence lives in `docs/PHASE_*_CLOSEOUT.md`; this
ledger tracks the effective state of the working tree after the
August 2026 engineering pass.
---

## Historical snapshot (August 2026 engineering pass)


Living summary of what is verified in this repository and what remains
open. Claim-level evidence lives in `docs/PHASE_*_CLOSEOUT.md`; this
ledger tracks the effective state of the working tree after the
August 2026 engineering pass.

## Round-8 security & correctness wave (2026-08-30)

A full-tree audit pass. Every finding was reproduced before it was
fixed; pins live in `tests/test_hardening_wave8.py` (16 tests).

### Credentials that were in the repository

- A **live Telegram bot token** and a **live provider API key** were
  committed: the token appeared 1,342 times in
  `realrun-evidence/server.log` and 13 times in
  `realrun-evidence/round5/engine.log` (Bot API request URLs embed the
  credential), and both were hard-coded as module constants in
  `scripts/e2e_round5_setup.py`, `realrun-evidence/env_common.py`,
  `realrun-evidence/s7_console_session.py`, and
  `realrun-evidence/s2_start_server.sh`. A webhook secret was
  hard-coded in `scripts/e2e_round5_engine.sh`.
- Fixed: every literal is now a required environment read that fails
  closed by name (`E2E_BOT_TOKEN`, `E2E_PROVIDER_KEY`,
  `E2E_WEBHOOK_SECRET`, `REALRUN_BOT_TOKEN`, `REALRUN_API_KEY`); the
  logs were redacted and untracked; `.gitignore` excludes `*.log`; and
  `test_no_live_credentials_in_tracked_files` scans every tracked text
  file for Telegram/OpenAI/AWS/GitHub/Slack credential shapes.
- **Operator action outstanding:** the exposed values are in published
  git history. Revoke the bot token via @BotFather, rotate the provider
  key, and rewrite history if the old objects must be purged. Until
  then, treat both as compromised.

### Other fixes

- The admin GUI's CSRF token was `sha256("csrf:" + session_id)`, so a
  leaked session id yielded the matching token; tokens are now random
  per session and dropped with the session.
- `DockerExecutor` passed the full host environment to the `docker` CLI
  process; `docker_cli_env()` now passes only `PATH`, locale, and the
  daemon locators.
- The auth middleware ran two synchronous SQLite reads on the event loop
  for every authenticated request; both now go through
  `run_in_threadpool` with the actor `ContextVar` re-bound in the worker
  thread.
- `Database` never released cached connections, so `ResourceWarning:
  unclosed database` (CPython ≥ 3.13) failed unrelated tests
  non-deterministically under warnings-as-errors; a `weakref.finalize`
  now closes them. A test-side `with sqlite3.connect(...)` — which
  commits but does not close — was the second source.
- Tests resolved the operator's real `$ZERO_HOME`, so the live
  `owner_only` access policy denied **every** interface intake (25
  failures) and home-writing tests mutated the operator's config; a
  session-scoped fixture isolates it.
- Suites driving a real loopback HTTP server now skip behind a
  `loopback_http_works()` probe instead of failing with `ReadTimeout`
  where loopback delivers requests but never returns responses.
- Two order-dependent assertions were fixed: the polling-heartbeat tests
  waited a fixed 0.3s and asserted an iteration count (they now wait for
  the condition), and `test_resolve_sink_path_from_env` compared a
  POSIX-spelled path string on Windows (it now compares `Path` objects).
- The package version contradicted the tree (`pyproject.toml` said
  `0.1.0` at v0.8.5, so the wheel, `/healthz`, `--version`, and the MCP
  handshake all reported a stale version). `zero.__version__` is the
  single source of truth at `0.8.5`; `pyproject.toml` reads it via
  `[tool.setuptools.dynamic]`.
- `_now_utc_iso` was defined 17 times with a byte-identical body; the
  canonical timestamp format now lives once in `zero/app/clock.py`.

## Verified locally (deterministic, Windows host)

- Full deterministic suite green after the round-8 wave: **1107 passed,
  0 failed, 48 skipped** (1155 collected) under `ZERO_ENV=test` on
  CPython 3.14. Skips are platform-gated POSIX cases, optional extras
  (tokenizer, psycopg), credential-gated live tests, and the
  loopback-HTTP suites that skip where the environment cannot complete a
  loopback round trip. The earlier figure was 794 passed / 31 skipped
  against a tree with 25 environment-induced failures.

## Verified locally (deterministic, Windows host)

- Full deterministic suite green: **794 passed, 0 failed, 31 skipped**
  (825 collected) under `ZERO_ENV=test`. Skips are platform-gated
  POSIX cases, optional extras (tokenizer, psycopg), and
  credential-gated live tests that skip by design.
- Ruff lint and format clean across `src/`, `tests/`, and `scripts/`.
- HTTP API decomposed into `zero.app.routers`: sixteen per-domain
  router modules plus shared `deps.py`/`models.py`; `app/api.py`
  keeps application assembly only (304 lines). Golden route/OpenAPI
  tables prove the public surface identical across the move.
- Release artifact gate green: wheel and sdist build from a clean
  committed tree; `scripts/validate_release_artifacts.py` exits 0
  (all 32 migrations matched, required modules present, no unsafe
  paths, no credential matches).
- Installed-console fail-closed contract holds: `zero-develop serve`
  without `ZERO_ENV` prints the exact configuration error and exits
  non-zero; the CI release job asserts this.
- Fail-closed configuration boundary (ADR 0004) holds: structural
  validation (pool bounds) runs before capability gates; production
  mode still refuses missing secrets and development database paths.

## Behavior fixes landed in the August 2026 engineering pass

- Wizard group discovery crashed with `NameError`
  (`probes.telegram_recent_chats` referenced an undefined global);
  fixed with a real-HTTP regression test.
- Pool-bound misconfiguration surfaced as an install hint when the
  `[pg]` extra was absent; structural validation now precedes the
  capability gate.
- Release gate rejected every build once migrations 0029/0030
  shipped (stale hand-maintained migration list); the expected set is
  now derived from the source tree, with a regression test covering
  both drift and silent empty-set failure modes.
- CI release smoke invoked bare `zero-develop`, which dies in
  argparse now that subcommands are required; smoke steps use
  `zero-develop serve`.
- Provider retry test used a wall-clock budget that flaked under
  full-suite load; it now asserts the recorded backoff sleeps
  directly.
- `$ZERO_HOME` had seven duplicated resolvers; consolidated into
  `zero.manage.core.config.zero_home` (behavior unchanged).

## Still open / not verified here

- Live Telegram, OpenAI, and Anthropic integrations remain
  unverified (credential-gated suites skip unless explicitly enabled
  — see `docs/LIVE_TESTING.md`).
- PostgreSQL integration tests require a disposable server via
  `ZERO_TEST_PG_URL` and were skipped; dialect unit tests ran.
- Production rollout blockers in `README.md` ("Not production ready
  yet") and `docs/PHASE_9_CLOSEOUT.md` remain authoritative: this
  checkpoint is development-only.
