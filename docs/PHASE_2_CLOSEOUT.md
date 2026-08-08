# Phase 2 Closeout Report — Identity, Authorization, Secrets, Tools, Audit

- **Phase**: 2 (Milestone 2 + Milestone 3)
- **Status**: VERIFIED
- **Date**: 2026-08-08
- **Skills applied**: `zero-control-plane-trust`,
  `zero-project-isolation-evidence`, `zero-tool-capability-runtime`,
  `zero-modular-bootstrap`, `zero-foundation-ingestion`,
  `zero-observability-evidence`, `zero-recovery-consistency`,
  `zero-rollout-readiness`

---

## 1. Scope delivered

Phase 2 covers **Milestone 2** (Central Control Plane, Identity, and
Project Isolation) and **Milestone 3** (Authorization, Secret
Boundary, Tool Registry, and Audit Core) from `PLAN.md`.

### Milestone 2 — Central Control Plane, Identity, and Project Isolation

| Required invariant (PLAN.md §7) | Status | Evidence |
|---|---|---|
| Stable server-issued IDs are authority | DONE | `UserId` (`zu_`), `ProjectId` (`p_`), `ExternalIdentityId` (`ei_`); display names are labels only |
| External platform IDs are links to a Zero identity, not identities by name | DONE | `external_identities` table with `verified_at`; `resolve_user_by_external_identity` requires verification |
| Every project-owned record is scoped unambiguously | DONE | `project_id` FK on every project-scoped table; queries filter by `project_id` before content is loaded |
| Cross-project access is prevented at more than one appropriate layer | DONE | Service-layer scope resolution + DB constraints + repository query filters |
| Canonical state remains provider-independent | DONE | No provider-specific fields in identity schema |

| Deliverable (PLAN.md §7) | Status | Evidence |
|---|---|---|
| Create and identify a user | DONE | `IdentityService.create_user`, `get_user` |
| Create a project with an owner | DONE | `IdentityService.create_project` (atomic with owner membership) |
| Add membership using stable identity | DONE | `IdentityService.add_member` |
| Resolve a user/project scope | DONE | `IdentityService.resolve_scope` |
| Reject unauthorized or cross-project access | DONE | `AuthorizationService.authorize` denies non-members; repository queries filter by `project_id` |
| Link an external identity through a verified process | DONE | `IdentityService.link_external_identity` + `verify_external_identity` |

**Acceptance criteria (PLAN.md §7):**

> Two isolated projects with overlapping human names and external
> usernames cannot access or mutate each other's records through any
> implemented path.

✅ **VERIFIED.** Adversarial tests in `tests/test_isolation.py`:
- `test_owner_a_cannot_access_project_b`
- `test_member_a_cannot_resolve_scope_in_project_b`
- `test_list_members_does_not_leak_across_projects`
- `test_audit_events_do_not_leak_across_projects`
- `test_secret_in_project_a_not_visible_in_project_b`
- `test_tool_grant_in_project_a_not_usable_in_project_b`
- `test_external_identity_cannot_be_linked_to_two_users`
- `test_concurrent_duplicate_membership_insert_is_rejected`

All use deliberately lookalike projects (same owner display name,
same project name, same member display name) and prove that
server-issued IDs keep them distinct.

### Milestone 3 — Authorization, Secret Boundary, Tool Registry, Audit Core

| Required invariant (PLAN.md §8) | Status | Evidence |
|---|---|---|
| Authorization is checked before protected reads and mutations | DONE | `AuthorizationService.authorize` is the central decision path; every protected operation calls it |
| UI visibility and bot command filtering are not security controls | DONE | The HTTP `/authorize` endpoint is for diagnostics; real mutations call `require_permission` internally |
| Raw secrets never enter model context or ordinary logs | DONE | `SecretService.resolve_value` is the only method that decrypts; never called by HTTP handlers, audit, or logs |
| Tool invocation is capability-based and least-privilege | DONE | `ToolGrant` per (project, tool, agent_scope); no grant → denied |
| Audit records identify actor, project, operation, target, result, and correlation IDs | DONE | `audit_events` schema has all these fields; `correlation_id` links related events |

