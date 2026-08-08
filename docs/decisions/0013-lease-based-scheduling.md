# ADR 0013 — Lease-Based Task Scheduling with Idempotent Claims

- Status: ACCEPTED
- Date: 2026-08-08
- Milestone: 5 (Main Worker and Durable Execution Graph)
- Skills applied: `zero-planner-worker-contract`, `zero-recovery-consistency`,
  `zero-tool-capability-runtime`

## Context

`PLAN.md` §10 (Milestone 5) requires:
- Retries and duplicate events are idempotent.
- Replayed scheduler events do not duplicate work.

`zero-recovery-consistency` §"Leases distinguish ownership from
history": "A running state without a current lease can survive forever
after a crash. A lease identifies which worker currently owns progress
and when ownership may be reconsidered. An expired lease does not
prove failure. It proves that current ownership is absent;
reconciliation inspects process, artifact, and external evidence."

`zero-planner-worker-contract` §"Idempotency is part of normal
operation": "one execution per approved plan revision; one accepted
transition per external event ID; one task attempt per scheduled
attempt ID; one integration result per immutable input set."

## Decision

Adopt a lease-based scheduling model:

1. **Claim**: `claim_task(execution_id, task_id, lease_owner)` creates
   a new `TaskAttempt` with an incremented `attempt_number` and a
   `lease_expires_at` timestamp. The task transitions `ready -> running`.
2. **Complete**: `complete_task(...)` marks the attempt `succeeded`
   and the task `completed`. Dependents' readiness is recomputed.
3. **Fail**: `fail_task(...)` marks the attempt `failed` and the task
   `failed`. Dependents are marked `blocked`. The execution may
   transition to `paused` if no work remains.
4. **Recovery**: `recover_after_restart(...)` finds tasks in `running`
   state, marks their last attempt `unknown`, and transitions them
   back to `ready` for re-claiming.

### Why leases?

A lease identifies which worker currently owns a task's progress and
when ownership may be reconsidered. Without leases:

- A worker crashes mid-task; the task stays `running` forever.
- Another worker cannot re-claim the task because it's already
  "running".
- The execution stalls indefinitely.

With leases:

- A worker claims a task with a lease duration (default 300 seconds).
- If the worker crashes, the lease expires.
- Recovery finds tasks in `running` state with expired/absent leases
  and transitions them back to `ready`.
- The last attempt is marked `unknown` (not `failed`) because we
  don't know if the worker completed the side effect before crashing.

### Why `unknown` instead of `failed`?

Per `zero-tool-capability-runtime` §"Cancellation reaches tools through
durable identity": "`unknown` is safer than invented failure or success
when process state cannot be proven."

A worker that crashed mid-task may have:
- completed the side effect but not the response (we should not retry);
- failed before the side effect (we should retry);
- be still running on another host (we should not interfere).

Marking the attempt `unknown` preserves the uncertainty. The human
(or an automated reconciliation process) can inspect the actual side
effects and decide whether to retry.

### Why incremented attempt numbers?

Each claim creates a new attempt with `attempt_number = len(existing_attempts) + 1`.
This gives us:

- A complete audit trail of every attempt on a task.
- The ability to distinguish the first attempt from retries.
- Idempotency: `UNIQUE(task_id, attempt_number)` prevents duplicate
  attempts with the same number.

### Why not a general-purpose queue?

Per `zero-modular-bootstrap` §"A queue is a behavior, not a default
component": "Durable asynchronous work may eventually require queue
semantics: claiming, visibility, retry, deduplication, cancellation,
and recovery. Early development may express those semantics with
database-backed task state and one worker process."

Our task table IS the queue. The `ready` state is the queue; `claim_task`
is the dequeue operation; the lease is the visibility timeout. A
dedicated broker (Redis, RabbitMQ, etc.) is deferred until measured
throughput exceeds what the database can handle.

## Rejected alternatives

- **In-memory task queue**: rejected by
  `zero-planner-worker-contract` §"Durable state is stronger than
  agent memory". All state is in the database.
- **Marking interrupted tasks as failed**: rejected by
  `zero-recovery-consistency`. `unknown` is safer.
- **External broker from day 1**: rejected by
  `zero-modular-bootstrap`. Adds operational complexity before
  measured demand requires it.
- **Auto-retry on failure without human intervention**: rejected by
  PLAN.md M5: "Human-decision conflicts pause rather than being
  guessed away." Failed tasks block and pause; the human decides
  whether to retry.

## Consequences

- The scheduler is restart-safe: a crashed worker's tasks are
  recovered and re-claimed.
- The audit trail records every attempt, including `unknown` ones.
- Adding a real task runner (M6) is a local change: it calls
  `claim_task`, runs the work, and calls `complete_task` or
  `fail_task`. No scheduler changes needed.
- Migrating to an external broker later is a refactor along an
  existing seam (the `claim_task` / `complete_task` / `fail_task`
  interface), not a rewrite.
