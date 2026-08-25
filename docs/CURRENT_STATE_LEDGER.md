# Current State Ledger

Living summary of what is verified in this repository and what remains
open. Claim-level evidence lives in `docs/PHASE_*_CLOSEOUT.md`; this
ledger tracks the effective state of the working tree after the
August 2026 engineering pass.

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
