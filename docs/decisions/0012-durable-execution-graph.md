# ADR 0012 — Durable Execution Graph with Topological Dependencies

- Status: ACCEPTED
- Date: 2026-08-08
- Milestone: 5 (Main Worker and Durable Execution Graph)
- Skills applied: `zero-planner-worker-contract`, `zero-recovery-consistency`

## Context

`PLAN.md` §10 (Milestone 5) requires:
- Worker accepts only a valid approved plan revision.
- Task state is typed and durable, not held only in model context.
- Dependencies determine readiness and concurrency.
- Retries and duplicate events are idempotent.
- Human-decision conflicts pause rather than being guessed away.

`zero-planner-worker-contract` §"Worker decomposition preserves
intent": "The Worker turns an approved outcome into tasks. It may
choose technical ordering, split independent work, or request
specialist agents. It may not silently drop acceptance criteria or
reinterpret exclusions."

`zero-planner-worker-contract` §"Durable state is stronger than agent
memory": "The task graph, approvals, workspaces, running processes,
test outcomes, and blockers live in canonical backend state. After
restart, the system should derive which tasks are complete, which are
ready, which were interrupted, which worktrees belong to them, and
what evidence exists — without asking a model to remember what
happened."

`zero-recovery-consistency` §"Idempotency makes retries ordinary":
"one execution per approved plan revision; one task attempt per
scheduled attempt ID."

## Decision

Adopt a durable execution graph with five layers:

1. **Execution**: one per approved plan revision (UNIQUE constraint).
   Has a state machine: pending -> running -> paused/completed/failed/
   cancelled. `blocker_reason` is set when paused for a human decision.
2. **Task**: a node in the graph. Has a state machine: pending -> ready
   -> running -> completed/failed/blocked/cancelled. Each task carries
   objective, permitted_scope, expected_evidence, and optional
   agent_type_id (set in M7).
3. **TaskDependency**: an edge. `task_id` depends on
   `depends_on_task_id`. CHECK constraint prevents self-dependencies.
4. **TaskAttempt**: individual run attempts (for retries). Each claim
   creates a new attempt with an incremented attempt_number. Has a
   lease_owner and lease_expires_at for ownership tracking.
5. **ExecutionSnapshot**: durable restart-safe state. A JSON document
   capturing the full task graph state (task IDs, states, dependencies,
   terminal evidence references). Versioned; the highest version is
   the current restart-safe state.

### Cycle detection

Before creating the graph, the Worker runs a topological sort
(Kahn's algorithm). If a cycle is detected, the operation is rejected
with `CycleError` listing the nodes in cycles. This prevents the graph
from being created in an inconsistent state.

### Readiness computation

A task is `ready` when:
- its state is `pending` (not yet started);
- it has no dependencies (independent task); OR
- all its dependencies are in the `completed` state.

A task is `blocked` when any dependency is in a blocking state
(failed, blocked, cancelled). Blocked tasks cannot proceed until the
human resolves the blocker or the dependency is retried successfully.

### Idempotency

- `UNIQUE(plan_revision_id)` on executions: one execution per approved
  revision. Duplicate creation requests return the existing execution.
- `UNIQUE(task_id, attempt_number)` on task_attempts: each claim
  creates a new attempt with a unique number.
- `UNIQUE(task_id, depends_on_task_id)` on task_dependencies:
  duplicate dependency edges are idempotent.

### Restart recovery

`recover_after_restart`:
1. Tasks in `running` state (with an expired or absent lease) are
   transitioned back to `ready` so they can be re-claimed.
2. Their last attempt is marked `unknown` (per
   `zero-tool-capability-runtime`: `unknown` is safer than invented
   failure or success).
3. Pending tasks have their readiness recomputed.
4. If the execution was `running` and no tasks are running, it
   transitions to `paused` with a blocker_reason.
5. A snapshot is taken with reason `restart_recovery`.

### Cancellation propagation

`cancel_execution`:
1. Tasks in terminal states (completed, cancelled) are not changed.
2. Tasks in non-terminal states (pending, ready, running, blocked) are
   transitioned to `cancelled`.
3. Running attempts are transitioned to `cancelled`.
4. The execution transitions to `cancelled`.
5. A snapshot is taken.

### Human-decision conflicts pause execution

When a task fails and no tasks are running or ready, the execution
transitions to `paused` with a `blocker_reason`. This prevents the
Worker from guessing how to proceed; the human must resolve the
blocker (retry the task, cancel the execution, or change the plan).

## Rejected alternatives

- **General workflow engine**: explicitly rejected by PLAN.md M5: "Do
  not create a general workflow engine unless current needs exceed a
  small explicit task graph." Our task graph is small and explicit.
- **In-memory task state**: explicitly rejected by
  `zero-planner-worker-contract` §"Durable state is stronger than
  agent memory". All state is in the database.
- **Marking interrupted tasks as failed**: rejected by
  `zero-recovery-consistency` §"Leases distinguish ownership from
  history". An expired lease does not prove failure; we mark the
  attempt `unknown` and transition the task back to `ready`.
- **Auto-retrying failed tasks without human intervention**: rejected
  by PLAN.md M5: "Human-decision conflicts pause rather than being
  guessed away." Failed tasks block dependents and pause the
  execution.

## Consequences

- The execution graph is fully durable: after restart, the Worker
  reconstructs the same graph and statuses from the database.
- Cycles are impossible: topological sort rejects them before
  creation.
- Independent tasks become ready together; dependent tasks remain
  blocked until prerequisites succeed; failed prerequisites block
  dependents safely.
- Replayed scheduler events do not duplicate work: each claim creates
  a new attempt, and idempotency constraints handle duplicates.
- Cancellation propagates explicitly: terminal tasks are preserved,
  non-terminal tasks are cancelled, running attempts are cancelled.
- The graph is auditable: every transition produces an audit event
  with a correlation ID linking related events.
