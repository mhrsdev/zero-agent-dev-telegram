# GAP 8 Design — Subagent Delegation

Status: design accepted · Phase 6 (after Phase 2 streaming/chat)

## Problem

`AgentRuntime` runs tasks sequentially; there is no way for a running
agent to delegate a bounded subtask to an isolated child context.

## Architecture

A `delegate` builtin tool registered by the runtime (not the static
tool registry, because it needs runtime internals). Claude-code parity:
depth cap 3; hermes parity: fresh child context, narrowed tools,
result returned inline as tool result.

```
agent calls delegate(objective, agent_type?, tools?, model?, context_budget?)
    └─ DelegateToolHandler (src/zero/app/delegation.py)
         ├─ depth check: parent_depth >= 3 → tool error result
         ├─ resolve child policy: agent type (default caller's),
         │    tools ∩ parent permitted_tools (never widened),
         │    model override, context budget ≤ parent's
         ├─ create synthetic child task on the SAME execution
         │    (key=f"delegate:{parent_task}:{n}", expected_evidence=("provider_response",))
         ├─ spawn AgentRuntime.run_task(...) synchronously with:
         │    - isolated conversation (fresh messages)
         │    - depth = parent_depth + 1 (thread-local context var)
         │    - lease duration inherited from parent's lease
         │    - usage tagged agent_scope="sub_agent" ⇒ is_whole_tree=False
         └─ return {"status","task_id","content"} as ToolCallResult payload
```

- **Depth tracking**: `contextvars.ContextVar("zero_delegate_depth")`
  set around `run_task`; root tasks are depth 0; limit is depth ≥ 3.
- **Concurrency**: delegated tasks lease instances of the same agent
  type via the existing atomic `lease_instance_for_task`, so
  `max_concurrent_instances` is enforced unchanged; a saturated type
  fails the delegation with a typed error result (never deadlocks).
- **Usage**: provider requests during a delegation pass
  `agent_scope="sub_agent"`; `_record_usage` already computes
  `is_whole_tree=False` for that scope, so whole-tree aggregation sums
  correctly while per-scope queries can filter.

## Data model changes

None. Child tasks are ordinary rows with a synthetic key prefix and
dependency edge on the parent (`TaskDependency(parent→child)`), which
also gives GET /executions/{id} a free lineage view.

## API surface

New tool visible to models:

```json
{"name": "delegate",
 "description": "Delegate a bounded subtask to an isolated sub-agent.",
 "input_schema": {"type":"object","properties":{
   "objective": {"type":"string"},
   "agent_type": {"type":"string"},
   "tools": {"type":"array","items":{"type":"string"}},
   "model": {"type":"string"},
   "context_budget": {"type":"integer","minimum":1000}},
  "required":["objective"]}}
```

Registered only when `AgentRuntime` has a delegation wiring flag
(`enable_delegation=True` constructor arg, default False; enabled in
composition root).

## Security considerations

- Tool narrowing is intersection-only: a child can never see a tool
  the parent lacks.
- Recursion bomb contained by depth cap + instance concurrency caps +
  attempt budgets.
- The delegate handler reuses `ToolService.invoke` authorization path;
  audit op `"tool.delegate"` records parent/child linkage without
  content echo.

## Test strategy

- Unit: depth-limit rejection at 3; tool-narrowing intersection;
  context-budget clamping; concurrency-limit failure surfaces as tool
  error not exception; usage tagged sub_agent / is_whole_tree=False.
- Integration (fake provider): parent delegates → child task created
  with dependency → child completes → parent receives inline result
  text; aggregation query sums whole-tree correctly.
- Regression: default runtime (delegation disabled) never declares the
  tool.

## Migration path

Additive constructor flag + tool registration; off by default until
composition enables it.

## Rollback strategy

Disable flag; tool disappears from declarations; existing rows remain.

## Acceptance criteria

- Parent receives subtask result inline; usage tracked separately and
  summed correctly; nesting capped at 3; instance limits respected.
