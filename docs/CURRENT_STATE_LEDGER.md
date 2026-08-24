# Zero Develop — Audited Current-State Ledger

This ledger reports behavior exercised in the current effective source tree. `VERIFIED` means
verified by deterministic local tests or a named local probe; it does not mean production
readiness or live external integration.

| # | Milestone | State | Evidence boundary |
|---|---|---|---|
| 0 | Foundation ingestion and build readiness | VERIFIED | Foundation inventory and repository audit |
| 1 | Repository bootstrap and executable skeleton | VERIFIED | Configuration, migrations, health, source, and installed-artifact gates |
| 2 | Identity and project isolation | VERIFIED | Backend and authenticated HTTP isolation tests |
| 3 | Authorization, secrets, tools, audit | PARTIAL | Auth, capability, and redaction tests; arbitrary in-process handler timeouts unsupported |
| 4 | Plan lifecycle and Main Planner | PARTIAL | Lifecycle and rollback tests; concurrent transition hardening remains |
| 5 | Main Worker and durable execution graph | PARTIAL | State, lease, retry, and recovery tests; claim/completion races remain |
| 6 | Isolated branch/worktree execution | VERIFIED | Local Git/worktree and execution-boundary tests |
| 7 | Dynamic Sub Agent Type lifecycle | PARTIAL | Deterministic lifecycle passes; concurrency and complete knowledge rollback remain |
| 8 | Artifact store, memory, project RAG | VERIFIED | Deterministic local isolation and rebuild tests |
| 9 | Retrieval, context, budgeting, compaction | VERIFIED | Deterministic context and recovery tests |
| 10 | Provider adapters and usage reconciliation | PARTIAL | Fake adapter, replay, cancellation, and accounting tests; no billing truth |
| 11 | Integration review and controlled merge | VERIFIED | Local deterministic integration, provenance, and lineage tests |
| 12 | Primary website vertical slices | PARTIAL | ASGI tests; no browser/mobile/accessibility audit |
| 13 | Telegram/Discord secondary adapters | PARTIAL | Canonical event and deterministic adapter tests; no live platform run |
| 14 | Observability, recovery, security hardening | PARTIAL | Recovery, redaction, migration, and database-lineage tests; backup requires configured encryption authority |
| 15 | End-to-end verification and controlled rollout | PARTIAL | Post-audit remediation suite (563 tests) and release gates; no production rollout rehearsal |

## Current verification evidence

- **563 tests passed, 16 platform-skipped** under `ZERO_ENV=test`. Reference-grounded additions (Anthropic adapter, LLM compaction summarizer, tool-round nudge) plus coherent retry lifecycle: retry-aware execution pausing, blocked-dependency revival, expired-lease terminal recording, worktree cleanup wired into recovery.
- Python compilation, full scoped Ruff, and Ruff formatting checks pass for `src`, `tests`, and
  `scripts`.
- The effective schema contains **30** migration files (including `0027_remaining_project_lineage` and `0028_secret_key_versioning`). Migration IDs use complete filename stems;
  numeric prefixes alone are not unique because three migrations begin with `0012`.
- Direct SQL lineage tests cover both INSERT and UPDATE mismatch attempts for the identified
  denormalized project-scoped tables.
- The final staged wheel/sdist artifact gate passed: both artifacts contain exactly 30 migrations and
  all required runtime modules; the sdist also contains the key scripts and lineage regressions.
- The final installed wheel passed import, fresh migration/rerun/integrity, fail-closed configuration,
  and loopback root/health/readiness probes.
- Fresh, rerun, atomicity, concurrency, and populated-upgrade probes passed for the 30-migration set.

## Deployment boundary

Deployment remains a separate owner-authorized action and is blocked by the `PARTIAL` items above:
live external adapters, browser/accessibility coverage, concurrency hardening, external persistence,
and disaster-recovery rehearsal. Backup operations additionally require separately protected
`ZERO_SECRET_KEY` authority.
