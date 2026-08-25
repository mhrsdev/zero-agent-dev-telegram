# Live Integration Testing (GAP 1)

The deterministic suite uses fakes exclusively. This directory
(`tests/integration_live/`) holds the only tests that talk to real
external services. They exist to close the release-validator gap:
*"no live provider or Telegram behavior has been verified."*

## Gating

Every test is skipped unless **both** hold:

1. `ZERO_ENABLE_LIVE_TESTS=1` is explicitly set; and
2. the credentials that test needs are present.

So `pytest` (any CI run, any dev box) never touches the network from
this package.

## Credentials

| Variable | How to obtain |
|---|---|
| `LIVE_TELEGRAM_BOT_TOKEN` | Talk to [@BotFather](https://t.me/BotFather) → `/newbot`. Store the token. |
| `LIVE_TELEGRAM_CHAT_ID` | Add the bot to a private test group; resolve the id via a getUpdates call or @userinfobot. |
| `LIVE_OPENAI_API_KEY` | platform.openai.com → API keys. A few cents of credit is plenty (tests cap `max_tokens`). |
| `LIVE_ANTHROPIC_API_KEY` | console.anthropic.com → API keys. |

Optional overrides: `LIVE_OPENAI_BASE_URL`,
`LIVE_ANTHROPIC_BASE_URL` (for gateways/proxies).

## Running locally

```bash
ZERO_ENABLE_LIVE_TESTS=1 \
LIVE_TELEGRAM_BOT_TOKEN=123:ABC \
LIVE_TELEGRAM_CHAT_ID="-1001234567890" \
LIVE_OPENAI_API_KEY=sk-... \
LIVE_ANTHROPIC_API_KEY=sk-ant-... \
pytest -m "live_telegram or live_provider" tests/integration_live -v
```

## Running in CI

GitHub repository secrets: add the four variables above, then dispatch
**"Live integration tests"** from the Actions tab
(`.github/workflows/live-tests.yml`). The workflow triggers on
`workflow_dispatch` only — never on push or pull_request — so live
costs and tokens are only exercised deliberately.

## What each test proves

| Test | Assertion |
|---|---|
| `test_live_telegram_get_me` | token valid; non-empty username; `is_bot=True` |
| `test_live_telegram_send_message` | message delivered; integer `message_id` returned |
| `test_live_telegram_poll_once` | one poll cycle: empty batch or valid results, cursor advances |
| `test_live_openai_completion` | non-empty content; server usage tokens > 0 |
| `test_live_anthropic_completion` | same via the Anthropic Messages adapter |
| `test_live_provider_streaming` | multiple `text_delta` events arrive over time (not buffered), then a terminal event |

Cost guardrails: completions use small models and `max_tokens ≤ 48`;
the polling test uses `timeout=0`; nothing runs tool loops.