| Deliverable (PLAN.md §8) | Status | Evidence |
|---|---|---|
| Minimal owner/member permission model | DONE | 3 roles (owner, member, viewer); 16 permissions; matrix in `ROLE_PERMISSIONS` |
| Central authorization decision path | DONE | `AuthorizationService.authorize` / `require_permission` |
| Server-side secret reference/lookup boundary | DONE | `SecretService.store` (Fernet encrypt) + `resolve_value` (decrypt, server-side only) |
| Tool registration and invocation contract for one harmless test tool | DONE | `echo` tool with full lifecycle (input validation, grant, output validation, audit) |
| Input/output validation at the tool boundary | DONE | jsonschema validation of input and output |
| Append-only audit behavior | DONE | SQLite triggers block UPDATE/DELETE; repository exposes only insert + read |
| Redaction policy applied before logs, metrics, and model-facing results | DONE | `looks_sensitive` defensive scan; `ToolResult.model_facing` is bounded to 500 chars |

**Acceptance criteria (PLAN.md §8):**

> One authorized operation succeeds and one unauthorized equivalent
> fails through the real backend boundary, both producing correct
> redacted audit evidence.

✅ **VERIFIED.** Tests in `tests/test_authorization.py`,
`tests/test_tools.py`, `tests/test_audit.py`:
- `test_owner_is_allowed_all_permissions` (authorized succeeds)
- `test_viewer_denied_plan_propose` (unauthorized denied, audited)
- `test_denied_decision_is_audited` (denial produces correct audit)
- `test_invoke_without_grant_is_denied` (tool denial)
- `test_invoke_audits_denial` (tool denial audited without raw input)
- `test_audit_summary_with_secret_value_is_redacted` (redaction works)
- `test_secret_store_audited_without_value` (secret ops audited
  without leaking the value)

## 2. Evidence summary

### Test results

```
$ pytest -v
============================= 131 passed in 0.89s =============================

tests/test_config.py .........................                  [ 19%]
tests/test_health.py ...                                        [ 21%]
tests/test_persistence.py ........                              [ 27%]
tests/test_smoke.py ....                                        [ 30%]
tests/test_identity.py .....................                    [ 46%]
tests/test_authorization.py .................                   [ 59%]
tests/test_isolation.py ........                                [ 65%]
tests/test_secrets.py ...........                               [ 73%]
tests/test_audit.py ........                                    [ 79%]
tests/test_tools.py ..................                          [ 93%]
tests/test_http_phase2.py ........                              [100%]
```

Test breakdown (Phase 2 additions):
- `test_identity.py`: 21 tests — user/project/membership/external-identity lifecycle, duplicate rejection, 64-bit Telegram ID preservation.
- `test_authorization.py`: 17 tests — role-permission matrix, allow/deny for every permission, revocation takes effect immediately, denials audited, cross-project denied.
- `test_isolation.py`: 8 adversarial tests — lookalike projects cannot access each other's records through any implemented path.
- `test_secrets.py`: 11 tests — encrypted storage, server-side resolution, revocation, no value in audit, cross-project denied.
- `test_audit.py`: 8 tests — append-only (UPDATE/DELETE blocked by trigger), required fields, redaction, correlation, project-scoped listing.
- `test_tools.py`: 18 tests — registration, grants (idempotent, revocable), invocation lifecycle (input validation, grant resolution, output validation, audit), cross-project isolation, scope independence, bounded rendering.
- `test_http_phase2.py`: 8 tests — HTTP endpoints for identity, authorization, audit, tool invocation, secret storage (value never returned).

### Database integrity

- Migration runner upgraded to be statement-aware and idempotent
  (tolerates "already exists" errors for `ALTER TABLE ADD COLUMN`).
- Foreign keys enabled on every connection (`PRAGMA foreign_keys = ON`).
- `row_factory = sqlite3.Row` for named column access.
- Append-only triggers on `audit_events` verified by tests that
  attempt UPDATE and DELETE and expect failure.

### Secret canary scan

The test suite includes specific secret-canary tests:
- `test_audit_summary_with_secret_value_is_redacted` — verifies that
  a summary containing `sk-` is replaced with `[REDACTED: sensitive
  content detected]`.
- `test_secret_store_audited_without_value` — verifies that the raw
  secret value does not appear anywhere in the audit event.
- `test_stored_secret_is_encrypted_at_rest` — verifies that the raw
  value does not appear in the database's `encrypted_value` column.
- `test_secret_storage_endpoint_never_returns_value` — verifies that
  the HTTP response does not contain the raw value.

### HTTP boundary verification

