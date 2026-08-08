# Zero Develop — Requirement Ledger

## Purpose

This ledger separates confirmed requirements, non-negotiable invariants,
implementation freedom, current artifacts, absent implementation, and known
blockers. It is the Milestone 0 deliverable required by `PLAN.md` Section 5
and follows the discipline taught by the `zero-foundation-ingestion` skill:
planned capability is never reported as existing capability.

Source evidence: every entry below is derived from a foundation file under
`project-foundation/`. Foundation hashes were verified against
`MANIFEST.sha256` on `2026-08-08`; all 26 files matched.

---

## 1. Confirmed requirements (product behavior)

These describe what Zero Develop must do, regardless of how it is built.

1. Multi-agent platform for concurrent software development by human teams.
   One project, many team members, many AI agents working different parts
   simultaneously without colliding. (`SUMMARY.md` §Product Definition)
2. Three primary surfaces: backend control plane (authoritative), website
   (primary management interface), messaging adapters (Telegram/Discord/etc.,
   thin clients). (`SUMMARY.md` §Main Product Surfaces)
3. Two fixed Main Agent roles: `Main Planner` (interpret → propose plan) and
   `Main Worker / Orchestrator` (approved plan → durable task graph →
   coordinated execution). (`SUMMARY.md` §Agent Architecture)
4. Dynamic Sub Agent Types created per project based on real project shape,
   not a fixed catalog. Each type has explicit responsibility, memory scope,
   tool rights, model/provider policy, context budget, and concurrency limit.
   (`SUMMARY.md` §Dynamic Sub Agents)
5. Sub Agent Type reorganization (split/merge/retire) must be lossless,
   validated, archived, and reversible. (`SUMMARY.md` §Knowledge Preservation
   During Reorganization)
6. Built-in Integration/Compatibility Sub Agent type that inspects changed
   files + impacted contracts instead of rereading the whole project.
   (`SUMMARY.md` §Integration / Compatibility Sub Agent)
7. Concurrent coding tasks execute on isolated branches + worktrees; never
   share a working directory. Every output has a diff, tests, and status.
   (`SUMMARY.md` §Concurrent and Isolated Execution)
8. Project memory is fully isolated per project. Initial deep analysis once;
   afterwards agents receive diffs, related files, affected contracts, RAG
   snippets — not the whole repo. (`SUMMARY.md` §Memory and Token
   Optimization)
9. Project RAG: central retrieval layer available to Main + Sub agents for
   project-wide knowledge. Retrieval must be relevance-ranked, scoped,
   budgeted, and provenance-tracked. (`SUMMARY.md` §Project RAG and
   Suggested Retrieval)
10. Model- and provider-agnostic. Owner may assign different model/provider
    to Main Planner, Main Worker, any Sub Agent Type. Switching must not
    destroy identity or memory. (`SUMMARY.md` §Multiple Model and Provider
    Support)
11. Website is the primary management surface; messaging platforms are thin
    clients of the same control plane. (`SUMMARY.md` §Website and Management
    Panel; §Telegram and Messaging-Platform Behavior)
12. User identity flows: website registration → stable `Zero User ID` →
    external platform IDs linked to that account. Display names/usernames
    never authority. (`SUMMARY.md` §User Identity)
13. Backend-enforced permission model. Owner decides per-user capabilities.
    UI hiding and bot command filtering are not security controls.
    (`SUMMARY.md` §User Permissions)
14. Tool registry + capability-based tool access. Secrets never reach model
    context. Inputs/outputs validated. Calls audited. (`SUMMARY.md` §Tool
    Registry and APIs)
15. Audit log records actor, project, time, source platform, operation,
    target, related plan/task/execution ID, success/failure, before/after
    when needed. Never contains raw secrets. (`SUMMARY.md` §Audit Log)
16. End-to-end change flow: discuss → planner proposes plan → user
    Approve/Reject/Edit → approved plan reaches Worker → tasks split into
    graph → concurrent isolated execution → integration review → controlled
    merge → state + memory + RAG updated from valid changes only → audit
    recorded. (`SUMMARY.md` §End-to-End Change Flow)

## 2. Non-negotiable invariants (cannot weaken)

From `PLAN.md` §2 and reinforced by every skill:

1. Backend/control plane is the authoritative source of project, identity,
   permission, plan, execution, memory, tool, and audit state.
2. Website account + stable `Zero User ID` is primary identity. Display
   names and usernames are never authority.
3. Projects are isolated across data, retrieval, memory, execution, tools,
   and logs.
4. Backend authorization is required for every protected read and mutation.
5. `Main Planner` and `Main Worker` are fixed roles.
6. Sub Agent Types are project-specific and dynamic.
7. Sub Agent Type evolution (split/merge/retire) is lossless, validated,
   archived, reversible.
8. No execution starts without an approved plan from an authorized human.
9. Concurrent coding tasks never write to one working directory.
10. Project RAG and persistent memory live outside provider context.
11. Provider context and prompt caching are optimizations, never durable
    truth.
12. Context is relevance-ranked, dependency-aware, incremental, and
    token-budgeted.
13. Models never receive raw tool or provider secrets.
14. Tool access is least-privilege and owner-controlled per project and
    agent role/type.
