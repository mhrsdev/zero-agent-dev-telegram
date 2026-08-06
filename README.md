# Zero v2 — Enterprise Telegram AI Agent Platform

A production-ready, Telegram-based AI collaboration platform for development teams. Zero v2 wraps the LLM of your choice (Gemini, OpenAI, OpenRouter, or any OpenAI-compatible endpoint) behind a **Router abstraction** so the agent loop, tools, memory, and approval workflow are all provider-agnostic.

> **Status:** Enterprise-ready. 432 tests passing. Real Telegram + Gemini integration verified end-to-end. Docker support included.

---

## Table of Contents

- [What Zero v2 Is](#what-zero-v2-is)
- [Architecture](#architecture)
- [Key Features](#key-features)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [LLM Providers](#llm-providers)
- [CLI Reference](#cli-reference)
- [Docker Deployment](#docker-deployment)
- [Testing](#testing)
- [Project Layout](#project-layout)
- [Hermes Agent Features Ported](#hermes-agent-features-ported)
- [Troubleshooting](#troubleshooting)
- [License](#license)

---

## What Zero v2 Is

Zero v2 is a Telegram bot that runs an autonomous AI agent. It is **not** a chatbot — it is a full agent platform with:

- **Three scope modes** (Personal / Normal / Development) with strict data isolation
- **Multi-agent orchestration** with sub-agent context isolation and budget enforcement
- **Tool registry** with deferred loading, approval workflow, and SSRF protection
- **Persistent memory** (facts, decisions, semantic, episodic) with TF-IDF retrieval
- **Approval workflow** (Approve / Reject / Edit / Request Changes) via Telegram inline keyboards
- **Voice messages** — download → transcribe → agent → TTS → sendVoice
- **MCP server + client** — expose Zero's tools to Claude Desktop / Cursor, or consume external MCP servers
- **Docker sandbox** — full file/network/process isolation for code execution
- **GitHub webhooks** — HMAC-verified, signature-checked

### Why "Zero"?

Zero is a **pure HTTP consumer** of "the Router" via the OpenAI protocol. The Router may be:
- A separate model-gateway service (production multi-tenant)
- A local in-process **RouterShim** that proxies to Gemini/OpenAI/OpenRouter (single-instance dev)
- Any OpenAI-compatible endpoint (custom)

Zero **never picks models** — it sends `effort_tier` hints and lets the Router decide. This is enforced by a structural test that greps for model-selection functions.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Telegram (User)                             │
└────────────────────────────┬────────────────────────────────────────┘
                             │ Bot API (long-poll / webhook)
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      TelegramBot (aiogram 3.x)                      │
│  ┌─────────────┐  ┌──────────────┐  ┌───────────────────────────┐   │
│  │ /commands   │  │ voice_router │  │ callback_handler          │   │
│  │ /clear      │  │ (Edge TTS)   │  │ (clarify + approval)      │   │
│  │ /memory     │  │              │  │                           │   │
│  │ /todos      │  │              │  │                           │   │
│  └─────────────┘  └──────────────┘  └───────────────────────────┘   │
└────────────────────────────┬────────────────────────────────────────┘
                             │ IncomingMessage + ModeResolutionResult
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    ZeroAgentRunner (the glue)                       │
│  ┌──────────┐  ┌──────────────┐  ┌────────────┐  ┌──────────────┐   │
│  │ Scope    │→ │ Memory       │→ │ AgentLoop  │→ │ Conversation │   │
│  │ resolve  │  │ retrieval    │  │ (compress) │  │ persist      │   │
│  └──────────┘  └──────────────┘  └─────┬──────┘  └──────────────┘   │
│                                       │                              │
│                                       ▼                              │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │                    AgentLoop                                 │   │
│  │  ┌─────────┐  ┌──────────┐  ┌──────────┐  ┌─────────────┐   │   │
│  │  │ budget  │  │ Router   │  │ tool     │  │ context     │   │   │
│  │  │ check   │  │ call     │  │ dispatch │  │ compress    │   │   │
│  │  └─────────┘  └────┬─────┘  └────┬─────┘  └─────────────┘   │   │
│  └────────────────────┼───────────────┼─────────────────────────┘   │
└───────────────────────┼───────────────┼─────────────────────────────┘
                        │               │
                        ▼               ▼
┌─────────────────────────────┐  ┌────────────────────────────────────┐
│      RouterClient           │  │       ToolRegistry                 │
│  (OpenAI protocol,          │  │  read_file, write_file, patch,     │
│   X-Zero-Scope-* headers,   │  │  bash_exec, web_fetch, todo,       │
│   retry, cost tracking)     │  │  clarify, delegate_task,           │
└──────────────┬──────────────┘  │  send_message, approval_request,   │
               │                  │  cronjob, memory_search, git_status│
               │ HTTP             └─────────────┬──────────────────────┘
               ▼                               │
┌──────────────────────────────────────────────┘
│
│  ┌────────────────────────────────────────────────────────────────┐
│  │                    RouterShim (HTTP server)                    │
│  │  Listens on 127.0.0.1:<port>/v1                                │
│  │  Translates OpenAI protocol → provider-native calls            │
│  │  Adds x-zero-cost-usd, x-zero-request-id headers               │
│  └───────────────────────────┬────────────────────────────────────┘
│                              │
│              ┌───────────────┼───────────────┐
│              ▼               ▼               ▼
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│  │    Gemini    │  │    OpenAI    │  │ OpenRouter   │
│  │  Provider    │  │  Provider    │  │  Provider    │
│  └──────────────┘  └──────────────┘  └──────────────┘
│
│  ┌────────────────────────────────────────────────────────────────┐
│  │              SQLite (three-file isolation)                     │
│  │  ┌────────────┐  ┌────────────┐  ┌────────────┐                │
│  │  │ personal.db│  │ normal.db  │  │   dev.db   │                │
│  │  │ (usr data) │  │ (grp data) │  │ (prj data) │                │
│  │  └────────────┘  └────────────┘  └────────────┘                │
│  │  ATTACH forbidden — cross-schema access impossible              │
│  └────────────────────────────────────────────────────────────────┘
```

---

## Key Features

### 1. Three Scope Modes (Strict Isolation)

| Mode | Use Case | Memory Kinds Allowed | DB Schema |
|------|----------|---------------------|-----------|
| **PERSONAL** | 1:1 private chat with the bot | semantic, episodic, fact, decision, preference | `personal.db` |
| **NORMAL** | Group chat (no project binding) | semantic, episodic, preference (NO facts/decisions) | `normal.db` |
| **DEVELOPMENT** | Topic bound to a Project | All kinds + ADR | `dev.db` |

**Adversarial defense:** Personal memory is **never** retrieved in DEVELOPMENT mode, under any condition. Enforced at SQL level (separate DB files), Python level (scope filter), and structural test level.

### 2. Multi-Agent Orchestration

- **Orchestrator** spawns sub-agents with context isolation
- Sub-agents get fresh conversation (no parent history)
- Sub-agents' tool allowlist = parent's minus blocked tools (`delegate_task`, `clarify`, `memory`, `send_message`, `cronjob`, `approval_request`)
- Max spawn depth = 1 (no grandchildren by default)
- Max concurrent children = 3 (configurable)
- Budget enforced before every Router call

### 3. Tool Registry (Deferred Loading)

Three-tier progressive disclosure (ported from Hermes `tool_search.py`):
- **Tier 0:** ≤4 tools → send full schemas
- **Tier 1:** >4 tools, total schema ≤4000 chars → send full schemas
- **Tier 2:** >4 tools, total schema >4000 chars → send name + description only; full schema loaded on first call via `tool_describe` / `tool_call` bridge tools

### 4. Approval Workflow

Four-choice workflow (ported from Hermes `approval.py`):
- **Approve** — action executes
- **Reject** — action blocked
- **Edit** — approver modifies params, request goes back to PENDING
- **Request Changes** — approver leaves a note, request stays PENDING

**Self-approval forbidden** — requester cannot approve their own request. Enforced in `ApprovalResolver.resolve()`.

Telegram inline keyboards with 4 buttons are sent automatically when the `approval_request` tool is called.

### 5. Memory System (Fact Promotion)

Memory kinds (retrieval priority high → low):
1. **Fact** — requires `approved_by` (promoted from semantic by a maintainer)
2. **Decision** — requires `approved_by`
3. **Semantic** — long-term knowledge
4. **Episodic** — event-based memory
5. **Preference** — user/group preferences
6. **Scratch** — short-term topic notes (30-day retention)

Retrieval: TF-IDF cosine similarity (local, no API calls) + substring boost + priority sort + token budget.

### 6. Voice Messages

Full pipeline:
1. Download voice file from Telegram
2. Validate Opus OGG format
3. Transcribe via RouterShim (OpenAI Whisper-compatible endpoint) OR Gemini
4. Run agent loop on transcribed text
5. Synthesize response via Edge TTS (free, no API key) or Router
6. Send voice reply via `sendVoice`
7. Fall back to text on any error

### 7. MCP (Model Context Protocol)

**Server mode:** `zero mcp serve` exposes Zero's tools to external MCP clients (Claude Desktop, Cursor). Implements `initialize`, `notifications/initialized`, `tools/list`, `tools/call`.

**Client mode:** `McpClient` connects to external MCP servers (stdio or SSE transport). Security scan at **save time** AND **spawn time** (dual check) blocks:
- Exfiltration patterns (curl, wget, nc, socat, /dev/tcp, python requests, urllib)
- Persistence patterns (authorized_keys, ~/.ssh/, /etc/pam.d, /etc/sudoers, cron, .bashrc)

### 8. Security

- **SSRF net_guard** — 5 rules block requests to private IP ranges, localhost, metadata endpoints
- **Path validation** — all file operations confined to a sandbox directory
- **Secret management** — all secrets stored as `secret://env/VAR_NAME` references, resolved at call time, two-layer masking in logs
- **Persistent revocable sessions** — token hash stored, constant-time comparison, lockout after failures
- **Threat patterns database** — regex patterns for prompt injection, data exfiltration, privilege escalation
- **Audit log** — append-only, every approval/permission/scope change recorded

---

## Quick Start

### Prerequisites

- **Python 3.11+** (3.12 recommended)
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))
- An LLM API key (Gemini / OpenAI / OpenRouter)

### Install

```bash
cd zero-v2
pip install -e ".[dev]"
pip install edge-tts jsonschema types-PyYAML  # optional extras
```

### Configure

Create `~/.zero/config.yaml` (or run `zero init`):

```yaml
database:
  backend: sqlite
  sqlite_dir: ~/.zero/db

telegram:
  bot_token: secret://env/TELEGRAM_BOT_TOKEN

router:
  provider: gemini  # gemini | openai | openrouter | custom
  api_key: secret://env/GEMINI_API_KEY
  default_model: gemini-2.0-flash

logging:
  level: info
  format: json
  redact_secrets: true
```

### Set Environment Variables

```bash
export TELEGRAM_BOT_TOKEN="1234567890:your_bot_token"
export GEMINI_API_KEY="your_gemini_api_key"
# OR for OpenAI:
# export OPENAI_API_KEY="your_openai_api_key"
# OR for OpenRouter:
# export OPENROUTER_API_KEY="your_openrouter_api_key"
```

### Run

```bash
# Long-polling mode (default)
zero serve --provider gemini

# Dry run (verify setup without starting the bot)
zero serve --dry-run --provider gemini

# Webhook mode (requires webhook.url in config)
zero serve --mode webhook
```

### Verify

Send a message to your Telegram bot. It should respond using the real LLM. Try:
- `/status` — show current mode + scope
- `/memory <query>` — search your long-term memory
- `/todos` — list your todos
- `/clear` — clear conversation history
- Any text message — the agent will respond

---

## Configuration

### Config Sources (priority high → low)

1. CLI args
2. Env vars `ZERO_<SECTION>__<KEY>` (double underscore separates nesting)
3. `~/.zero/config.yaml` (user)
4. `/etc/zero/config.yaml` (system)
5. Code defaults

### Key Config Fields

| Field | Default | Description |
|-------|---------|-------------|
| `database.backend` | `sqlite` | `sqlite` or `postgres` |
| `database.sqlite_dir` | `~/.zero/db` | Directory for 3 SQLite files |
| `telegram.bot_token` | (required) | `secret://env/TELEGRAM_BOT_TOKEN` |
| `telegram.webhook_url` | `None` | If set, use webhook mode |
| `router.provider` | `custom` | `gemini` / `openai` / `openrouter` / `custom` / `shim` |
| `router.base_url` | `http://127.0.0.1:8080/v1` | Used when `provider=custom` |
| `router.api_key` | (required) | `secret://env/...` |
| `router.default_model` | `None` | Model hint (router may override) |
| `router.timeout_seconds` | `60.0` | Per-call timeout |
| `router.max_retries` | `3` | Retries on 5xx/timeout |
| `router.shim_port` | `0` | RouterShim port (0 = auto-pick) |
| `agent.max_turns` | `100` | Max agent loop iterations |
| `agent.budget_default_usd` | `5.0` | Default per-agent budget |
| `security.approval_timeout_seconds` | `300` | 5 min approval timeout |
| `security.session_ttl_seconds` | `86400` | 24h session TTL |
| `memory.max_retrieval_tokens` | `4000` | Token budget for memory context |

### Secret References

All secrets MUST be `secret://` references. Raw values are rejected at parse time.

```yaml
telegram:
  bot_token: secret://env/TELEGRAM_BOT_TOKEN  # ✅
  bot_token: "123:abc"                         # ❌ rejected
```

Supported schemes:
- `secret://env/VAR_NAME` — from environment variable
- `secret://file/path/to/file` — from file contents
- `secret://vault/path` — from HashiCorp Vault (extension)

---

## LLM Providers

Zero ships 4 provider adapters. Each speaks the OpenAI Chat Completions protocol internally, so the `RouterClient` is unchanged.

### GeminiProvider (recommended for free tier)

```yaml
router:
  provider: gemini
  api_key: secret://env/GEMINI_API_KEY
  default_model: gemini-2.0-flash
```

- Endpoint: `https://generativelanguage.googleapis.com/v1beta/openai`
- Pricing: built-in table (gemini-2.0-flash: $0.10/1M in, $0.40/1M out)
- Get a key: https://aistudio.google.com/app/apikey

### OpenAIProvider

```yaml
router:
  provider: openai
  api_key: secret://env/OPENAI_API_KEY
  default_model: gpt-4o-mini
```

- Endpoint: `https://api.openai.com/v1`
- Pricing: built-in table (gpt-4o, gpt-4o-mini, o1, o3-mini, etc.)

### OpenRouterProvider

```yaml
router:
  provider: openrouter
  api_key: secret://env/OPENROUTER_API_KEY
  default_model: openai/gpt-4o-mini
```

- Endpoint: `https://openrouter.ai/api/v1`
- Pricing: read from `x-openrouter-cost-usd` header (dynamic catalog)
- Browse models: https://openrouter.ai/models

### GenericOpenAIProvider (custom)

```yaml
router:
  provider: custom
  base_url: http://my-router.internal:8080/v1
  api_key: secret://env/ZERO_ROUTER_API_KEY
```

- Use this for any OpenAI-compatible endpoint (vLLM, LM Studio, Ollama, etc.)

### RouterShim (how providers plug in)

When `provider != "custom"`, Zero starts an in-process HTTP server (`RouterShim`) on `127.0.0.1:<port>` that:
1. Accepts `POST /v1/chat/completions` (OpenAI protocol)
2. Translates to provider-native call
3. Returns OpenAI-format response
4. Adds `x-zero-cost-usd`, `x-zero-request-id` headers

This keeps the architectural boundary intact: **Zero code only ever talks to "the Router"**, never to a specific LLM provider directly.

---

## CLI Reference

```bash
zero --help                    # show all commands
zero --version                 # show version

zero init                      # create ~/.zero/config.yaml
zero config set <key> <value>  # set a config value
zero doctor                    # run health checks

zero serve [--mode polling|webhook] [--provider gemini|openai|openrouter|custom|shim] [--dry-run] [--drop-pending]
zero mcp serve [--name <name>] # run as MCP server on stdio

zero-migrate                   # migrate from v1 to v2 (separate entry point)
```

### Examples

```bash
# Start with Gemini (default)
zero serve --provider gemini

# Start with OpenAI
zero serve --provider openai

# Verify setup without starting the bot
zero serve --dry-run --provider gemini

# Drop pending Telegram updates on startup
zero serve --drop-pending

# Run as MCP server (for Claude Desktop)
zero mcp serve --name zero-agent
```

---

## Docker Deployment

### Quick Start

```bash
# Create .env file
cp .env.example .env
# Edit .env with your tokens

# Build and run
docker compose up -d

# View logs
docker compose logs -f zero-agent

# Stop
docker compose down
```

### Dockerfile Features

- **Multi-stage build** (builder + runtime) — smaller image
- **Non-root user** (`zero:zero`, uid 1001) — security hardening
- **Tini as PID 1** — proper signal handling (graceful shutdown on SIGTERM)
- **ffmpeg installed** — for voice message transcoding
- **Health check** — `zero doctor` every 30s
- **Resource limits** — 1G memory, 2 CPUs

### docker-compose.yml Features

- 19 environment variables pre-configured
- Persistent volume (`zero-data`) for DB + logs
- Health check with 15s start period
- Resource limits + reservations
- Log rotation (10MB × 5 files)

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | ✅ | — | Telegram bot token |
| `ZERO_ROUTER_PROVIDER` | ❌ | `gemini` | LLM provider |
| `ZERO_ROUTER_API_KEY` | ✅ | — | `secret://env/GEMINI_API_KEY` |
| `GEMINI_API_KEY` | If provider=gemini | — | Gemini API key |
| `OPENAI_API_KEY` | If provider=openai | — | OpenAI API key |
| `OPENROUTER_API_KEY` | If provider=openrouter | — | OpenRouter API key |
| `ZERO_ROUTER_DEFAULT_MODEL` | ❌ | `gemini-2.0-flash` | Default model |
| `ZERO_LOGGING_LEVEL` | ❌ | `info` | Log level |
| `ZERO_DROP_PENDING_UPDATES` | ❌ | `true` | Drop pending TG updates |

---

## Testing

### Run All Tests

```bash
pytest                            # all tests
pytest -m unit                    # only unit tests
pytest -m integration             # only integration tests
pytest -m adversarial             # only adversarial tests
pytest -m contract                # only contract tests
```

### Test Categories

| Category | Count | Description |
|----------|-------|-------------|
| Unit | 320+ | Fast (<10ms each), no external deps |
| Integration | 22+ | Real DB, multiple components |
| Contract | 33+ | Boundary contracts (Router, Platform, golden files) |
| Adversarial | 18 | Deliberate break attempts |
| Real E2E | 9 | Live Telegram + Gemini API (requires tokens) |

### Real Integration Tests

These run only when env vars are set:

```bash
export ZERO_REAL_BOT_TOKEN="your_telegram_bot_token"
export GEMINI_API_KEY="your_gemini_api_key"
pytest tests/integration/test_real_telegram.py tests/integration/test_real_e2e.py -v
```

### Type Checking & Linting

```bash
mypy zero                         # strict type check
ruff check zero tests             # lint
ruff format zero tests            # format
```

---

## Project Layout

```
zero-v2/
├── pyproject.toml                ← Python 3.11+, mypy --strict, ruff, pytest
├── README.md                     ← this file
├── IMPLEMENTATION_STATUS.md      ← detailed implementation status
├── Dockerfile                    ← multi-stage, non-root, tini, ffmpeg
├── docker-compose.yml            ← production single-instance deployment
├── .env.example                  ← secret template
├── .dockerignore
│
├── zero/
│   ├── __init__.py
│   ├── core/                     ← Phase 1 (Scope, Config, DB, Audit, Errors, Events, Jobs, Logging, Permissions, Secret)
│   ├── contracts/v1/             ← Phase 0 (5 JSON Schemas)
│   ├── db/                       ← Phase 1 (three-file SQLite isolation, ATTACH forbidden)
│   ├── memory/                   ← Phase 6 (Fact Promotion, TF-IDF, scope-bound retrieval)
│   ├── agents/
│   │   ├── definition.py         ← AgentDefinition with effort_tier
│   │   ├── run.py                ← AgentRun lifecycle
│   │   ├── loop.py               ← AgentLoop + context compression
│   │   ├── orchestrator.py       ← Sub-agent spawning with context isolation
│   │   ├── budget.py             ← Per-project + per-agent budget enforcement
│   │   ├── sandbox.py            ← Temp-dir sandbox (low-risk agents)
│   │   ├── docker_sandbox.py     ← Docker sandbox (full isolation, high-risk agents)
│   │   ├── router_client.py      ← OpenAI-protocol client (no model selection)
│   │   ├── llm_provider/         ← NEW: Gemini/OpenAI/OpenRouter adapters + RouterShim
│   │   │   ├── base.py           ← LLMProvider protocol, ProviderMessage, ProviderToolDef
│   │   │   ├── generic.py        ← GenericOpenAIProvider (base class)
│   │   │   ├── gemini.py         ← GeminiProvider (OpenAI-compat endpoint)
│   │   │   ├── openai_provider.py← OpenAIProvider
│   │   │   ├── openrouter.py     ← OpenRouterProvider
│   │   │   ├── router_shim.py    ← In-process HTTP server (OpenAI protocol)
│   │   │   └── factory.py        ← build_provider_from_config()
│   │   └── runner/               ← NEW: ZeroAgentRunner (production entrypoint)
│   │       └── __init__.py       ← Wires TelegramBot + AgentLoop + Router + LLM
│   ├── tools/
│   │   ├── base.py               ← Tool, ToolContext, ToolSpec, ToolError
│   │   ├── registry.py           ← ToolRegistry with deferred loading
│   │   ├── builtin.py            ← Basic tools (legacy)
│   │   ├── enterprise_builtin.py ← Enterprise tools (read/write/patch/bash/web/todo/clarify/git/memory + delegate/send_message/approval/cronjob)
│   │   ├── patch_parser.py       ← V4A patch format (Hermes)
│   │   ├── context_compressor.py ← Trajectory compression (Hermes)
│   │   ├── checkpoint_manager.py ← State checkpoints
│   │   └── enterprise_builtin.py ← All enterprise tools
│   ├── security/
│   │   ├── approval.py           ← 4-choice approval workflow
│   │   ├── net_guard.py          ← SSRF prevention (5 rules)
│   │   ├── session.py            ← Persistent revocable sessions
│   │   ├── path.py               ← Path validation
│   │   └── threat_patterns.py    ← Prompt injection / exfiltration patterns
│   ├── messaging/                ← Platform-neutral PlatformConnection abstraction
│   ├── telegram/
│   │   ├── bot.py                ← Full aiogram 3.x bot (long-poll + webhook)
│   │   ├── commands.py           ← Command framework (/clear, /memory, /todos)
│   │   ├── topic_binding.py      ← TopicBinding + GroupPolicy + resolve_mode()
│   │   ├── voice_handler.py      ← Voice message pipeline
│   │   ├── db_stores.py          ← DB-backed topic binding stores
│   │   └── mode_isolation_tests.py
│   ├── github/webhook.py         ← GitHub webhook handler (HMAC verified)
│   ├── platform/                 ← Phase P contracts (capability, health, events, remote command)
│   ├── mcp/
│   │   ├── server.py             ← MCP server (expose Zero's tools)
│   │   ├── client.py             ← MCP client (consume external MCP servers)
│   │   └── security.py           ← MCP security scanner (save + spawn dual check)
│   ├── voice/
│   │   ├── opus.py               ← Opus OGG utilities
│   │   ├── transcriber.py        ← RouterVoiceTranscriber + StubVoiceTranscriber
│   │   └── tts.py                ← EdgeTTSClient + StubTTSClient
│   ├── stores/                   ← DB-backed stores
│   │   ├── todo_store.py         ← DbTodoStore (all scopes)
│   │   ├── conversation_store.py ← DbConversationStore (all scopes)
│   │   ├── role_store.py         ← DbRoleStore
│   │   ├── approval_store.py     ← DbApprovalStore + DbApprovalResolver
│   │   ├── session_store.py      ← SessionStore
│   │   └── rate_limiter.py       ← RateLimiter
│   ├── observability/            ← Telemetry, metrics
│   └── cli/
│       ├── main.py               ← Click CLI (serve, init, doctor, mcp serve)
│       └── migrate.py            ← v1→v2 migration tool
│
└── tests/
    ├── conftest.py
    ├── unit/                     ← 320+ millisecond tests
    │   ├── test_scope.py
    │   ├── test_secret.py
    │   ├── test_llm_provider.py          ← NEW (22 tests)
    │   ├── test_enterprise_tools_v2.py   ← NEW (24 tests)
    │   ├── test_agent_loop_compression.py← NEW (4 tests)
    │   └── ... (20+ more test files)
    ├── integration/
    │   ├── test_database.py
    │   ├── test_real_telegram.py         ← Live Telegram tests
    │   └── test_real_e2e.py              ← NEW: Live end-to-end tests
    ├── contract/
    │   ├── test_router_integration.py    ← 16 tests with respx + golden files
    │   ├── test_platform_contracts.py
    │   └── test_structural.py
    └── adversarial/
        └── test_adversarial.py           ← 18 break attempts
```

---

## Hermes Agent Features Ported

Zero v2 was informed by a feature catalog of the [Hermes Agent](https://github.com/nousresearch/hermes-agent) repository. The following features were ported (verbatim or adapted):

### Ported Verbatim (24 features)
1. ToolRegistry + self-registration + check_fn TTL cache
2. Progressive tool disclosure (Tier 0/1/2)
3. Approval three-tier patterns
4. Per-thread interrupt tracking
5. URL safety / SSRF prevention
6. Threat patterns database
7. MCP security save+spawn dual check
8. Write approval staging
9. Tool result persistence (3-level defense)
10. Patch parser V4A format
11. BaseEnvironment + BoundedOutputCollector pattern
12. Dashboard auth provider framework
13. Lifecycle hook dispatcher
14. Constant-time token comparison
15. Per-thread ContextVar propagation
16. Telegram polling loop with aiogram 3.x
17. Voice message pipeline
18. Edge TTS as free default
19. MCP stdio transport with stderr-to-log
20. Context compression in AgentLoop (trajectory_compressor)
21. Multi-provider LLM support (Gemini/OpenAI/OpenRouter)
22. RouterShim (in-process Router for single-instance deployments)
23. Delegate task tool (sub-agent spawning)
24. Cron job management tool

### Adapted (12 features)
1. AIAgent conversation loop → AgentLoop
2. Memory tool frozen-snapshot → Fact Promotion
3. SessionDB SQLite schema → three PostgreSQL schemas / three SQLite files
4. Trajectory compressor → adversarial test corpus + AgentLoop integration
5. Config YAML + migrations → SQLite config
6. Profiles → three scope modes
7. Web dashboard → secondary to Telegram
8. Router transcriber → RouterVoiceTranscriber
9. MCP client → with security scan at spawn time
10. delegate_tool.py → DelegateTaskTool
11. send_message_tool.py → SendMessageTool
12. cronjob_tools.py → CronJobTool

### Skipped (Hermes-specific)
- Multi-provider auth (we ship our own provider layer)
- Discord/Slack/Feishu/Microsoft Graph (Telegram-only by design)
- Computer use (not in scope)
- Nous-specific managed tool gateway
- Copilot/Vercel/Azure/DingTalk auth
- Wake word detection, WhatsApp bridge

---

## Troubleshooting

### `ERROR: Package 'zero-v2' requires a different Python: 3.11.x not in '>=3.12'`

This is fixed in the latest version — Zero v2 now requires Python 3.11+. If you have an older archive, either:
- Upgrade to Python 3.12+, OR
- Edit `pyproject.toml` and change `requires-python = ">=3.12"` to `>=3.11`

### Telegram bot token revoked

Telegram auto-revokes bot tokens that are posted publicly. If you see `401 Unauthorized`:
1. Open [@BotFather](https://t.me/BotFather)
2. Send `/revoke <old_token>`
3. Send `/newbot` to create a new bot (or `/token` to regenerate)

### Gemini API `429 quota exceeded`

The free tier has strict rate limits. Options:
- Wait (quota resets daily)
- Enable billing at https://ai.google.dev/pricing
- Use OpenRouter (`provider=openrouter`) which aggregates many providers
- Use a local model via `provider=custom` + `base_url=http://localhost:1234/v1` (LM Studio)

### `secret reference does not resolve`

The env var referenced by `secret://env/VAR_NAME` is not set. Check:
```bash
echo $TELEGRAM_BOT_TOKEN
echo $GEMINI_API_KEY
```

### RouterShim not starting

The RouterShim only starts when `provider != "custom"`. If you set `provider=custom`, Zero talks directly to `router.base_url` (no shim).

### Docker build fails

Ensure Docker has at least 2GB RAM allocated. The build compiles `cryptography` which needs C extensions.

### Tests fail with `aiogram.exceptions.TelegramUnauthorizedError`

The `ZERO_REAL_BOT_TOKEN` env var is either not set or invalid. These tests are skipped by default — they only run when the env var is set.

---

## License

Proprietary. See `pyproject.toml` for details.
