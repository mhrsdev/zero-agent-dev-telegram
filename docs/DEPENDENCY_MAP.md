# Zero Develop — Milestone Dependency Map

Source: `PLAN.md` §3 (Dependency Order) and §5–§20 (Milestone definitions).

The dependency order below is the safe construction sequence. Each milestone
is built only on verified earlier milestones. Research, tests, and
documentation may proceed in parallel; production implementation parallelism
requires explicit task dependencies and isolated branches/worktrees.

```
Milestone 0 — Foundation Ingestion and Build Readiness
    │
    ▼
Milestone 1 — Repository Bootstrap and Executable Skeleton
    │
    ▼
Milestone 2 — Central Control Plane, Identity, and Project Isolation
    │
    ▼
Milestone 3 — Authorization, Secret Boundary, Tool Registry, and Audit Core
    │
    ├─────────────────┐
    ▼                 ▼
Milestone 4        Milestone 6 (needs M5+M3)
Plan Lifecycle      Isolated Execution
Main Planner         Branches/Worktrees
    │                 │
    ▼                 │
Milestone 5           │
Main Worker           │
Durable Exec Graph    │
    │                 │
    ▼                 ▼
Milestone 7 — Dynamic Sub Agent Type Lifecycle (needs M5, M6)
    │
    ▼
Milestone 8 — Artifact Store, Persistent Agent Memory, Project RAG
              (needs M2, M3, M5, M7)
    │
    ▼
Milestone 9 — Retrieval Router, Context Builder, Token Budgeting, Compaction
              (needs M8, M5)
    │
    ▼
Milestone 10 — Provider Adapters and Usage Reconciliation (needs M9)
    │
    ▼
Milestone 11 — Integration / Compatibility Review and Controlled Merge
               (needs M6, M7, M10)
    │
    ▼
Milestone 12 — Primary Website Vertical Slices
              (one slice per verified backend milestone)
    │
    ▼
Milestone 13 — Telegram, Discord, Secondary Interface Adapters
              (needs M12 + relevant backend workflow verified)
    │
    ▼
Milestone 14 — Observability, Operational Recovery, Security Hardening
              (instruments every earlier milestone; completed here)
    │
    ▼
Milestone 15 — End-to-End Verification and Controlled Rollout
              (needs every capability used by the scenario individually verified)
```

## Why this order

- **Identity before authorization.** Authorization cannot be evaluated
  without a stable actor identity (Milestone 2 before Milestone 3).
- **Authorization before plans.** Plan approval is a typed authenticated
  event; without authorization it is just a state flag (Milestone 3 before
  Milestone 4).
- **Plans before execution.** No execution begins before an approved plan
  (Milestone 4 before Milestone 5) — this is invariant #8.
- **Execution graph before isolated runner.** The runner needs a task with
  dependencies to know what to execute (Milestone 5 before Milestone 6).
- **Isolated runner before dynamic topology.** Sub Agent Types need
  somewhere safe to execute (Milestone 6 before Milestone 7).
- **Topology before memory/RAG.** Memory is scoped per agent type, so the
  type lifecycle must exist first (Milestone 7 before Milestone 8).
- **Memory before retrieval.** Retrieval retrieves from canonical records
  that do not yet exist without Milestone 8.
- **Retrieval before providers.** Provider adapters render the context the
  retrieval router assembled (Milestone 9 before Milestone 10).
- **Providers before integration.** Integration review may invoke provider
  adapters for compatibility checks (Milestone 10 before Milestone 11).
- **Backend before website.** A website surface is exposed only after its
  backend milestone is verified (Milestone 12 follows each backend).
- **Website before messaging.** Messaging adapters are thinner clients of
  the same control plane (Milestone 13 follows Milestone 12).
- **Observability completes earlier instrumentation.** Cross-system
  behavior is finished here (Milestone 14).
- **End-to-end proves the integrated system.** Only after every capability
  used by the scenario is individually verified (Milestone 15).

## Parallel-safe opportunities

Within a milestone, parallel work is safe when:

- Each task has an explicit isolated branch and worktree (Milestone 6+).
- Dependencies between tasks are encoded in the task graph (Milestone 5+).
- The Worker has verified each subagent's returned artifacts; subagent
  self-report is not proof (per `PLAN.md` §21).

## Phase boundaries (how milestones group into phases)

This implementation groups milestones into phases for manageable review:

| Phase | Milestones | Deliverable shape |
|---|---|---|
| **Phase 1** (current) | M0 + M1 | Foundation understanding + executable skeleton with health endpoint, config validation, isolated test persistence, smoke test |
| Phase 2 | M2 + M3 | Identity, projects, membership, authorization, secret boundary, tool registry, audit core |
| Phase 3 | M4 + M5 | Plan lifecycle, Main Planner, execution graph, Main Worker |
| Phase 4 | M6 + M7 | Isolated runner, dynamic Sub Agent Type lifecycle |
| Phase 5 | M8 + M9 | Artifact store, memory, Project RAG, retrieval router, context builder, compaction |
| Phase 6 | M10 + M11 | Provider adapters, integration/merge gates |
| Phase 7 | M12 | Primary website vertical slices |
| Phase 8 | M13 | Telegram/Discord adapters |
| Phase 9 | M14 + M15 | Observability, recovery, end-to-end verification, controlled rollout |

Phase grouping is for review cadence only. The dependency order above is
the source of truth; if a phase groups milestones that the dependency order
says are sequential, the phase still executes them sequentially.

## Phase 1 scope (this implementation)

Phase 1 covers **Milestone 0 + Milestone 1**. The acceptance criteria from
`PLAN.md`:

### Milestone 0 acceptance

- A model with no prior context can explain the product, trust boundaries,
  dependency order, and first slice using only repository artifacts. ✓
  (See `REQUIREMENT_LEDGER.md`, `CURRENT_STATE_LEDGER.md`, this file, and
  ADRs 0001–0005.)
- No unresolved contradiction affects the first slice. ✓
- The chosen first slice can run and be tested without fake downstream
  systems. ✓ (Smoke test starts the real app and probes the real health
  endpoint.)

### Milestone 1 acceptance

- A fresh implementation environment can start and stop the application
  using documented commands. ✓ (`README.md` documents `uvicorn
  zero.main:app --reload` and the smoke test does it programmatically.)
- The smoke check proves the same executable path intended for later
  milestones. ✓ (Same `zero.main:app` ASGI app, same config validation,
  same persistence layer.)
- Clean setup from documented commands. ✓ (`pip install -e .` then run.)
- Production-mode build or equivalent succeeds. ✓ (`python -c "import
  zero.main"` succeeds; `pytest` succeeds.)
- Health check succeeds against a running process. ✓ (HTTP GET
  `/healthz` returns 200 with JSON body.)
- Invalid configuration fails closed with a useful error. ✓ (Missing
  `ZERO_ENV` or pointing at a forbidden production value raises
  `ConfigError` at startup.)
- Test data is demonstrably isolated. ✓ (`ZERO_ENV=test` forces a
  temporary SQLite file under `tests/.tmp/` or in-memory; production
  env refuses to run tests.)
