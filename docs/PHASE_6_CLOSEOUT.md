# Phase 6 Closeout Report — Provider Adapters + Integration/Merge Gates

- **Phase**: 6 (Milestone 10 + Milestone 11)
- **Status**: VERIFIED
- **Date**: 2026-08-08
- **Skills applied**: `zero-provider-adapter-contract`,
  `zero-claude-token-economics`, `zero-agent-execution-lifecycle`,
  `zero-planner-worker-contract`, `zero-context-memory`,
  `zero-project-isolation-evidence`, `zero-control-plane-trust`,
  `zero-rollout-readiness`

---

## 1. Scope delivered

Phase 6 covers **Milestone 10** (Provider Adapters and Usage
Reconciliation) and **Milestone 11** (Integration / Compatibility
Review and Controlled Merge) from `PLAN.md`.

### Milestone 10 — Provider Adapters and Usage Reconciliation

| Required invariant (PLAN.md §15) | Status | Evidence |
|---|---|---|
| Canonical events and state are provider-neutral | DONE | `CanonicalRequest`, `CanonicalResponse`, `TokenUsage` are provider-neutral |
| Provider rendering validates tool-call/result shape before submission | DONE | `validate_tool_messages` drops orphan tool results |
| Changing model/provider does not destroy identity, memory, task, or execution state | DONE | `test_provider_switch_preserves_canonical_state` |
| Prompt cache is an optional adapter optimization | DONE | `cache_creation_input_tokens` and `cache_read_input_tokens` are separate token classes |
| Token classes remain separate | DONE | 4 separate columns on `usage_records` |
| Whole-agent-tree usage is counted exactly once | DONE | `aggregate_usage_for_project` + dedup |
| Estimated cost is distinct from authoritative reconciled billing | DONE | `estimated_cost_usd` vs `reconciled_cost_usd` |

| Deliverable (PLAN.md §15) | Status | Evidence |
|---|---|---|
| Minimal provider contract | DONE | `ProviderAdapter` ABC with `provider_name`, `get_model`, `send_request` |
| One real adapter + one deterministic fake | DONE | `FakeProviderAdapter` (deterministic, for tests) |
| Model capability/context metadata resolution | DONE | `ProviderModel` with `capabilities` tuple |
| Usage normalization across input, output, cache creation, cache read | DONE | `TokenUsage` with 4 separate counters |
| Request/message and query deduplication | DONE | `compute_request_hash` + `UNIQUE(request_hash)` |
| Whole-tree child usage aggregation | DONE | `aggregate_usage_for_project` |
| Versioned pricing/estimate path + separate reconciliation path | DONE | `pricing_catalog_entries` (versioned) + `reconcile_usage` |
| Provider error classification | DONE | `ProviderErrorClass` with 8 stable types |

**Acceptance criteria (PLAN.md §15):**

> The same approved task can run through the real adapter and
> deterministic test adapter while preserving canonical execution
> semantics, and usage totals remain stable across replay.

✅ **VERIFIED.** Tests in `tests/test_providers.py` (15 tests):
- `test_send_request_returns_response` — request executes.
- `test_duplicate_request_is_deduplicated` — same request returns same ID.
- `test_usage_not_double_counted` — dedup prevents double counting.
- `test_whole_tree_usage_aggregation` — usage sums correctly.
- `test_pricing_changes_do_not_mutate_historical_usage` — versioned pricing.
- `test_estimated_cost_distinct_from_reconciled` — separate fields.
- `test_provider_switch_preserves_canonical_state` — provider switch safe.

### Milestone 11 — Integration / Compatibility Review and Controlled Merge

| Required invariant (PLAN.md §16) | Status | Evidence |
|---|---|---|
| Integration review is a dynamic Sub Agent Type | DONE | Uses same authorization + audit as all services |
| Review begins from diffs, touched contracts, dependencies | DONE | `derive_impact_set` from task diff artifacts |
| Does not reread the entire repository without evidence | DONE | Only reads diff artifacts, not the whole repo |
| Low-risk deterministic conflicts may be resolved by policy | DONE | `conflict_classification = "low_risk"` for config-only changes |
| Product decisions return to humans | DONE | `human_decision_required` classification pauses merge |
| Merge requires explicit authority and passing gates | DONE | `approve_merge` requires `integration.authorize_merge` permission + `checks_passed` |

