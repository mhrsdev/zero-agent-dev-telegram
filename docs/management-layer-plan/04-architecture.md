# 04 — Proposed Architecture (Zero Dev Telegram management layer)

Single Python runtime, single repo, layered per existing ADR style. The
management layer is a **sibling of the engine**, never a wrapper that bypasses
domain rules.

```
┌────────────────────────── Interfaces ──────────────────────────┐
│ CLI (`zero`)   TUI (Textual)   Local Web GUI (/admin)   Bot    │
└───────┬──────────────┬───────────────┬──────────────┬──────────┘
        │              │               │              │ (runtime unchanged)
┌───────▼──────────────▼───────────────▼──────────────▼──────────┐
│ Application services (zero.manage.services)                    │
│ SetupService · TelegramAdminService · ProviderAdminService     │
│ AccessPolicyService · UsageReportService · DiagnosticsService  │
│ BackupService(adapter) · UpdateService · ServiceManager        │
│ ConfigService · SecretRefService                               │
└───────┬─────────────────────────────────────────────┬──────────┘
        │ reads/writes                                │ drives
┌───────▼───────────────────────────┐   ┌─────────────▼──────────┐
│ Core domain (zero.manage.core)    │   │ Engine (existing zero.*)│
│ config schema v1 (pydantic)       │   │ provider_service (chain)│
│ validation + migrations           │   │ interface_service       │
│ setup state machine               │   │ identity_service        │
│ access policy model               │   │ secret_service          │
│ routing: catalog/health/score     │   │ backup_service          │
│ usage accounting model            │   │ migrations, audit       │
└───────────────────────────────────┘   └────────────────────────┘
Adapters: systemd/Docker (ServiceManager), filesystem (atomic config,
locks), provider HTTP probes, Telegram getMe probe, OS keyring (optional).
```

## Rules

1. **One config truth.** `ConfigService` owns `config.yaml` (schema v1).
   Env vars remain supported and override file values at load (12-factor),
   but every writer (wizard/TUI/GUI/CLI) goes through ConfigService.
2. **One wizard engine.** `SetupService` exposes
   `steps() / answer(step_id, value) / validate() / commit()`; the three UIs
   render the same state machine (doc 06). No UI writes files directly.
3. **Secrets stay in the engine's Fernet store.** Wizard collects values →
   SecretRefService stores via `secret_service` and keeps only `sec_…`
   references in config.yaml. Diagnostics/exports redact by construction.
4. **Router-port is upstream of the chain.** Catalog+health+score select an
   ordered candidate list; existing `send_request_with_fallback` executes it.
   Circuit breaker state persists in SQLite (new table via migration 0029).
5. **Access policy is enforced in intake**, pre-LLM: new
   `AccessPolicyService.check(platform_event, binding, project)` runs before
   identity resolution in `interface_service.process_inbound_event`; default
   mode `owner_only`. Denials are logged with reason codes, respond with a
   generic text, leak nothing.
6. **Process ownership:** native install = systemd unit `zero.service`
   running `zero-develop serve` as user `zero`; Docker = compose file with
   the same command. `zero status/start/stop/logs` shells out to systemctl /
   docker compose — no second supervisor.

## New modules (paths)

| Path | Purpose |
|---|---|
| `src/zero/manage/__init__.py` | package root |
| `core/config.py` | schema v1 models, load/validate/migrate, atomic save (tmp+rename, fsync, lockfile), last-known-good copy |
| `core/setup_machine.py` | step registry, durable draft state, transitions |
| `core/policy.py` | GroupPolicy/AccessMode models + pure decision fn |
| `routing/catalog.py` | providers.yaml/models.yaml pydantic port |
| `routing/health.py` | breaker state machine (ported concept) |
| `routing/score.py` | weighted candidate ordering (ported concept) |
| `services/*.py` | table above (thin, testable, no I/O beyond adapters) |
| `adapters/systemd.py`, `adapters/compose.py` | service manager backends |
| `cli.py` | argparse `zero` entry (doc 07) |
| `tui/app.py` | Textual app (doc 08) |
| `web/admin_*.py` + `web/templates/admin/*` | GUI (doc 09) |
| `branding.py` | logo/animation over real progress callbacks |
| `scripts/install.sh` | POSIX installer (spec §1) |

## Data additions

- Migration **0029_routing_and_policy.sql**: `provider_health`,
  `group_policies`, `usage_counters` (aggregates only — no message bodies),
  `admin_users` (scrypt hashes), `setup_tokens`.
- Config file locations (native): `/etc/zero/config.yaml` (root install) or
  `$ZERO_HOME/config.yaml`; draft state `state/setup-draft.json` next to it;
  last-known-good `config.last-good.yaml`.

## Framework choices (no second runtime)

Textual (TUI), Jinja2+htmx (GUI), PyYAML (catalog/config), stdlib
hashlib.scrypt (admin passwords), secrets/token for setup tokens. Everything
else already in dependencies.
