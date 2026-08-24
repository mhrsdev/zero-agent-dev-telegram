# GAP 10 Design — LLM-Driven Task Decomposition

Status: design accepted · Phase 3

## Problem

`SchedulerService.run_once` creates exactly one `implementation` task
per approved handoff regardless of plan complexity.

## Architecture

New `TaskDecomposer` (`src/zero/app/task_decomposition.py`) consulted
by the scheduler when creating an execution from a handoff:

```
decompose(revision) -> list[TaskSpec] | None
    ├─ prompt LLM with revision content (objective/scope/constraints/
    │  acceptance criteria) + strict JSON contract:
    │  [{"key","objective","scope":[...],"depends_on":[key,...]}, ...]
    ├─ strip code fences, parse JSON, validate graph:
    │     ≤256 nodes, ≤1024 edges, acyclic (Kahn), unique keys,
    │     non-empty objectives, depends_on references existing keys
    ├─ cache by plan_revision_id (in-process dict + evidence artifact)
    └─ any failure (parse/validate/LLM error) → None → single-task fallback
```

- Scheduler behavior: `decomposition.enabled=false` (default) or
  `None` result ⇒ current single-task `TaskSpec(key="implementation")`
  path byte-for-byte.
- When decomposition succeeds, tasks are created with
  `depends_on` edges via the existing dependency mechanism in
  `WorkerService.create_execution_from_handoff` TaskSpec extension
  (`depends_on: tuple[str, ...] = ()`), which maps to
  `TaskDependency` rows already supported by the execution graph.
- Config: `ZERO_DECOMPOSITION_ENABLED=1`, model/provider reused from
  scheduler call parameters; temperature 0.0;
  idempotency key `decompose:{revision_id}`.

## Data model changes

None. Uses existing task-dependency tables. Evidence artifacts store
the raw prompt/response pair (kind `"other"`, producer
`task-decomposer:{revision_id}`) per requirement to log evidence.

## API surface

No new HTTP routes. `GET /executions/{id}` already returns tasks and
dependencies; multi-node graphs surface there.

## Security considerations

- Plan content is operator-approved text; still redacted before audit.
- Output size bounded at 64 KiB before parsing; node/edge caps prevent
  graph bombs; cycle detection prevents unrunnable executions.
- Decomposition failure never blocks work: fallback guarantees an
  execution exists.

## Test strategy

- Validator unit tests: valid DAGs; >256 nodes rejected; >1024 edges;
  cyclic graphs rejected; duplicate keys; empty objective; dangling
  dependency; oversized payload.
- Fake-provider tests: happy path producing 3-node chain → three tasks
  with correct dependencies; malformed JSON → single-task fallback;
  disabled flag → no provider call.
- Idempotency test: same revision twice → cached specs, one provider
  request (request-hash dedup also proves this).
- Backward-compat test: default config produces identical single task.

## Migration path

Additive module + config flag; scheduler keeps fallback path as the
code default.

## Rollback strategy

Set `ZERO_DECOMPOSITION_ENABLED=0`; remove module later.

## Acceptance criteria

- Simple plans still produce single-task graphs (default off).
- Complex plans produce multi-node dependency graphs when enabled.
- Parse/validation failure falls back gracefully to single-task.
- Suite green with no behavioral change under default configuration.
