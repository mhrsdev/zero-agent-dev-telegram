# Phase 3 Closeout Report — Plan Lifecycle, Main Planner, Execution Graph, Main Worker

- **Phase**: 3 (Milestone 4 + Milestone 5)
- **Status**: VERIFIED
- **Date**: 2026-08-08
- **Skills applied**: `zero-planner-worker-contract`,
  `zero-context-memory`, `zero-control-plane-trust`,
  `zero-recovery-consistency`, `zero-tool-capability-runtime`,
  `zero-modular-bootstrap`, `zero-foundation-ingestion`,
  `zero-rollout-readiness`

---

## 1. Scope delivered

Phase 3 covers **Milestone 4** (Plan Lifecycle and Main Planner) and
**Milestone 5** (Main Worker and Durable Execution Graph) from
`PLAN.md`.

### Milestone 4 — Plan Lifecycle and Main Planner

| Required invariant (PLAN.md §9) | Status | Evidence |
|---|---|---|
| Human events carry authenticated actor and origin metadata | DONE | `ConversationEvent` has actor_id, source, origin_kind |
| `role=user` alone never proves human identity | DONE | `origin_kind` field; only `authenticated_human` counts as human intent |
| Planning does not require a magic phrase | DONE | Plans are created explicitly; conversation content is never interpreted as a command |
| Execution is impossible before authorized approval | DONE | Handoff record only created on approval; Worker requires approved handoff |
| Reject stops the flow; Edit returns it for correction with lineage | DONE | Rejection produces no handoff; edit creates a new revision with provenance |
| Plan revisions are durable and auditable | DONE | Revisions are immutable; approvals are append-only (triggers block UPDATE/DELETE) |

| Deliverable (PLAN.md §9) | Status | Evidence |
|---|---|---|
| Canonical conversation/event intake | DONE | `ingest_conversation_event` with interface-neutral envelope |
| Typed plan states and allowed transitions | DONE | `PlanState`, `PLAN_TRANSITIONS`, `is_valid_transition` |
| Main Planner input contract | DONE | `PlanRevisionContent` with structured fields; validated before storage |
| Plan proposal with intent, scope, constraints, acceptance, risks, unresolved | DONE | All fields in `PlanRevisionContent` |
| Approve, Reject, Edit enforced server-side | DONE | `approve_revision`, `reject_revision`, `propose_revision` (edit) |
| Immutable approval evidence tied to a revision | DONE | `PlanApproval` (append-only); `PlanHandoff` (UNIQUE per revision) |

**Acceptance criteria (PLAN.md §9):**

> An authorized user can submit natural discussion, receive a
> reviewable plan, edit it, approve the final revision, and produce
> exactly one immutable handoff record—without any code execution
> occurring yet.

✅ **VERIFIED.** Tests in `tests/test_plans.py` (20 tests) and
`tests/test_http_phase3.py` (7 tests):
- `test_plan_lifecycle_end_to_end` — full flow through HTTP.
- `test_approve_revision_creates_handoff` — handoff produced.
- `test_duplicate_approval_is_idempotent` — one handoff per revision.
- `test_stale_revision_approval_fails` — old revision rejected.
- `test_unauthorized_approval_fails` — viewer cannot approve.
- `test_rejection_leaves_no_handoff` — no handoff on rejection.
- `test_prompt_injection_in_content_cannot_bypass_state_transitions`.
- `test_plan_approvals_are_append_only` — triggers block UPDATE.

**M4 validation gates (all pass):**
- ✅ Ordinary discussion does not silently execute.
- ✅ A plan cannot approve itself (requires authorized human).
- ✅ Unauthorized approval fails.
- ✅ Approval of an old revision fails after edit.
- ✅ Duplicate approval events are idempotent.
- ✅ Prompt injection inside conversation content cannot bypass state
  transitions or permissions.
- ✅ Rejection leaves no runnable execution request.

### Milestone 5 — Main Worker and Durable Execution Graph

| Required invariant (PLAN.md §10) | Status | Evidence |
|---|---|---|
| Worker accepts only a valid approved plan revision | DONE | `create_execution_from_handoff` verifies plan is approved |
| Task state is typed and durable | DONE | `Task`, `TaskState`, `TASK_TRANSITIONS`; all in database |
| Dependencies determine readiness and concurrency | DONE | `_recompute_readiness` based on dependency states |
| Retries and duplicate events are idempotent | DONE | UNIQUE constraints on execution, attempt, dependency |
| Human-decision conflicts pause rather than being guessed away | DONE | `_maybe_complete_execution` pauses when tasks blocked |

