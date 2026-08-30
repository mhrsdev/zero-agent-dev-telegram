# Changelog

All notable changes to Zero Develop are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/); versions
are milestone-based rather than semver until 1.0.

## [Unreleased] — real-server hardening (live gateway + autonomous pipeline)

Fixes proven against a live server (`uvicorn zero.main:app`) driving a
real Telegram bot, a real OpenAI-compatible provider, and real
multi-agent executions; each item below corresponds to an observed
real-run failure.

### Fixed

- **Round-7 "FULLY" verification wave — inline keyboard, decomposition,
  approval** (real-credentials evidence: approval profile 21/21 phases,
  decomposition profile 13/13 phases, both against the live engine +
  real bot + real group + real claude-opus-5). Four real bugs were
  found and fixed by this wave:
  - **Webhook-delivered button presses were never answered on the Bot
    API.** The engine's webhook adapter holds no bot credential (tokens
    are per-binding secrets resolved at action time), so the round-7
    answer attempt died silently at `_api_url` with a swallowed
    `WebhookAuthError` — every press spun until Telegram's ~10s query
    timeout. `InterfaceTransportService.process_webhook` now owns the
    webhook-path acknowledgement: after the durable dispatch it
    resolves the binding credential and answers the press with the
    outcome toast (`✅ Plan approved` / `✖️ Plan rejected` /
    `⛔ Not allowed` / `⚠️ Failed — see logs`); the crash path is
    answered too, then the exception propagates. Presses from
    unresolved actors (strangers) are not answered — the durable denial
    is authoritative. Pinned by
    `test_webhook_press_answered_via_binding_credential` +
    `test_webhook_press_by_stranger_gets_no_credential_resolution` and
    proven live (answerCallbackQuery requests visible on the real Bot
    API; a REAL 200 toast observed when a real group member pressed a
    real card button through the polling path).
  - **One stale/expired callback query killed the whole polling
    worker.** `answerCallbackQuery` for an expired/already-answered
    query id returns HTTP 400 `QUERY_ID_INVALID`, which surfaces as a
    plain `RuntimeError` (ok=false), NOT an `AdapterError` — it
    propagated out of `poll_once` and out of `poll_forever`'s
    exception tuple, taking the bot offline. The acknowledgement is now
    best-effort in the strongest sense (no failure class can break
    intake or the polling loop). Pinned by
    `test_stale_query_id_400_never_kills_the_polling_worker`.
  - **`routing.primary_model` never reached task execution or
    decomposition.** The scheduler tick resolved its model from
    `settings.openai_model` (the gpt-4o-mini default) while config_sync
    aligned only the planner and the chat bridge. When the operator's
    gateway stopped serving the gpt-4o-mini default outright (every
    decomposition/task call died with a CDN-edge 403 while the aligned
    planner kept succeeding), approved plans could never execute.
    config_sync now pins the scheduler tick to `routing.primary_model`
    (`SchedulerService.set_tick_routing`) — one routing truth for
    planner, chat, decomposition, and task execution. Pinned by
    `test_config_sync_pins_scheduler_tick_to_routing_primary_model`.
  - **One transient CDN-edge 403 silently degraded every decomposition
    to the single-task fallback.** The decomposer's two prompt attempts
    escalate OUTPUT QUALITY, not transport resilience — a single
    transient gateway error returned `None` immediately. The decomposer
    now burns a bounded transport retry budget first
    (`transport_retries=4`, backoff 5/15/30/60s — sized to the
    observed multi-minute gateway flaps) before the mandatory fallback;
    definitive auth failures stay fail-fast. Pinned by
    `test_transient_edge_403_is_retried_not_degraded` /
    `test_transient_403_exhausting_budget_still_falls_back` /
    `test_auth_failure_stays_fail_fast`.
- **Approval boundary matrix** (service-level pins in
  `tests/test_telegram_approval_buttons.py`, 18 tests): the ✖️ Reject
  button runs the same durable pipeline as approve (plan → rejected,
  one-shot consumption); forged callback_data is a loud error with zero
  plan-state impact; a linked non-member is denied at the membership
  gate and a read-only `viewer` member is denied at the per-action
  permission check — both leave the token UNUSED for the legitimate
  approver; expired tokens are rejected; replayed tokens are
  idempotent; the outcome toast mapping is exact.
- **Round-7 E2E driver**: profile gate (`E2E_PROFILE=approval |
  decomposition | full`) splits the drive across 10-minute tool
  windows with incremental JSONL evidence (`evidence-<profile>.jsonl`)
  that survives a timeout kill; the P4c honesty fix (a "sent" failure
  notice is a FAIL) and the P4d multi-task-graph assertion
  (≥2 tasks, ALL completed) are shared predicates across profiles; the
  actionable request is an analysis deliverable so every decomposed
  task carries provider_response evidence (file-editing tasks fail
  closed in this sandbox by GAP-3 design — no docker/firejail backend
  — an environmental boundary, documented in the grade report).

- **SSE-only provider gateway killed every conversational reply with
  granted tools** (live round-5 finding): `api.justwoker.icu` answers
  every `chat/completions` request that declares `tools` with
  `text/event-stream` chunks EVEN WHEN the request did not ask for
  streaming. The toolless planner received clean JSON, but chat turns
  with granted tools (web_search/MCP) died with "provider returned
  invalid JSON" on every message. The OpenAI-compatible adapter now
  detects a forced-SSE body (content-type or `data:` prefix) and
  aggregates the delta stream into one canonical response — text
  concatenated in arrival order, tool-call fragments merged by index
  (ids kept, argument fragments concatenated), last `usage` and
  `finish_reason` win, `[DONE]` terminates; non-SSE garbage bodies
  still fail loudly. Hermes parity: `anthropic_adapter` documents the
  same effectively-SSE-only gateway class. Pinned by
  `test_openai_adapter_aggregates_forced_sse_body` /
  `test_openai_adapter_still_rejects_garbage_body` and proven live
  against the real gateway (`verify_sse_fix_live.py`: real SSE body →
  aggregated content + usage 87/814, finish_reason=stop).
- **MCP tool registration crashed engine restarts on a reused
  database** (live round-5 finding): every boot re-discovers the
  configured MCP servers and re-registers their tools; on an existing
  database the second registration hit `ToolAlreadyExistsError` and
  spammed warnings. Registration is now idempotent — an existing tool
  row with the same name has its description/input schema refreshed
  in place (schemas may legitimately change between restarts).
- **Round-5 E2E driver hardening** (real-run infrastructure): the
  driver resolves the live project/binding scope from the engine
  database instead of hardcoding ids minted by a previous setup run,
  and the setup deterministically pre-links the operator's Telegram
  identity through the real identity pipeline — on a live bot the real
  group delivers real messages around the clock and a real member's
  message could race the driver for the one-shot auto-link-owner
  bootstrap. `scripts/run_round5_e2e.py` boots the real engine as a
  detached subprocess, waits for health, drives all 16 phases, and
  tears the engine down — the whole real-credentials e2e is now a
  single reproducible command.
