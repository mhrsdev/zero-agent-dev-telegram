# 03 — Zero Router Reuse Matrix

Verdicts from full read of `Zero-router` (TS monorepo). Rule: reuse
**validated concepts/schemas**, never the platform shell. Zero Dev Telegram
already owns wire adapters + fallback/retry/cost in Python — port only what
fills real gaps, integrated *around* `provider_service.py`, not beside it.

## 1. Component map (what each package is)

| Path | What it is | Framework |
|---|---|---|
| apps/gateway | Hono HTTP API `/v1/chat/completions`,`/v1/models`; auth/rate-limit/idempotency/audit middleware | Hono |
| packages/registry | YAML→Zod catalog loader; indexed ModelRegistry; boot-time validation | TS/Zod |
| packages/router | SmartRouter candidate filter + weighted score + alias routing | TS |
| packages/engine | FailoverEngine, circuit/health tracker, credential rotation, rate limit (Redis optional) | TS |
| packages/provider-sdk | adapter contract, PlanBuilder capability planning, token/cost math | TS |
| packages/adapters/{openai-compatible,anthropic,google} | wire translation | TS |
| packages/protocol / canonical | dialect translation; neutral types + capability enum | TS |
| packages/crypto | envelope AES-256-GCM key wrapping; router API-key format | TS |
| packages/store | Postgres multi-tenant store (orgs, keys, usage billing) | TS |
| config/ | data-only YAML catalogs (13 providers, model files, aliases) | data |

## 2. Capability verdicts

### a. Provider/model registry & metadata — **REFACTOR+PORT** ✅ highest value
`config/providers.yaml` (+`registry/src/schema.ts:86-112`) and
`config/models/*.yaml` carry exactly what our wizard needs: base_url, auth
kind, quirks, regions, retains_data; per-model context_window,
max_output_tokens, capabilities[], speed tier, pricing inputs.
Port as **pydantic models + two YAML files shipped inside `zero/manage`**;
keep their "fail at boot on invalid catalog" philosophy and
deprecation/reference checks (`registry.ts:69-109`). Indexed lookup pattern
ports 1:1.

### b. Health checking — **ARCHITECTURE-ONLY, port state machine** ⚙️
No active pings anywhere; passive rolling-window breaker per
`(provider,model)` with closed/open/half-open + TTFT
(`engine/src/health.ts:10-49,62-185`). Port the ~200-line state machine into
`zero/routing/health.py` keyed by `(provider_id, model)`; skip Redis variant
(single-process product). Add one active probe we lack: wizard "test
completion" already planned (spec §6).

### c. Routing/fallback ordering — **REFACTOR+PORT core ideas** ⚙️
Weighted scoring normalized within candidate set, mode presets,
reliability-friendly defaults (`router/src/score.ts:39-95`); deterministic
alias resolution; context-fit pre-checks; open-circuit exclusion;
rejection tracing (`router/src/router.ts:123-340`). Failover ordering:
credential-rotation before model substitution, terminal-vs-rotatable error
classes, jittered backoff, first-event commit barrier for streams
(`engine/src/failover.ts:54-231`). Integrate as a **candidate-selector that
feeds our existing linear fallback chain** in `provider_service`
(`send_request_with_fallback`), plus breaker gate + rejection reasons.
Do NOT port first-event barrier yet (we expose no SSE to clients).

### d. Cost estimation/pricing — **REFACTOR+PORT small pure module**
Cache-aware `estimateCost()` with correct cache-read semantics
(`provider-sdk/src/model.ts:59-74`) matches our TokenUsage classes; port math
into existing pricing path (we already store 4 token classes) so estimates
improve without schema change.

### e. Credential handling — **SPLIT verdict**
- PORT concept: error→credential-status classification
  (invalid/exhausted/rate_limited/cooldown + retry_after)
  (`engine/src/credentials.ts:110-163`) → drives cooldown before retry.
- REJECT storage: Postgres/org-scoped store; keep Zero's Fernet secret
  references. Optional later: envelope-crypto idea
  (`crypto/src/envelope.ts:82-141`) if we ever need multi-key at-rest —
  current single-key HKDF+Fernet is adequate for this product.

### f. Capability detection — **DECLARATIVE ONLY today**
Capabilities are declared in YAML and matched against request needs
(`provider-sdk/src/plan.ts:79-146`); **no active tool-call/stream probes
exist in Zero-router either.** Adopt declarative matching now; the wizard's
active probe (spec §6 steps 7-8) is net-new work for us.

## 3. Reject list (do not import/port)

| Hazard | Evidence |
|---|---|
| Router API-key format + auth middleware (`zr_live_…`, SHA-256, scopes) | `crypto/src/api-key.ts:32-78`, `gateway/middleware/auth.ts:13-58` |
| Multi-tenant org/team/billing store, ApiKey rpm/tpm, idempotency replay tables | `store/src/types.ts:3-113` |
| PG migrations + row mappers; Redis client/scripts | `store/src/pg/*`, `engine/src/redis/*` |
| Gateway HTTP app/pipeline (Hono context) | `apps/gateway/src/*` |

## 4. Overlap guard (already exist in zero-agent-dev-telegram — do not duplicate)

Wire translation, streaming, tool-message sanitation, usage normalization
(`app/provider_adapter.py`), linear fallback + same-provider bounded retry +
error classification + Decimal cost estimation + pricing registration
(`app/provider_service.py`). Router-port integrates **upstream** of these:
catalog → candidates → health/breaker gate → existing fallback chain.

## 5. Final reuse tally

| Verdict | Items |
|---|---|
| REFACTOR+PORT | provider/model YAML catalog (a); scoring/candidate selection + breaker-aware exclusion (c-core); breaker state machine (b); cost math refinement (d); credential-status classification (e-part) |
| ARCHITECTURAL REFERENCE ONLY | Redis variants; PlanBuilder emulation rewrites (later, optional) |
| REJECT | gateway app, org auth/API-keys, billing/multi-tenant store, PG/Redis infra, protocol/canonical packages (superseded by our canonical layer) |
