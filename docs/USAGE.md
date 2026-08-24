# Zero Develop — Complete Usage & Operations Guide

**Audience:** anyone running Zero locally or deploying it for a team.
**Verified against:** tree state with **553 passing tests**, live-booted server
(88 API paths / 103 operations) and the full governance flow exercised over
real HTTP. Every example below matches a shipped request schema.

> **What Zero is:** a human-governed control plane for parallel AI software
> teams. Humans approve versioned plans; durable background tasks execute in
> isolated Git worktrees; only evidence-backed, integration-reviewed work is
> merged. It is a **server** (FastAPI + SQLite) — not a chat CLI.

---

## Table of contents

1. [Requirements](#1-requirements)
2. [Install](#2-install)
3. [Configuration (all environment variables)](#3-configuration)
4. [Environments: development vs test vs production](#4-environments)
5. [Running the server](#5-running-the-server)
6. [CLI reference](#6-cli-reference)
7. [First run walkthrough (development)](#7-first-run-walkthrough)
8. [The full governed workflow](#8-the-full-governed-workflow)
9. [Providers & model routing](#9-providers--model-routing)
10. [Tools, grants & worktrees](#10-tools-grants--worktrees)
11. [Agent types & project knowledge](#11-agent-types--project-knowledge)
12. [RAG & artifacts](#12-rag--artifacts)
13. [Telegram / Discord interfaces](#13-telegram--discord-interfaces)
14. [Observability & recovery](#14-observability--recovery)
15. [Running the verification suite](#15-running-the-verification-suite)
16. [Production checklist](#16-production-checklist)
17. [Known limitations](#17-known-limitations)
18. [Troubleshooting](#18-troubleshooting)

---

## 1. Requirements

| Component | Version | Notes |
|---|---|---|
| Python | **3.11 / 3.12** | `pyproject` requires ≥3.11 |
| Git | any recent | worktree isolation uses real `git` |
| uv *(recommended)* | latest | `pip install uv`, or <https://docs.astral.sh/uv/> |
| OS | Linux / macOS / Windows | Windows fully supported (POSIX-only tests skip automatically) |

Runtime dependencies are installed from `pyproject.toml`: FastAPI, uvicorn,
pydantic v2, httpx, jinja2, jsonschema, cryptography.

---

## 2. Install

```bash
git clone <your-fork-url> zero-agent-dev     # or unpack this release bundle's source/
cd zero-agent-dev

uv venv --python 3.12
source .venv/bin/activate                    # Windows: .venv\Scripts\Activate.ps1
uv pip install -e ".[dev]"                   # runtime + test tooling
```

This installs the console script **`zero-develop`** plus everything needed to
run the test suite.

---

## 3. Configuration

Configuration is a typed, fail-cloaded trust boundary (`src/zero/config.py`).
All settings come from **process environment variables** (or an `.env` file
passed explicitly via `--env-file`). `.env.example` is the annotated template;
it is never loaded automatically.

### 3.1 Core

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `ZERO_ENV` | always | – | `development` \| `test` \| `production` |
| `ZERO_DATABASE_URL` | prod: yes | dev/test: isolated defaults | SQLite only (`sqlite:///path.db`). Other schemes are refused. |
| `ZERO_LOG_LEVEL` | no | `INFO` | `DEBUG/INFO/WARNING/ERROR` |
| `ZERO_SECRET_KEY` | prod: yes, ≥32 bytes | – | Master key material (Fernet secrets/backups derive from it). Never logged. |
| `ZERO_AUTH_REQUIRED` | no | `true` in production | Bearer-token middleware on/off. Cannot be disabled in production. |
| `ZERO_BOOTSTRAP_TOKEN` | prod: yes* | – | One-time header token for `POST /auth/bootstrap`. *Or set `ZERO_ALLOW_MANUAL_PROVISIONING=1`. |
| `ZERO_ALLOW_MANUAL_PROVISIONING` | no | `0` | Escape hatch for externally provisioned first users. |

### 3.2 Providers (both optional; both may be set → automatic fallback chain)

```bash
# OpenAI-compatible chat completions
ZERO_OPENAI_API_KEY=sk-...
ZERO_OPENAI_BASE_URL=https://api.openai.com/v1
ZERO_OPENAI_MODEL=gpt-4o-mini
ZERO_OPENAI_TIMEOUT_SECONDS=60

# Anthropic Messages API
ZERO_ANTHROPIC_API_KEY=sk-ant-...
ZERO_ANTHROPIC_BASE_URL=https://api.anthropic.com
ZERO_ANTHROPIC_MODEL=claude-sonnet-4
ZERO_ANTHROPIC_TIMEOUT_SECONDS=60
```

When more than one adapter is registered they form an ordered fallback chain
for retryable errors (`transient`, `rate_limit`). Unknown models skip forward
to the next provider instead of aborting.

### 3.3 Execution & retries

| Variable | Default | Purpose |
|---|---|---|
| `ZERO_WORKTREE_ISOLATION_MODE` | `disabled` | `host_bounded` enables command execution. **Refused in production** until a real sandbox backend exists. |
| `ZERO_WORKTREE_ALLOWED_COMMANDS` | *(empty = deny all)* | Comma list of bare command names agents may run, e.g. `pytest,python,git` |
| `ZERO_WORKTREE_ROOT` | `<temp>/zero-worktrees` | Parent dir for task worktrees |
| `ZERO_TASK_MAX_ATTEMPTS` | `0` (off) | Total attempts per task (first run + auto-requeues). Max 16. |

### 3.4 Background workers

| Variable | Default | Purpose |
|---|---|---|
| `ZERO_WORKERS_ENABLED` | `1` (forced off in tests) | Host scheduler/delivery/polling loops inside the ASGI app |
| `ZERO_SCHEDULER_INTERVAL_SECONDS` | `5` | Tick cadence for handoff claiming / task draining / reviews |
| `ZERO_DELIVERY_INTERVAL_SECONDS` | `2` | Outbound result-delivery drain cadence |
| `ZERO_POLLING_INTERVAL_SECONDS` | `1` | Telegram long-poll cadence |

### 3.5 Integration review

| Variable | Default | Purpose |
|---|---|---|
| `ZERO_COMBINED_TEST_COMMAND` | *(unset)* | e.g. `pytest -q` — scheduler runs it automatically after review creation |
| `ZERO_COMBINED_TEST_TIMEOUT_SECONDS` | `300` | Cap ≤300 s |

### 3.6 Messaging verifiers

```bash
ZERO_TELEGRAM_WEBHOOK_SECRET=<random>          # enables Telegram webhook verify + polling worker
ZERO_DISCORD_APPLICATION_PUBLIC_KEY=<hex>      # enables Discord Ed25519 webhook verify
```

Adapters fail closed: without the verifier the webhook route returns 503.

---

## 4. Environments

| Mode | Auth | DB default | Notes |
|---|---|---|---|
| `development` | off (actor fields trusted) | `./zero_develop.db` | For local hacking; `/web` dashboards list all rows |
| `test` | off | in-memory, shared | Forced by the suite; refuses prod-shaped DB URLs |
| `production` | **required**, cannot disable | must be explicit file path | Requires `ZERO_SECRET_KEY` ≥32 bytes + bootstrap token (or manual-provision flag); refuses host-bounded execution |

Fail-closed rules live in `Settings._enforce_fail_closed_rules()`; invalid
config exits non-zero at startup.

---

## 5. Running the server

### Option A — console script (recommended)

```bash
zero-develop serve --host 127.0.0.1 --port 8000          # development
zero-develop serve --env-file ./secrets/.env --port 8000 # explicit env file
```

### Option B — uvicorn directly

```bash
export ZERO_ENV=development
uvicorn zero.main:app --host 127.0.0.1 --port 8000 [--reload]
```

Then open:

| URL | What |
|---|---|
| <http://127.0.0.1:8000/web/> | Server-rendered control surface (projects/plans/executions/audit) |
| <http://127.0.0.1:8000/docs> | Interactive OpenAPI UI (try every endpoint here) |
| <http://127.0.0.1:8000/healthz> | Liveness (always 200 once up) |
| <http://127.0.0.1:8000/readyz> | Readiness (503 unless DB+migrations ok) |
| <http://127.0.0.1:8000/metrics> | Low-cardinality counters/histograms |
| <http://127.0.0.1:8000/capabilities> | Honest "what can this deployment do" report |

With `ZERO_WORKERS_ENABLED=1` the same process runs the scheduler, delivery
drain and Telegram polling loops — one deployment makes approved work progress
autonomously.

---

## 6. CLI reference

```
zero-develop [--version]
zero-develop serve   [--host H] [--port P] [--env-file PATH]
zero-develop migrate             [--env-file PATH]   # apply pending migrations, print JSON
zero-develop check-config        [--env-file PATH]   # validate config; print redacted settings + capabilities
zero-develop reconcile           [--env-file PATH]   # run startup recovery once (provider/worktree/merge/delivery)
```

Exit codes: `0` success · `2` configuration error · non-zero otherwise.
`migrate` is idempotent — running it twice reports `{"applied": 0}` the second
time. Run it before first boot and after upgrades.

---

## 7. First-run walkthrough

### Development (fastest)

```bash
export ZERO_ENV=development
zero-develop migrate
zero-develop serve
```

Auth middleware is **off**: requests carry identity through plain body fields
(`"actor_id": "<zu_...>"`). Jump straight to §8.

### Production-style bootstrap

```bash
export ZERO_ENV=production
export ZERO_DATABASE_URL=sqlite:///var/lib/zero/prod.db
export ZERO_SECRET_KEY=$(python -c "import secrets;print(secrets.token_urlsafe(48))")
export ZERO_BOOTSTRAP_TOKEN=$(python -c "import secrets;print(secrets.token_urlsafe(48))")
zero-develop migrate && zero-develop serve
```

Create the first user and get a bearer token:

```bash
curl -X POST http://127.0.0.1:8000/auth/bootstrap \
  -H "x-zero-bootstrap-token: $ZERO_BOOTSTRAP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"display_name":"Alice"}'
# -> {"user_id":"zu_...","bootstrap":true,...}

curl -X POST http://127.0.0.1:8000/auth/tokens \
  -H "Content-Type: application/json" -d '{"user_id":"zu_..."}'
# -> {"token":"<opaque>","expires_at":...}   (stored hashed, 24h expiry)

# All subsequent calls:
curl http://127.0.0.1:8000/users/zu_... -H "Authorization: Bearer <token>"
```

Tokens are random 32-byte strings stored **only as SHA-256 hashes** with 24 h
expiry. Revoke with `DELETE /auth/tokens/current`.

---

## 8. The full governed workflow

Every step below is a real endpoint, shown as curl (dev mode: no auth header).
`$B` is `http://127.0.0.1:8000`; jq optional.

### 8.1 Identity & project

```bash
OWNER=$(curl -s -X POST $B/users -H 'Content-Type: application/json' \
        -d '{"display_name":"Alice"}' | jq -r .id)

PROJECT=$(curl -s -X POST $B/projects -H 'Content-Type: application/json' \
        -d "{\"name\":\"Apollo\",\"owner_id\":\"$OWNER\"}" | jq -r .id)

# add a member (roles: owner|member|viewer)
curl -X POST $B/projects/$PROJECT/members -H 'Content-Type: application/json' \
  -d '{"user_id":"zu_...","role":"member","actor_id":"'$OWNER'"}'

# link a Telegram/Discord platform identity to this human (optional)
curl -X POST $B/users/$OWNER/external-identities -H 'Content-Type: application/json' \
  -d '{"platform":"telegram","external_id":"12345678","external_username":"alice"}'
```

Permissions are enforced server-side from the membership matrix:
owner=all 16 permissions; member=everything except tool/secret/member
management and audit view; viewer=`project.view`,`execution.view_diffs`,
`cost.view`.

### 8.2 Discuss → plan → revise

```bash
EVENT=$(curl -s -X POST $B/projects/$PROJECT/conversation-events \
  -H 'Content-Type: application/json' \
  -d '{"actor_id":"'$OWNER'","source":"web",
       "origin_kind":"authenticated_human",
       "content":"Add OAuth login to the service."}' | jq -r .id)

PLAN=$(curl -s -X POST $B/projects/$PROJECT/plans \
  -H 'Content-Type: application/json' -d '{"actor_id":"'$OWNER'"}' | jq -r .id)

curl -X POST $B/projects/$PROJECT/plans/$PLAN/revisions \
  -H 'Content-Type: application/json' -d '{
    "actor_id":"'$OWNER'",
    "objective":"Add OAuth login",
    "scope":["auth/"],
    "acceptance_criteria":["Login works end-to-end"],
    "constraints":[],
    "risks":["Token storage"],
    "unresolved_questions":[],
    "source_event_ids":["'$EVENT'"]}'
```

Rules enforced durably: revisions are immutable (new revision = new number),
every `source_event_id` must be a real authenticated-human event in this
project, and approvals use optimistic concurrency.

### 8.3 Approve (creates the handoff)

```bash
APPROVE=$(curl -s -X POST $B/projects/$PROJECT/plans/$PLAN/approve \
  -H 'Content-Type: application/json' \
  -d '{"actor_id":"'$OWNER'","expected_revision_number":1,
       "idempotency_key":"approve-r1"}')
HANDOFF=$(echo "$APPROVE" | jq -r .handoff.id)
```

* `expected_revision_number` **must equal the plan's current revision** or you
  get a stale-revision error (this is the human-approval fencing).
* Duplicate approvals with the same idempotency key return the same handoff.
* Rejection mirror: `POST .../reject`.

### 8.4 Create the execution graph

```bash
EXEC=$(curl -s -X POST $B/projects/$PROJECT/handoffs/$HANDOFF/executions \
  -H 'Content-Type: application/json' -d '{
    "actor_id":"'$OWNER'",
    "task_specs":[
      {"key":"impl","objective":"Implement OAuth flow",
       "permitted_scope":["auth/"],"expected_evidence":["diff","test_report","exit_status"]}
    ],
    "dependency_specs":[]
  }' | jq -r .id)
```

* Up to 256 tasks / 1024 dependency edges; cycles rejected up-front.
* `expected_evidence` labels supported by the runtime: `provider_response`,
  `transcript`, `artifact`, `diff`, `test_report`, `exit_status`, `stdout`,
  `stderr`, `source_snapshot`.
* Creation is idempotent per handoff — replaying returns the same execution.

### 8.5 Execute

**Autonomous mode** (workers enabled): the hosted scheduler claims unclaimed
handoffs, builds a single `"implementation"` task when none was supplied,
drains ready tasks through the model/tool loop, advances reviews, enqueues
result deliveries — every `ZERO_SCHEDULER_INTERVAL_SECONDS`.

**Manual mode** (or immediate kick):

```bash
curl -X POST $B/projects/$PROJECT/executions/$EXEC/run-ready \
  -H 'Content-Type: application/json' -d '{
    "actor_id":"'$OWNER'",
    "lease_owner":"worker-1",
    "provider":"fake",                 # or openai-compatible / anthropic
    "model_name":"fake-standard",      # or gpt-4o-mini / claude-sonnet-4
    "agent_scope":"main_worker"
  }'
```

The runtime loop: initial model call → up to `max_tool_rounds` (default 8,
max 32) tool rounds with per-round lease renewal → if still unresolved, **one
final toolless nudge request** asking for a summary → evidence capture →
fenced completion. Cancellation mid-flight ends the task `cancelled`
(propagate with `POST .../executions/$EXEC/cancel`).

One poisoned task does not starve siblings: batch failures are isolated.

### 8.6 Integration review & controlled merge

For repository-backed executions the scheduler auto-creates the review and
(combined test command configured) runs combined tests in a detached temp
worktree merging **all** source branches:

```bash
# create/inspect manually if you prefer
curl -X POST $B/projects/$PROJECT/integration/reviews \
  -H 'Content-Type: application/json' \
  -d '{"execution_id":"'$EXEC'","source_task_ids":["task_..."],"actor_id":"'$OWNER'"}'

curl -X POST $B/projects/$PROJECT/integration/reviews/$REVIEW/combined-test \
  -H 'Content-Type: application/json' \
  -d '{"command":"pytest","args":["-q"],"timeout_seconds":300,"actor_id":"'$OWNER'"}'

# checks passed -> human proposes, approves, then executes the merge
curl -X POST $B/projects/$PROJECT/integration/proposals \
  -H 'Content-Type: application/json' \
  -d '{"integration_review_id":"'$REVIEW'","execution_id":"'$EXEC'",
       "source_task_ids":["task_..."],"actor_id":"'$OWNER'"}'

curl -X POST $B/projects/$PROJECT/integration/proposals/$PROPOSAL/approve ...
curl -X POST $B/projects/$PROJECT/integration/proposals/$PROPOSAL/execute ...
```

`execute_merge` performs a real `git merge --no-ff` of every source branch,
runs `git diff --check`, commits as `Zero Integration`, then CAS-advances the
target branch (`update-ref old→new`) with crash-window reconciliation on next
boot. Failures roll the ref back.

### 8.7 Result delivery

Terminal executions enqueue deliveries to every enabled interface binding;
the delivery worker drains them through Telegram/Discord with exponential
backoff (cap 1 h). Pull manually anytime:
`GET /projects/{id}/result-deliveries` and `POST .../result-deliveries/drain`.

---

## 9. Providers & model routing

* Registered adapters appear in `GET /providers`; models resolve lazily and
  cache capability metadata (`GET /providers/{provider}/{model}`).
* Tool calls cross two trust boundaries: arguments are model output, validated
  against the registry's JSON Schema before anything runs. Every request
  advertises **real argument schemas** (never empty stubs).
* Requests are deduplicated by scoped SHA-256 payload hash; explicit
  idempotency keys reject payload mismatches; unknown outcomes land in an
  operator queue: `GET /projects/{id}/providers/requests/unknown` then
  `POST .../requests/{rid}/reconcile {"resolution":"confirmed_not_dispatched"
  | "confirmed_dispatched"}`.
* Usage accounting keeps four token classes (input/output/cache-write/cache-read)
  and computes Decimal cost estimates against the versioned pricing catalog;
  reconciliation writes `reconciled_cost_usd` separately
  (`POST .../providers/usage/...` see `/docs`).

**Planner:** with a provider configured, inbound human conversation events can
be turned into proposed revisions automatically by the messaging path
(`PlannerService`); REST clients typically author revisions explicitly as in
§8.2.

---

## 10. Tools, grants & worktrees

Built-in tools (registered automatically outside tests):
`read_file`, `write_file`, `run_command`, `capture_diff` (+ `echo` available
for experiments).

```bash
# grant a tool to a scope (tool.manage permission required)
curl -X POST $B/projects/$PROJECT/tool-grants -H 'Content-Type: application/json' \
  -d '{"tool_id":"<from GET /tools>","agent_scope":"main_worker",
       "max_invocations":100,"timeout_seconds":120}'
# revoke immediately
curl -X DELETE "$B/projects/$PROJECT/tool-grants/<tool_id>?agent_scope=main_worker"

# direct synchronous invocation (what the agent loop does internally)
curl -X POST $B/projects/$PROJECT/tool-invocations -H 'Content-Type: application/json' \
  -d '{"tool_name":"run_command","agent_scope":"main_worker",
       "input_data":{"command":"pytest","args":["-q"],"timeout_seconds":120}}'
```

Worktree safety model: private root (chmod 700), branch `zero/<wt_id>` per
task, scrubbed environment (`PATH=/usr/bin:/bin`, `HOME=<worktree>`, git
prompts/config disabled), output capped and truncated with markers, process-
group SIGKILL on timeout, symlink/traversal rejection everywhere, removal is
non-force so dirty worktrees refuse to die. Recovery marks orphaned active
worktrees `interrupted` and sweeps terminal-execution worktrees through
cleanup (bounded 25/pass).

---

## 11. Agent types & project knowledge

```bash
curl -X POST $B/projects/$PROJECT/agent-types -H 'Content-Type: application/json' -d '{
  "name":"Backend Implementer",
  "responsibility":"Implements approved backend tasks with tests.",
  "memory_scope":"Backend conventions and migration history.",
  "permitted_tools":["read_file","write_file","run_command","capture_diff"],
  "model_policy":{"provider":"anthropic","model":"claude-sonnet-4"},
  "context_budget_tokens":120000,
  "max_concurrent_instances":2 }'
```

Assign `agent_type_id` on a TaskSpec (or let the scheduler default to the
oldest active type). The type **narrows** tools, overrides provider/model,
caps context budget and atomically caps concurrency.

Knowledge records feed retrieval:

```bash
curl -X POST $B/projects/$PROJECT/agent-types/$TYPE_ID/knowledge \
  -H 'Content-Type: application/json' \
  -d '{"kind":"decision","content":"We use Pydantic v2 models for all API IO.",
       "state":"approved"}'
```

Topology operations (`split`, `merge`, `retire`, snapshot rollback) live under
the same prefix — rollback restores types **and** knowledge routing from
snapshots.

---

## 12. RAG & artifacts

```bash
# ingest (only 'approved' documents enter the FTS index)
curl -X POST $B/projects/$PROJECT/rag -H 'Content-Type: application/json' -d '{
  "source_type":"design_doc","source_id":"oauth-v2","title":"OAuth design v2",
  "content":"...", "state":"approved"}'

# lexical search (FTS5 BM25; queries are sanitized — operators stripped)
curl -X POST $B/projects/$PROJECT/rag/search \
  -H 'Content-Type: application/json' -d '{"query":"oauth token storage","limit":5}'

# rebuild index from canonical documents
curl -X POST $B/projects/$PROJECT/rag/rebuild
```

Artifacts are content-addressed and immutable per project; evidence artifacts
carry provenance binding them to exact execution/task/attempt ids.

---

## 13. Telegram / Discord interfaces

1. **Store the bot credential as a secret** (value never returns from the API):

```bash
curl -X POST $B/projects/$PROJECT/secrets -H 'Content-Type: application/json' \
  -d '{"name":"tg-bot-token","secret_type":"token","value":"<BOT TOKEN>",
       "actor_id":"'$OWNER'"}'
TOKEN_REF=$(... | jq -r .id)   # sec_...
```

2. **Bind a chat/topic:**

```bash
curl -X POST $B/projects/$PROJECT/interfaces -H 'Content-Type: application/json' -d '{
  "platform":"telegram","chat_id":"-1001234567890","topic_id":null,
  "bot_token_ref":"'$TOKEN_REF'","is_enabled":true,"actor_id":"'$OWNER'"}'
```

3. **Choose a transport**

*Webhook:* set `ZERO_TELEGRAM_WEBHOOK_SECRET`, point Telegram at
`POST /webhooks/telegram/{project}/{binding}`; signature verified
(constant-time) **before** binding lookup. Discord: register the public key;
Ed25519 headers verified likewise (PING answered automatically).

*Polling:* keep `ZERO_WORKERS_ENABLED=1`; the polling worker resolves each
enabled binding's token from the secret store and long-polls Telegram with
per-binding cursors and poison-update skipping.

Inbound messages become canonical events (deduped by update id), resolve to a
verified external identity **and** project membership, then either log as
conversation intake or drive inline approve/reject buttons via opaque one-shot
callback tokens bound to the exact plan revision.

Outbound results render through the same bindings with bounded, redacted text.

---

## 14. Observability & recovery

* **Health:** `/healthz` (liveness), `/readyz` (503 unless migrations ok),
  `/capabilities` (truthful availability + reasons), worker status embedded in
  capabilities payload.
* **Metrics:** `provider_requests_total{result}`, `provider_request_duration_ms`,
  `task_transitions_total{result}`, `tool_invocations_total{result}`,
  `tool_invocation_duration_ms` — low-cardinality labels only
  (`result∈{success,failure,error,cancelled,denied}`,
  `source∈{web,telegram,discord,system,internal}`). Project ids deliberately
  excluded (use audit).
* **Audit:** append-only redacted events, filterable via
  `GET /projects/{id}/audit?operation=&limit=`.
* **Secret canary sweep:** scans audit/artifacts/context/deliveries tables for
  credential-shaped strings (see `/docs` for the admin trigger).
* **Backups:** encrypted-at-rest SQLite dumps via `BackupService`
  (HKDF-derived key from `ZERO_SECRET_KEY`, atomic replace, restore into an
  isolated staging DB with integrity/FK/schema checks). Currently exposed at
  the service layer — wire an operator job to call it; restore verifies before
  swapping.
* **Recovery:** `zero-develop reconcile` runs stale-provider-request
  requeue/unknown marking, stuck executions, interrupted worktree marking,
  partial-compaction completion, inflight-merge crash reconciliation, stale
  delivery reset and the bounded worktree cleanup sweep. It also runs
  automatically at app startup (non-test).

---

## 15. Running the verification suite

```bash
# deterministic suite (553 tests; POSIX-only ones self-skip on Windows)
ZERO_ENV=test PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python -m pytest -p no:cacheprovider

# static gates
ruff check --no-cache src tests scripts
ruff format --check --no-cache src tests scripts
PYTHONDONTWRITEBYTECODE=1 python -m compileall -q src tests scripts
```

Live smoke (boots the real server on :8765 and walks the whole governance
chain over HTTP — the script used to certify this release):

```powershell
$env:PYTHONPATH="$PWD\src"; python <path-to>\real_http_verify.py
```

Release artifact gate (wheel/sdist contents, credentials scan, unsafe paths):

```bash
python scripts/validate_release_artifacts.py dist/
```

---

## 16. Production checklist

- [ ] External reverse proxy with TLS; never expose uvicorn raw.
- [ ] `ZERO_ENV=production`, explicit `ZERO_DATABASE_URL` on persistent volume
      (SQLite WAL is enabled; single-writer — scale reads via replicas of the
      file, not concurrent writers).
- [ ] `ZERO_SECRET_KEY` ≥32 bytes from a secret manager; rotating it requires
      re-storing secrets (key-ids make failures explicit, not silent).
- [ ] `ZERO_BOOTSTRAP_TOKEN` issued once, bootstrap endpoint disabled after
      first user (manual provisioning flag off).
- [ ] `ZERO_WORKTREE_ISOLATION_MODE` stays `disabled` unless a genuine sandbox
      backend exists — production **refuses** `host_bounded` by design.
- [ ] `ZERO_COMBINED_TEST_COMMAND` pinned to your repo's suite.
- [ ] Back up: scheduled `BackupService` dump + rehearsed restore (staging
      checks run automatically during restore).
- [ ] Monitor `/readyz`, `/metrics`, error-rate via logs (secret-redacted
      formatter is always on).
- [ ] Superevise the process (systemd/NSSM); workers live in-process, so one
      unit suffices.

---

## 17. Known limitations

Declared honestly by the tree itself (see
`docs/CURRENT_STATE_LEDGER.md`): no live provider/Telegram/Discord
qualification yet (all protocol code is deterministic-test verified);
SQLite-only persistence; host-bounded execution is not a hostile-code sandbox;
planner creates one plan per event (no multi-plan reasoning); task
decomposition is currently a single `"implementation"` task unless callers
supply specs; token counting is bytes÷4 (gates only, not billing); memory
delta artifacts are reserved but not written; browser/mobile/accessibility
passes not performed. Treat as **development-grade**: excellent for piloting
and internal tooling, not yet for hostile multi-tenant production.

---

## 18. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `ConfigError` at boot, exit 2 | Missing/short `ZERO_SECRET_KEY`, missing DB URL, or prod auth flags — run `zero-develop check-config` for the precise message. |
| `/readyz` 503 | Migrations missing → `zero-develop migrate`; wrong DB path → check `ZERO_DATABASE_URL`. |
| `409/400 … StaleRevision` on approve | Someone proposed revision N+1 — re-read `GET .../revisions` and approve the current number. |
| Tasks stay `ready`, nothing executes | Workers disabled (`ZERO_WORKERS_ENABLED=0`) or no provider registered — call `run-ready` manually or configure a provider; check `GET /capabilities`. |
| Task ends `paused`, blocker "awaiting automatic retry" | `ZERO_TASK_MAX_ATTEMPTS` budget not exhausted — the scheduler will requeue; raise the budget or requeue manually via API/CLI reconcile. |
| Task `blocked` with "provider outcome unknown" | Operator decision required: `GET .../providers/requests/unknown` then `POST .../requests/{rid}/reconcile`. |
| `run_command` fails with policy error | Command not in `ZERO_WORKTREE_ALLOWED_COMMANDS`, or timeout > cap — bare names only, comma-separated allowlist. |
| Merge proposal execute refuses | Target moved (stale ancestry), dirty target worktree, or combined checks not passed — re-run combined tests, then retry; crash windows self-heal via `reconcile`. |
| Telegram bot silent | Binding disabled, secret ref revoked, external identity unverified/unlinked, or user not a project member — event log at `GET .../interfaces/events` states exactly which (`ignored_unlinked`, `denied`, ...). |
| Webhook returns 503 | Verifier secret/public key unset for that platform in this process. |
| Where did my worktree go? | Terminal executions get swept (25/boot, non-force). Dirty trees survive by design. |

---

*Generated from the remediated tree; request/response shapes match
`src/zero/app/api.py` schemas. When in doubt, `/docs` on your running instance
is the executable truth.*
