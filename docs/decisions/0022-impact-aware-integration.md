# ADR 0022 — Impact-Aware Integration Review with Controlled Merge Gates

- Status: ACCEPTED
- Date: 2026-08-08
- Milestone: 11 (Integration / Compatibility Review and Controlled Merge)
- Skills applied: `zero-agent-execution-lifecycle`, `zero-planner-worker-contract`

## Context

`PLAN.md` §16 (Milestone 11) requires:
- Impact-set derivation from task outputs.
- Compatibility review contract and evidence format.
- Combined test/integration workspace isolated from task worktrees.
- Conflict classification and escalation.
- Controlled merge proposal containing source tasks, diffs, checks,
  risks, and required approval.
- Post-integration memory/RAG update only from accepted results.

`zero-agent-execution-lifecycle` §"Integration is impact review, not
diff aesthetics": "The Integration / Compatibility Sub Agent is another
dynamic type with a special responsibility: determine whether
independently correct changes remain correct together. Its useful input
begins with: immutable base revisions, diffs and changed paths, touched
contracts and dependencies, schema/type/API/config changes, test
results and failure artifacts, approved plan constraints."

`zero-planner-worker-contract` §"Merge is a controlled product
transition": "A clean Git merge proves textual compatibility. Zero
additionally needs: combined tests, migration ordering, contract
compatibility, required human decisions, source task and approval
provenance, authority to merge, recoverable target state."

## Decision

Adopt an impact-aware integration review with controlled merge gates:

1. **Impact-set derivation**: `derive_impact_set(execution_id,
   task_ids)` reads the diff artifacts from each task's worktree and
   extracts changed file paths. Each `ImpactEntry` records the file
   path, change type (added/modified/deleted), and whether the file is
   a contract (schema, API, type, config).

2. **Contract detection**: `_is_contract_file` uses path-segment and
   extension heuristics to identify contract files. Contract files
   trigger compatibility review because other tasks may depend on them.

3. **Conflict detection**: `detect_conflicts` identifies contract file
   changes that require review. Conflicts are classified as:
   - `none`: no contract files changed.
   - `low_risk`: only config files changed (deterministic, resolvable
     by policy).
   - `human_decision_required`: schema/API/type files changed (product
     decision needed).

4. **Combined test result**: `record_combined_test_result` records
   whether combined tests passed. If tests fail and there are conflicts,
   the review escalates to `human_decision_paused`.

5. **Merge proposal**: `create_merge_proposal` creates a proposal only
   from an approved review. The proposal carries source tasks, source
   diffs, checks_passed, and risks.

6. **Merge gates**: `approve_merge` enforces:
   - The actor must have `integration.authorize_merge` permission.
   - `checks_passed` must be True.
   The `execute_merge` operation requires the proposal to be in
   `approved` state.

7. **Post-integration memory update**: rejected integrations do not
   update accepted memory. Only accepted (merged) results can be
   ingested into Project RAG.

8. **Merge provenance**: the merge proposal traces every included task
   (`source_tasks`), the approver (`approved_by`), and the merge time
   (`merged_at`). Audit events record propose, approve, and execute
   operations.

## Rejected alternatives

- **Reread the entire repository on every review**: explicitly rejected
  by `zero-agent-execution-lifecycle` §"It does not reread the entire
  repository without evidence that broad inspection is needed" and
  PLAN.md M11. Review begins from diffs and touched contracts.
- **Auto-merge on green unit tests**: explicitly rejected by PLAN.md
  M11: "A deceptive green unit test cannot bypass failed combined
  tests." Combined tests must pass, not just individual task tests.
- **Auto-resolve human-decision conflicts**: explicitly rejected by
  PLAN.md M11: "product decisions return to humans."
- **Update memory before merge**: explicitly rejected by PLAN.md M11:
  "Rejected integration does not update accepted memory."
- **Merge without explicit authority**: explicitly rejected by PLAN.md
  M11: "Merge requires explicit authority and passing gates."

## Consequences

- Two independently produced changes are combined in an isolated
  integration environment, impact-reviewed, tested, and either
  presented as a safe merge proposal or blocked with precise evidence.
- Compatible independent changes integrate cleanly.
- Conflicting schema/type/API changes are detected.
- Human-decision conflicts pause merge.
- Rejected integration does not update accepted memory.
- Merge provenance traces every included task and approval.
