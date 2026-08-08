# ADR 0002 — Modular Monolith (Single Process, Explicit Internal Boundaries)

- Status: ACCEPTED
- Date: 2026-08-08
- Milestone: 1
- Skills applied: `zero-modular-bootstrap`

## Context

`zero-modular-bootstrap` SKILL.md §"Architecture begins with change
boundaries" lists the change boundaries Zero must respect:

- identity and authorization
- plans and approvals
- execution graphs
- tools and providers
- artifacts and knowledge
- interface adapters

The same skill §"Wrong example" warns against: "identity-service,
planner-service, worker-service, memory-service, Telegram-service,
provider-service, and a message bus before one user exists."

`PLAN.md` §22 explicitly permits representing a boundary as "a function,
module, service object, process, or database constraint."

## Decision

Adopt a **modular monolith**: one Python process containing explicit
internal modules that mirror the change boundaries. Distribution into
separate services is deferred until a real scaling, security, deployment,
or ownership boundary requires it.

The internal module layout (see ADR 0003 for the full tree):

```
src/zero/
├── main.py              # ASGI entry point; wires app together
├── config.py            # configuration validation (trust boundary)
├── domain/              # pure domain logic + types
├── app/                 # application operations (use cases)
├── persistence/         # database access + migrations
└── adapters/            # external SDKs and transports (HTTP, Telegram, providers)
```

Dependency direction is **inward**:

```
adapters  →  app  →  domain
                ↑
          persistence
```

- `domain` depends on nothing inside Zero (only stdlib + typing).
- `app` depends on `domain` and on persistence interfaces (not
  implementations).
- `persistence` implements interfaces declared in `domain`/`app`.
- `adapters` translate external payloads into canonical domain events and
  translate domain results back into transport-specific responses.
- `main.py` is the only place that wires concrete implementations
  together.

## Rejected alternatives

- **Microservices from day 1**: pays deployment, consistency, tracing, and
  failure costs without a current scaling or ownership need. Explicitly
  rejected by `zero-modular-bootstrap`.
- **Single flat `app/` package with no internal boundaries**: would make
  every external upgrade a core migration (per `zero-modular-bootstrap`
  §"SDK types in core state") and removes the change-boundary signal.
- **Hexagonal / clean architecture with formal port/adapter interfaces**:
  adds abstraction before a second adapter exists. We will extract formal
  ports the moment a second implementation of any boundary appears
  (e.g. when the second provider adapter is added in Milestone 10).

## Consequences

- The first vertical slice has one process to start, one process to test,
  one process to debug.
- Adding a second adapter (Telegram in Milestone 13, a second provider in
  Milestone 10) is a local change in `adapters/`, not a top-level
  restructure.
- Extracting a service later (e.g. an isolated runner process for
  Milestone 6) is a refactor along an existing seam, not a rewrite.
- The price is discipline: developers must not let `domain` import from
  `adapters` or `persistence` directly. A simple import-cycle check is
  part of the test suite to enforce this.
