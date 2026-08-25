-- GENERATED from 0008_provider_adapters.sql by scripts/gen_pg_migrations.py.
-- PostgreSQL dialect translation of the canonical SQLite schema.
-- Do not edit directly; re-run the generator instead.

-- Zero Develop — Milestone 10 schema: provider adapters and usage reconciliation.
--
-- This migration adds:
--   * provider_models — registered provider models with capabilities.
--   * provider_requests — durable records of each provider request.
--   * usage_records — normalized token usage per request.
--   * pricing_catalog_entries — versioned pricing for cost estimation.
--
-- Design invariants (per zero-provider-adapter-contract and
-- zero-claude-token-economics):
--   * Canonical events and state are provider-neutral.
--   * Provider rendering validates tool-call/result shape before submission.
--   * Changing model/provider does not destroy identity, memory, task, or
--     execution state.
--   * Prompt cache is an optional adapter optimization.
--   * Token classes remain separate (input, output, cache creation, cache read).
--   * Whole-agent-tree usage is counted exactly once.
--   * Estimated cost is distinct from authoritative reconciled billing.
--   * Persist adapter/model/version with every request and usage record.

-- ------------------------------------------------------------------
-- Provider models (registered models with capabilities)
-- ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS provider_models (
    id              TEXT PRIMARY KEY,
    provider        TEXT NOT NULL,
    model_name      TEXT NOT NULL,
    -- context_window: max tokens the model can process.
    context_window  INTEGER NOT NULL,
    -- max_output_tokens: max tokens the model can generate.
    max_output_tokens INTEGER NOT NULL,
    -- capabilities: JSON array of supported capabilities (streaming,
    -- native_tools, structured_output, prompt_caching, image_input, etc.).
    capabilities    TEXT NOT NULL DEFAULT '[]',
    -- is_active: whether this model can be used for new requests.
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL
                    DEFAULT (to_char(clock_timestamp(), 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')),
    UNIQUE (provider, model_name)
);

CREATE INDEX IF NOT EXISTS idx_provider_models_provider
    ON provider_models(provider);

-- ------------------------------------------------------------------
-- Provider requests (durable records of each provider request)
-- ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS provider_requests (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id),
    execution_id    TEXT REFERENCES executions(id),
    -- provider and model identify which adapter was used.
    provider        TEXT NOT NULL,
    model_name      TEXT NOT NULL,
    -- request_hash: a hash of the request payload for deduplication.
    -- If the same request is submitted twice, the second is a no-op.
    request_hash    TEXT NOT NULL,
    -- state: pending, streaming, completed, failed, cancelled, unknown.
    state           TEXT NOT NULL DEFAULT 'pending'
                    CHECK (state IN ('pending','streaming','completed','failed','cancelled','unknown')),
    -- error_class: classified error type (auth_failure, rate_limit,
    -- invalid_request, context_limit, transient, policy_refusal,
    -- cancelled, unknown_outcome).
    error_class     TEXT,
    -- error_message: redacted error message (no secrets).
    error_message   TEXT,
    -- response_artifact_id: artifact containing the full response.
    response_artifact_id TEXT REFERENCES artifacts(id),
    started_at      TEXT NOT NULL
                    DEFAULT (to_char(clock_timestamp(), 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')),
    completed_at    TEXT,
    UNIQUE (request_hash)
);

CREATE INDEX IF NOT EXISTS idx_provider_requests_project
    ON provider_requests(project_id);
CREATE INDEX IF NOT EXISTS idx_provider_requests_execution
    ON provider_requests(execution_id);
CREATE INDEX IF NOT EXISTS idx_provider_requests_state
    ON provider_requests(state);

-- ------------------------------------------------------------------
-- Usage records (normalized token usage per request)
-- ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS usage_records (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id),
    provider_request_id TEXT NOT NULL REFERENCES provider_requests(id),
    execution_id    TEXT REFERENCES executions(id),
    -- provider_message_id: the provider's message ID for deduplication
    -- of streamed steps. Per zero-claude-token-economics: parallel
    -- tool calls may emit repeated assistant messages with the same
    -- message ID and identical usage. Count each unique message ID once.
    provider_message_id TEXT,
    -- Token classes (kept separate per zero-claude-token-economics):
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_input_tokens     INTEGER NOT NULL DEFAULT 0,
    -- estimated_cost_usd: client-side estimate (NOT billing truth).
    estimated_cost_usd TEXT NOT NULL DEFAULT '0',
    -- pricing_catalog_version: which pricing version was used.
    pricing_catalog_version INTEGER NOT NULL DEFAULT 1,
    -- reconciled_cost_usd: authoritative cost from provider billing.
    -- NULL until reconciled.
    reconciled_cost_usd TEXT,
    -- is_whole_tree: whether this usage record includes subagent
    -- usage (whole-tree). Per zero-claude-token-economics: prefer
    -- provider per-model whole-tree usage when available.
    is_whole_tree   INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
                    DEFAULT (to_char(clock_timestamp(), 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')),
    -- Deduplication: one usage record per (provider_request_id,
    -- provider_message_id). If provider_message_id is NULL, the
    -- request-level usage is stored once.
    UNIQUE (provider_request_id, provider_message_id)
);

CREATE INDEX IF NOT EXISTS idx_usage_records_project
    ON usage_records(project_id);
CREATE INDEX IF NOT EXISTS idx_usage_records_execution
    ON usage_records(execution_id);
CREATE INDEX IF NOT EXISTS idx_usage_records_request
    ON usage_records(provider_request_id);

-- ------------------------------------------------------------------
-- Pricing catalog entries (versioned pricing for cost estimation)
-- ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS pricing_catalog_entries (
    id              TEXT PRIMARY KEY,
    catalog_version INTEGER NOT NULL,
    provider        TEXT NOT NULL,
    model_name      TEXT NOT NULL,
    -- Prices in USD per million tokens.
    input_price_per_million  TEXT NOT NULL,
    output_price_per_million TEXT NOT NULL,
    cache_creation_price_per_million TEXT NOT NULL DEFAULT '0',
    cache_read_price_per_million     TEXT NOT NULL DEFAULT '0',
    -- effective_at: when this pricing takes effect.
    effective_at    TEXT NOT NULL
                    DEFAULT (to_char(clock_timestamp(), 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')),
    created_at      TEXT NOT NULL
                    DEFAULT (to_char(clock_timestamp(), 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')),
    UNIQUE (catalog_version, provider, model_name)
);

CREATE INDEX IF NOT EXISTS idx_pricing_catalog_version
    ON pricing_catalog_entries(catalog_version);