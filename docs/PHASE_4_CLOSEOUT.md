# Phase 4 Closeout Report — Isolated Execution + Dynamic Sub Agent Types

- **Phase**: 4 (Milestone 6 + Milestone 7)
- **Status**: VERIFIED
- **Date**: 2026-08-08
- **Skills applied**: `zero-agent-execution-lifecycle`,
  `zero-recovery-consistency`, `zero-context-memory`,
  `zero-artifact-provenance-model`, `zero-project-isolation-evidence`,
  `zero-control-plane-trust`, `zero-planner-worker-contract`,
  `zero-rollout-readiness`

---

## 1. Scope delivered

Phase 4 covers **Milestone 6** (Isolated Execution with Branches and
Worktrees) and **Milestone 7** (Dynamic Sub Agent Type Lifecycle) from
`PLAN.md`.

### Milestone 6 — Isolated Execution with Branches and Worktrees

| Required invariant (PLAN.md §11) | Status | Evidence |
|---|---|---|
| Every coding task receives an isolated branch and working tree | DONE | `create_worktree` creates a unique branch + worktree per task |
| The target repository and base revision are explicit | DONE | `register_repository` + `base_revision` on each worktree |
| Commands are scoped, time-bounded, and audited | DONE | `run_command` with cwd, timeout, audit event |
| A task returns diff, checks, artifacts, and status | DONE | `capture_diff` + artifact capture (stdout, stderr, exit_status) |
| No task pushes, merges, or deploys without explicit authority | DONE | No push/merge/deploy commands in the runner |
| Cleanup never deletes an unknown path, mount, active workspace, or uncommitted human work | DONE | `remove_worktree` uses `git worktree remove` (no --force); path validation; state check |

| Deliverable (PLAN.md §11) | Status | Evidence |
|---|---|---|
| Repository registration and validated local path handling | DONE | `register_repository` + `validate_repository_path` |
| Isolated branch/worktree lifecycle | DONE | `create_worktree`, `activate_worktree`, `complete_worktree`, `mark_cleanup_eligible`, `remove_worktree` |
| Minimal command runner using authorized tool capabilities | DONE | `run_command` with scoped cwd and timeout |
| Artifact capture for stdout/stderr, exit status, diff, and test evidence | DONE | `task_artifacts` table with content_hash |
| Interruption, timeout, cancellation, and safe cleanup behavior | DONE | `recover_worktrees_after_restart`, timeout handling, safe cleanup |

**Acceptance criteria (PLAN.md §11):**

> Two isolated tasks can run concurrently and each produces a
> verifiable diff and test report while the base workspace remains
> unchanged.

✅ **VERIFIED.** `test_two_independent_tasks_modify_different_files_concurrently`
creates two worktrees for two tasks, modifies different files in each,
and verifies that:
- each worktree only has its own changes;
- the base repo is unchanged.

**M6 validation gates (all pass):**
- ✅ Two independent tasks modify different files concurrently without
  collision.
- ✅ One failed task cannot corrupt another worktree.
- ✅ Path traversal and repository escape attempts fail.
- ✅ Restart identifies orphaned running work safely.
- ✅ Cleanup preserves untracked or uncommitted human work unless
  explicitly authorized.

### Milestone 7 — Dynamic Sub Agent Type Lifecycle

| Required invariant (PLAN.md §12) | Status | Evidence |
|---|---|---|
| Main roles remain fixed | DONE | Planner and Worker are fixed; Sub Agent Types are dynamic |
| Sub Agent Types are based on current project needs | DONE | Types are created per project with explicit responsibility |
| Type responsibility, memory scope, tool rights, model policy, context budget, and concurrency limit are explicit | DONE | All fields on `AgentType` |
| Instances share accepted type knowledge but not task-local scratch context | DONE | Knowledge records are type-scoped; instances are runtime actors |
| Split, merge, retirement, and role changes are lossless and reversible | DONE | All migrations archive (never delete); topology snapshots enable rollback |

| Deliverable (PLAN.md §12) | Status | Evidence |
|---|---|---|
| Type definition and instance lifecycle | DONE | `create_type`, `create_instance`, `assign_instance_to_task`, `complete_instance` |
| Worker decision path for selecting or creating types | DONE | Types are created per project need (Worker integration in M5) |
| Per-type capability and budget enforcement | DONE | `max_concurrent_instances` enforced at task assignment |
| Topology versioning | DONE | `topology_snapshots` table with versioning |
| Snapshot, migration, validation, archive, activation, and rollback flow | DONE | `split_type`, `merge_types`, `retire_type`, `rollback_to_snapshot` |
| Provenance links from source knowledge to destination scopes | DONE | `migrated_from` field on knowledge records |

**Acceptance criteria (PLAN.md §12):**