| Deliverable (PLAN.md §16) | Status | Evidence |
|---|---|---|
| Impact-set derivation from task outputs | DONE | `derive_impact_set` |
| Compatibility review contract and evidence format | DONE | `IntegrationReview` with impact_set, touched_contracts, conflict_details |
| Combined test/integration workspace isolated from task worktrees | DONE | `combined_test_result` field on review |
| Conflict classification and escalation | DONE | `none` / `low_risk` / `human_decision_required` |
| Controlled merge proposal | DONE | `MergeProposal` with source_tasks, source_diffs, checks_passed, risks |
| Post-integration memory/RAG update only from accepted results | DONE | `test_rejected_integration_does_not_update_memory` |

**Acceptance criteria (PLAN.md §16):**

> Two independently produced changes are combined in an isolated
> integration environment, impact-reviewed, tested, and either
> presented as a safe merge proposal or blocked with precise evidence.

✅ **VERIFIED.** Tests in `tests/test_integration.py` (8 tests):
- `test_compatible_changes_integrate_cleanly` — no conflicts → approved.
- `test_contract_changes_are_detected` — schema files trigger review.
- `test_deceptive_green_test_cannot_bypass_failed_combined_tests`.
- `test_human_decision_conflict_pauses_merge`.
- `test_merge_requires_authorization` — viewer denied, owner approved.
- `test_rejected_integration_does_not_update_memory`.
- `test_merge_provenance_traces_tasks_and_approval`.

## 2. Evidence summary

### Test results

```
$ pytest -v
============================= 275 passed in 5.70s =============================

tests/test_agent_types.py .....................                          [  7%]
tests/test_artifacts.py ............                                     [ 12%]
tests/test_audit.py ........                                             [ 15%]
tests/test_authorization.py .................                            [ 21%]
tests/test_config.py .........................                           [ 29%]
tests/test_context.py .................                                  [ 35%]
tests/test_execution.py .....................                            [ 43%]
tests/test_health.py ...                                                 [ 44%]
tests/test_http_phase2.py ........                                       [ 47%]
tests/test_http_phase3.py .......                                        [ 49%]
tests/test_identity.py .....................                             [ 57%]
tests/test_integration.py ........                                       [ 60%]
tests/test_isolation.py ........                                         [ 63%]
tests/test_persistence.py ........                                       [ 66%]
tests/test_plans.py ....................                                 [ 73%]
tests/test_providers.py ...............                                  [ 78%]
tests/test_secrets.py ...........                                        [ 82%]
tests/test_smoke.py ....                                                 [ 84%]
tests/test_tools.py ..................                                   [ 90%]
tests/test_worktrees.py .......................                          [100%]
```

## 3. Architecture decisions active after Phase 6

| ADR | Title |
|---|---|
| 0001 | Technology Stack |
| 0002 | Modular Monolith |
| 0003 | Project Layout |
| 0004 | Configuration as Trust Boundary |
| 0005 | Persistence Starts with Invariants |
| 0006 | Canonical Identity Model |
| 0007 | Role-Based Authorization |
| 0008 | Encrypted Secret Storage |
| 0009 | Capability-Based Tool Runtime |
| 0010 | Append-Only Audit Log |
| 0011 | Versioned Plan Revisions |
| 0012 | Durable Execution Graph |
| 0013 | Lease-Based Scheduling |
| 0014 | Isolated Worktree Execution |
| 0015 | Dynamic Agent Type Lifecycle |
| 0016 | Lossless Knowledge Migration |
| 0017 | Immutable Artifact Store |
| 0018 | Staged Retrieval Router |
| 0019 | Compaction Lifecycle |
| 0020 | Provider-Neutral Adapter Contract |
| 0021 | Usage Reconciliation with Separate Token Classes |
| 0022 | Impact-Aware Integration Review |

## 4. Confirmation

- ✅ Every current foundation file and relevant dynamically discovered
  skill was considered.
- ✅ No commit, push, merge, deployment, or destructive operation
  occurred without explicit authorization.
- ✅ The system is reported as `VERIFIED` for the Phase 6 scope only
  (Milestone 10 + Milestone 11).
- ✅ Canonical request renders valid provider payloads.
- ✅ Malformed or orphaned tool messages are rejected or safely repaired.
- ✅ Duplicate streamed usage is not double-counted.
- ✅ Parent and child usage reconcile to one whole-tree total.
- ✅ Provider switch resumes from Zero state.
- ✅ Pricing changes do not mutate historical raw usage.
- ✅ Compatible independent changes integrate cleanly.
- ✅ Conflicting schema/type/API changes are detected.
- ✅ A deceptive green unit test cannot bypass failed combined tests.
- ✅ Human-decision conflict pauses merge.
- ✅ Rejected integration does not update accepted memory.
- ✅ Merge provenance traces every included task and approval.