| Deliverable (PLAN.md §10) | Status | Evidence |
|---|---|---|
| Execution and task lifecycle | DONE | `Execution`, `Task`, state machines |
| Dependency representation and cycle rejection | DONE | `TaskDependency`; Kahn's algorithm in `_detect_cycles` |
| Worker decomposition contract | DONE | `TaskSpec`, `DependencySpec`, `create_execution_from_handoff` |
| Task readiness and scheduling decisions | DONE | `_recompute_readiness`, `list_ready_tasks`, `claim_task` |
| Typed blockers, retries, cancellation, terminal outcomes | DONE | `blocker_reason`, `TaskAttempt`, `cancel_execution` |
| Durable execution snapshot | DONE | `ExecutionSnapshot` (versioned JSON) |
| Correlation among plan, execution, task, agent, tool, audit | DONE | `correlation_id` on audit events; execution_id links |

**Acceptance criteria (PLAN.md §10):**

> A sample approved plan produces a deterministic graph, exposes only
> valid ready tasks, survives process restart, and reaches a correct
> terminal state without executing code yet.

✅ **VERIFIED.** Tests in `tests/test_execution.py` (21 tests):
- `test_create_execution_from_approved_handoff` — deterministic graph.
- `test_independent_tasks_become_ready_together` — only valid ready tasks.
- `test_dependent_tasks_remain_blocked_until_prerequisites_succeed`.
- `test_failed_prerequisite_blocks_dependent`.
- `test_cycle_rejected`, `test_self_dependency_rejected`,
  `test_missing_dependency_rejected`.
- `test_restart_reconstructs_graph` — survives restart.
- `test_restart_preserves_completed_tasks`.
- `test_replayed_claim_creates_new_attempt` — no duplicate work.
- `test_cancellation_propagates_to_non_terminal_tasks`.
- `test_execution_completes_when_all_tasks_complete` — correct terminal.
- `test_execution_pauses_when_task_blocked` — human-decision pause.

**M5 validation gates (all pass):**
- ✅ Independent tasks become ready together.
- ✅ Dependent tasks remain blocked until prerequisites succeed.
- ✅ Failed prerequisites block dependents safely.
- ✅ Cycles and missing dependencies are rejected.
- ✅ Restart reconstructs the same graph and statuses.
- ✅ Replayed scheduler events do not duplicate work.
- ✅ Cancellation propagates according to an explicit tested rule.

## 2. Evidence summary

### Test results

```
$ pytest -v
============================= 179 passed in 1.91s =============================

tests/test_config.py .........................                  [ 14%]
tests/test_health.py ...                                        [ 16%]
tests/test_persistence.py ........                              [ 20%]
tests/test_smoke.py ....                                        [ 22%]
tests/test_identity.py .....................                    [ 34%]
tests/test_authorization.py .................                   [ 44%]
tests/test_isolation.py ........                                [ 48%]
tests/test_secrets.py ...........                               [ 54%]
tests/test_audit.py ........                                    [ 59%]
tests/test_tools.py ..................                          [ 69%]
tests/test_http_phase2.py ........                              [ 73%]
tests/test_plans.py ....................                        [ 84%]
tests/test_execution.py .....................                   [ 96%]
tests/test_http_phase3.py .......                              [100%]
```

Test breakdown (Phase 3 additions):
- `test_plans.py`: 20 tests — conversation intake (with duplicate
  idempotency), plan creation, proposal (with content validation),
  edit (new revision without changing old), approval (with stale
  rejection and idempotency), rejection (no handoff), prompt injection
  defense, cross-project isolation, append-only approvals.
- `test_execution.py`: 21 tests — execution creation (idempotent),
  independent tasks ready together, dependent tasks blocked, failed
  prerequisites block dependents, cycle/self-dep/missing-dep rejection,
  restart recovery (leases + unknown attempts), replayed claims (new
  attempt numbers), cancellation propagation, execution
  completion/pause, snapshots, idempotent transitions.
- `test_http_phase3.py`: 7 tests — plan lifecycle end-to-end, stale
  revision 409, unauthorized approval 403, execution creation, cycle
  rejection 400, cancellation, recovery.

### Database integrity