15. Important human and system actions are auditable without leaking
    sensitive content.
16. Every integrated code change has a diff, test evidence, and integration
    decision.
17. Website is the primary management surface. Messaging platforms are
    clients of the same control plane.
18. Implementation remains provider-agnostic at canonical domain boundaries.
19. No speculative capability, schema, or abstraction is created without a
    present requirement.

## 3. Implementation freedom (deliberately open)

From `PLAN.md` §22 and §5; `zero-modular-bootstrap` skill:

- Language and framework where not already decided.
- Internal module and file organization.
- Database access style.
- Test organization.
- Scheduling algorithm satisfying the dependency contract.
- Retrieval/ranking implementation meeting isolation + holdout gates.
- UI design and component strategy.
- Provider SDKs and platform libraries.
- Whether a boundary is a function, module, service object, process, or
  database constraint.
- Numbering/labels for plan states (e.g. `DRAFT → PROPOSED → APPROVED` is
  illustrative, not mandatory).

## 4. Current artifacts (what exists today)

| Artifact | Location | Status |
|---|---|---|
| `SUMMARY.md` product definition | `project-foundation/SUMMARY.md` | Documented, hash verified |
| `PLAN.md` 15-milestone plan | `project-foundation/PLAN.md` | Documented, hash verified |
| `MANIFEST.sha256` file inventory | `project-foundation/MANIFEST.sha256` | Documented, hash verified |
| 16 skill `SKILL.md` files | `project-foundation/skills/*/SKILL.md` | Documented, all hashes verified |
| `zero-context-memory` reference Python module | `…/zero-context-memory/scripts/context_management.py` | Standalone-tested (7 tests pass) |
| `zero-context-memory` reference test suite | `…/zero-context-memory/scripts/test_context_management.py` | Passes |
| `zero-context-memory` source findings | `…/zero-context-memory/references/SOURCE_FINDINGS.md` | Documented |
| `zero-claude-token-economics` reference Python module | `…/zero-claude-token-economics/scripts/token_accounting.py` | Standalone-tested (6 tests pass) |
| `zero-claude-token-economics` reference test suite | `…/zero-claude-token-economics/scripts/test_token_accounting.py` | Passes |
| `zero-claude-token-economics` source findings | `…/zero-claude-token-economics/references/SOURCE_FINDINGS.md` | Documented |
| `zero-interface-adapter-model` Telegram findings | `…/zero-interface-adapter-model/references/TELEGRAM_FINDINGS.md` | Documented |
| Reference repos cloned for code reference | `/home/z/my-project/reference-repos/{hermes-agent,grok-build,claude-code}` | Cloned (depth=1) |

Reference repos provide code-level evidence for the patterns the foundation
already extracted into `SOURCE_FINDINGS.md`. They are not copied wholesale.

## 5. Absent implementation (what does not exist yet)

The following capabilities are **documented but not implemented**. They will
be built in subsequent milestones.

- Any executable Zero Develop application code.
- Health/readiness endpoint.
- Configuration validation.
- Database schema or migrations.
- Identity model (Zero User, External Identity, Project, Membership).
- Authorization decision path.
- Secret reference/lookup boundary.
- Tool registry + capability grants.
- Audit log store.
- Plan lifecycle (DRAFT → PROPOSED → APPROVED) and Main Planner.
- Execution graph, task lifecycle, Main Worker.
- Branch/worktree runner.
- Sub Agent Type lifecycle (split/merge/retire).
- Artifact store.
- Project RAG.
- Retrieval router, context builder, token accountant, compaction service.
- Provider adapters.
- Integration/merge gates.
- Website.
- Telegram/Discord adapters.
- Observability (logs, metrics, traces).
- Recovery, backup, restore procedures.

## 6. Known blockers and evidence limitations

- **Network access for live Telegram/Discord/Provider APIs**: deferred to
  later milestones. Phase 1 uses deterministic test adapters only.
- **Provider billing truth**: client-side estimates are explicitly not
  billing truth (`zero-claude-token-economics/SOURCE_FINDINGS.md` §3).
  Reconciliation path is deferred until a real provider adapter exists.
- **Grok Build Rust test execution**: foundation notes `cargo` was
  unavailable during initial research; we will not depend on Rust tests.
- **Claude Code runtime source**: not published; we treat only documented
  contracts as evidence, never inferred binary behavior.
- **No conflicts between foundation documents**: the ingestion pass found
  no non-reversible product or security conflicts. All wording differences
  are non-blocking (e.g. "modules" vs "service objects" describe the same
  in-process boundary).

## 7. Skills applied to Phase 1

Phase 1 (Milestone 0 + Milestone 1) is shaped by these skills:

- `zero-foundation-ingestion` — read-before-build discipline, requirement
  normalization, current-state vs roadmap-state separation.
- `zero-modular-bootstrap` — smallest stack supported by evidence, one
  deployable control plane, one executable path, configuration as trust
  boundary, persistence starts with invariants.
- `zero-control-plane-trust` — conceptual model for identity, project
  scope, authorization, capabilities, secrets, audit (used to design the
  schema direction even though full implementation arrives in Milestone 2).
- `zero-rollout-readiness` — PASS/PARTIAL/BLOCKED status discipline and
  claim-to-evidence mapping used throughout this ledger.
