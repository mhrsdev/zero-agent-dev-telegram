# 13 — Ordered Implementation Plan (M0–M9)

Small reviewable commits; each milestone ends green on the full suite and
carries its own rollback note. Estimates are effort-units, not dates.

## M0 — Groundwork (1)
- Branch `feat/management-layer`; this plan committed under
  `docs/management-layer-plan/`.
- Add `zero = zero.manage.cli:main` console script (empty root command +
  `--version`) so UX shape lands early.
- **Accept:** `zero --version` works; existing suite untouched-green.
- Rollback: drop branch.

## M1 — Canonical config core (2)
- `manage/core/config.py` schema v1 + validation + atomic save/lock +
  last-good + env-override merge + importer (`--from-env`).
- Migration `0029_management.sql` skeleton (admin_users, setup_tokens).
- **Accept:** unit matrix doc12 §1-config; `zero config show|validate|
  export --redact` functional.
- Rollback: package unused by engine; delete files.

## M2 — Setup state machine + CLI wizard/non-interactive (3)
- Steps per doc 06 with draft persistence; Telegram getMe probe; provider
  auth/completion probes via existing adapters; secret-ref storage through
  engine SecretService.
- Fix R5 blocker: expose identity verification (route
  `POST /users/{id}/external-identities/verify` + wizard auto-verify on
  first inbound message when policy owner matches) — unblocks live bots.
- **Accept:** doc12 integration "fresh install flow", "resume",
  "invalid token"; README quick-start swapped to wizard path.
- Rollback: feature-flag `ZERO_MANAGE=0` hides commands.

## M3 — Service management + installer (3)
- `scripts/install.sh` (POSIX): URL var centralized; distro/arch/pm detect;
  prereqs; native venv primary (/opt/zero, user `zero`), docker-compose
  optional path; checksum verify from SHA256SUMS artifact; idempotent;
  resume-state file; systemd unit + health check; launches `zero setup`.
  Uninstall command per spec (confirm, app-vs-data, optional backup).
- `zero start/stop/restart/status/logs` adapters (systemd/compose).
- Branding module over real phase callbacks (NO_COLOR/CI/tty guards).
- **Accept:** fresh-container runs for ubuntu/debian in CI; rerun idempotent;
  failure mid-step resumes.
- Rollback: script-only; `zero uninstall`.

## M4 — Access policy + groups (3)
- GroupPolicy model + `group_policies` table + intake gate in
  interface_service (pre-LLM); modes incl. public-confirm; discovery flow
  (bot added → updates probe lists candidate chats w/ titles → confirm);
  per-group rate/token budgets enforced pre-dispatch; denial reason codes.
- CLI: telegram groups*/access set-mode; silent-skip fix: unresolved token
  logs warning + surfaces in doctor/status.
- **Accept:** doc12 groups/unauthorized cases; existing interfaces tests
  still green (default preserves current behavior until configured).
- Rollback: gate disabled by config flag.

## M5 — Routing port (catalog/health/score/breaker) (3)
- YAML catalog ported (providers/models subset we support) shipped as
  package data; pydantic models; boot validation.
- HealthTracker state machine persisted in SQLite; scored candidates feed
  existing fallback chain; rejection traces exposed at `/providers` detail
  and TUI/GUI.
- Cost math refinement (cache-aware) into estimate path.
- **Accept:** doc12 routing cases; provider tests untouched-green.
- Rollback: selector returns static chain when catalog absent.

## M6 — Usage/cost + limits (2)
- usage_counters aggregation on request completion (no bodies), filters,
  soft/hard enforcement hook in access/routing path; CSV/JSON exports.
- **Accept:** counters invariant test; limits block + polite message.

## M7 — Diagnostics/doctor + backup/restore/update (3)
- `zero doctor` checks per spec w/ --json/--fix/--bundle(secret-scanned,
  preview); wraps BackupService for backup/list/verify/restore-stage;
  update flow channels stable/beta: preflight→backup→apply(tag switch)→
  health→auto-rollback; uninstall command.
- **Accept:** doc12 backup/restore + failed-update-rollback cases.

## M8 — TUI (Textual) (4)
- Screens per doc 08 sharing services; redaction/masking components;
  confirm modals; log tail streaming.
- **Accept:** smoke-render test of each screen offline (Textual pilot
  runner); keyboard map documented.

## M9 — Local Web GUI (4)
- /admin per doc 09: auth(setup-token→scrypt), sessions, CSRF, headers,
  redaction goldens, audit admin actions; wizard + dashboard first, then
  remaining pages.
- **Accept:** GUI security cases; consistency contract test vs SetupService.

## M10 — Hardening & docs & release (2)
- pip-audit job; platform matrix CI (ubuntu/debian/arm64/compose/upgrade);
  docs rewrite (60-second quick start, manual install, guides per spec §18
  with fake tokens only); UX acceptance harness run recorded; cut
  `mgmt-v1` tag + installer URL flip.
- **Accept:** doc §19 checklist executed end-to-end on clean container.

Total ≈ 28 effort-units. Dependencies: M2 needs M1; M3 independent after M1;
M4 needs M1(+M2 for wizard UI of policies); M5 independent after M1;
M6 after M4/M5; M7 after M3; M8/M9 after their services exist (M2+M4+M6).
Critical path: M1→M2→M4→M6→M9.
