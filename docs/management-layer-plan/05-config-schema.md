# 05 — Canonical Configuration Schema (v1)

One file, typed, versioned: `config.yaml`. Env vars override at load for
containers/CI; the file is what every UI edits. Secrets are **references** —
values live only in the engine's encrypted secret store.

```yaml
schema_version: 1

server:
  host: 127.0.0.1            # admin/GUI bind; warn+confirm on 0.0.0.0
  port: 8000
  environment: development   # development|test|production (maps ZERO_ENV)

telegram:
  mode: bot_api              # v1 ships bot_api only
  default_agent: main_worker
  webhook:
    enabled: false
    secret_ref: null         # sec_… reference
  polling_interval_seconds: 1.0

access:
  mode: owner_only           # owner_only|users|groups|users_and_groups|public
  public_confirmed_at: null  # ISO ts; required when mode=public
  allow_users: []            # telegram external ids
  groups:                    # verified groups only (wizard-confirmed)
    - chat_id: "-1001234567890"
      title: "Apollo Dev"
      kind: supergroup       # private|group|supergroup|forum
      topic_id: null
      enabled: true
      default_agent: main_worker
      allowed_features: [chat, plan, approve]   # feature gates
      rate_limit_per_min: 10
      daily_token_budget: 200000
      provider_policy: null  # optional {providers:[...], models:[...]}
      added_by: zu_…
      added_at: now

providers:                   # instances (catalog supplies defaults)
  - id: openai-primary
    protocol: openai_compatible   # | anthropic | google | custom_openai
    display_name: OpenAI
    base_url: https://api.openai.com/v1
    api_key_ref: sec_…        # REQUIRED reference, never inline
    enabled: true
    fallback_priority: 1      # lower = tried first
    models: [gpt-4o-mini, gpt-4o]
  - id: anthropic-primary
    protocol: anthropic
    base_url: https://api.anthropic.com
    api_key_ref: sec_…
    fallback_priority: 2
    models: [claude-sonnet-4]

routing:
  primary_model: gpt-4o-mini
  fallback_models: [claude-sonnet-4]
  request_timeout_seconds: 120
  max_attempts_per_provider: 2     # maps ZERO_PROVIDER_MAX_ATTEMPTS
  breaker:
    failure_threshold: 5
    cooldown_seconds: 60

agents:
  main_worker:
    system_suffix_file: null  # optional path; content NOT stored in git
    tools: [read_file, write_file, run_command, capture_diff]

usage:
  soft_daily_tokens: 500000       # warn threshold
  hard_daily_tokens: 1000000      # hard stop threshold
  per_group_daily_tokens: {}      # chat_id -> budget
  search_daily_calls: 200

websearch:
  enabled: false
  provider_id: null          # must reference providers[] entry
  api_key_ref: null
  per_group_enabled: []

memory:
  database_url_ref: null     # optional; else server.database drives engine
  compaction_threshold_percent: 85

backups:
  schedule: daily            # off|daily|hourly
  retention: 7               # kept archives
  include_secrets: false     # export encrypted secrets too (explicit)

updates:
  channel: stable            # stable|beta
  auto_check: true
  auto_apply: false

privacy:
  telemetry_enabled: false   # opt-in; no payload shipped in v1 anyway

admin:
  gui_bind_warning_acknowledged_at: null
```

## Rules encoded in pydantic (`core/config.py`)

- `schema_version` literal 1; unknown keys → error (strict).
- Cross-field validation: `access.groups[].chat_id` uniqueness;
  `routing.fallback_models ⊆ ∪ providers.models`; `websearch.provider_id`
  must exist when enabled; `mode=public` requires `public_confirmed_at`;
  every `*_ref` matches `^sec_[a-z0-9_]+$` and is checked resolvable at
  commit time.
- **Atomic writes:** write tmp in same dir → fsync → `os.replace`; lockfile
  `.config.lock` (fcntl/msvcrt); on save, previous file copied to
  `config.last-good.yaml`.
- **Migrations:** `MIGRATIONS = {1: …}` map; loader upgrades older versions,
  backing up original to `backups/config-v{n}-{ts}.yaml` first.
- **Env override layer:** `ZERO_*` still honored by the engine; ConfigService
  exposes `effective()` merge and reports any env-overridden key in wizard
  ("managed elsewhere") so UIs never fight the environment.
- **Export:** `export(include_secrets=False)` replaces refs with
  `"__REDACTED__"`; diagnostics always use redacted form.

## Migration of existing installs

Importer reads current `ZERO_*` env (or systemd EnvironmentFile) → builds v1
file; secrets stay where they are (engine store) and refs are created by
looking up existing `secret_references` rows. Old-style-only users keep
working: if no config.yaml exists, engine behaves exactly as today.
