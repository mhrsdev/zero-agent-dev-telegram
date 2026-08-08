# ADR 0024 — Interface Adapter Model with Opaque Callback Tokens

- Status: ACCEPTED
- Date: 2026-08-08
- Milestone: 13 (Telegram, Discord, Secondary Interface Adapters)
- Skills applied: `zero-interface-adapter-model`, `zero-control-plane-trust`,
  `zero-project-isolation-evidence`

## Context

`PLAN.md` §18 (Milestone 13) requires:
- External IDs map to stable Zero User IDs through a verified link.
- Owner selects enabled project/channel/topic scopes.
- Telegram General and unrelated topics are not enabled by default.
- Normal conversation does not become execution.
- Approval actions use the same plan revision and authorization rules.
- Adapter-local storage is not authoritative project state.
- Duplicate webhook/update delivery is idempotent.
- Edited or stale approval messages cannot approve a newer revision.
- Website and messaging actions observe the same durable state.
- Platform outage does not lose backend execution state.

`zero-interface-adapter-model` §"One canonical event envelope": different
transports describe the same human action differently. A useful canonical
envelope carries the facts the control plane needs without treating
transport payloads as trusted domain state.

`zero-interface-adapter-model` §"UI controls carry opaque references, not
authority": callback payloads and URLs are small, replayable, and visible
to users. They may carry an opaque action token, but the server still
resolves current state and permission.

## Decision

Adopt an interface adapter model with:

1. **Interface bindings (scope configuration)**: `InterfaceBinding`
   records connect a project to a platform/chat/topic combination. The
   owner explicitly enables each binding; `is_enabled` defaults to
   `False` (General is NOT enabled by default per TELEGRAM_FINDINGS).

2. **Canonical event envelope**: `NormalizedEvent` carries the facts the
   control plane needs: platform, external_event_id (transport
   idempotency key), external_actor_id, chat_id, topic_id, event_kind,
   content, and optional callback_token. Transport-specific details
   (Telegram update_id, Discord interaction token) stay in the adapter.

3. **Event log (idempotent processing)**: `interface_event_log` with
   `UNIQUE(platform, external_event_id)` ensures duplicate delivery is
   a no-op. Each event is logged with its processing result (processed,
   ignored_unlinked, ignored_disabled, denied, error).

4. **Opaque callback tokens**: `CallbackToken` records carry the plan
   ID, revision number, action (approve/reject/edit), and expiry. The
   callback_data sent to the platform is the short opaque token ID
   (e.g. `ct_abc123`), not trusted role or ownership data. The server
   resolves current state and permission when the callback is used.

5. **Stale callback defense**: when a callback is used, the service
   checks that the token's `revision_number` matches the plan's
   `current_revision_number`. If they differ (the plan was edited after
   the callback button was sent), the callback is denied with
   `processing_result="denied"` and `processing_detail="stale callback:
   revision mismatch"`.

6. **Identity resolution**: external identities are resolved to Zero
   Users via `require_verified_external_identity`. Unlinked or
   unverified users are ignored with `processing_result="ignored_unlinked"`.

7. **Disabled scope defense**: events from disabled or unbound scopes
   are ignored with `processing_result="ignored_disabled"`. No planning
   or execution side effects occur.

8. **Normal conversation does not execute**: message events are ingested
   as conversation events via the plan service. No plan is created, no
   execution is triggered. The Main Planner (future LLM integration)
   would decide when to propose a plan from conversation.

9. **Platform outage resilience**: disabling a binding does not modify
   canonical project state. Conversation events, plans, and executions
   remain in the backend. Re-enabling the binding allows new events
   without data loss.

## Rejected alternatives

- **Adapter owns domain state**: explicitly rejected by
  `zero-interface-adapter-model` §"Adapter owns domain state".
  Reconnect and multi-interface consistency fail.
- **External username used as identity**: explicitly rejected by
  `zero-interface-adapter-model` §"External username used as identity"
  and by PLAN.md M13: "External IDs map to stable Zero User IDs."
- **Project membership implies enabled scope**: explicitly rejected by
  `zero-interface-adapter-model` §"Project membership implies enabled
  scope" and by TELEGRAM_FINDINGS: General is NOT enabled by default.
- **Callback payload trusted**: explicitly rejected by
  `zero-interface-adapter-model` §"Callback payload trusted" and by
  TELEGRAM_FINDINGS §11: callback_data is replayable client data.
- **Platform acknowledgement equals domain success**: explicitly
  rejected by `zero-interface-adapter-model` §"Platform acknowledgement
  equals domain success" and by TELEGRAM_FINDINGS §3.

## Consequences

- An authorized user can propose and approve one plan from an explicitly
  enabled messaging scope, observe the same plan on the website, and
  trigger exactly one backend execution handoff.
- Unknown and unlinked users cannot act.
- Disabled topics/channels produce no side effects.
- Duplicate delivery is idempotent.
- Stale approval messages cannot approve a newer revision.
- Website and messaging actions observe the same durable state.
- Platform outage does not lose backend execution state.
- Telegram-specific state (update_id, chat_id, topic_id) stays in the
  adapter; core plan/execution records carry source-neutral equivalents.
