# Management Layer Plan — Zero Dev Telegram

Status: **PLANNING COMPLETE / IMPLEMENTATION NOT STARTED**
Branch for delivery: `feat/management-layer`
Scope: replace the manual installation experience with installer + wizard +
TUI + local Web GUI + CLI + safe config/secrets + doctor/backup/update, while
keeping this project **Zero Dev Telegram** (Telegram-first, small teams,
simpler than Zero Dev Web).

## Documents

| # | File | Content |
|---|------|---------|
| 01 | [repository-audit.md](01-repository-audit.md) | What exists today (install path, config surface, Telegram runtime, gaps) |
| 02 | [installation-failure-report.md](02-installation-failure-report.md) | Reproduced failures R1–R7 with evidence |
| 03 | [router-reuse-matrix.md](03-router-reuse-matrix.md) | Zero Router verdicts: reuse / port / reference-only / reject |
| 04 | [architecture.md](04-architecture.md) | Layers, modules, data flow, frameworks chosen |
| 05 | [config-schema.md](05-config-schema.md) | Canonical typed config v1 (+secrets separation) |
| 06 | [setup-state-machine.md](06-setup-state-machine.md) | 19-step durable wizard spec |
| 07 | [cli-design.md](07-cli-design.md) | Full `zero` command surface |
| 08 | [tui-information-architecture.md](08-tui-information-architecture.md) | Textual TUI screens/keys |
| 09 | [gui-information-architecture.md](09-gui-information-architecture.md) | Local admin GUI pages/security |
| 10 | [threat-model.md](10-threat-model.md) | STRIDE-style risks + controls |
| 11 | [migration-plan.md](11-migration-plan.md) | Existing installs → managed installs |
| 12 | [test-plan.md](12-test-plan.md) | Unit/integration/security/platform matrix |
| 13 | [implementation-plan.md](13-implementation-plan.md) | Milestones M0–M9, acceptance criteria, rollback |

## Executive summary of findings

- The repository already ships a strong **control-plane core** (identity,
  plans, durable executions, worktrees, providers incl. OpenAI-compatible +
  Anthropic adapters, usage accounting, audit) and a working Telegram
  adapter/polling/webhook runtime — but **zero management tooling**.
- A new user today must: install Git+uv manually, create a venv, export env
  vars (bare start exits with a config error), boot an API server, then drive
  ~8 REST calls with JSON bodies to reach a live bot — and even then hits a
  **dead end**: external-identity verification exists only as a Python method
  (`identity_service.py:355`) with no route, so inbound messages are denied
  (`ignored_unlinked`). Reproduced as R1–R7 in doc 02.
- There is **no access policy** beyond "verified identity ∧ project member":
  any member's linked Telegram account can use every feature of every group
  the bot is in. Public-bot accidents are one enabled binding away.
- Zero Router contains four genuinely portable concepts (declarative
  YAML provider/model catalog, weighted-score candidate selection with
  circuit-breaker-aware exclusion, health state machine, envelope crypto)
  and large rejected surfaces (gateway app, org/team auth, billing store,
  Redis infra). Verdicts in doc 03.

## Product decisions taken (none blocking)

| Decision | Choice | Why |
|---|---|---|
| Install URL | `https://getzerodev.ai/install.sh`, fallback `https://raw.githubusercontent.com/mhrsdev/zero-agent-dev-telegram/main/scripts/install.sh`; single `INSTALL_URL_BASE` variable in script | spec allows temporary centralized URL |
| Language/runtime | Python only (no second runtime) | repo is Python; Textual covers TUI without Node |
| TUI framework | **Textual** (maintained, keyboard-first, NO_COLOR aware) | spec §8 |
| GUI stack | Server-rendered Jinja2 + htmx, mounted at `/admin`, loopback-only default | reuses existing FastAPI/Jinja2; no SPA build chain |
| Bot API vs User-session | Ship **Bot API only** in v1; User Session out-of-scope until designed safely | spec §4 warns against mixing; no session code exists today |
| Telemetry | None by default; opt-in stub only | spec §15 |
| Channels | `stable` (git tags) and `beta` (branch); no `dev` channel publicly | simplicity |
| Where management code lives | `src/zero/manage/**` + thin `zero` console script in the same package/repo | one repo, one runtime, clean import boundary from core |
