# ADR 0010 — Append-Only Audit Log

- Status: ACCEPTED
- Date: 2026-08-08
- Milestone: 3 (Authorization, Secret Boundary, Tool Registry, Audit Core)
- Skills applied: `zero-control-plane-trust`, `zero-observability-evidence`,
  `zero-recovery-consistency`

## Context

`PLAN.md` §8 (Milestone 3) requires:
- Append-only audit behavior for implemented sensitive operations.
- Audit records identify actor, project, operation, target, result,
  and correlation IDs.
- Redaction policy applied before logs, metrics, and model-facing
  results.

`zero-control-plane-trust` §"Audit is evidence, not a transcript
dump": "An audit event explains who caused what transition, in which
project, through which interface, against which revision, and with
what result. It normally does not need the raw conversation, source
file, prompt, tool output, or secret."

`zero-observability-evidence` §"Audit explains authority": "Audit
records answer who caused a protected transition and under which
policy. Operational logs answer what the process experienced. Audit
should not disappear with log rotation, and logging every function
call does not create an audit trail."

`zero-recovery-consistency` §"Idempotency makes retries ordinary":
"the audit trail is durable authority evidence; it must not be
silently mutated."

## Decision

Adopt an append-only audit log with:

1. **Schema**: `audit_events` table with stable ID, project_id
   (nullable for system events), actor_id (nullable for system
   events), source, operation, target_type, target_id, result,
   correlation_id, redacted_summary, created_at.
2. **Append-only enforcement**: SQLite triggers block UPDATE and
   DELETE on `audit_events`. The application exposes only `insert`
   and read methods on the repository; there is no update or delete
   path.
3. **Redaction policy**: the repository defensively scans
   `redacted_summary` for sensitive patterns (`sk-`, `Bearer `,
   `password=`, `secret=`, `token=`, `api_key=`) and replaces
   suspicious summaries with `[REDACTED: sensitive content
   detected]`. The primary control is careful construction at the
   call site; the scan is a safety net.
4. **Correlation ID**: every event carries an optional
   `correlation_id` that links related events (e.g. an execution ID
   linking plan approval, task creation, and tool invocation events).
5. **Project-scoped reads**: `list_for_project` filters by
   `project_id` before any row is loaded. Events from other projects
   are never returned even if the caller guesses an ID.

### What goes into the audit log

- Identity transitions: `user.create`, `project.create`,
  `member.add`, `member.remove`, `external_identity.link`,
  `external_identity.verify`.
- Authorization decisions: `authz.<permission>` with
  `result=denied` for denied decisions.
- Secret operations: `secret.store`, `secret.revoke` (never the
  value, never the name in the summary).
- Tool invocations: `tool.invoke` with the tool name, status,
  duration, and optional error class (never the raw input or
  output).
- Future: plan approvals, execution transitions, integration
  decisions, topology changes (added in their respective
  milestones).

### What does NOT go into the audit log

- Raw secrets, API keys, tokens, passwords.
- Raw conversation content, source files, prompts.
- Raw tool input or output.
- Display names (stable IDs only).
- External platform usernames (stable external IDs only, and only
  in the target_id field, not in the summary).

### Failure shapes

Per `zero-control-plane-trust` §"Failure shapes teach the boundary",
the audit log records typed results:
- `success`: the operation completed as intended.
- `denied`: the operation was refused by the authorization layer.
- `failure`: the operation failed due to invalid input or
  precondition.
- `error`: the operation failed due to an internal error.

## Rejected alternatives

- **Use application logs as audit**: rejected by
  `zero-observability-evidence` §"Audit explains authority".
  Application logs answer "what did the process experience?";
  audit answers "who caused a protected transition?". They serve
  different purposes and have different retention requirements.
- **Allow UPDATE for corrections**: rejected. Audit must be durable
  authority evidence. Corrections are made by appending a new event
  that supersedes the earlier one (with a `superseded_by` field in
  a future milestone if needed), never by mutating the original.
- **Store raw payloads for debugging**: rejected by
  `zero-control-plane-trust` §"Audit is evidence, not a transcript
  dump". Raw payloads increase breach impact without improving
  state reconstruction. Diagnostic artifacts (with separate
  retention and access controls) arrive in M14.
- **Skip the defensive redaction scan**: rejected. The scan is a
  safety net that catches accidental leaks at the call site. The
  primary control is careful construction of summaries; the scan
  does not replace it.

## Consequences

- The audit log is durable authority evidence. It survives log
  rotation and process restart.
- Tampering with the audit log requires dropping the triggers or
  modifying the database file directly — both are detectable
  through integrity checks (a future M14 task).
- The audit log is project-scoped: no event from project A appears
  in project B's audit listing.
- Adding a new audited operation is a single call to
  `AuditService.record` with carefully constructed summary.
- The defensive redaction scan may produce false positives (e.g. a
  legitimate summary that happens to contain `sk-`). The trade-off
  is acceptable: false positives produce a `[REDACTED]` summary
  that is still useful for correlation, while false negatives would
  leak secrets.
