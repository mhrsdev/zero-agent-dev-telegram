# Zero Develop — Audited Current-State Ledger

This ledger reports only behavior exercised in the Phase 9 source tree. `VERIFIED` means
verified in deterministic local tests; it does not mean production deployment readiness.

| # | Milestone | State | Evidence boundary |
|---|---|---|---|
| 0 | Foundation ingestion and build readiness | VERIFIED | Foundation inventory and hashes |
| 1 | Repository bootstrap and executable skeleton | VERIFIED | Config, migration, health, source and installed-wheel smoke |
| 2 | Identity and project isolation | VERIFIED | Backend and authenticated HTTP isolation tests |
| 3 | Authorization, secrets, tools, audit | PARTIAL | Auth/caps/redaction tested; arbitrary handler timeouts unsupported |
| 4 | Plan lifecycle and Main Planner | PARTIAL | Lifecycle/rollback pass; concurrent transition hardening remains |
| 5 | Main Worker and durable execution graph | PARTIAL | State/lease/retry pass; claim/completion races remain |
| 6 | Isolated branch/worktree execution | VERIFIED | Local Git/worktree tests only |
| 7 | Dynamic Sub Agent Type lifecycle | PARTIAL | Deterministic lifecycle passes; concurrency and complete knowledge rollback remain |
| 8 | Artifact store, memory, project RAG | VERIFIED | Deterministic local isolation/rebuild tests |
| 9 | Retrieval, context, budgeting, compaction | VERIFIED | Deterministic context and recovery tests |
| 10 | Provider adapters and usage reconciliation | PARTIAL | Fake adapter/replay/accounting only; no billing truth |
| 11 | Integration review and controlled merge | VERIFIED | Local deterministic integration tests |
| 12 | Primary website vertical slices | PARTIAL | ASGI tests; no browser/mobile/accessibility audit |
| 13 | Telegram/Discord secondary adapters | PARTIAL | Canonical event model only; no live platform run |
| 14 | Observability, recovery, security hardening | PARTIAL | Recovery/redaction tests; product backup dump is unencrypted |
| 15 | End-to-end verification and controlled rollout | PARTIAL | Isolated E2E passes; no production rollout rehearsal |

## Current verification evidence

- The complete source suite passes under `ZERO_ENV=test`.
- Python 3.12 clean-wheel install and isolated runtime smoke pass.
- Wheel inventory: 11 migrations, 9 HTML templates, and static CSS present.
- Installed-wheel smoke: import provenance, migrations, health, identity, project, and
  scoped audit flows pass from outside the source tree.
- Fresh/re-run/upgrade migration smoke and critical Ruff runtime rules pass.
- Full Ruff remains at 28 non-critical broad-exception/test-style findings.
- Deployment remains a separate owner-authorized action and is currently blocked by the
  `PARTIAL` items above.
