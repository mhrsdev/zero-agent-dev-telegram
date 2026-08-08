# ADR 0001 — Technology Stack

- Status: ACCEPTED
- Date: 2026-08-08
- Milestone: 1 (Repository Bootstrap and Executable Skeleton)
- Skills applied: `zero-modular-bootstrap`, `zero-foundation-ingestion`

## Context

`PLAN.md` §5 leaves language, framework, database, and repository layout as
an evidence decision: "Prefer a boring modular control plane over
speculative distributed services. Do not create independent services until
a real scaling, security, deployment, or ownership boundary requires them."

`zero-modular-bootstrap` SKILL.md says: "The existing validated references
are Python, the runtime environment already supports Python, and the first
slice needs HTTP plus transactional persistence. A small Python web stack
minimizes new moving parts."

The two foundation reference modules that we must reuse as contract
examples are Python (`context_management.py`, `token_accounting.py`). The
production team that will maintain Zero Develop will work in Python.

## Decision

Adopt the following stack for Phase 1:

| Layer | Choice | Rationale |
|---|---|---|
| Language | Python 3.12 | Matches reference modules; environment default; team skill |
| HTTP framework | FastAPI (ASGI, on Starlette/Uvicorn) | Small surface, native type validation via Pydantic, async-ready for later streaming, OpenAPI docs included |
| Validation | Pydantic v2 | Already pulled in by FastAPI; structured config + boundary validation |
| Persistence | SQLite (via `sqlite3` stdlib) | Transactional, file-backed, zero-ops, sufficient for first vertical slice; no separate server process |
| Migrations | Plain SQL files under `src/zero/persistence/migrations/`, applied in lexical order, tracked in a `schema_migrations` table | Smallest verifiable mechanism; no Alembic until migration complexity demands it |
| Testing | pytest + httpx (ASGI transport) | pytest is the Python ecosystem default; httpx lets us exercise the real ASGI app without a network port |
| Process model | Single Python process (uvicorn) | "Modular monolith" per `zero-modular-bootstrap`; queue/broker deferred until measured demand |
| Packaging | `pyproject.toml` (PEP 621), editable install | Standard; no setup.py |
| Linting (advisory) | ruff if added later | Not a Phase 1 requirement; deferred |

## Rejected alternatives

- **Node.js / TypeScript**: would require translating reference modules and
  losing the Python ecosystem alignment. The foundation's source findings
  are Python-flavored.
- **Go / Rust**: would require more upfront plumbing for a Python-shaped
  control plane. Grok Build's Rust patterns are reference material, not a
  language mandate.
- **PostgreSQL from day 1**: would add an external dependency that the
  first vertical slice does not need. SQLite's transactional guarantees are
  sufficient; the migration path to PostgreSQL is well-understood if a
  later milestone produces a constraint SQLite cannot satisfy (concurrent
  cross-process writers, etc.).
- **Django / Flask**: Django pulls in ORM/admin/auth that we explicitly do
  not want yet (per `zero-foundation-ingestion` §"Scaffolding before a
  vertical slice"). Flask is fine but lacks the native type validation
  FastAPI gives us at the boundary; we would end up writing that by hand.
- **Alembic from day 1**: adds a tool dependency before any schema
  complexity exists. Plain SQL migrations are easier to review and reason
  about for the first slice.

## Consequences

- We commit to Python 3.12+ as the implementation language.
- We commit to FastAPI as the HTTP boundary; provider adapters and
  interface adapters will translate at this edge.
- We commit to SQLite as the initial canonical store; the schema must be
  written in a PostgreSQL-compatible dialect (no SQLite-specific types
  beyond what's portable) so a future switch is mechanical.
- We commit to single-process execution; durable worker semantics will be
  expressed with database-backed task state and one worker process, per
  `zero-modular-bootstrap` §"A queue is a behavior, not a default
  component."
- We do not commit to any specific queue, cache, vector database, or
  observability vendor. Each is added only when a measured need appears.