- **Polling `TransportError` wall on flaky/filtered networks** (reported
  session: `zero logs` showed `worker error: polling:ib_…: TransportError`
  every ~4 seconds for 8+ minutes, then sudden 200 OKs — the operator's
  egress path to api.telegram.org was dropping in and out). Four bugs
  made the outage undiagnosable and the log unreadable; each is pinned
  by `tests/test_transport_resilience.py` and was verified against the
  live Bot API and a deliberately dead egress path:
  - `TransportError` now carries a bounded, bot-token-redacted summary
    of the underlying cause (`provider transport failed after retries —
    ConnectError: [Errno 111] Connection refused`) instead of discarding
    the chained httpx exception; the polling worker logs that detail on
    the FIRST failure of a streak and compact type-only lines after.
  - Generic transport failures now earn a per-binding exponential
    backoff (2s doubling to 60s, reset on success) instead of
    hot-looping at the 1s polling interval — the previous behavior
    produced the observed warning every ~4 seconds.
  - After 3 consecutive failures a ONE-TIME actionable hint names
    `ZERO_TELEGRAM_PROXY_URL` / `HTTPS_PROXY` for filtered networks;
    the first successful poll logs `Telegram bot online: @username
    (id=…)` via getMe, and recovery after a failure streak is logged at
    INFO.
  - NEW: `ZERO_TELEGRAM_PROXY_URL` routes Telegram Bot API traffic
    (polling + outbound messages) through an explicit proxy —
    `http://`, `https://`, `socks5://`, `socks5h://` (the last resolves
    DNS through the proxy, which matters when local DNS is poisoned).
    Validated fail-closed at boot; credentials in the URL are masked in
    every repr/log path; `httpx[socks]` is now a base dependency. The
    shared messaging client also gets an explicit timeout budget
    (35s / connect 15s) instead of httpx's silent 5s default, and the
    doctor/wizard Telegram probes honor the same proxy so they exercise
    the exact egress path the engine will use.
- **Outbound sends could stall the delivery drain ~30s per message** on
  a slow/broken network (default 3 attempts x 10s). Outbound adapters
  now use one clean attempt with a real 30s budget; the durable
  delivery queue's exponential `retry_after` owns the second chance,
  and the recorded delivery error now carries the sanitized cause.
- **Telegram bot completely dead (engine database drift)** (reported
  session: bot did not respond even to `/start`; only the local web UI
  worked; engine log showed `SecretNotFoundError` for every configured
  `sec_...` reference). Root cause: the engine resolved its development
  database as `sqlite:///./zero_develop.db` — RELATIVE TO THE PROCESS
  CWD. `zero setup` stored the operator's secrets into the database it
  saw from its own directory; `zero start` later booted the engine from
  a different directory, silently created a fresh, secret-less database
  with a ghost "Zero Management" project, and every secret reference in
  config.yaml failed to resolve — so no provider was registered, no
  Telegram binding was ever created, and the bot could neither receive
  nor reply to a single message. Multi-layer fix, each layer verified
  with real processes:
  - `Settings.load()` now defaults its `.env` path to
    `$ZERO_HOME/.env` (previously the engine NEVER read the file the
    wizard and the key bootstraps persist values into); process
    environment variables still win.
  - `zero setup` pins the ABSOLUTE database URL it stored secrets into
    into `$ZERO_HOME/.env`, making the secret store location stable
    regardless of the directory later commands run from.
  - New `zero doctor --fix` capability: when configured references do
    not resolve, the doctor scans the known candidate databases
    (CLI-recorded usage history, `$ZERO_HOME`, its state dir and parent,
    the CWD and its one-level subdirectories) for the one that holds
    ALL of them, backs up `$ZERO_HOME/.env`, pins that database, and
    re-verifies through a fresh engine. The reference checks
    (`secret-references`, `database-drift`, `secret-key`) are new.
  - config sync self-heal: when a secret reference fails to resolve and
    `ZERO_TELEGRAM_BOT_TOKEN` / `ZERO_OPENAI_API_KEY` /
    `ZERO_ANTHROPIC_API_KEY` is set, the credential is stored into the
    encrypted store and config.yaml is repointed (survives restarts
    without the env var). Without a recovery path the failure is now a
    LOUD error naming `zero doctor --fix` instead of a scattered
    warning.
  - config sync repairs a STALE `owner_project_id` in config.yaml (a
    ghost project id persisted by the drifted engine made the
    owner_only policy gate deny every message even after the database
    repair).
- **Duplicate Telegram polling (HTTP 409 conflicts)**: with the drift
  repaired, `zero start` and `zero-develop serve` running side by side
  would both long-poll the same bot token — Telegram answers the loser
  with 409 forever and updates get split between two engines. A
  cross-process, per-token advisory lock (`$ZERO_HOME/poll-locks/`,
  Windows-safe, stale-lock stealing by pid liveness) makes the second
  engine SKIP polling with a clear one-time log; a genuine 409 from a
  foreign poller is now the typed `TelegramConflictError` and the
  polling worker backs off exponentially (5s doubling to 60s) instead
  of hot-looping.
