# 01 — Repository Audit (zero-agent-dev-telegram @ main)

## 1. Identity of the codebase

Python 3.11+ package `zero-develop` (`pyproject.toml`), console script
`zero-develop = zero.cli:main`. Layered per ADRs: `domain/` → `app/` services
→ `persistence/` (SQLite + 30 SQL migrations) → `web/` + `adapters/`.
Test suite: 563 passing / 16 platform-skips on the remediated tree.

## 2. Today's install path (the thing we are replacing)

`README.md:105-123` — manual: install Git **and uv** → `git clone` →
`uv venv --python 3.12` → activate → `uv pip install -e ".[dev]"` →
`export ZERO_ENV=development` → `zero-develop` → open `/web`, `/docs`.
`scripts/run_dev.sh:17-34` is a POSIX dev helper (PYTHONPATH=src, uvicorn
--reload); it explicitly defers supervision ("not a deployment story").
No Dockerfile, no compose, no systemd unit anywhere in the tree.
`.env.example` documents variables but is never auto-loaded
(`README.md:167-169`; `main.py:57-68` reads process env only).

## 3. Configuration surface today

Parsed in `src/zero/config.py load()`:
`ZERO_ENV` (required), `ZERO_DATABASE_URL`, `ZERO_LOG_LEVEL`,
`ZERO_SECRET_KEY` (prod ≥32B), `ZERO_AUTH_REQUIRED`,
`ZERO_BOOTSTRAP_TOKEN` (+`ZERO_ALLOW_MANUAL_PROVISIONING`),
`ZERO_WORKTREE_ALLOWED_COMMANDS`, `ZERO_WORKTREE_ISOLATION_MODE`,
`ZERO_OPENAI_API_KEY/_BASE_URL/_MODEL/_TIMEOUT_SECONDS`,
`ZERO_ANTHROPIC_API_KEY/_BASE_URL/_MODEL/_TIMEOUT_SECONDS`,
`ZERO_TASK_MAX_ATTEMPTS`, `ZERO_PROVIDER_MAX_ATTEMPTS`,
`ZERO_TELEGRAM_WEBHOOK_SECRET`, `ZERO_DISCORD_APPLICATION_PUBLIC_KEY`,
`ZERO_WORKERS_ENABLED`, `ZERO_SCHEDULER_/DELIVERY_/POLLING_INTERVAL_SECONDS`,
`ZERO_COMBINED_TEST_COMMAND/_TIMEOUT_SECONDS`, `ZERO_WORKTREE_ROOT`.
Fail-closed rules in `_enforce_fail_closed_rules()`; prod refuses dev DBs,
missing keys, host-bounded execution.

Secrets model is already right: bot tokens are **Fernet-encrypted project
secrets** referenced by bindings (`api.py:741-773`;
`interface_service.py:227-243`) — never env, never returned by API.

## 4. Telegram runtime reality (what "go-live" costs today)

1. Non-test env with `ZERO_WORKERS_ENABLED=1`.
2. Store token: `POST /projects/{id}/secrets`.
3. Binding: `POST /projects/{id}/interfaces`
   `{platform:"telegram", chat_id, topic_id?, bot_token_ref, is_enabled}`.
4. Link human: `POST /users/{id}/external-identities`
   `{platform, external_id, external_username?}` → row written with
   `verified=False`.
5. **DEAD END**: verification exists only as
   `identity_service.py:355-401 verify_external_identity()`; grep of
   `api.py` shows **no route** calls it. Polling/webhook intake resolves
   identities and denies unverified senders as `ignored_unlinked`
   (`interface_service.py:450-474`). A new user cannot complete onboarding
   through any documented interface. *(Reproduced — doc 02 R5.)*
6. Webhook mode additionally requires `ZERO_TELEGRAM_WEBHOOK_SECRET` in the
   same process (`interface_transport_service.py:69-95`), else 503.

Access control beyond binding enable/disable: none. Gates are verified
identity ∧ project membership ∧ permission (`interface_service.py:429-502`);
static role matrix in `domain/authorization.py:102-121` (member holds
`agent.manage`, so binding management is not owner-exclusive). No group
allow-lists, no owner-only mode, no public-bot confirmation.

Polling resilience note: `background_workers.py` skips bindings whose token
fails to resolve **silently** (debug-level) — an onboarding trap with no
surface feedback.

## 5. Management affordances present vs required

| Required (spec) | Present today |
|---|---|
| one-command installer | ✗ |
| setup wizard | ✗ |
| full TUI | ✗ |
| local Web GUI | partial: operator web pages exist (`web/controller.py`: login/dashboard/users/projects/plans/executions/audit) but no wizard/providers/groups/usage/system pages, no setup-token auth model |
| CLI for automation | partial: `serve|migrate|check-config|reconcile` only (`cli.py:36-68`) |
| doctor / backup / restore / update / uninstall | ✗ (backup exists only as `BackupService` service-layer; recovery via `reconcile`) |
| usage/cost views | REST only (`api.py` providers/usage) |
| group & access policy UI | ✗ (no policy model at all) |

## 6. CI/packaging

`.github/workflows/ci.yml`: quality job (pip install -e .[dev], pytest 3.12,
ruff, compileall) + release job (clean-tree gate → git-archive build →
`scripts/validate_release_artifacts.py` → fresh venv wheel install → double
migration → loopback health smoke). Good bones to reuse for installer
artifact checksums and platform matrix extension.

## 7. Tests covering this plan's blast radius

`test_config.py` (fail-closed env), `test_smoke.py` (boot/health),
`test_http_phase2.py` (identity/secrets endpoints), `test_interfaces.py` +
`test_interface_remediation_red.py` (binding/intake flows),
`test_providers.py`/`test_provider_streaming.py`/`test_real_provider_adapter.py`
(provider contracts incl. retry/backoff added earlier),
`test_observability.py` (backup/restore service), `test_release_validator.py`.
New management code must add its own suites (doc 12) without weakening these.
