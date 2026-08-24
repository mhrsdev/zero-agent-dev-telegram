# 02 — Current Installation Failure Report (reproduced)

Environment of reproduction: Windows 11, Python 3.11.x (also representative
of a fresh Linux box without uv). Clone: `mhrsdev/zero-agent-dev-telegram@main`.

| ID | Reproduced failure / trap | Evidence | Severity |
|----|---------------------------|----------|----------|
| R1 | **Documented prerequisite missing by default.** README step 1 requires `uv`; `Get-Command uv` → not found on a typical machine. A non-expert stalls before any Zero code runs. | shell output: `uv NOT INSTALLED -> README step 1 fails` | Blocker for target persona |
| R2 | **Bare start hard-fails.** Running the documented final command without exporting env first prints `zero: configuration error: ZERO_ENV is required (one of: development, test, production).` and exits non-zero. No hint that a wizard/config file could exist. | R4 output | High (first-impression dead end) |
| R3 | **No service lifecycle.** After boot there is no `status/stop/restart/logs`, no systemd unit, no autostart; closing the terminal kills Zero. `run_dev.sh` explicitly defers deployment ("not a production story"). | audit §2/§4 | High |
| R4 | **Telegram onboarding is an undocumented REST gauntlet.** Go-live requires ≥8 correct JSON calls in exact order (user → project → membership → secret → external-identity → binding → enable → workers on) — none surfaced by README. | audit §4 checklist | High |
| R5 | **Onboarding dead end (functional bug):** `POST /users/{id}/external-identities` always writes `verified=False`; verification exists only as `identity_service.verify_external_identity()` with **no HTTP route**. Every inbound Telegram message from that user is then denied (`ignored_unlinked`, `interface_service.py:450-474`). Live bot impossible via public interfaces even after the gauntlet. | grep `verify` in api.py → 0 route hits; intake denial path cited | **Blocker** |
| R6 | **Silent polling skip:** bindings whose bot token cannot be resolved are skipped at debug level (`background_workers.py` token-resolve loop), so a typo'd/expired secret looks identical to "everything fine". | audit §4 note | Medium |
| R7 | **No group/access policy.** Any verified member account can drive every feature in every chat the bot occupies; nothing distinguishes owner-only vs public; enabling a binding for a big group ≈ accidental public bot. | interface_service gates; authorization matrix | High (safety) |
| R8 | **Management commands absent:** `zero --help` lists only serve/migrate/check-config/reconcile. No install/setup/status/doctor/logs/backup/update/uninstall. | R6 help output | High |
| R9 | **Secrets UX:** tokens must be posted as JSON over HTTP to a dev server; no masked input flow, no rotation command, no export-without-secrets. | audit §4/§5 | Medium |

## Root causes

1. The project grew as an **engine**, not a product: excellent durable core,
   zero "last mile" (install→wizard→operate).
2. Configuration exists only as process-env + fail-closed validation; there is
   no persistent typed config file to power a wizard/GUI.
3. The Telegram path was built runtime-first; the **join** between a human's
   platform identity and Telegram was left half-implemented (missing verify
   surface), which alone blocks all live usage.

## Required outcome (acceptance for this plan)

A new user runs ONE curl|sh, answers wizard prompts (provider key, bot token,
groups), watches a final test message arrive, and is done — no manual venv,
no JSON REST gauntlet, no source edits; returning reconfiguration ≤3 minutes;
every error prints a safe next action. Detailed acceptance in doc 13 (M9).
