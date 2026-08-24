# GAP 6 Design — Interactive Chat Endpoint

Status: design accepted · Phase 2 (with GAP 5)

## Problem

Getting a model response requires the batch pipeline
plan→approve→execute→run-ready. There is no way to send one message and
receive a response for interactive experimentation.

## Architecture

A new `ChatService` (in `src/zero/app/chat_service.py`) orchestrates an
ephemeral single-turn conversation:

```
POST /admin/chat  {message, agent_scope?, max_tool_rounds?}
    └─ ChatService.complete(...)
         ├─ build CanonicalRequest(system=..., messages=[user msg], tools=granted)
         ├─ ProviderService.send_request_with_fallback(...)   [durable row + usage]
         ├─ optional tool rounds ≤ max_tool_rounds (default 3)
         │    reuses AgentRuntime._run_tool_rounds semantics via shared helper
         ├─ record usage normally (accounting + estimate_cost)
         └─ return {"content", "tool_calls_executed", "usage",
                    "provider_request_id"}
```

- **Ephemeral context**: no plan/execution/task rows are created. The
  provider request row and its response artifact remain durable
  (accounting integrity) but carry `execution_id=None` and
  idempotency scope `"chat:{project}:{hash}"`.
- **Tools**: only tools with an existing grant for
  `(project, "main_worker")` are declared; invocations flow through
  `ToolService.invoke` so capability authorization, budgets, redaction,
  and audit apply unchanged.
- **System prompt**: fixed, short, chat-scoped; no project knowledge
  retrieval in v1 (keeps the endpoint side-effect free).

## Data model changes

None. Provider requests/usage already tolerate null execution ids.

## API surface

```http
POST /admin/chat
{"message": "...", "agent_scope": "main_worker", "max_tool_rounds": 3}
200 → {"content": "...", "tool_calls_executed": [{"tool_name","arguments","result"}],
       "usage": {...}, "provider_request_id": "..."}
429 → rate limited   401/403 → auth
```

Auth: admin session cookie + CSRF token (same as other mutating /admin
routes). Rate limit: token bucket per session, default 10 requests/min,
configurable `ZERO_CHAT_RATE_LIMIT_PER_MIN`.

## Security considerations

- Prompt content is user input; it is never echoed into logs; provider
  request artifacts follow existing storage rules.
- Tool round cap (≤8, default 3) bounds cost per call; rate limiter
  bounds aggregate cost.
- CSRF enforced; responses do not include raw secrets (output passes
  tool-layer redaction).

## Test strategy

- Service tests with FakeProviderAdapter: happy path, tool-call loop,
  round-cap nudge, fallback chain on transient error, usage recorded
  once, no execution/task rows created.
- Endpoint tests: auth required, CSRF enforced, 429 after burst,
  schema validation errors.
- Rate-limiter unit tests (fake clock).

## Migration path

Additive module + route registration in `manage/web.py` admin router.

## Rollback strategy

Remove router registration; service is inert.

## Acceptance criteria

- A POST returns model content inline with usage accounting recorded.
- No persistent plan/execution state is created.
- Rate limiting and admin auth enforced; suite green.
