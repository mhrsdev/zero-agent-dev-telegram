# ADR 0007 — Role-Based Authorization with a Central Decision Path

- Status: ACCEPTED
- Date: 2026-08-08
- Milestone: 3 (Authorization, Secret Boundary, Tool Registry, Audit Core)
- Skills applied: `zero-control-plane-trust`, `zero-tool-capability-runtime`

## Context

`PLAN.md` §8 (Milestone 3) requires:
- Authorization is checked before protected reads and mutations.
- UI visibility and bot command filtering are not security controls.
- Minimal owner/member permission model covering the next vertical
  slice.
- Central authorization decision path.

`zero-control-plane-trust` §"Authorization is a domain decision":
"A centralized decision path does not require one giant authorization
class. It means every protected route converges on the same domain
policy instead of duplicating partial checks in controllers, bots,
and UI components."

`zero-control-plane-trust` §"UI controls are not security": "Hiding a
button improves usability. It does not secure an endpoint."

## Decision

Adopt a role-based authorization model with three roles and a fixed
permission matrix. All authorization decisions go through one
:class:`AuthorizationService` method: :meth:`authorize` (or its
raising variant :meth:`require_permission`).

### Roles

- **owner**: full project authority. Can do everything, including
  managing members, tools, secrets, and audit.
- **member**: operational authority. Can propose/edit/approve plans,
  start/stop executions, view diffs, authorize merges, manage
  agents, change models. Cannot manage members, tools, secrets, or
  view audit.
- **viewer**: read-only. Can view the project and view diffs but
  cannot mutate anything.

The role is assigned per membership: a user's role in project A is
independent of their role in project B.

### Permission matrix

The matrix is defined in `zero.domain.authorization.ROLE_PERMISSIONS`
as a `dict[ProjectRole, frozenset[Permission]]`. The matrix is the
authoritative source; any change is a security-relevant decision and
must be reviewed.

Permissions cover the next vertical slice (M4–M11):
- `project.view` — read project state.
- `plan.propose`, `plan.edit`, `plan.approve`, `plan.reject` — plan
  lifecycle (M4).
- `execution.start`, `execution.stop`, `execution.view_diffs` —
  execution lifecycle (M5, M6).
- `integration.authorize_merge` — merge gates (M11).
- `agent.manage` — Sub Agent Type lifecycle (M7).
- `model.change` — provider/model policy (M10).
- `tool.manage`, `secret.manage`, `member.manage` — admin operations.
- `cost.view`, `audit.view` — visibility.

### Central decision path

`AuthorizationService.authorize(actor_id, project_id, permission)`
returns an :class:`AuthorizationDecision` with `allowed=True/False`
and a typed reason. Denied decisions are recorded as audit events so
the denial is observable.

Every protected HTTP endpoint, future Telegram adapter, and internal
service calls this method (or `require_permission`) before performing
any operation. There is no second authorization path.

### Why not capability-based for everything?

Capability-based authorization (per `zero-tool-capability-runtime`)
is used for tools, where the grant is bounded to (project, tool,
agent_scope). For project-level operations (plan, execution,
membership), role-based is simpler and sufficient. The two models
coexist: role-based for project operations, capability-based for
tool invocation.

## Rejected alternatives

- **Attribute-based access control (ABAC)**: too flexible for the
  current vertical slice. The matrix is small enough to express
  directly. ABAC can be added later if the matrix grows beyond
  what is readable.
- **Per-permission checks scattered across handlers**: explicitly
  rejected by `zero-control-plane-trust` §"UI controls are not
  security" and by the "central decision path" requirement.
- **Skipping authorization for read operations**: rejected by
  `zero-project-isolation-evidence` §"Scope begins before access".
  Reads must also be authorized.

## Consequences

- Adding a new permission is a single line in
  `ROLE_PERMISSIONS` plus tests for allow/deny.
- Adding a new role is a single entry in `ROLE_PERMISSIONS` plus
  tests.
- The authorization matrix is auditable: it is data, not code spread
  across handlers.
- Denied decisions are observable in the audit log, which supports
  security review and incident response.
