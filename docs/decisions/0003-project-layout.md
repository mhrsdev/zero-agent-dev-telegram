# ADR 0003 — Project Layout and Dependency Direction

- Status: ACCEPTED
- Date: 2026-08-08
- Milestone: 1
- Skills applied: `zero-modular-bootstrap`, `zero-foundation-ingestion`

## Context

`zero-modular-bootstrap` SKILL.md §"Dependency direction protects the
core": "External SDK objects and transport payloads should be translated
at the edge. Canonical domain state should not import Telegram update
classes or provider SDK response objects."

The skill's "Correct shape":

```
Telegram payload -> adapter event -> application operation -> canonical records
provider response -> adapter result -> normalized usage/event records
```

We need a layout that makes this direction obvious from the file tree
alone.

## Decision

Adopt the following layout:

```
zero-develop/
├── README.md                       # how to run, how to test
├── pyproject.toml                  # PEP 621 packaging
├── .env.example                    # documented env vars (no real secrets)
├── .gitignore                      # excludes .venv, __pycache__, test dbs, etc.
├── docs/
│   ├── REQUIREMENT_LEDGER.md
│   ├── CURRENT_STATE_LEDGER.md
│   ├── DEPENDENCY_MAP.md
│   └── decisions/
│       ├── 0001-technology-stack.md
│       ├── 0002-modular-monolith.md
│       ├── 0003-project-layout.md
│       ├── 0004-configuration-as-trust-boundary.md
│       └── 0005-persistence-starts-with-invariants.md
├── src/
│   └── zero/
│       ├── __init__.py
│       ├── main.py                 # ASGI app factory + lifespan
│       ├── config.py               # validated configuration
│       ├── domain/                 # pure types and rules
│       │   ├── __init__.py
│       │   └── health.py
│       ├── app/                    # application operations
│       │   ├── __init__.py
│       │   └── api.py              # FastAPI router
│       ├── persistence/            # database layer
│       │   ├── __init__.py
│       │   ├── connection.py
│       │   ├── migrations.py
│       │   └── migrations/
│       │       └── 0001_initial.sql
│       └── adapters/               # external transports and SDKs
│           └── __init__.py
├── tests/
│   ├── __init__.py
│   ├── conftest.py                 # shared fixtures (test config, isolated db)
│   ├── test_smoke.py               # starts the real ASGI app
│   ├── test_health.py              # /healthz behavior
│   ├── test_config.py              # config validation, fail-closed
│   └── test_persistence.py         # schema migration, isolation
└── scripts/
    └── run_dev.sh                  # convenience dev launcher
```

## Dependency direction rules

1. `domain/` may import only from the Python stdlib and from itself.
2. `app/` may import from `domain/` and from `persistence/` interfaces
   (e.g. a `Protocol` declared in `domain/`). It may not import from
   `adapters/`.
3. `persistence/` implements interfaces declared in `domain/`. It may
   import from `domain/` but not from `app/` or `adapters/`.
4. `adapters/` may import from `domain/` and `app/` (to invoke
   application operations). It may not be imported by `domain/`,
   `app/`, or `persistence/`.
5. `main.py` is the only module that imports from every layer (it wires
   concrete implementations together).
6. `tests/` may import from anywhere.

A simple test (`tests/test_dependency_direction.py` will be added in a
later milestone when more layers exist) will enforce that `domain/`
does not import from `adapters/` or `persistence/`.

## Why this layout

- A reader who knows the change boundaries (identity, plans, execution,
  tools, artifacts, adapters) can predict where each future module
  lives without reading code.
- The directory tree itself signals dependency direction. New
  developers can navigate by file path.
- Adding a new interface adapter (Telegram in M13, Discord later) is a
  new file under `adapters/`, not a restructure.
- Adding a new provider (M10) is a new file under `adapters/providers/`
  (created in M10), not a restructure.
- Test isolation: tests under `tests/` use a forced-test config fixture
  so no test ever accidentally points at a production database.

## Rejected alternatives

- **Flat package** (`src/zero/{main,config,health,api,...}.py`): loses
  the change-boundary signal. Acceptable for very small projects, but
  Zero has at least six distinct change boundaries.
- **Domain-first layout** (`src/zero/{identity,plans,execution,...}/`
  each containing its own `domain.py`, `app.py`, `persistence.py`):
  attractive for larger systems but premature before each bounded
  context has more than one file. Will revisit when M2+ has multiple
  contexts that warrant the split.
