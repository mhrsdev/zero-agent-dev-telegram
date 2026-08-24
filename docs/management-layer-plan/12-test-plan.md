# 12 — Test Plan

Principles: no weakened existing checks; every new feature ships with tests
in the same commit; security controls get adversarial tests; platform claims
only where CI actually runs.

## 1. Unit (pytest, fast, in-memory)

| Area | Cases |
|---|---|
| core/config | schema v1 accept/reject matrix; unknown key strictness; ref regex; cross-field rules (fallbacks ⊆ models, websearch provider exists, public needs confirm); atomic write crash simulation (tmp leftover ignored); lock contention; migration v0→v1 importer from env map; last-good rotation; export redaction |
| core/policy | decision fn × mode × sender kinds (private/group/supergroup/forum/channel/migrated id/anonymous-admin) incl. denied reasons stable + non-leaking |
| setup machine | happy path 20 steps; back-with-cleanup for ✱ steps; resume after kill at each step; cancel keeps draft; validation failure preserves prior steps; skip rules; commit order (validate→write→lastgood→reload→health) |
| routing/catalog | catalog load fail-at-boot on bad YAML; alias resolution; capability match; context-fit filter |
| routing/score+health | ordering determinism; breaker open excludes candidate; half-open probe path; cooldown expiry; rejection reasons recorded |
| usage counters | aggregation math (tokens/cost estimate), soft vs hard threshold, per-group budget, no-content invariant (schema has no body columns — enforced by test reading sqlite_master) |
| cli | arg parsing → service call mapping (mock services), exit-code table, secret-not-in-argv assertion by construction |
| branding | NO_COLOR/CI/non-tty disable; narrow width fallback; copy override |

## 2. Integration (real app via ASGI transport; tmp dirs)

- Fresh install flow: `zero setup --non-interactive --from-env` end-to-end →
  config written → engine boots with merged config → readyz ok.
- Resume: kill draft mid-provider step; rerun resumes at same step with
  values intact.
- Invalid Telegram token: wizard step 5 fails w/ safe message; no secret
  stored; retry succeeds with good token.
- Groups: add two groups via discovery stub; enable/disable flips policy;
  unauthorized chat_id gets generic denial + reason logged; owner passes.
- Provider auth failure: wizard probe fails → save-unverified requires flag;
  router marks provider degraded; fallback serves.
- Rate limit path: mock 429 w/ Retry-After → backoff honored (sleep patched)
  → breaker opens after threshold → next request skips provider (rejection
  trace), recovers after cooldown.
- Missing websearch: agent system note says unavailable; zero search calls
  attempted (counter assert).
- Backup/restore: create→verify→restore-stage→commit roundtrip incl. group
  policies; zip-slip archive rejected.
- Failed update rollback: fake target tag missing health → auto-rollback to
  previous symlink + last-good config restored.
- GUI: login w/ setup token→password; CSRF negative; session revoke;
  redaction golden responses; rate-limit lockout.
- TUI/GUI consistency: both render from SetupService.steps() snapshot
  (contract test comparing exposed step ids/order).

## 3. Security tests

Command injection via argv is structurally impossible (no shell) — assert
exec argv building never concatenates strings; log redaction fuzz over
token-shaped strings; config file perms 0600 asserted post-save; public-bind
warning flow; malicious provider response (huge/invalid JSON/tool-name
mismatch) handled without crash/secret echo; Telegram update replay
(duplicate update_id ignored); dependency scan job (pip-audit) advisory.

## 4. Platform matrix (CI)

ubuntu-22.04 x86_64 (native install script in container), debian-12,
arm64 job (ubuntu-22.04-arm where available), docker-compose path,
upgrade path test (previous tag → current, migrations double-run),
Windows dev-only job (existing suite green; installer explicitly
unsupported there and says so). Docs claim only what this matrix runs.

## 5. UX acceptance (doc §19) automated skeleton

Scripted harness `tests/ux_acceptance.py` driving CLI non-interactive mode
through the 16-step persona journey asserting time budget (<10 min mocked-
network) and "no secret in ps/logs" greps.

## 6. Gates

PR: unit+integration+lint+compileall. Nightly: platform matrix +
pip-audit. Release: existing release job + new installer smoke inside
fresh containers for each supported distro.
