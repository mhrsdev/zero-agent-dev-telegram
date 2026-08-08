# ADR 0005 — Persistence Starts with Invariants

- Status: ACCEPTED
- Date: 2026-08-08
- Milestone: 1
- Skills applied: `zero-modular-bootstrap`, `zero-control-plane-trust`,
  `zero-project-isolation-evidence`, `zero-recovery-consistency`

## Context

`zero-modular-bootstrap` SKILL.md §"Persistence starts with invariants":

> "The initial schema should enforce current facts, not every roadmap
> entity. Database constraints are often the smallest durable
> implementation of uniqueness, ownership, revision, and project scope."

> "The schema is part of the domain model, not a passive storage
> detail."

The "Correct example" given: "A unique relation prevents two executions
from being created for one approved plan revision."

The "Wrong example": "Application code checks for an existing execution,
then inserts later without a constraint; concurrent retries both
succeed."

`zero-control-plane-trust` §"Atomicity follows the business fact":
"Operations that represent one fact should not leave half-facts."

`zero-project-isolation-evidence` §"Canonical constraints and policy
complement each other": constraints enforce ownership and lineage;
application policy decides whether the actor may perform the operation.
Neither replaces the other.

## Decision

The Phase 1 schema is **minimal but invariant-enforcing**. We create
only the tables needed for Milestone 1's vertical slice (health/readiness
+ a tiny persistent marker that proves the database is wired), but we
already encode the discipline that later milestones will rely on:

1. Every table has a server-issued stable primary key.
2. Every project-scoped table has a `project_id` column with a foreign
   key to `projects`. (Phase 1 creates the `projects` table as a
   placeholder so the FK target exists; identity/membership arrives in
   Milestone 2.)
3. Unique constraints express facts that must be true regardless of
   application code.
4. A `schema_migrations` table records applied migrations for safe
   restart and rollback.
5. All writes go through one persistence module; no raw SQL scattered
   across the codebase.

### 5.1 Phase 1 schema (`migrations/0001_initial.sql`)

```sql
-- Phase 1 minimal schema. Identity/membership (M2), permissions (M3),
-- plans (M4), executions (M5), tasks (M6), agent types (M7), artifacts
-- and memory (M8) all arrive in later milestones. This file only
-- creates what Milestone 1's vertical slice needs: a persistent
-- marker that proves the database is wired and migrations run.

CREATE TABLE IF NOT EXISTS schema_migrations (
    id           TEXT PRIMARY KEY,
    applied_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS projects (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- Tiny durable marker written by the smoke test to prove the database
-- is wired and migrations ran. Dropped in a later milestone once real
-- tables exercise the same path.
CREATE TABLE IF NOT EXISTS runtime_markers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    value        TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
```

### 5.2 Migration runner

`src/zero/persistence/migrations.py`:

- Lists `*.sql` files in `migrations/` in lexical order.
- For each not yet in `schema_migrations`, applies it inside a
  transaction and records the id.
- On restart, idempotently skips already-applied migrations.
- A failed migration rolls back its own transaction and leaves the
  `schema_migrations` table unchanged, so a retry is safe.

### 5.3 Connection management

`src/zero/persistence/connection.py`:

- Resolves `database_url` to a SQLite connection.
- For `:memory:` databases, caches the connection per-process so tests
  that share a config see the same in-memory database.
- For file databases, opens a new connection per use with
  `check_same_thread=False` (FastAPI is async; we use a thread pool for
  SQLite calls). Foreign keys are enabled on every connection
  (`PRAGMA foreign_keys = ON`).

### 5.4 What later milestones will add (NOT in Phase 1)

- Milestone 2: `users`, `external_identities`, `project_memberships`.
- Milestone 3: `permissions`, `tools`, `tool_grants`, `audit_events`,
  `secret_references`.
- Milestone 4: `plans`, `plan_revisions`, `plan_approvals`.
- Milestone 5: `executions`, `tasks`, `task_dependencies`,
  `task_attempts`.
- Milestone 7: `agent_types`, `agent_instances`, `topology_versions`.
- Milestone 8: `artifacts`, `memory_records`, `rag_projections`.

Each milestone's schema additions go in a new `migrations/NNNN_*.sql`
file. The runner applies them in order. Rollback is by reversing the
migration in a new file (forward repair) or restoring a snapshot —
never by editing applied migrations in place.

## Consequences

- Phase 1's smoke test can write a `runtime_markers` row, restart the
  process, and read it back to prove persistence works end-to-end.
- The discipline of "every project-scoped table has `project_id` with a
  FK" starts now, so Milestone 2+ does not need to retrofit isolation.
- Migrations are auditable, restart-safe, and reversible-by-addition.
- We do not create empty tables for imagined future entities (per
  `zero-foundation-ingestion` §"Scaffolding before a vertical slice").
  The `projects` table is created now only because it is the FK target
  for project-scoped tables in the same migration file, and because
  Milestone 2 will need it.
