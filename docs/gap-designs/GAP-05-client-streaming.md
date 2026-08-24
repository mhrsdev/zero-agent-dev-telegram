# GAP 5 Design — Client-Facing Streaming (SSE)

Status: design accepted · Phase 2

## Problem

Provider SSE parsing exists internally
(`OpenAICompatibleProviderAdapter.send_request_stream` /
`AnthropicMessagesProviderAdapter.send_request_stream`) but responses
are always collected into a complete `CanonicalResponse`
(`ProviderService._collect_stream`). No HTTP endpoint streams tokens to
clients.

## Architecture

Three layers:

1. **Runtime event tap** — `AgentRuntime.run_task(..., stream_callback=None)`.
   When provided, `_run_tool_rounds` uses
   `ProviderService.send_request_with_fallback(..., stream=True)` mode
   that yields `CanonicalStreamEvent`s; each event is forwarded to the
   callback *and* accumulated exactly as today so evidence/usage paths
   are untouched. `stream_callback: Callable[[dict], None]` receives
   already-serialized JSON-safe dicts:
   `{"type": "text_delta", "text": …}`,
   `{"type": "tool_call", "name": …, "arguments": {…}}`,
   `{"type": "done", "finish_reason": …}`.
   ProviderService gains `send_request_events(...)` mirroring
   `send_request_with_fallback` but returning an iterator of canonical
   events while still recording the durable provider request row and
   usage (finalization happens on stream end; cancellation mid-stream
   marks state unknown — same semantics as `_collect_stream`).

2. **SSE endpoint** — `GET /admin/executions/{eid}/stream`
   (FastAPI `StreamingResponse`, media type `text/event-stream`):
   - frames: `data: {"type":"text_delta","text":"…"}\n\n`,
     `data: {"type":"tool_call","name":"…","arguments":{…}}\n\n`,
     `data: {"type":"done","finish_reason":"stop"}\n\n`;
   - heartbeat comment frame `: keepalive\n\n` every 15 s via a
     background timer merged into the byte generator;
   - a bounded in-process hub (`ExecutionStreamHub`) keyed by execution
     id holds a `queue.SimpleQueue` per subscriber; the runtime's
     `stream_callback` publishes into every queue for that execution.
     If no subscriber is connected, events are dropped (streams are
     observability, not storage).
   - disconnect-safe: generator stops on `ClientDisconnect`; queues are
     removed in `finally`.

3. **Clients**:
   - GUI (`manage/web.py` dashboard): chat panel using `fetch()` +
     `ReadableStream` reading `/admin/executions/{eid}/stream`,
     appending deltas progressively.
   - TUI (`manage/tui/app.py`): new Chat screen polling the same
     endpoint with httpx and rendering tokens in a scrollable
     `RichLog`.

## Data model changes

None (events are ephemeral). Usage/evidence persistence is unchanged.

## API surface

| Route | Method | Auth | Notes |
|---|---|---|---|
| `/admin/executions/{eid}/stream` | GET | admin session cookie | SSE; `Cache-Control: no-cache`; heartbeats |
| `/admin/chat/{project_id}` | POST | admin session cookie + CSRF | GAP 6 non-streaming chat |

## Security considerations

- Admin-session auth only (same scrypt/session scheme as other /admin
  routes); CSRF not applicable to GET but endpoint is read-only.
- Raw prompts/responses are NOT logged by the hub; audit records keep
  existing redaction rules.
- Rate limiting: subscriber creation per execution id capped (default
  4 concurrent streams per process); excess → 429.
- Heartbeats keep proxies from buffering/closing; response sets
  `X-Accel-Buffering: no` for nginx.

## Test strategy

- Hub unit tests: subscribe/publish/broadcast/disconnect cleanup,
  drop-when-no-subscriber, cap enforcement.
- Endpoint tests over httpx ASGI transport: full event sequence for a
  fake-provider execution including heartbeat timing (fake clock) and
  `text/event-stream` content type.
- Runtime tests: `stream_callback` receives deltas in order and final
  evidence/usage identical to non-streaming run (same request hash).
- Manual smoke documented: `curl -N http://127.0.0.1:8000/admin/executions/X/stream`.

## Migration path

Purely additive endpoints + optional runtime parameter.

## Rollback strategy

Remove routes/hub; runtime default (`stream_callback=None`) restores
previous behavior exactly.

## Acceptance criteria

- `curl -N` shows incremental text deltas for a running execution.
- GUI panel renders tokens progressively (manual verification step in docs).
- Existing non-streaming endpoints unchanged; suite green.
