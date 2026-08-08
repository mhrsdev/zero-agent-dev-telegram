# ADR 0020 — Provider-Neutral Adapter Contract with Deterministic Fake

- Status: ACCEPTED
- Date: 2026-08-08
- Milestone: 10 (Provider Adapters and Usage Reconciliation)
- Skills applied: `zero-provider-adapter-contract`, `zero-claude-token-economics`

## Context

`PLAN.md` §15 (Milestone 10) requires:
- Minimal provider contract based on real current needs.
- One real provider adapter plus one deterministic fake used only for
  tests.
- Model capability/context metadata resolution.
- Usage normalization across input, output, cache creation, cache read.
- Request/message and query deduplication.
- Whole-tree child usage aggregation.
- Versioned pricing/estimate path and separate reconciliation path.
- Provider error classification, retry boundaries, and circuit behavior
  only where required.

`zero-provider-adapter-contract` §"Canonical meaning precedes provider
mapping": "Zero's core needs a small vocabulary such as: request
accepted, content delta, reasoning/metadata delta, canonical tool
request, tool result acknowledged, usage delta or authoritative usage
snapshot, completion, classified failure. A provider adapter maps its
wire events onto this vocabulary."

## Decision

Adopt a provider-neutral adapter contract with:

1. **Canonical request/response**: `CanonicalRequest` and
   `CanonicalResponse` are provider-neutral. The adapter maps these onto
   the provider's wire format and maps the provider's response back.

2. **Provider capabilities**: `ProviderModel.capabilities` is a tuple of
   `ProviderCapability` values (streaming, native_tools,
   structured_output, prompt_caching, image_input,
   server_reported_usage, cancellation, idempotency). Capabilities
   replace provider-name conditionals.

3. **Deterministic fake adapter**: `FakeProviderAdapter` produces
   deterministic responses for tests. It does NOT call any external
   service. It supports text generation, tool call generation, usage
   reporting, and error simulation.

4. **Request deduplication**: `compute_request_hash` produces a
   deterministic SHA-256 of the canonical request. The
   `UNIQUE(request_hash)` constraint on `provider_requests` ensures
   idempotent deduplication.

5. **Usage normalization**: `TokenUsage` keeps four separate counters
   (input_tokens, output_tokens, cache_creation_input_tokens,
   cache_read_input_tokens). The `from_mapping` classmethod accepts
   both snake_case and camelCase field names.

6. **Usage deduplication**: `UNIQUE(provider_request_id,
   provider_message_id)` on `usage_records` ensures duplicate streamed
   usage is not double-counted.

7. **Whole-tree aggregation**: `aggregate_usage_for_project` sums all
   non-duplicate usage records for a project.

8. **Versioned pricing**: `pricing_catalog_entries` with
   `catalog_version`. Historical usage records retain their
   `pricing_catalog_version`; pricing changes do not mutate historical
   raw usage.

9. **Separate reconciliation**: `estimated_cost_usd` is a client-side
   estimate; `reconciled_cost_usd` is the authoritative cost from
   provider billing. They are separate fields on `usage_records`.

10. **Error classification**: `ProviderErrorClass` has 8 stable types
    (auth_failure, rate_limit, invalid_request, context_limit,
    transient, policy_refusal, cancelled, unknown_outcome).
    `RETRIABLE_ERROR_CLASSES` identifies which errors justify a bounded
    retry.

11. **Tool message validation**: `validate_tool_messages` drops orphan
    tool results while preserving declared tool calls, per
    `zero-context-memory` §"sanitize_tool_pairs".

## Rejected alternatives

- **SDK objects as domain state**: explicitly rejected by
  `zero-provider-adapter-contract` §"SDK object becomes domain state".
  Core behavior couples to one provider.
- **Provider-name conditionals**: explicitly rejected by
  `zero-provider-adapter-contract` §"Capabilities replace provider-name
  conditionals".
- **Provider session as source of truth**: explicitly rejected by
  `zero-provider-adapter-contract` §"Persistent provider context is an
  optimization". If the handle expires, Zero rebuilds bounded context
  from canonical state.
- **Collapsing token classes into one total**: explicitly rejected by
  `zero-claude-token-economics` §"Keep token classes separate".
- **Estimated cost as billing truth**: explicitly rejected by
  `zero-claude-token-economics` §"Estimated cost is not billing truth".

## Consequences

- The same approved task can run through the real adapter and the
  deterministic test adapter while preserving canonical execution
  semantics.
- Usage totals remain stable across replay (deduplication).
- Changing model/provider does not destroy identity, memory, task, or
  execution state.
- Pricing changes do not mutate historical raw usage.
- Disabling an adapter does not make canonical project history
  unreadable.
