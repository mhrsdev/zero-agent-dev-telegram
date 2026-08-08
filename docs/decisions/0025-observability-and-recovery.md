# ADR 0025 — Observability with Redacted Logs, Low-Cardinality Metrics, and Secret Canary Scan

- Status: ACCEPTED
- Date: 2026-08-08
- Milestone: 14 (Observability, Operational Recovery, Security Hardening)
- Skills applied: `zero-observability-evidence`, `zero-recovery-consistency`

## Context

`PLAN.md` §19 (Milestone 14) requires:
- Metrics use low-cardinality dimensions.
- Raw prompts, source files, tool parameters/results, credentials, and
  private messages are excluded by default.
- Every execution can be traced across plan, tasks, agents, tools,
  provider requests, integration, and result.
- Recovery uses durable state, not model recollection.
- Backups and restores preserve project isolation and encryption.

## Decision

Adopt an observability layer with:

1. **Structured redacted logs**: `RedactedLogFormatter` scans log
   output for secret-like patterns (API keys, Bearer tokens, passwords)
   and replaces them with `[REDACTED]`. The primary control is careful
   construction at the call site; the formatter is a safety net.

2. **Low-cardinality metrics**: `MetricsService` accepts only
   pre-defined result values (`success`, `denied`, `failure`, `error`,
   `cancelled`) and source values (`web`, `telegram`, `discord`,
   `system`, `internal`). Project IDs, user names, file paths, prompt
   text, and tool arguments are NEVER used as labels.

3. **Secret canary scan**: `SecretCanaryScan` scans all system surfaces
   (audit events, artifacts, conversation events, knowledge records)
   for secret-like patterns. Returns findings per surface. An empty
   list means no secrets found.

4. **Backup and restore**: `BackupService` uses SQLite's `iterdump` to
   produce a portable SQL dump. Restore verifies schema integrity,
   table counts, and `PRAGMA integrity_check`.

5. **Recovery procedures**: `RecoveryService` handles:
   - Stuck executions (running state → recover_after_restart → paused).
   - Orphan worktrees (active state → interrupted).
   - Partial compactions (non-terminal compaction state → failed).

6. **Correlation IDs**: every audit event carries a `correlation_id`
   linking related events across the control plane (plan → execution →
   task → tool → provider request → integration → merge).

## Rejected alternatives

- **Raw payloads in logs/metrics**: explicitly rejected by PLAN.md M14
  and by `zero-observability-evidence` §"Raw payloads are exceptional
  protected artifacts, not default log fields."
- **High-cardinality labels**: explicitly rejected by
  `zero-observability-evidence` §"Wrong labels: prompt text, error
  message, user name, file path, tool arguments."
- **Model recollection for recovery**: explicitly rejected by
  `zero-recovery-consistency` §"Recovery uses durable state, not model
  recollection."

## Consequences

- An operator can diagnose and recover representative failures from
  server-side evidence without opening raw model prompts or exposing
  secrets.
- Restored state passes isolation and integrity checks.
- Secret canary scan catches leaks across all surfaces.
- Recovery procedures handle stuck executions, orphan worktrees, and
  partial compactions.
