# ADR 0018 — Staged Retrieval Router with Authorization-First Design

- Status: ACCEPTED
- Date: 2026-08-08
- Milestone: 9 (Retrieval Router, Context Builder, Token Budgeting, Compaction)
- Skills applied: `zero-context-memory`, `zero-claude-token-economics`,
  `zero-project-isolation-evidence`

## Context

`PLAN.md` §14 (Milestone 9) requires:
- Staged Retrieval Router: authorize, generate candidates, rank,
  deduplicate/diversify, budget, render, and record provenance.
- Deterministic Context Builder with stable and volatile regions.
- Token Budget Manager supporting provider counts and a conservative
  fallback.
- Context-injection ledger explaining selected and omitted records.

`zero-context-memory` §"Build retrieval as a staged router": "Use this
order: 1. authorize project/user/agent scope; 2. generate candidates
from lexical, embedding, symbol/code-graph, and decision indexes; 3.
rerank by task relevance, agent type, provenance quality, freshness,
and decision state; 4. deduplicate and diversify; 5. allocate a token
budget; 6. render bounded snippets with source IDs; 7. record
injections for audit and evaluation."

`zero-context-memory` §"Do not force recall": "An empty result is
better than cross-project leakage or irrelevant context."

## Decision

Adopt a staged retrieval router with seven stages:

1. **Authorize**: all repository queries filter by `project_id` before
   any content is loaded. No candidate from another project can enter
   the pipeline.

2. **Generate candidates**: from two sources:
   - Project RAG (FTS5 full-text search on approved `rag_documents`);
   - Agent-type-scoped knowledge records (from `knowledge_records`
     where `state` is `candidate` or `approved`).

3. **Rank**: candidates are scored. RAG candidates get a BM25 score
   from FTS5. Knowledge records get a simple relevance score (1.0 if
   the query appears in the content, 0.1 otherwise). Higher score =
   higher rank.

4. **Deduplicate**: by `(source, record_id)`. The same record cannot
   appear twice.

5. **Budget**: candidates are added to the selection until the token
   budget is exhausted. Candidates that don't fit are recorded as
   "omitted" with reason `budget_exceeded`.

6. **Render**: candidates carry their content; the context builder
   renders them into the `retrieved_context` region.

7. **Record**: an `InjectionLedger` is stored with:
   - `selected`: list of `(source, record_id, token_count)` for
     injected records.
   - `omitted`: list of `(source, record_id, reason)` for omitted
     records.
   - `total_candidates`, `total_tokens`, `budget_tokens`.

### Context builder with named regions

Per `zero-claude-token-economics` §"Reserve output before filling
input", the context builder assembles the context from named regions
with independent budgets:

1. `system_policy` (immutable)
2. `project_identity`
3. `plan_contract`
4. `execution_snapshot` (survives compaction)
5. `retrieved_context` (from the retrieval router)
6. `conversation_tail`
7. `compaction_summary` (if compacted)

The output reserve (default 15% of context window) is subtracted
before filling input. The retrieval budget is capped at 30% of the
context window.

### Token accountant

One token-accounting contract (`estimate_tokens`, `exceeds_threshold`,
`context_remaining`) drives preflight, thresholds, and context filling.
The conservative fallback is `len(text.encode('utf-8')) // 4`, matching
the `zero-context-memory` reference implementation.

## Rejected alternatives

- **Retrieve globally, filter after ranking**: explicitly rejected by
  `zero-project-isolation-evidence` §"Scope begins before access" and
  `zero-context-memory` §"Authorization happens before candidate
  retrieval". Filtering after retrieval would expose forbidden
  candidates to processing, caches, traces, or a model.
- **Vector embeddings**: deferred per PLAN.md M8. FTS5 is sufficient
  for the current vertical slice.
- **Separate token estimator per subsystem**: explicitly rejected by
  `zero-claude-token-economics` §"One token-accounting contract drives
  preflight, thresholds, telemetry, and UI".
- **Force recall**: explicitly rejected by `zero-context-memory`.
  Empty retrieval is preferred over unauthorized or irrelevant
  retrieval.

## Consequences

- Authorization happens before any content is loaded.
- The injection ledger provides full auditability of what was selected
  and what was omitted.
- The token budget is enforced exactly; candidates that don't fit are
  omitted, not truncated.
- Cross-project leakage is impossible through the retrieval router.
- The context builder produces deterministic named regions with
  independent budgets.
