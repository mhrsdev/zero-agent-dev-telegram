# GAP 1 Design — Live Integration Qualification

Status: design accepted · Phase 9 (last; qualifies everything above)

## Problem

Every external call is faked; the release validator states no live
Telegram/provider behavior has been verified. We add opt-in,
env-gated live tests plus a manual-dispatch CI workflow.

## Architecture

New test package `tests/integration_live/` with strict gating:

```python
LIVE_ENABLED = os.environ.get("ZERO_ENABLE_LIVE_TESTS") == "1"
TELEGRAM_READY = LIVE_ENABLED and os.environ.get("LIVE_TELEGRAM_BOT_TOKEN")
...
pytestmark = [
    pytest.mark.live_telegram,  # new marker
    pytest.skipif(
        not TELEGRAM_READY, reason="live Telegram creds + ZERO_ENABLE_LIVE_TESTS=1 required"
    ),
]
```

Markers registered in `pyproject.toml`
(`live_telegram`, `live_provider`, `pg_integration`) with
`addopts` unchanged — deterministic suite never collects them by
default because skip conditions are unmet in CI/dev.

### Tests

| File | What it proves |
|---|---|
| `test_live_telegram_get_me.py` | adapter getMe → non-empty username, `is_bot=True` |
| `test_live_telegram_send_message.py` | sendMessage to `LIVE_TELEGRAM_CHAT_ID` → message_id returned |
| `test_live_telegram_poll.py` | one `poll_once` cycle → empty result or valid update structure |
| `test_live_openai_completion.py` | minimal OpenAI completion → non-empty content, usage tokens > 0 |
| `test_live_anthropic_completion.py` | same via Anthropic adapter |
| `test_live_provider_streaming.py` | SSE events arrive incrementally (≥2 text_delta events or delta+end within timeout) |

Adapters under test are the real production classes
(`TelegramAdapter` with default httpx transport;
`OpenAICompatibleProviderAdapter`; `AnthropicMessagesProviderAdapter`)
— no fakes anywhere in this package.

### CI

`.github/workflows/live-tests.yml`: `workflow_dispatch` trigger ONLY.
Secrets: `LIVE_TELEGRAM_BOT_TOKEN`, `LIVE_TELEGRAM_CHAT_ID`,
`LIVE_OPENAI_API_KEY`, `LIVE_ANTHROPIC_API_KEY` mapped to env;
`ZERO_ENABLE_LIVE_TESTS: "1"` set in the workflow. Never runs on
push/PR (no other triggers). Job marked `continue-on-error: false` so a
manual run failing is visible.

## Data model changes

None.

## API surface

None.

## Security considerations

- Credentials only from environment; never written to disk by tests;
  test output must not echo tokens (assert messages use redaction).
- Chat-scoped sends use the configured test chat id only.
- Cost guardrails: completions use `max_tokens ≤ 32`; streaming test
  uses the smallest model defaults; no tool loops.

## Test strategy

This package IS the strategy. Additionally a conftest enforces the
double gate and skips fast when unset (zero network attempts).

## Migration path

Additive package + workflow + docs (`docs/LIVE_TESTING.md`) covering:
creating a BotFather bot, resolving a chat id, issuing provider keys
with spend caps, running locally (`ZERO_ENABLE_LIVE_TESTS=1 pytest -m live_telegram`),
and dispatching the workflow manually.

## Rollback strategy

Delete directory/workflow; nothing else depends on it.

## Acceptance criteria

- Live tests pass manually with real credentials.
- Workflow exists but never runs automatically.
- Deterministic suite unaffected (skips are silent).
