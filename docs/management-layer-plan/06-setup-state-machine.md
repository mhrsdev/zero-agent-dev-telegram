# 06 — Setup State Machine (durable wizard core)

One engine, three renderers (CLI prompts, TUI screens, GUI pages). State is
durable: every answered step is persisted to `state/setup-draft.json` with
schema `{version, current_step, data{}, validations{}}` — crash/resume safe,
"Save draft / Resume later" is inherent.

## Step registry (id → contract)

| # | id | inputs | validate (side-effectful ✱) | rollback/cleanup |
|---|----|--------|------------------------------|------------------|
| 1 | welcome | – | show install summary; no-op | – |
| 2 | environment | install mode, paths | disk/RAM/cpu/ports/dns/time checks (read-only) | – |
| 3 | version | channel stable/beta | resolve tag/branch exists ✱ | pin file |
| 4 | telegram_mode | bot_api (v1) | fixed choice; user_session → explicit "not available in this release" | – |
| 5 | telegram_credentials | token (masked) | getMe ✱ → store secret ref via SecretRefService ✱; capture bot username | delete created ref on back |
| 6 | router | primary/fallback ordering prefs | catalog ids exist | – |
| 7 | provider_add | pick known/custom, base_url, key(masked) | auth probe (models list) ✱; store key ref ✱ | ref delete on back |
| 8 | provider_test | – | minimal completion ✱ (fixed 8-token prompt "ping", never user data); tool/stream probes best-effort ✱ | – |
| 9 | model_assign | primary + fallbacks | models ⊆ provider catalog or manually-confirmed | – |
| 10 | access_mode | owner_only/users/groups/users_and_groups/public(+confirm) | policy sanity | – |
| 11 | groups | discover→confirm loop | getChat/getUpdates probe ✱; title shown; store GroupPolicy rows | remove added policies on back |
| 12 | agents | per-group default agent + features | agent ids valid | – |
| 13 | memory_storage | compaction %, db location | path writable ✱ | – |
| 14 | websearch | enable?, provider, key | probe search call ✱ when enabled | refs cleaned on disable |
| 15 | privacy | telemetry opt-in (default off) | – | – |
| 16 | updates | channel confirm, auto_apply? | – | – |
| 17 | backup_policy | schedule/retention/include_secrets | write test backup to target dir ✱ | delete test archive |
| 18 | final_validation | – | run full config validation + engine boot smoke (in-process create_app+readyz) ✱ | – |
| 19 | test_message | optional target chat_id | sendMessage "Zero is ready" ✱ | – |
| 20 | complete | – | commit draft→config.yaml (atomic), write last-good, reload engine config, print next actions | prior config kept as restore point |

## Semantics

- **Transitions:** `next`, `back`, `skip`(optional only), `retry`,
  `cancel`. `back` after side-effectful steps triggers the listed cleanup
  handler before moving. `cancel` keeps draft, marks state `aborted`;
  nothing committed.
- **Never lose validated work:** each step stores
  `data[step_id] = {value, validated_at}`; later failures cannot clear
  earlier steps.
- **Redaction:** every log line for steps 5/7/14 runs the engine's
  `redact_sensitive_text`; values rendered masked (`tok_…abcd`).
- **Idempotency:** re-running a ✱ probe is safe (getMe/test completion are
  read-only; secret store upserts by name).
- **Completion commit order:** validate → atomic write config.yaml → copy
  last-good → ConfigService.reload() → engine env adapter applied → healthz
  probe → mark `complete`.
- **Resume:** `zero setup --resume` loads draft, jumps to `current_step`.

## Machine-readable contract

```python
class Step(Protocol):
    id: str; title: str; optional: bool
    def render(self) -> StepInfo            # prompt/help/redacted-current
    def validate(self, value) -> StepResult # errors[] + warnings[]
    def apply(self, value) -> None          # side effects (✱)
    def rollback(self) -> None
```

SetupService persists transitions and exposes the same API to TUI/GUI/CLI;
UIs contain **zero** business logic.
