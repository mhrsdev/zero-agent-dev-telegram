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
| Executable ASGI control plane | `src/zero/main.py`, `src/zero/app/` | Implemented and covered by the local suite |
| SQLite schema and migration runner | `src/zero/persistence/` | Implemented; 30 migrations, full-stem IDs, transactional application |
| Project-lineage hardening | `src/zero/persistence/migrations/0025_project_lineage_hardening.sql`, `0026_project_ownership_and_legacy_provider_recovery.sql` | Implemented with direct SQL INSERT/UPDATE regressions and immutable ownership triggers |
| Provider/runtime cancellation contract | `src/zero/app/agent_runtime.py`, provider tests | Implemented and regression-tested |
| Installable package metadata | `pyproject.toml` | Wheel/sdist package data declares templates, static files, and migrations |
| Clean release artifact gate | `scripts/validate_release_artifacts.py` | Implemented; checks wheel/sdist contents independently |
| CI quality and release workflow | `.github/workflows/ci.yml` | Implemented; runs tests, static gates, artifact, startup, and health checks |
| Development launcher | `scripts/run_dev.sh` | Implemented and executable |

Reference repositories and foundation documents remain evidence sources for design patterns, not
runtime dependencies or copied implementations.

## 5. Absent implementation and rollout gaps

The original Milestone 0 list described the planned system before implementation began. It is
retained here as a requirement history, not as a claim about the current tree. The remaining gaps
are operational or deliberately deferred:

- Product-level encrypted backup and rehearsed restore.
- Live provider, Telegram, and Discord qualification with securely provisioned credentials.
- Browser, mobile, and accessibility validation.
- Concurrency and linearizability hardening around plan approval, task claiming/completion,
  provider idempotency, agent limits, and topology rollback.
- Production deployment, TLS, supervision, external persistence, and disaster-recovery rehearsal.

## 6. Known blockers and evidence limitations

- **External integrations**: deterministic local adapters do not prove live provider, Telegram, or
  Discord behavior.
- **Provider billing truth**: local usage accounting is not provider billing truth.
- **Execution isolation**: host-bounded worktree execution is not a hostile-code sandbox.
- **Backup security**: backup/restore operations require a stable configured encryption key and
  fail closed when it is absent; the deployment must protect that key separately from the archive.
- **Release evidence**: clean wheel/sdist contents, installed startup, and 30-migration upgrade
  probes are separate gates from the source-tree test suite.

## 7. Verification discipline applied to remediation

The remediation follows the evidence boundaries from the foundation and the runtime references:

- test-first regressions for callback compatibility and database lineage;
- database-level enforcement rather than service-only checks;
- full filename-stem migration identity rather than numeric-prefix identity;
- isolated temporary databases and installed artifacts for release probes;
- no secrets in source, tests, logs, reports, or release artifacts.
