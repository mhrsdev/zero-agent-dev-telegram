# ADR 0015 — Dynamic Sub Agent Type Lifecycle with Topology Versioning

- Status: ACCEPTED
- Date: 2026-08-08
- Milestone: 7 (Dynamic Sub Agent Type Lifecycle)
- Skills applied: `zero-agent-execution-lifecycle`, `zero-context-memory`

## Context

`PLAN.md` §12 (Milestone 7) requires:
- Main roles (Planner, Worker) are fixed. Sub Agent Types are
  project-specific and dynamic.
- Type responsibility, memory scope, tool rights, model policy,
  context budget, and concurrency limit are explicit.
- Instances share accepted type knowledge but not task-local scratch
  context.
- Split, merge, retirement, and role changes are lossless and
  reversible.

`zero-agent-execution-lifecycle` §"Topology evolution is a data
migration": "A safe transition has these conceptual stages: freeze
writes or establish a version boundary; snapshot the source scope;
copy/transform records into destination scopes with provenance links;
rebuild destination indexes; run retrieval and count/hash reconciliation
checks; activate the destination topology; archive the source topology;
retain rollback metadata. Abort activation if any mandatory record
cannot be accounted for. Archive; never hard-delete as part of topology
evolution."

`zero-context-memory` §"Non-negotiable invariants": "Removing,
splitting, or merging a sub-agent type never deletes its knowledge."

## Decision

Adopt a dynamic Sub Agent Type lifecycle with topology versioning:

1. **Agent types**: project-specific definitions with explicit
   responsibility, memory_scope, permitted_tools, model_policy,
   context_budget_tokens, and max_concurrent_instances. State machine:
   ``active → archived → retired`` (retired is terminal).
2. **Agent instances**: runtime actors of a type. State machine:
   ``idle → running → completed/failed/cancelled``. The concurrency
   limit is enforced when assigning an instance to a task (transitioning
   to ``running``), not when creating an idle instance.
3. **Knowledge records**: agent-type-scoped memory with kind (decision,
   fact, constraint, contract, failure, other), content, content_hash,
   provenance, state (candidate/approved/superseded/archived),
   superseded_by, and migrated_from. Records are never hard-deleted;
   they are archived.
4. **Topology snapshots**: frozen topology state (JSON) captured before
   every split/merge/retire/rollback. Versioned; the highest version is
   the current restart-safe state.

### Split

``split_type(source_type_id, destination_specs, knowledge_routing)``:
1. Take a topology snapshot (``before_split``).
2. Create destination types.
3. Route knowledge records to destinations (reassign with
   ``migrated_from`` provenance).
4. Archive unrouted records (never delete).
5. Reconcile: verify every source record still exists (migrated or
   archived).
6. Archive the source type (set ``superseded_by``).

### Merge

``merge_types(source_type_ids, destination_spec)``:
1. Take a topology snapshot (``before_merge``).
2. Create the destination type (union of tool permissions, max budget).
3. Migrate all knowledge records from all source types to the
   destination (with ``migrated_from`` provenance).
4. Archive all source types (set ``superseded_by`` to destination).

### Retire

``retire_type(type_id)``:
1. Verify no running instances (blocked if any).
2. Take a topology snapshot (``before_retire``).
3. Archive all knowledge records.
4. Reconcile: verify no records in non-archived state.
5. Transition the type to ``retired`` (terminal).

### Rollback

``rollback_to_snapshot(snapshot_id)``:
1. Take a new snapshot (``rollback``) for audit.
2. Reactivate types that were active at snapshot time but are now
   archived.
3. Archive types that were created after the snapshot.
4. Never delete any types or knowledge records.

## Rejected alternatives

- **Fixed catalog of specialist types**: explicitly rejected by
  ``zero-agent-execution-lifecycle`` §"Dynamic does not mean arbitrary"
  and by PLAN.md M7: "Do not prebuild a catalog of imagined specialist
  types."
- **Hard-delete on retire**: explicitly rejected by
  ``zero-context-memory`` §"Non-negotiable invariants" and by
  ``zero-agent-execution-lifecycle`` §"Never hard-delete source topology
  or memory as part of evolution."
- **In-memory topology state**: rejected by
  ``zero-planner-worker-contract`` §"Durable state is stronger than
  agent memory". All state is in the database.
- **Auto-reconcile without verification**: rejected by PLAN.md M7:
  "Retirement is blocked until reconciliation passes." The
  reconciliation step verifies every record is accounted for before
  activation.

## Consequences

- A type can be created, instantiated, split, merged, retired, and
  rolled back with every mandatory knowledge record accounted for.
- Knowledge is never lost: unrouted records are archived; migrated
  records carry provenance links.
- Topology snapshots enable rollback to any previous state.
- Cross-project type or memory access returns nothing (project-scoped
  queries).
- Instance concurrency respects the type's limit (enforced at task
  assignment).