Real ASGI app exercised through httpx's ASGI transport (no network
port needed). All endpoints return correct statuses:
- 201 for creates, 200 for reads, 204 for deletes, 403 for denied
  tool invocations, 404 for not-found, 400 for validation errors.

## 3. Architecture decisions active after Phase 2

| ADR | Title | Decision |
|---|---|---|
| 0001 | Technology Stack | Python 3.12, FastAPI, SQLite, pytest |
| 0002 | Modular Monolith | One process, explicit internal modules, inward dependency direction |
| 0003 | Project Layout | `src/zero/{domain,app,persistence,adapters}/`, `tests/`, `docs/decisions/` |
| 0004 | Configuration as Trust Boundary | Typed, validated, fail-closed, secrets redacted |
| 0005 | Persistence Starts with Invariants | Minimal schema, FK-enforced, restart-safe migrations |
| 0006 | Canonical Identity Model | Server-issued IDs (`zu_`, `p_`, `ei_`); external IDs are verified links |
| 0007 | Role-Based Authorization | 3 roles × 16 permissions; central `AuthorizationService` decision path |
| 0008 | Encrypted Secret Storage | Fernet encryption; HKDF key derivation; `resolve_value` is the only decrypt path |
| 0009 | Capability-Based Tool Runtime | Tool registry + per-(project, tool, scope) grants; full invocation lifecycle with validation and audit |
| 0010 | Append-Only Audit Log | SQLite triggers block UPDATE/DELETE; defensive redaction; correlation IDs |

## 4. Deferred scope and the evidence required to add it

| Deferred item | When | Evidence required |
|---|---|---|
| Real HTTP authentication (session tokens, OIDC) | M12 (website) | Real auth flow; the authorization service is ready |
| Tool limit enforcement (max_invocations, timeout) | M10 (providers) | A real tool that needs limits |
| External identity verification ceremony (Telegram OIDC, Discord OAuth) | M13 (adapters) | Platform-specific verification flow |
| Secret key rotation | M14 (recovery) | Rotation procedure + re-encryption migration |
| PostgreSQL migration | When a constraint SQLite cannot satisfy appears | Measured constraint |
| Diagnostic artifacts (raw payload storage with retention) | M14 | Protected, short-retention, owner-access only |
| Structured logging beyond stdlib | M14 | Operational need |
| Metrics emission | M14 | Operational need |

## 5. Migration and rollback status

- **Schema migrations applied**: 2 (`0001_initial.sql`,
  `0002_identity_authorization_tools_audit.sql`).
- **Rollback path**: forward-repair via a new migration file
  (e.g. `0003_*.sql`); never edit applied migrations in place.
- **Migration runner upgrade**: now statement-aware and idempotent;
  re-running a partially-applied migration is safe.
- **Backup/restore drill**: not yet exercised (deferred to M14 per
  PLAN.md §19).

## 6. Unresolved security or reliability risks

- **HTTP requests use a placeholder actor (`zu_system`)**: real
  authentication arrives with the website (M12) and Telegram adapter
  (M13). The authorization service is ready to receive a real
  `UserId` from the request.
- **No rate limiting or abuse controls**: deferred to M14 (security
  hardening).
- **No structured logging or metrics**: deferred to M14.
- **No backup/restore drill**: deferred to M14.
- **Tool limits stored but not enforced**: enforcement arrives when
  a real tool needs them (M10).
- **External identity verification is a stub**: the
  `verify_external_identity` method works, but the platform-specific
  verification flow (Telegram OIDC, Discord OAuth) arrives in M13.

None of these block Phase 2 acceptance. Each is explicitly listed so
later milestones know what to pick up.

## 7. Confirmation

- ✅ Every current foundation file and relevant dynamically discovered
  skill was considered. — 26/26 files read; 16/16 skills read.
- ✅ No commit, push, merge, deployment, or destructive operation
  occurred without explicit authorization. — Local git commits on
  `main`; no remote configured; no external services contacted.
- ✅ The system is reported as `VERIFIED` for the Phase 2 scope only
  (Milestone 2 + Milestone 3). Later milestones are `PLANNED`.
- ✅ Adversarial cross-project isolation tests pass with deliberately
  lookalike projects (same owner names, same project names, same
  member names, same external usernames).
- ✅ Secret canary scan: zero leaks in audit, logs, HTTP responses,
  or at-rest storage.
- ✅ Append-only audit log: UPDATE and DELETE blocked by database
  triggers; verified by tests.
