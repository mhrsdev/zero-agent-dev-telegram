# GAP 12 Design — Rate-Limit-Aware Task Retry

Status: design accepted · Phase 1

## Problem

`SchedulerService.run_once` requeues failed tasks immediately while
attempt budget remains (`scheduler_service.py` §auto-retry block).
Provider rate limits (429 + Retry-After) therefore cause tight retry
loops that amplify the original failure. Hermes parity: exponential
backoff with decorrelated jitter and Retry-After honoring
(`agent/retry_utils.py::jittered_backoff`,
`parse_retry_after_seconds`), capped.

## Architecture

Backoff computation is a pure function; scheduling state lives on the
task row. No new services.

```
compute_retry_delay(attempt_number, error_text="") -> int  (seconds)
    ├─ Retry-After parsed from error text ("(retry_after=N)") → min(N, 3600)
    └─ base = min(60 * 2^(attempt-1), 3600)
       delay = base + uniform_random(0, base // 2)   (jitter)
```

- `Retry-After` extraction matches the format the provider layer already
  embeds in rate-limit errors (`_rate_limit_detail` in
  `provider_adapter.py`: `" (retry_after=N)"`) so no plumbing change is
  needed between provider errors and the scheduler.
- Jitter uses `random.uniform`; injectable RNG seed via parameter for
  deterministic tests.

### Scheduler changes

In the auto-retry loop of `SchedulerService.run_once`:

1. Before requeueing, read the task's `next_retry_at` from
   `blocker_reason` metadata (below). If it is in the future → skip.
2. Compute delay from attempt count (`len(attempts)`) and the last
   failed attempt's `error_message`.
3. Requeue, then stamp `next_retry_at` onto the task record.

### Where to store next_retry_at

The Task dataclass has `blocker_reason: str | None` but adding a real
column is cleaner and queryable. Decision: **new nullable column
`next_retry_at TEXT` on `tasks`** plus a migration
(`0030_task_retry_backoff.sql`). The `Task` dataclass gains a matching
field with default `None` — additive, all existing constructors keep
working.

## Data model changes

```sql
ALTER TABLE tasks ADD COLUMN next_retry_at TEXT;
```

- `ExecutionRepository.update_task_state` unchanged; new repository
  method `set_task_next_retry_at(task_id, value)` executed inside the
  scheduler's transaction path via WorkerService facade
  (`WorkerService.schedule_retry(...)`) so authorization/audit stays in
  one place (audit op `"task.retry_scheduled"`, reason redacted).

## API surface

- `GET /executions/{id}/tasks` response items gain
  `"next_retry_at": "…\u200b| null"` (serialized only when non-null;
  schema addition is backward compatible for existing consumers).
- New public helpers (pure functions, exported from
  `src/zero/app/retry_backoff.py`):
  ```python
  RETRY_BASE_DELAY_SECONDS = 60
  RETRY_MAX_DELAY_SECONDS = 3600
  def compute_retry_delay(attempt_number: int, error_text: str = "", *, rng=random) -> int
  def parse_retry_after_seconds(error_text: str) -> int | None
  ```

## Security considerations

- Error text passed to backoff parsing is redacted through
  `redact_sensitive_text` before storage in audit records (existing
  convention); `error_message` on attempts is already bounded to 4096.
- Delay values are clamped (≥0, ≤3600) so a hostile Retry-After cannot
  park a task forever or poison integer fields.

## Test strategy

- Pure-function tests: formula at attempts 1..N, cap at 3600, jitter
  bounds (seeded rng), Retry-After honored, malformed Retry-After
  ignored, negative values rejected.
- Scheduler tests (fake worker/repos): failed task inside its budget is
  not requeued before `next_retry_at`; after the timestamp passes it is;
  permanent failure exhausts budget and blocks; Retry-After from a
  simulated rate-limit error message wins over the formula.
- API test: tasks endpoint surfaces `next_retry_at`.

## Migration path

One additive migration `0030_task_retry_backoff.sql`. Old rows have
`NULL` ⇒ behave exactly as today (immediate eligibility).

## Rollback strategy

Revert scheduler gating (one block); column is harmless if left in
place, drop optional.

## Acceptance criteria

- Failed tasks wait exponentially longer between retries.
- Retry-After from provider rate limits is honored (capped 3600s).
- Tasks stuck in permanent failure eventually exhaust budget and block.
- Existing tests unaffected; suite green.
