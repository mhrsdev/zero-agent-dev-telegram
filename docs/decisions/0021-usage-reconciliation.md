# ADR 0021 — Usage Reconciliation with Separate Token Classes

- Status: ACCEPTED
- Date: 2026-08-08
- Milestone: 10 (Provider Adapters and Usage Reconciliation)
- Skills applied: `zero-claude-token-economics`, `zero-provider-adapter-contract`

## Context

`PLAN.md` §15 (Milestone 10) requires:
- Token classes remain separate.
- Whole-agent-tree usage is counted exactly once.
- Estimated cost is distinct from authoritative reconciled billing.
- Pricing changes do not mutate historical raw usage.

`zero-claude-token-economics` §"Keep token classes separate": "Store
these as separate non-negative counters. Never collapse them into one
``total_tokens`` field before persistence. They have different prices
and different diagnostic meaning."

`zero-claude-token-economics` §"Estimated cost is not billing truth":
"Persist three separate concepts: 1. provider-reported token counters;
2. client-estimated cost with calculator version and timestamp; 3.
authoritative reconciled billing imported from the provider or gateway."

## Decision

Adopt a usage reconciliation model with:

1. **Four separate token classes**: `input_tokens`,
   `output_tokens`, `cache_creation_input_tokens`,
   `cache_read_input_tokens`. Each is a non-negative integer stored in
   its own column on `usage_records`. They are never collapsed into a
   single `total_tokens` before persistence.

2. **Deduplication**: `UNIQUE(provider_request_id,
   provider_message_id)` ensures that duplicate streamed usage (same
   provider message ID) is not double-counted. When a duplicate insert
   is attempted, the repository returns `False` and the existing record
   is kept.

3. **Whole-tree aggregation**: `aggregate_usage_for_project` sums all
   non-duplicate usage records for a project into a single `TokenUsage`.
   The `is_whole_tree` flag indicates whether a record includes
   subagent usage (preferred when available).

4. **Versioned pricing**: `pricing_catalog_entries` with
   `catalog_version`. Each usage record stores its
   `pricing_catalog_version`. When pricing changes (new catalog
   version), historical records retain their original version;
   estimated costs are NOT recalculated.

5. **Separate estimated and reconciled costs**: `estimated_cost_usd` is
   computed from the token counts and the pricing catalog at request
   time. `reconciled_cost_usd` is NULL until the authoritative billing
   is imported from the provider. They are separate fields; the
   estimated cost is never overwritten by the reconciled cost.

6. **Cost estimation**: `estimate_cost(usage, pricing)` computes the
   estimated cost from the four token classes and the four prices
   (input, output, cache creation, cache read). Uses `Decimal` for
   precision.

7. **Reconciliation path**: `reconcile_usage(usage_id,
   reconciled_cost_usd)` sets the authoritative cost on a usage record.
   This is a separate operation from cost estimation; it does not
   recalculate the estimated cost.

## Rejected alternatives

- **Single `total_tokens` field**: explicitly rejected by
  `zero-claude-token-economics`. Different token classes have different
  prices and diagnostic meanings.
- **Overwriting estimated cost with reconciled cost**: rejected. The
  estimated cost is a record of what was estimated at request time; the
  reconciled cost is the authoritative billing. Keeping them separate
  allows audit of estimation accuracy.
- **Recalculating historical costs on pricing change**: explicitly
  rejected by `zero-claude-token-economics` §"Pricing changes do not
  mutate historical raw usage".
- **Summing streamed cumulative snapshots**: explicitly rejected by
  `zero-provider-adapter-contract` §"Usage has scope and authority".
  Every cumulative snapshot is added as a new delta, multiplying usage.

## Consequences

- Token classes are always separate; derived metrics (total_input,
  cache_read_ratio) are computed on read, not stored.
- Duplicate streamed usage is not double-counted.
- Whole-tree usage is counted exactly once.
- Estimated and reconciled costs are clearly separated.
- Pricing changes do not mutate historical raw usage.
- The audit trail records which pricing version was used for each
  estimate.