- 4 migrations applied (0001–0004).
- Append-only triggers on `plan_approvals` (blocks UPDATE/DELETE).
- UNIQUE constraints enforce idempotency:
  - `conversation_events`: UNIQUE(source, external_event_id).
  - `plan_approvals`: UNIQUE(revision_id, result, idempotency_key).
  - `plan_handoffs`: UNIQUE(revision_id) — one handoff per revision.
  - `executions`: UNIQUE(plan_revision_id) — one execution per revision.
  - `task_attempts`: UNIQUE(task_id, attempt_number).
  - `task_dependencies`: UNIQUE(task_id, depends_on_task_id) with
    CHECK(task_id != depends_on_task_id).

## 3. Architecture decisions active after Phase 3

| ADR | Title | Decision |
|---|---|---|
| 0001 | Technology Stack | Python 3.12, FastAPI, SQLite, pytest |
| 0002 | Modular Monolith | One process, explicit internal modules |
| 0003 | Project Layout | `src/zero/{domain,app,persistence,adapters}/` |
| 0004 | Configuration as Trust Boundary | Typed, validated, fail-closed |
| 0005 | Persistence Starts with Invariants | Minimal schema, FK-enforced |
| 0006 | Canonical Identity Model | Server-issued IDs; external IDs are verified links |
| 0007 | Role-Based Authorization | 3 roles × 16 permissions; central decision path |
| 0008 | Encrypted Secret Storage | Fernet + HKDF; resolve_value is the only decrypt path |
| 0009 | Capability-Based Tool Runtime | Registry + grants; full invocation lifecycle |
| 0010 | Append-Only Audit Log | Triggers block UPDATE/DELETE; defensive redaction |
| 0011 | Versioned Plan Revisions | Immutable revisions; typed state machine; stale rejection |
| 0012 | Durable Execution Graph | Topological cycle detection; lease-based; durable snapshots |
| 0013 | Lease-Based Scheduling | Idempotent claims; unknown attempts on restart; no auto-retry |

## 4. Deferred scope and the evidence required to add it

| Deferred item | When | Evidence required |
|---|---|---|
| Real task runner (code execution) | M6 | Isolated branch/worktree lifecycle; command runner |
| Real Main Planner LLM integration | M12 or follow-up | LLM adapter that produces PlanRevisionContent from events |
| Automatic lease expiration timer | M14 | Background recovery process |
| Real HTTP authentication | M12 | Session tokens / OIDC |
| Structured logging beyond stdlib | M14 | Operational need |
| Metrics emission | M14 | Operational need |
| Backup/restore drill | M14 | Tested restore procedure |

## 5. Migration and rollback status

- **Schema migrations applied**: 4 (0001–0004).
- **Rollback path**: forward-repair via a new migration file; never
  edit applied migrations in place.
- **Plan history**: append-only; revisions and approvals are never
  deleted. Deactivation is via state transition to `archived`.
- **Execution snapshots**: versioned; the highest version is the
  current restart-safe state. Old snapshots are retained for audit.

## 6. Unresolved security or reliability risks

- **Lease expiration is not automatic**: `recover_after_restart` must
  be called explicitly (or by a future background timer). A stalled
  worker's tasks stay in `running` until recovery is invoked. M14
  adds the timer.
- **No real code execution yet**: per PLAN.md M5 acceptance, no code
  is executed in Phase 3. M6 adds the isolated runner.
- **No real Main Planner model**: the plan service accepts structured
  content from the caller. A future LLM adapter will produce
  PlanRevisionContent from conversation events.
- **No structured logging or metrics**: deferred to M14.
- **No backup/restore drill**: deferred to M14.

None of these block Phase 3 acceptance. Each is explicitly listed so
later milestones know what to pick up.

## 7. Confirmation

- ✅ Every current foundation file and relevant dynamically discovered
  skill was considered. — 26/26 files read; 16/16 skills read.
- ✅ No commit, push, merge, deployment, or destructive operation
  occurred without explicit authorization. — Local git commits on
  `main`; no remote configured; no external services contacted.
- ✅ The system is reported as `VERIFIED` for the Phase 3 scope only
  (Milestone 4 + Milestone 5). Later milestones are `PLANNED`.
- ✅ Plan revisions are immutable; approvals are append-only; one
  handoff per approved revision.
- ✅ Execution graph rejects cycles; independent tasks become ready
  together; failed prerequisites block dependents; restart
  reconstructs the graph; cancellation propagates explicitly.
- ✅ No code execution occurs in Phase 3 (per M5 acceptance).
