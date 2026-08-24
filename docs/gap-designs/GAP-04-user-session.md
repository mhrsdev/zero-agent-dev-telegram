# GAP 4 Design — User-Session Telegram Mode

Status: design accepted · Phase 8 (after Phase 2)

## Problem

Only Bot API is implemented. Some operators want the agent to act as
their personal Telegram account (Telethon-style user session).

## Architecture

New adapter `src/zero/adapters/user_session.py` implementing the same
intake contract used by `TelegramAdapter` (normalize updates into
`NormalizedEvent`, dispatch through `InterfaceAdapterService`), backed
by Telethon under the optional `[session]` extra.

```
UserSessionTelegramAdapter
    ├─ TelethonClient(StringSession(session_string), api_id, api_hash)
    ├─ inbound: events.NewMessage → NormalizedEvent(platform="telegram",
    │    event_kind="message", external_event_id=message.id …)
    ├─ outbound: send_message → client.send_message (rate-limited)
    └─ rate limiter: token bucket 30 msgs/min outbound (anti-spam),
         excess waits or raises typed AdapterRateLimitError
```

- The polling worker gains a second binding mode: when the interface
  binding's config says `mode: user_session`, it builds this adapter
  instead of `TelegramAdapter`. Access policy applies identically —
  events flow through the same `process_inbound_event` gate with
  `owner_only` default.

## Configuration schema

Settings stays env-driven; the wizard persists these into the project's
secret store and interface-binding metadata:

```yaml
telegram:
  mode: bot_api | user_session
  session:
    api_id: int                      # non-secret
    api_hash_ref: sec_…              # encrypted ref (secret store)
    phone_ref: sec_…                 # encrypted ref
    session_string_ref: sec_…        # encrypted Fernet blob
```

Env surface for composition:
`ZERO_TELEGRAM_MODE`, plus refs resolved via existing secret service.

## Setup wizard branch (step "User Session")

In `manage/services/setup.py` + wizard forms + GUI/TUI renderers:

1. Explicit disclaimer screen: ToS implications, automation ban risk,
   recommendation to prefer Bot API. Requires typed confirmation
   (`I UNDERSTAND`) before proceeding.
2. Masked input collection of api_id / api_hash.
3. Interactive login performed by a dedicated CLI command
   (`zero telegram session-login`): phone → OTP prompt (getpass, memory
   only) → optional 2FA password (getpass). Produces
   `StringSession`; OTP/password are NEVER written to disk, logs,
   audit, or diagnostics.
4. Session string stored encrypted via `SecretService.store`
   (Fernet profile identical to other secrets, key-versioned).
5. Mode remains disabled unless `[session]` extra is installed AND
   `ZERO_TELEGRAM_MODE=user_session` explicitly set; anything else ⇒
   Bot API path unchanged.

## Data model changes

None new; uses secret_references + interface binding metadata columns.

## API surface

No new HTTP routes. Interface bindings gain a mode field in their
existing metadata JSON. Wizard CLI/GUI steps extended.

## Security considerations

- Session string = full account access: encrypted at rest, resolved
  only in the adapter at runtime, never logged (Telethon logger
  silenced for sensitive fields; our redact_sensitive_text covers
  `session=` patterns defensively).
- OTP/2FA held in local variables only; no persistence path exists.
- Outbound rate limiting (30/min default, configurable ≤60) protects
  the account from spam bans caused by agent loops.
- Access policy: same owner_only gate as bot messages; no bypass.

## Test strategy

- Unit tests with Telethon stubbed (protocol-level fake): event
  normalization to NormalizedEvent, rate limiter behavior (30/min,
  burst refusal), mode gating (missing extra/explicit flag).
- Secret-handling tests: session string stored encrypted; resolution
  failure surfaces typed error; OTP never persisted (assert store/log
  capture contains nothing).
- Wizard tests: disclaimer confirmation required; masked input paths;
  opt-in default false.

## Migration path

Additive extra + adapter + wizard step; Bot API remains default.

## Rollback strategy

Set mode back to `bot_api`; revoke/delete stored session secret.

## Acceptance criteria

- User-session mode disabled unless extra installed AND explicitly
  enabled; OTP/session material never appears in logs/audit/diags;
  access policy identical to Bot API path.
