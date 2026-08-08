# ADR 0009 — Capability-Based Tool Runtime

- Status: ACCEPTED
- Date: 2026-08-08
- Milestone: 3 (Authorization, Secret Boundary, Tool Registry, Audit Core)
- Skills applied: `zero-tool-capability-runtime`, `zero-control-plane-trust`

## Context

`PLAN.md` §8 (Milestone 3) requires:
- Tool invocation is capability-based and least-privilege.
- Tool registration and invocation contract for one harmless test
  tool.
- Input/output validation at the tool boundary.

`zero-tool-capability-runtime` SKILL.md §"Registry metadata and
runtime capability differ": "The registry describes what a tool can
do. A capability grant describes who may invoke one bounded part of
it in one context."

`zero-tool-capability-runtime` §"Tool schemas are trust boundaries":
"Model output is untrusted input. Validation covers type, shape,
length, allowed values, project ownership, path normalization, and
domain preconditions before side effects begin."

`zero-tool-capability-runtime` §"Tool choice and tool permission are
separate": "A model may reason that web search is relevant. The
control plane still decides whether that project and agent type may
invoke it, under what cost and rate limits. A denied tool does not
become available through a different interface or child agent."

## Decision

Adopt a capability-based tool runtime with:

1. **Tool registry**: tools are registered with a name, description,
   JSON Schema for input, JSON Schema for output, and a server-side
   `handler_key`. The handler_key maps to an in-process Python
   callable. The registry is server-side only; tool schemas are
   never sent to models with secrets embedded.
2. **Capability grants**: a grant is bounded to (project_id, tool_id,
   agent_scope). The grant is unique per (project, tool, scope);
   without a grant, invocation is denied. Grants optionally carry
   limits (max_invocations, timeout_seconds) — these are stored but
   not yet enforced in Phase 2; enforcement arrives when the
   relevant limits are needed.
3. **Invocation lifecycle**: `ToolService.invoke` performs:
   1. Resolve tool by name.
   2. Validate input against the tool's input schema (jsonschema).
   3. Resolve the capability grant; deny if no grant.
   4. Invoke the handler with a `ToolContext` carrying project scope,
      actor, agent_scope, and the `SecretService` (for handlers that
      need to resolve secrets at invocation time).
   5. Validate the handler's output against the tool's output schema.
   6. Construct a `ToolResult` with the validated output, a bounded
      model-facing rendering, and timing.
   7. Record an audit event with the operation, target, result, and
      correlation ID — never the raw input or output.
4. **Built-in test tool**: `echo` is registered for Phase 2. It
   exercises the full lifecycle (input validation, grant resolution,
   output validation, audit) without side effects.

### Trust boundary

The handler receives:
- The validated input (already passed schema validation).
- A `ToolContext` with project scope, actor, agent_scope, and a
  reference to `SecretService` (for resolving secret references at
  invocation time).

The handler does NOT receive:
- Raw secrets in its arguments (secrets are resolved through
  `SecretService` referenced by the context).
- The capability grant itself (the grant was already checked; the
  handler trusts the runtime).
- Any way to widen its own authority (delegation can narrow but not
  invent authority).

### Output policy

The handler returns a dict matching the tool's output schema. The
runtime constructs a `ToolResult` with:
- `output`: the validated output dict.
- `model_facing`: a compact, redacted, JSON rendering capped at 500
  characters. For tools with large outputs, the handler should
  provide a custom rendering in a future milestone; for now the
  runtime truncates.
- `error`: optional error message on failure, never containing
  secrets or credentials.
- `duration_ms`: wall-clock duration.

### Audit

Every invocation — success, denial, validation failure, or handler
error — produces an audit event with:
- `operation`: `tool.invoke`.
- `target_type`: `tool`; `target_id`: the tool name.
- `result`: `success`, `denied`, `failure`, or `error`.
- `correlation_id`: a fresh ID per invocation, linkable to related
  events.
- `redacted_summary`: a compact, safe description (tool name, status,
  duration, optional error class) — never the raw input or output.

## Rejected alternatives

- **Boolean tool access (``tool=true``)**: explicitly rejected by
  `zero-tool-capability-runtime` §"Registry metadata and runtime
  capability differ". Boolean access hides operation, target,
  duration, and limits.
- **Handler receives raw secrets in arguments**: explicitly rejected
  by `zero-tool-capability-runtime` §"Secrets resolve at the last
  responsible moment".
- **Skip output validation**: rejected. A buggy handler that returns
  the wrong shape would silently corrupt downstream state. Output
  validation catches handler bugs.
- **Generic command runner**: rejected by
  `zero-tool-capability-runtime` §"Tool schemas are trust
  boundaries". A generic command runner would let the model supply
  arbitrary shell text, which is unsafe.
- **Plugin marketplace / dynamic code loading**: rejected for now.
  Tools are registered at startup; dynamic loading adds extension
  risk before there is a need.

## Consequences

- Adding a new tool requires: register it (name, schemas,
  handler_key, handler), then grant it per (project, scope). The
  lifecycle is explicit and auditable.
- Tool invocation is the same path whether called from HTTP, future
  Telegram adapter, or the agent runtime. There is no second path.
- The model-facing rendering is bounded; large outputs are
  truncated. Full output is preserved in the `ToolResult` for the
  caller (and in a future artifact store).
- The audit log records every invocation with stable correlation
  IDs, supporting debugging and cost analysis.
