# Zero Develop

A multi-agent control plane for concurrent software development by human
teams. Multiple team members make decisions about one project while
multiple AI agents work on different parts of that project at the same
time — without mixing files, contexts, memories, or changes.

> **Phase 1 status: VERIFIED.** This phase implements Milestone 0
> (Foundation Ingestion and Build Readiness) and Milestone 1
> (Repository Bootstrap and Executable Skeleton) from
> `PLAN.md`. See `docs/CURRENT_STATE_LEDGER.md` for the full
> milestone-by-milestone status.

## Quick start

```bash
# 1. Install (editable, with dev extras)
pip install -e ".[dev]"

# 2. Run the test suite
pytest

# 3. Start the dev server
ZERO_ENV=development uvicorn zero.main:app --reload

# 4. Probe the health endpoints
curl http://127.0.0.1:8000/healthz
curl http://127.0.0.1:8000/readyz
curl http://127.0.0.1:8000/
```

## Configuration

Configuration is a typed, validated, fail-closed trust boundary. See
ADR 0004 (`docs/decisions/0004-configuration-as-trust-boundary.md`) for
the full rules.

Required environment variables:

| Variable | Required in | Notes |
|---|---|---|
| `ZERO_ENV` | always | `development`, `test`, or `production` |
| `ZERO_DATABASE_URL` | production | auto-set in development/test |
| `ZERO_SECRET_KEY` | production | >= 32 bytes; never logged |
| `ZERO_LOG_LEVEL` | optional | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

Copy `.env.example` to `.env` for local development. **Never commit a
real `.env`.**

## Project layout

```
zero-develop/
├── docs/                  # requirement ledger, state ledger, ADRs
├── src/zero/
│   ├── main.py            # ASGI entry point
│   ├── config.py          # configuration (trust boundary)
│   ├── domain/            # pure domain types and rules
│   ├── app/               # application operations (FastAPI router)
│   ├── persistence/       # database access + migrations
│   └── adapters/          # external transports and SDKs
├── tests/                 # pytest suite
└── scripts/               # convenience scripts
```

Dependency direction is inward: `adapters → app → domain`, with
`persistence` implementing interfaces declared in `domain`. See ADR
0002 and ADR 0003 for the full rules.

## Foundation

The product definition, implementation plan, and conceptual skills
that shape this codebase live in `project-foundation/`. Every
foundation file was read completely during Milestone 0; hashes were
verified against `MANIFEST.sha256`.

## Status

Phase 1 (Milestone 0 + Milestone 1) is **VERIFIED**:

- All foundation files read and accounted for.
- Requirement ledger, current-state ledger, and dependency map
  produced.
- Five ADRs document the technology stack, modular-monolith shape,
  project layout, configuration discipline, and persistence
  invariants.
- Runnable ASGI app with `/healthz`, `/readyz`, and `/` endpoints.
- Configuration validated fail-closed: missing `ZERO_ENV`, missing
  production `ZERO_DATABASE_URL`, missing/short `ZERO_SECRET_KEY`,
  and test/production overlap are all refused at startup.
- Isolated test persistence: in-memory SQLite by default; file SQLite
  for restart-survival tests; foreign keys enforced on every
  connection.
- Smoke test starts the real ASGI app through httpx's ASGI transport
  and exercises the real database — the same executable path intended
  for later milestones.

The remaining 14 milestones (M2–M15) are PLANNED. See
`docs/CURRENT_STATE_LEDGER.md` for the full table.