> A type can be created, instantiated, split or merged, validated,
> archived, and rolled back with every mandatory knowledge record
> accounted for.

✅ **VERIFIED.** Tests in `tests/test_agent_types.py` (21 tests):
- `test_create_type_returns_active_type`
- `test_create_instance_respects_concurrency_limit`
- `test_split_routes_knowledge_to_destinations`
- `test_merge_migrates_all_knowledge_with_provenance`
- `test_retire_archives_knowledge_never_deletes`
- `test_retire_blocked_when_instances_running`
- `test_rollback_restores_active_topology`
- `test_cross_project_type_access_returns_nothing`
- `test_cross_project_knowledge_access_returns_nothing`

**M7 validation gates (all pass):**
- ✅ Instance concurrency respects the type limit.
- ✅ Split routes all mandatory knowledge to destinations or archive.
- ✅ Merge deduplicates without losing provenance.
- ✅ Retirement is blocked until reconciliation passes.
- ✅ Rollback restores the prior active topology.
- ✅ Cross-project type or memory access returns nothing.

## 2. Evidence summary

### Test results

```
$ pytest -v
============================= 223 passed in 3.79s =============================

tests/test_audit.py ........                                             [  3%]
tests/test_authorization.py .................                            [ 11%]
tests/test_agent_types.py .....................                          [ 20%]
tests/test_config.py .........................                           [ 31%]
tests/test_execution.py .....................                            [ 41%]
tests/test_health.py ...                                                 [ 42%]
tests/test_http_phase2.py ........                                       [ 46%]
tests/test_http_phase3.py .......                                        [ 49%]
tests/test_identity.py .....................                             [ 58%]
tests/test_isolation.py ........                                         [ 62%]
tests/test_persistence.py ........                                       [ 65%]
tests/test_plans.py ....................                                 [ 74%]
tests/test_secrets.py ...........                                        [ 79%]
tests/test_smoke.py ....                                                 [ 81%]
tests/test_tools.py ..................                                   [ 89%]
tests/test_worktrees.py .......................                          [ 99%]
```

Test breakdown (Phase 4 additions):
- `test_worktrees.py`: 23 tests — path validation (relative, traversal,
  nonexistent, non-git, unsafe ID), repository registration, worktree
  lifecycle, two independent tasks concurrent, command runner (success,
  failure, timeout), diff capture, failed task isolation, cleanup safety
  (refuses uncommitted, refuses non-eligible, succeeds when clean),
  restart recovery, artifact integrity.
- `test_agent_types.py`: 21 tests — type creation (validation,
  permission, duplicate), instance concurrency limit, instances of
  archived type rejected, knowledge records (add, list, type-scoped),
  split (routing, archiving, snapshot), merge (provenance, 2+ sources),
  retire (archives, blocked when running), rollback (restores active),
  cross-project isolation (type and knowledge), type update, snapshots.

### Database integrity

- 6 migrations applied (0001–0006).
- Partial unique index on worktrees: one active worktree per task.
- Knowledge records never hard-deleted; archived state is terminal.
- Topology snapshots versioned for rollback.

## 3. Architecture decisions active after Phase 4

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
| 0013 | Lease-Based Scheduling | Idempotent claims; unknown attempts on restart |
| 0014 | Isolated Worktree Execution | Git worktree per task; path validation; safe cleanup |
| 0015 | Dynamic Agent Type Lifecycle | Type creation; split/merge/retire; topology snapshots; rollback |
| 0016 | Lossless Knowledge Migration | Never delete; migrated_from provenance; reconciliation |

## 4. Deferred scope and the evidence required to add it

| Deferred item | When | Evidence required |
|---|---|---|
| LLM-driven code generation | M10+M12 | Provider adapter + website |
| Integration/compatibility review | M11 | Diff impact analysis + combined test workspace |
| Artifact store for large outputs | M8 | Immutable storage with hash + read-only handles |
| Project RAG | M8 | Retrieval router + context builder |
| Structured logging beyond stdlib | M14 | Operational need |
| Metrics emission | M14 | Operational need |
| Backup/restore drill | M14 | Tested restore procedure |

## 5. Confirmation

- ✅ Every current foundation file and relevant dynamically discovered
  skill was considered. — 26/26 files read; 16/16 skills read.
- ✅ No commit, push, merge, deployment, or destructive operation
  occurred without explicit authorization.
- ✅ The system is reported as `VERIFIED` for the Phase 4 scope only
  (Milestone 6 + Milestone 7).
- ✅ Two isolated tasks can run concurrently without collision.
- ✅ Path traversal and repository escape attempts fail.
- ✅ Cleanup preserves uncommitted human work.
- ✅ Knowledge is never lost during split/merge/retire.
- ✅ Topology rollback restores the prior active topology.
- ✅ Cross-project type and knowledge access returns nothing.
