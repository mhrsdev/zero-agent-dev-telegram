# ADR 0004 — Configuration as a Trust Boundary

- Status: ACCEPTED
- Date: 2026-08-08
- Milestone: 1
- Skills applied: `zero-modular-bootstrap`, `zero-control-plane-trust`,
  `zero-claude-token-economics`

## Context

`zero-modular-bootstrap` SKILL.md §"Configuration is a trust boundary":

> "Values that genuinely vary—database location, provider endpoint,
> secret references, limits—belong in validated configuration. Values
> that define current product semantics can remain code until variation
> is required."

> "A missing security-critical value should fail closed rather than
> select a convenient unsafe default."

The same skill §"Wrong example" warns against "forty environment
variables for imagined deployment modes, most with silent defaults."

`zero-control-plane-trust` SKILL.md §"Secrets are usable without being
visible": secrets are referenced, not embedded. `zero-claude-token-
economics` reinforces: never hardcode model windows, token prices, or
provider TTLs in core policy.

`PLAN.md` §6 invariants for Milestone 1:

- "Configuration is environment-aware without embedding secrets."
- "Test and development data cannot point accidentally at production."

## Decision

Configuration is a **typed, validated, fail-closed trust boundary**.

### 4.1 Configuration shape

A single `Settings` Pydantic model in `src/zero/config.py` is the only
way the rest of the application reads configuration. It is constructed
from environment variables (with optional `.env` file for local
development only).

Fields in Phase 1:

| Field | Env var | Type | Required | Notes |
|---|---|---|---|---|
| `zero_env` | `ZERO_ENV` | `Literal["development","test","production"]` | yes | Selects runtime mode |
| `database_url` | `ZERO_DATABASE_URL` | `str` | yes in production; auto-set in development/test | SQLite path or `:memory:` |
| `log_level` | `ZERO_LOG_LEVEL` | `Literal["DEBUG","INFO","WARNING","ERROR"]` | no (default `INFO`) | Structured log level |
| `secret_key` | `ZERO_SECRET_KEY` | `str` | yes in production | Used for session signing later; **never logged** |

Future milestones will add: provider endpoints, provider secret references
(not values — see below), tool timeout defaults, model windows, pricing
catalog version, etc. Each is added only when its milestone requires it.

### 4.2 Fail-closed rules

1. **Missing `ZERO_ENV`**: startup raises `ConfigError`.
2. **`ZERO_ENV=production` without `ZERO_DATABASE_URL`**: startup raises
   `ConfigError`. Production never gets an automatic test database.
3. **`ZERO_ENV=production` without `ZERO_SECRET_KEY`**: startup raises
   `ConfigError`.
4. **`ZERO_ENV=test` with `ZERO_DATABASE_URL` pointing at a path
   containing `prod` or `production`**: startup raises `ConfigError`.
   Defense in depth against accidental cross-environment pointing.
5. **`ZERO_ENV=test` with no `ZERO_DATABASE_URL`**: auto-set to an
   in-memory SQLite (`sqlite://:memory:`) so tests are fully isolated
   by default.
6. **`ZERO_ENV=development` with no `ZERO_DATABASE_URL`**: auto-set to
   `sqlite:///./zero_develop.db` (a local file). Never a production
   path.

### 4.3 Secret handling

- `ZERO_SECRET_KEY` is loaded into the `Settings` object but is never
  serialized into logs, audit records, error messages, or test
  fixtures.
- The `Settings` model's `__repr__` redacts `secret_key` to
  `[REDACTED]`.
- Future provider API keys, Telegram bot tokens, etc. will be stored
  as **secret references** (e.g. `provider_secret_ref="provider/p1/search"`)
  in the database, never as raw values in configuration. The raw value
  is resolved by the server-side capability runtime at the last
  responsible moment (per `zero-control-plane-trust` §"Secrets are
  usable without being visible" and `zero-tool-capability-runtime`
  §"Secrets resolve at the last responsible moment").

### 4.4 Test/production overlap prevention

`PLAN.md` §6 invariant: "Test and development data cannot point
accidentally at production."

We enforce this with three checks:

1. `ZERO_ENV=test` refuses a `ZERO_DATABASE_URL` containing `prod` or
   `production`.
2. `ZERO_ENV=production` refuses a `ZERO_DATABASE_URL` containing
   `:memory:` or starting with `sqlite://./` (the development default).
3. `ZERO_ENV=production` requires `ZERO_SECRET_KEY` to be at least 32
   bytes.

### 4.5 Loading

- `Settings.load()` is the single entry point. It reads env vars,
  validates with Pydantic, applies fail-closed rules, and returns an
  immutable `Settings` instance.
- `Settings.load_for_test()` is the only path that bypasses env-var
  loading. It accepts explicit kwargs and forces `zero_env="test"`.
  Used by the test suite.

### 4.6 What we explicitly do NOT do

- We do not invent forty environment variables for imagined deployment
  modes. Each variable earns its place by being needed today.
- We do not silently default security-critical values.
- We do not log configuration payloads wholesale.
- We do not store raw secrets in `.env` files committed to the repo.
  `.env.example` documents the variable names but contains placeholder
  values only.
- We do not load configuration in `domain/` or `app/` directly. They
  receive a `Settings` instance through dependency injection from
  `main.py`.

## Consequences

- The startup path is the only place configuration surprises can occur.
- A misconfigured production deployment fails loudly at boot rather
  than silently corrupting data.
- Tests cannot accidentally reach production because the config layer
  refuses the combination.
- Adding a future config value is a single Pydantic field plus a test;
  no scattered `os.environ.get` calls across the codebase.