- **Bot silent on /start and /help**: a healthy bot still produced no
  outbound artifact for commands — `/start` is the universal
  "is this bot alive" probe, so operators reasonably concluded the bot
  was still broken. `/start` and `/help` now get an immediate,
  best-effort welcome/help reply through the transport boundary,
  targeted at the chat the event actually came from (the polling-only
  binding's synthetic `chat_id="0"` must never receive a message).
- **Delivery path bypassed a configured Bot API gateway**: the
  result-delivery and command-reply transport built its Telegram
  adapter without `ZERO_TELEGRAM_API_BASE`, silently hitting
  api.telegram.org even when the setup/doctor probes and the polling
  worker honored the gateway. All outbound paths now share the escape
  hatch; the polling adapter honors it too.
- **`zero start`/`stop`/`status` broke on hosts where the `systemctl`
  binary exists but PID 1 is not systemd** (WSL, containers): the gate
  was the mere presence of the binary, so every systemctl call failed
  with "System has not been booted with systemd" and `zero start`
  started NOTHING. Systemd is now used only when it manages the machine
  AND the unit exists; the plain-process fallback runs otherwise.
- **`zero doctor` provider reachability probe hardcoded port 443**,
  failing every self-hosted gateway on a custom port or plain http; the
  probe now honors the base URL's scheme and port.
- **Dev banner lied about the database**: `zero-develop serve` printed
  "local SQLite at ./zero_develop.db" even when `$ZERO_HOME/.env` pinned
  an absolute database path; the banner is now printed from the RESOLVED
  settings and `_env_file_declares_zero_env` consults `$ZERO_HOME/.env`
  when no explicit `--env-file` is given.

### Fixed (earlier in this release)

- **Port-blind bugs in the serve/start pre-checks** (reported session:
  managed service running on 8000, operator ran ``zero-develop serve
  --port 8001`` and was refused with the false claim that "a foreground
  server cannot bind the same port"):
  - ``zero-develop serve`` refused whenever the managed service was
    running, regardless of the requested port. The pre-checks are now
    port-aware: a genuinely free port starts alongside the managed
    service (with an honest note that ``$ZERO_HOME`` state — database and
    Telegram poller — is shared), and only a real bind conflict is
    refused.
  - Both serve refusals suggested a hardcoded ``zero-develop serve
    --port 8001`` — the exact command the operator had just run, and in
    the busy-port branch possibly the port that just failed. The
    suggestion is now a port verified bindable at print time
    (``_suggest_free_port``), never the failing one.
  - The same-port refusal now names the managed service's ACTUAL bind
    (``on 127.0.0.1:8000``) instead of an assumed port.
  - ``zero start`` silently ignored ``server.host``/``server.port`` from
    config.yaml (the bind was hardcoded to 127.0.0.1:8000 in the
    busy-check, the spawn argv, and the /healthz probe). Both CLIs now
    resolve the managed bind through one shared helper
    (``zero.manage.cli._managed_bind``), so a configured port is honored
    end to end and the two CLIs can no longer disagree about where the
    managed service lives; a missing/invalid config still falls back to
    the loopback defaults so a fresh host stays startable.

- **The setup wizard's final "Send test message" step never sent anything**
  (reported console session, Windows): it collected a chat id, stored it in
  the draft, and moved on — nothing was delivered, yet the step read as
  verified; the CLI even printed the self-referencing transition
  ``ok -> test_message`` as the last line. A provided chat id now performs
  the real Bot-API ``sendMessage`` round-trip (new hardened probe
  ``telegram_send_message`` reporting the Telegram message id, with
  Telegram's own error description surfaced on failure), an empty chat id
  keeps the optional-step skip semantics, a resumed draft without a
  resolvable bot token soft-passes with a warning, and the wizard now
  prints "ok — setup complete" instead of the self-transition (same fix
  for the skipped-last-step path).

- **The wizard's "Enter=retry same answers" was a dead loop for
  deterministic validation errors** (same session: websearch enabled with
  the required provider id/key left empty — Enter failed identically
  forever). One identical failure now automatically re-asks the step's
  fields prefilled with the previous answers, while transient probe/network
  errors keep their one-keypress retry.

- **``zero-develop serve`` died on an ugly bind traceback when the service
  was already running** (WinError 10048 / EADDRINUSE in the reported
  session) and still exited 0. It now pre-checks the managed pid file and
  the port: a running managed service or a busy port prints an actionable
  refusal ("stop it first ('zero stop') or choose another port:
  zero-develop serve --port 8001") and exits 1.

- **``zero start`` spawned blindly**: no already-running guard (a second
  start overwrote ``zero.pid`` with a process doomed to die on the bind
  error) and no post-spawn verification — success was reported before the
  process had proven it survived startup. It now refuses when the service
  is already running, refuses when port 8000 is already occupied (checking
  whether the occupant is a healthy Zero service outside this pid file),
  and after spawning verifies liveness via ``Popen.poll()`` (the previous
  signal-0 probe never noticed a zombie child) plus a /healthz wait guarded
  against crediting a foreign service, with the log tail printed if the
  child dies. ``zero stop`` also stopped claiming "stopped" when nothing
  was running.

- **The dev-key banner lied on every start**: ``zero-develop serve`` printed
  "generated a development encryption key" even when an existing key was
  merely reloaded (operators reasonably read that as rotation; the key is
  never rotated) — the banner now distinguishes generated vs reused, and
  the .env persistence is idempotent instead of rewriting the file each
  run. The stale guidance ("run 'zero setup'") shown to operators who had
  already configured the installation now points at ``zero start``.

- **``zero-develop`` bare invocation printed argparse's terse "the
  following arguments are required: command"** instead of the full help
  (``zero`` already printed help) — both CLIs now share the same contract.

- **The websearch step accepted a provider id that could never validate**:
  ``ZeroConfig`` requires ``websearch.provider_id`` to reference a
  configured provider, but the wizard checked it nowhere, so an honest
  answer only exploded at commit — after all 18 steps were answered. The
  step now validates the id against the providers that will exist in the
  committed config (draft + existing), lists the available ids, explains
  the constraint in the prompt label, and ``model_assign`` warns when a
  fallback model equals the primary (no resilience).

- **Tool declarations fought the model instead of steering it** (real run,
  5-minute window 2026-08-28): ``capture_diff`` declared a zero-property
  object schema with ``additionalProperties=False``, so a frontier model
  that naturally passed arguments (``base``, ``paths``, …) failed input
  validation five consecutive times in one real task and burned its tool
  rounds; the model then tried to work around via ``delegate``, whose
  sub-agents correctly cannot call worktree tools, and finally recovered
  with ``git diff`` over ``run_command``. ``capture_diff`` is read-only and
  argument-free, so its schema now tolerates (ignores) extra keys and its
  description says "Takes NO arguments — call it with an empty object
  {}"; genuine zero-argument tools that still reject extra keys now say so
  plainly in the model-facing validation error; and the context-less
  worktree-tool denial names the policy ("delegation sub-agents cannot
  call worktree tools — the parent task must invoke them directly")
  instead of a mystery. The decomposer evidence guidance gained the
  missing aggregation rule: a task whose objective is to capture/produce
  the final diff requires ``["diff"]`` evidence (its artifact IS the
  diff), not ``["provider_response"]``.

- **The Telegram gateway could never receive a message**: the polling
  worker built its adapter with the default per-request HTTP timeout
  (10 s) while asking Telegram to hold the long poll open for 25 s —
  every long poll was aborted client-side, retried twice, and logged
  ``TransportError`` every ~33 s. The binding adapter now uses a
  per-request budget exceeding the long-poll hold (+10 s margin) and
  ``attempts=1`` (a completed long poll IS the wait). Healthy 25 s
  poll cycles verified live; real human messages now arrive and are
  policy-evaluated (``ignored_unlinked`` / ``ignored_disabled`` as
  designed).
- **Management layer silently skipped on every boot**: the backup
  daemon shutdown hook used the decorator form
  ``@app.router.on_shutdown``, but Starlette's ``Router.on_shutdown``
  is a plain list — "decorating" raised ``TypeError``, the broad
  except swallowed it, and the just-started daemon thread leaked each
  boot. Registered via ``app.router.add_event_handler`` instead; the
  catch-all warning now logs the error message, not just its type.
- **One refused tool call failed the whole task**: a raised
  ``ToolError`` from ``ToolService.invoke`` (e.g. ``run_command``
  refusing a non-allowlisted binary) propagated out of the agent
  tool loop and failed the task, even though invalid arguments,
  undeclared tools, and approval denials are recovered by feeding the
  model a structured error. Raised tool errors and denials now feed
  back as synthetic tool results (Hermes parity); the
  identical-failure loop breaker still bounds pathological retries.
- **The command allowlist was secret from the model**: ``run_command``
  described itself as "allowlisted" without naming the allowed
  binaries, so a frontier model naturally requested ``bash -c "…"``.
  The declaration now enumerates the exact permitted binaries and the
  no-shell rule, read from the enforcing service so advertisement and
  policy cannot drift.
- **Hidden ``pytest -q`` evidence command**: ``AgentRuntime``'s
  constructor defaulted ``test_command`` to ``("pytest", "-q")`` and
  ``build_services`` never passed one — every task whose
  ``expected_evidence`` required a test report failed with
  "command 'pytest' is not permitted". The evidence command is now
  explicit configuration (``ZERO_EVIDENCE_TEST_COMMAND``); unset means
  evidence-demanding tasks fail closed with a configuration hint.
- **Task worktrees could not see their dependencies' work**: every
  worktree branched from the bare repository default and succeeded
  worktrees never committed, so a "run the tests" task ran against a
  worktree with no test suite. Succeeded worktrees now commit their
  full state onto the task branch (evidence checkpoint), and a task's
  worktree branches from its succeeded dependency worktree branches
  (clean git merges for diamond DAGs; conflicts fail with a clear
  reason).
- **Bytecode noise satisfied diff evidence**: ``git add -A`` in the
  new checkpoint committed ``__pycache__/*.pyc``, so a task's diff
  evidence could pass on bytecode churn alone (a real task claimed
  completion without writing its file). New worktrees carry a
  worktree-local ``.gitignore`` committed as a hygiene baseline, and
  evidence commits stay clean.
- **Evidence validation broke on content deduplication**:
  ``store_artifact`` deduplicates by content hash, so a later attempt
  producing a byte-identical diff received the earlier artifact row
  whose ``provenance`` COLUMN carried the earlier task/attempt — and
  the validator rejected it ("evidence artifact does not belong to
  task"). Validation now honors the per-store ``artifact_provenance``
  rows (any row matching this task/attempt), matching the documented
  "deduplication does not merge provenance" contract.
- **The model could not see command output**: ``run_command`` returned
  only ``run_id``/``state``/``exit_code``/``artifact_ids`` — no
  stdout/stderr — so the agent could not read test failures or probe
  output and honestly reported objectives unmet. Bounded
  ``stdout``/``stderr`` are now declared result fields.
- **The model-facing render was capped at 500 characters**: a coding
  agent's ``read_file`` showed ~450 characters of a source file and
  every tool result was effectively invisible. The bounded render is
  now 20 000 characters (context-safe while carrying real content),
  with truncation still explicit.
- **Persisted tool schemas never evolved**: tool rows survive restarts
  while handlers re-bind in code; when a server-owned tool's declared
  schema changed, invocations failed output validation against the
  stale contract. The declaration is refreshed in lockstep on re-bind.
- **The interactive chat silently ran toolless**:
  ``ChatService._granted_tool_names`` called ``repo.get_tool`` — a
  method that has never existed (the accessor is ``get_tool_by_id``) —
  the degraded path swallowed the error, and no granted tool was ever
  declared to the chat model regardless of capability grants.
- **Decomposer evidence guidance matched fantasy, not reality**: the
  prompt's own example attached ``["diff","test_report","exit_status"]``
  to every code-changing task, so the first file-creation task failed
  its unittest run (no tests exist yet). The guidance now maps
  evidence to what can hold at each task's completion (read-only →
  ``provider_response``; pre-suite file work → ``diff``; the single
  suite-verification task → ``test_report``+``exit_status``, no diff).

## [Unreleased] — Hermes deep-read parity audit (2026-08-28)

A full cross-reference of this codebase against the audited Hermes
agent reference (nousresearch/hermes-agent — agent loop, tool
execution/safety, context compaction, error classification/fallback)
produced the following fixes; each is regression-tested (16 new tests
in ``tests/test_hermes_parity_audit.py`` plus updates) and most are
verified against the live server and real provider.

### Fixed

- **Model-level fallback routing did not exist**: the setup wizard
  writes ``routing.fallback_models`` into config.yaml, but the runtime
  consulted only provider-level fallbacks — with a single gateway
  adapter the chain was degenerate, so a primary-model outage failed
  every task despite configured alternatives. New
  ``ZERO_OPENAI_FALLBACK_MODELS`` (ordered, deduplicated) feeds
  ``ProviderService.set_fallback_models``; ``send_request_with_fallback``
  now routes through (provider, model) pairs — same-provider
  alternative models first, then other providers. Verified live three
  times: real gateway 524 storms on the primary model automatically
  advanced to claude-opus-4-8 and claude-opus-4-8-thinking before any
  task failure.
- **Auth failures never reached the fallback chain**: the
  OpenAI-compatible adapter raised a generic "HTTP 401" error that
  classified as ``invalid_request`` (terminal), so a bad/expired
  primary API key killed tasks even with a healthy fallback configured.
  401/403 now raise auth-flavored errors (matching the Anthropic
  adapter) and ``auth_failure`` is fallback-eligible (Hermes parity:
  fail fast, then failover).
- **The tracked-changes diff was silently empty forever (real bug
  #14)**: ``capture_diff`` invoked
  ``git --no-ext-diff --no-textconv diff <base>`` — but those flags
  belong to the ``diff`` subcommand, not git's global options, so git
  exited 129 with stderr swallowed and the "Tracked changes" section
  of every diff artifact was always empty (evidence showed only the
  untracked status section). Flag order fixed; a nonzero diff exit
  code is now surfaced in the artifact instead of masquerading as "no
  changes". Live r13 diff artifacts carry 200+ real tracked ``+``
  lines for the first time.
- **The hygiene baseline leaked into every task diff (real bug #15)**:
  once tracked diffs actually worked, the auto-committed worktree
  ``.gitignore`` baseline appeared as a tracked change in every
  task's diff — two tasks then "conflicted" on ``.gitignore`` and
  integration reviews demanded human decisions for server-managed
  infrastructure. ``base_revision`` is re-resolved after the baseline
  commit so diffs show only the task's own work.
- **Chained aggregation tasks could not prove diff evidence (real bug
  #16)**: the final "capture the whole diff" task branches from
  succeeded dependency branches whose evidence checkpoints already
  contain all earlier work — its incremental diff is empty even
  though the execution's change set is large, so the task failed with
  "required diff evidence contains no file change" (observed live in
  r10). ``capture_diff`` now falls back to a clearly-labeled
  cumulative diff against the repository's default base revision when
  a task changed nothing on top of its base.
- **Empty model responses completed tasks silently**: a response with
  neither content nor tool calls returned an empty deliverable (the
  transcript evidence dutifully recorded ``"content": ""``). The tool
  loop now runs a bounded empty-response ladder (nudge retries, Hermes
  parity) before accepting an empty terminal.
- **Identical-failure steering broke tool-call/result pairing**: the
  warn message was injected as a bare ``user`` message BETWEEN one
  batch's tool results — strict provider wire formats require tool
  messages to directly follow the assistant tool_calls turn. The
  steering now rides on the failing tool result as a bracketed
  suffix (Hermes parity).
- **Handler failures hid their reasons**: ``ToolService`` converted
  unexpected handler exceptions to a bare "Tool 'x' handler failed",
  leaving the model blind to which path was unreadable or which
  binary the policy refused. The error now carries the bounded,
  secret-redacted underlying reason (``Error executing tool 'x':
  <Type>: <detail>``); delegation and chat tool errors likewise
  include the reason instead of a bare exception class name.
- **Chat executed guessed arguments**: unparseable tool-call JSON was
  silently coerced to ``{}`` and the tool EXECUTED with empty inputs.
  Invalid arguments now return a structured
  ``invalid_tool_arguments`` error without invoking the handler
  (Hermes parity: guessed arguments are never executed).

## [Unreleased] — setup wizard UnicodeEncodeError (masked-key probe) + probe hardening

### Fixed

- **`zero setup` crashed at step 7/18 (Provider test)** with
  ``UnicodeEncodeError: 'ascii' codec can't encode character '\u2026'
  in position 11``: the wizard stores draft secrets *masked*
  (``sk-a…xyz`` — the mask's ellipsis is exactly header position 11 of
  ``Bearer sk-a…``) with the raw value under ``_raw``, but the
  provider-test validation probed the masked value read back from the
  draft. It now probes the raw secret; old drafts without ``_raw``
  fall back to the stored value so the probe's clean rejection
  applies (never a traceback).
- **Probes can no longer crash on bad secrets**: all five network
  probes (``telegram_get_me``, ``telegram_recent_chats``,
  ``openai_list_models``, ``openai_completion_probe``,
  ``anthropic_ping``) now sanitize/validate keys and tokens first
  (strip invisible paste artifacts: zero-width, NBSP, BOM; reject
  non-ASCII such as a literal ``…`` from a truncated copy) and treat
  *any* request-building/transport/body-parsing exception as a normal
  ``{"ok": false, "error": "…"}`` result. A non-JSON 200 body (proxy
  portal) no longer raises either.
- **Paste-safe secret input**: the CLI wizard strips invisible
  characters from pasted tokens/keys at the prompt and rejects values
  that still contain visible non-ASCII with an actionable message
  ("value looks like a truncated copy (contains '…') — paste the full
  token/key") BEFORE any network call; the same validation runs in
  ``SetupService.validate`` so TUI/GUI/non-interactive paths get it
  too.
- **Wizard retry menu on validation failure**: a failed step used to
  re-ask every field, so a transient probe error (``unreachable:
  ConnectError``) forced retyping the whole step. After a failure the
  wizard now asks ``[Enter=retry same answers · r=re-enter · b=back ·
  s=skip]``; re-entry pre-fills fields with the last attempted values.
- Regression suite ``tests/test_probe_hardening.py`` (12 tests) covers
  the masked-key replay of the reported crash end-to-end, dirty-secret
  rejection for every probe (no network, no raise), raw-vs-masked
  probing, and the retry menu. Full suite: 932 passed.

## [Unreleased] — `zero logs` crash + service-status Windows fix

### Fixed

- **`zero logs` crashed for everyone** (``AttributeError: 'Namespace'
  object has no attribute 'lines'``): the subparser defined only
  ``-n`` (argparse derives dest ``n``) while ``cmd_logs`` read
  ``ns.lines``. The parser now defines ``-n/--lines`` with an explicit
  dest; a non-positive ``-n 0`` no longer falls into Python's
  ``[-0:] == [0:]`` slice trap (which dumped the entire log).
- **`zero logs` ignored the file log whenever journalctl existed**:
  the handler exec'd ``journalctl -u zero`` even when the service runs
  as a plain process (``zero start`` writes ``zero.pid``/``zero.log``)
  and no systemd unit is installed — operators saw "No entries" while
  ``zero.log`` had fresh content. journalctl is now used only when the
  zero unit actually exists (``systemctl cat zero`` succeeds); the
  file branch is the fallback.
- **`zero status` killed the running service on Windows**:
  ``os.kill(pid, 0)`` is a harmless liveness probe on POSIX, but on
  Windows os.kill maps any non-CTRL signal to ``TerminateProcess`` —
  so merely checking status terminated the bot. Status detection now
  uses a query-only process handle (``OpenProcess`` +
  ``WaitForSingleObject``) on Windows; POSIX keeps signal-0. Stale or
  garbage pid files are reported as ``stopped (stale pid N)`` instead
  of crashing.
- **EOF on prompts**: a closed/piped stdin reaching an
  ``input()``/``getpass()`` prompt outside the wizard (e.g. ``zero
  telegram add-bot`` in a script) escaped as a raw traceback; ``main``
  now exits 2 with a one-line message (the wizard already handled
  Ctrl+C/EOF from the previous fix).
- **`providers add --probe` was a no-op**: ``store_true`` with
  ``default=True`` could never disable probing; it is now
  ``--probe/--no-probe`` (default: probe).
- **`backup-status`** no longer crashes when ``last-backup.json`` has
  a null ``epoch``.
- Regression tests for all of the above added to
  ``tests/test_manage_cli.py`` (10 new tests; full suite 920 passed).

## [Unreleased] — TUI crash + setup wizard deadlock fixes

### Fixed

- **TUI (`zero tui`) render crash**: every data panel overrode
  ``_render(self, payload)``, which collides with the private
  ``Widget._render()`` framework hook that Textual >= 0.86 calls with
  zero arguments during layout — the compositor died with
  ``TypeError: OverviewPanel._render() missing 1 required positional
  argument: 'o'`` on any screen refresh. The per-panel hook is renamed
  to ``_render_payload`` and ``refresh_data`` now renders data-layer
  errors in-panel instead of killing the app.
- **TUI panel switching (`DuplicateIds`)**: panels were re-mounted with
  the fixed id ``main`` after ``remove_children()``, which is
  asynchronous in Textual >= 1.0 — the stale widget still held the id,
  so pressing keys 2–9 crashed with ``DuplicateIds``. New panels mount
  first (id-free) and old panels are pruned by explicit reference; 'r'
  now refreshes every panel type including the providers DataTable;
  admin login for the chat stream moved off the UI event loop (the TUI
  no longer freezes until HTTP timeout).
- **Setup wizard deadlock (`zero setup`)**: the interactive driver only
  mapped a handful of steps and fed an empty value dict to every other
  step — typing the exact valid answer ``bot_api`` at ``telegram_mode``
  still failed validation in an infinite loop (same for the silently
  dropped ``environment`` answer and the unmapped ``model_assign``
  step, which deadlocked next). The wizard is now form-driven from the
  shared ``WIZARD_STEPS`` specs: it shows each field's label, available
  options, defaults and required flags, accepts option indices and
  forgiving variants (``Bot-API`` → ``bot_api``), can skip optional
  steps ('s'), navigates back ('b') at any prompt, and pauses cleanly
  on Ctrl+C with ``zero setup --resume``.
- **Wizard could never store secrets on a fresh host**: the engine
  bridge loaded settings strictly, so ``zero setup`` failed with
  ``ZERO_ENV is required`` the moment it tried to store the bot token —
  before the wizard had any .env to read. The management CLI now loads
  with the documented development fallback (explicit ZERO_ENV still
  wins), matching ``zero-develop serve``.
- **Wizard answers silently dropped**: groups answered in any UI never
  reached the committed config (only a synthetic ``confirmed`` list was
  read while every UI wrote a flat ``{chat_id, title}`` payload);
  openai_compatible providers committed with an empty models list
  (discovery wrote only ``discovered_models``). Both shapes are now
  consumed and discovery populates ``models``.
- **final_validation step now validates**: it builds the full config
  from the draft so schema/cross-field errors surface at a named step
  with a readable message instead of a traceback from ``commit()``.
- **`zero setup --non-interactive` without `--step`** now fails with
  guidance instead of attempting a guaranteed-to-fail commit;
  ``--resume`` reports where the draft stands.
- **`zero start` on a fresh home** created ``zero.pid``'s directory
  after opening the log inside it (FileNotFoundError); the daemonized
  spawn also crashed on Windows (``start_new_session`` is POSIX-only)
  and now uses ``DETACHED_PROCESS`` there.
- **Bare `zero`** now prints the full help (exit 2) instead of
  argparse's terse missing-argument error; Ctrl+C anywhere in the CLI
  exits 130 without a traceback.
- Regression suite added: ``tests/test_tui_wizard_regressions.py``
  (17 tests) guards the render-hook collision, the bot_api session from
  the bug report, step-form coverage, input parsing and final
  validation.

## [Unreleased] — Hermes-parity hardening (G1 + G2)

### Added

- Per-call tool approval gate (GAP 8b/G2, Hermes parity): opt-in via
  ``ZERO_TOOL_APPROVAL_MODE=manual``. Durable ``tool_approval_decisions``
  table (migration 0031) is both pending queue and decision record;
  service semantics port Hermes' layered approvals — hardline floor
  denies catastrophic arguments under any allowlist, deny rules outrank
  allows (``grain=always`` denial escalates to a TOOL-WIDE wildcard),
  standing always-allows key on canonical argument hash (tool-wide row
  supported), session grants scope in-process per execution, and the
  pending window carries a TTL so the runtime never blocks forever
  (model receives ``approval_pending``/``approval_denied`` as
  structured tool errors instead of dying). REST surface:
  ``GET /projects/{id}/tool-approvals`` (project.view) and
  ``POST .../tool-approvals/{request_id}/resolve`` (tool.manage);
  disabled deployments answer 409. Golden route tables updated.

### Changed

- AgentRuntime execution loop resilience (GAP 8b/G1, Hermes parity):
  malformed/unparseable or non-object tool arguments and undeclared
  tool names no longer raise-and-kill the attempt — the model receives
  structured error payloads (with declared-tool surface revealed) and
  can self-correct; guessed arguments are never executed.
  ``finish_reason=length`` together with unparseable arguments discards
  the broken assistant turn and re-asks with doubled ``max_tokens``
  (bounded 2 boosts, cap 32768) before anything executes. An
  identical-failure breaker counts repeated identical tool failures,
  injects an explicit change-approach steering note at the third
  failure and falls through to the summary nudge at five; terminal
  errors distinguish breaker-trip from round-budget exhaustion.

## [Unreleased] — Forced tool-call path (S7) and production env preset

### Added

- Canonical ``tool_choice`` support end to end:
  ``normalize_tool_choice`` folds modes (`auto`/`none`/`required`) and
  forced-function shorthands into one canonical shape;
  OpenAI-compatible and Anthropic adapters render it per protocol
  (nested `{type:function,function:{name}}` vs `{type:auto|any|tool}`);
  payloads stay byte-identical when unset, request hashes change only
  when set, provider-fallback reconstruction preserves it
  (task forcing survives a failover), and Anthropic's missing `none`
  mode fails loudly instead of silently doing nothing.
- Decomposition is now wired into the production composition root
  (services builder): the scheduler receives a real `TaskDecomposer`
  over the shared provider service, so `ZERO_DECOMPOSITION_ENABLED=1`
  takes effect on live servers (previously the flag existed but no
  decomposer was ever constructed — flipping it was a silent no-op).
- S7 recovery analytics (`zero.app.decomposition_analytics`): every
  decompose() outcome is recorded with attempts used, rescued
  near-miss dependency repairs (raw typo + Jaccard score), escalation,
  legacy degradation and single-task fallback facts, aggregated PER
  MODEL. Headline metric `typo_rate_per_graph` tracks per-model slug
  discipline over time; JSONL evidence sink via
  `ZERO_DECOMPOSITION_ANALYTICS_PATH` keeps durable audit lines from
  live servers without schema changes. Repair planning is factored
  into pure `plan_dependency_repairs` / `apply_dependency_repairs`
  while `repair_dangling_dependencies` keeps its exact contract.
- Decomposition ladder deepening (GAP 10 hardening):
  strict system prompt plus a single forced `emit_task_graph` tool
  call replaces reliance on free-text JSON; an escalated re-ask with
  even stricter phrasing covers one bad reply; deterministic recovery
  of structured output repairs near-miss dependency keys (unique-best
  snake_case Jaccard >= 0.5, duplicate edges collapsed) and
  normalizes dependency order before any re-ask is spent; providers
  without native tools (capability gate or gateway 4xx on tools)
  degrade once to the legacy text contract; transient failures stop
  the ladder immediately; idempotency keys are attempt-scoped.
- Live GLM evidence (real bridge to GLM-class model): all three probe
  plans produced validated multi-task DAGs on the first forced ask
  (10–22 tasks, 11–23 edges, 9.8s–23.8s), ahead of legacy free-text
  duration; two previously-failing raw responses recover exactly via
  key repair (`build_vendor_dashboard` -> declared key). Evidence:
  `scripts/logs/s7/{summary.md,probe_results.jsonl,raw/,bridge.log}`,
  harness `scripts/s7_decomposition_probe.py`, transport
  `scripts/ai_bridge/server.mjs`.
- `.env.example` restructured (core/workers/providers/execution/
  interfaces) with a go-live **production preset**; every knob name
  cross-checked against source, retry/backoff semantics documented,
  previously-undocumented operational knobs surfaced
  (`ZERO_DECOMPOSITION_ENABLED`, sandbox executor, PG pool bounds).

### Tests

- New `tests/test_s7_tool_call_decomposition.py`: 30+ cases covering
  canonicalization, both wire formats (incl. stream path omission),
  hash sensitivity/fallback propagation through a real service stack,
  extraction preference order, the full ladder matrix, repair rules,
  and degradation semantics.

## [Unreleased] — HTTP API decomposition

### Changed

- `src/zero/app/api.py` (2,801 lines) split into the
  `zero.app.routers` package: sixteen per-domain router modules
  (`auth`, `identity`, `authorization`, `secret`, `tool`, `audit`,
  `plan`, `execution`, `artifact`, `topology`, `provider`,
  `worktree`, `integration`, `interface`, `webhook`, `health`) plus
  shared `deps.py` (`authorized_actor`/`request_project_actor`;
  45 renamed call sites) and `models.py` (the `_StrictRequest`
  request-model family). `api.py` retains application assembly only
  (304 lines), with the sixteen `register_*` call sites kept in
  their original registration order.
- Public HTTP behavior was pinned before any motion by
  `tests/test_api_route_surface.py`: all 89 JSON routes, a
  duplicate-registration guard, and an OpenAPI contract that also
  covers the 19 management-web operations which reach the schema
  through the include wrapper. Green before, during, and after the
  split.
- Test-surface follow-up: the anti-spoofing test resolves
  `StoreArtifactRequest` from its canonical module
  (`zero.app.routers.models`) instead of the old `app.api`
  attribute surface; the assertions themselves are unchanged.

## [Unreleased] — engineering pass (analysis, fixes, consolidation)

### Fixed

- **Wizard group discovery crashed with `NameError`**:
  `probes.telegram_recent_chats` referenced an undefined module
  global; it now uses the shared `_telegram_base()` helper. Covered
  by a regression test driving a local HTTP stub through
  `ZERO_TELEGRAM_API_BASE`.
- **Pool misconfiguration masked by an install hint**: pool bounds
  are validated before the `[pg]` capability gate, so
  `ZERO_PG_POOL_MIN > ZERO_PG_POOL_MAX` reports the real error even
  on hosts without psycopg.
- **Release gate rejected every build**: the hand-maintained expected
  migration list stopped at 0028 while the tree ships 32 migrations;
  the set is now derived from the migrations directory, and a
  regression test guards against both list drift and a silently
  empty gate.
- **CI release smoke asserted the wrong CLI contract**: bare
  `zero-develop` exits in argparse now that subcommands are required;
  the fail-closed probe and server start both invoke
  `zero-develop serve` explicitly.
- **Load-flaky provider retry test**: the wall-clock budget
  (`elapsed < 2.0s`) measured loopback latency under load; the test
  now records `time.sleep` inputs and asserts Retry-After: 0
  produces exactly two zero-second sleeps.

### Changed

- Dev dependencies now include `httpx2>=2.0.0`: starlette >=1.0
  prefers it for `TestClient`, and its fallback warning tripped the
  repo's fail-closed warnings policy on fresh installs.
- `$ZERO_HOME` resolution consolidated into
  `zero.manage.core.config.zero_home`; seven duplicated readers were
  removed with unchanged behavior.

### Removed

- Accidentally committed SQLite sidecar files (`zero_develop.db-shm`,
  `zero_develop.db-wal`); ignore patterns extended to `*.db-shm` /
  `*.db-wal`. A byte-level scan found no secrets in the blobs.

### Documentation

- README corrected (Python floor is 3.11+, clone URL, `serve`
  subcommand, LICENSE reference); current-state ledger repopulated.

## [Unreleased] — independent audit fixes (Phase 3–17 findings)

Every fix has a reproduction + regression test under `tests/test_audit_*`
plus targeted suites; full evidence in the audit report.

### Fixed — Critical / High

- **Telegram intake crashed whenever managed config set
  `access.owner_project_id`** (`_CfgView` walrus/del leftover raised
  UnboundLocalError on every event). Groups now flow as plain dicts and
  `policy.build_gate` normalizes both dict- and object-shaped groups.
- **Owners were never recognized by the access-policy gate**: the owner
  lookup called a nonexistent repository method and the broad except
  swallowed it; now calls `list_external_identities_for_user` and logs
  lookup failures.
- **`POST /admin/providers/{id}/test` required no session or CSRF**,
  letting anyone who could reach the loopback port trigger paid provider
  probes. Now guarded like every other mutating admin route.
- **Engine bearer middleware made `/admin` unreachable in production**
  (two auth systems collided). `/admin/*` is exempt from the bearer
  gate; the GUI keeps its own scrypt-password + CSRF scheme.
- **`zero setup` could never finish**: no secret store was wired, so
  commit always refused with "secrets not stored". The CLI now persists
  secrets through the encrypted engine store, bootstraps
  `ZERO_SECRET_KEY` into `$ZERO_HOME/secret.key` + `.env` (0600), routes
  non-interactive steps through validation, and reports commit failures
  as clean exit-code-2 messages instead of tracebacks.

### Fixed — Medium

- `zero doctor` crashed with a raw YAML parser error on corrupted
  config.yaml; it now reports a failing `config` check.
- Three CLI commands (`capabilities`, `backup-daemon`, `backup-status`)
  had parsers but were unreachable from dispatch.
- Wizard silently dropped collected values: fallback-models CSV,
  agents default agent for groups, updates auto-apply, group discovery
  token field name. The unwired compaction-threshold field was removed
  rather than pretending to persist it.
- TUI hardcoded admin port 8787 while every server start used 8000;
  both now honor `ZERO_PANEL_PORT`.
- Dashboard linked to nonexistent `/web/projects/new`.
- Password change did not invalidate existing admin sessions; sessions
  are now purged on rotation. Login brute-force lockout added
  (5 failures / 10 min per client IP).
- `_ensure_setup_code` crashed first-run bootstrap when `$ZERO_HOME`
  did not exist.
- CLI engine bridge leaked one real HTTP client per invocation in dev
  mode; transports are closed after wizard secret operations.

### Changed

- Doctor `--fix` no longer claims automated fixes were applied.
- GUI usage loader logs query failures instead of rendering empty
  tables silently.
- Plugins receive real managed config and a name-scoped secret facade
  (management project only) at composition time.
- `probes.telegram_get_me` honors `ZERO_TELEGRAM_API_BASE` (self-hosted
  Bot API gateways / tests).
- README configuration table documents all new environment variables
  and optional extras.

## [Unreleased] — production-readiness gap closure (GAPs 1–12)

Design documents for every gap: `docs/gap-designs/` (committed before
any implementation).

### Added

- **GAP 11 — real token counting** (`zero/manage/core/tokenizer.py`,
  `[tokenizer]` extra): tiktoken exact counts for known GPT families
  (`o200k_base`, `cl100k_base`) with module-level encoding cache;
  bytes÷4 fallback preserved everywhere else. Threaded through
  `estimate_tokens`, the compaction fit ladder, context-builder
  budgets, retrieval scoring, and a new pre-flight
  `estimate_request_tokens`.
- **GAP 12 — rate-limit-aware task retry**
  (`zero/app/retry_backoff.py`, migration `0030`): exponential backoff
  (60 s × 2ⁿ capped at 1 h) with jitter; provider Retry-After honored;
  `tasks.next_retry_at` column + scheduler gating + API exposure on
  `GET /executions/{id}/tasks`.
- **GAP 5 — client-facing SSE streaming**: `ExecutionStreamHub`
  fan-out; provider-level `stream_observer` tap
  (`text_delta`/`tool_call`/`done`) that leaves durable bookkeeping
  unchanged; `AgentRuntime.run_task(..., stream_callback=...)`;
  `GET /admin/executions/{id}/stream` with 15 s keepalive heartbeats;
  GUI live-stream panel (fetch + ReadableStream) and TUI Chat screen.
- **GAP 6 — interactive chat endpoint**: `ChatService` ephemeral
  single-turn completions through the normal provider chain with
  optional granted-tool rounds, token-bucket rate limiting
  (`ZERO_CHAT_RATE_LIMIT_PER_MIN`, default 10), usage recorded via the
  standard path; `POST /admin/chat/{project_id}` JSON endpoint plus an
  admin chat panel.
- **GAP 9 — memory delta artifacts**
  (`zero/app/memory_delta.py`): "Accepted decisions" / "Blockers or
  failures" sections of LLM compaction summaries become durable
  KnowledgeRecords linked by a memory-delta artifact and recorded in
  the reserved `memory_delta_artifact_id`; opt-in per agent type via
  `model_policy["memory_delta_enabled"]`; deterministic fallback
  summaries never extract.
- **GAP 10 — LLM task decomposition**
  (`zero/app/task_decomposition.py`): validated JSON task graphs
  (≤256 nodes, ≤1024 edges, acyclic) cached per plan revision;
  scheduler falls back to the historical single task when disabled
  (default) or on any failure.
- **GAP 3 — production sandbox executors**
  (`zero/app/executors/`): `CommandExecutor` protocol with
  HostBounded/Docker/Firejail backends; Docker runs use no-network,
  pid/mem/cpu caps, no-new-privileges, cap-drop ALL (+CHOWN/SETUID),
  non-root uid, single worktree bind mount, watchdog kill on timeout;
  fail-closed availability probing at composition;
  `ZERO_SANDBOX_EXECUTOR=none|docker|firejail`; production permits
  host_bounded mode only with a genuine sandbox selected; capability
  report names the active executor.
- **GAP 2 — PostgreSQL backend**
  (`zero/persistence/pg_connection.py`, `dialect.py`,
  `migrations_pg/`, `[pg]` extra): pooled psycopg backend mirroring
  the SQLite facade (SAVEPOINT nesting, dict rows with positional
  access, sqlite3 exception mapping so all repositories stay
  backend-agnostic); bounded SQLite→PG SQL translation incl. all 105
  RAISE-guard triggers → plpgsql functions; generated committed
  `migrations_pg/*.sql` via `scripts/gen_pg_migrations.py`;
  dual-dialect migration runner with advisory-lock fencing;
  `postgresql://` URLs accepted only when psycopg is importable
  (fail-closed otherwise); `ZERO_PG_POOL_MIN/MAX` (2/20); optional
  compose `postgres` service; container tests marked `pg_integration`.
- **GAP 8 — subagent delegation** (`zero/app/delegation.py`):
  runtime-owned `delegate` tool; isolated in-process child contexts
  with fresh conversations and intersection-only tool narrowing
  (workspace tools excluded by default); depth cap 3 via ContextVar;
  child provider requests tagged `sub_agent_type` so
  `is_whole_tree=False` keeps whole-tree aggregation correct;
  structured error payloads never crash the parent.
- **GAP 7 — MCP client + plugin registry**
  (`zero/manage/core/mcp_client.py`,
  `zero/manage/plugins/registry.py`, `[mcp]` extra): MCP stdio
  JSON-RPC transport (initialize/tools list/call); tools registered as
  `mcp_<server>_<tool>` through the standard grant/redaction/audit
  pipeline; plugin discovery from `$ZERO_HOME/plugins` +
  `/opt/zero/plugins` with system→user alphabetical load order,
  `register(manage_context)` contract, and per-plugin failure
  isolation; sample plugin `examples/plugins/echo_upper.py`.
- **GAP 4 — user-session Telegram mode**
  (`zero/adapters/user_session.py`, `[session]` extra): Telethon-backed
  adapter gated on explicit `ZERO_TELEGRAM_MODE=user_session` AND
  importability; same NormalizedEvent intake/access-policy gate as Bot
  API; outbound 30/min token bucket (cap 60);
  `run_session_login` disclaimer → phone → OTP → 2FA flow held entirely
  in memory, returning the session string for encrypted storage.
- **GAP 1 — live integration qualification**
  (`tests/integration_live/`, `.github/workflows/live-tests.yml`,
  `docs/LIVE_TESTING.md`): six double-gated live tests (getMe /
  sendMessage / poll / OpenAI / Anthropic / incremental streaming)
  against the real production adapters; dispatch-only CI workflow.

### Changed

- Environment-compatibility hardening required by current toolchains:
  bodyless-204 routes drop PEP563 `-> None` annotations (fastapi),
  secret request payloads use `SecretStr` (pydantic ≥2.10 warning
  compliance), provider HTTP contract test parses JSON bodies instead
  of asserting transport-specific separators.

### Security

- Docker sandbox: no network namespace, dropped capabilities,
  non-root uid, worktree-only bind mount.
- User-session: OTP/2FA material never persisted or logged; session
  strings stored only as Fernet-encrypted secrets.
- Chat/stream endpoints require admin auth; prompts/responses are not
  logged; tool outputs pass existing redaction.
