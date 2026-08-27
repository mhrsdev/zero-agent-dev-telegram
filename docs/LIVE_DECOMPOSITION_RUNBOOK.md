# LIVE Decomposition Runbook (#15 closer)

One healthy GLM window closes the last open gate of task #15
(`executions_completed`). Everything else is already implemented,
tested, and committed: `ZERO_DECOMPOSITION_ENABLED`,
S7 per-model typo-rate analytics (JSONL ledger -> results.json ->
`/metrics` snapshot), Hermes gaps G1/G2/G3/G9.

## Execute

```bash
LDG_SOFT_DEADLINE=540 python3 /home/z/my-project/scripts/live_decomposition_group.py
```

The driver is fully self-contained (single Popen tree):

1. Spawns the hardened AI bridge (`scripts/ai_bridge/server.mjs`, free port)
   and pings GLM until 2 consecutive probes pass -- **0 team messages are
   burned while the window is closed**.
2. Boots the real server (ephemeral `.env`: `ZERO_DECOMPOSITION_ENABLED=1`,
   `ZERO_TOOL_APPROVAL_MODE=off`, `ZERO_TICK_PARALLEL_EXECUTIONS=4`,
   decomposition analytics sink on SQLite-backed paths).
3. Runs migrations, bootstraps operator + 3 linked users + project +
   supergroup binding, then posts Telegram-transport-shaped updates:
   2 decomposable TASK messages (fa + en) and 2 NORMAL chatter messages.
4. Classifies intake from the durable event log, approves every proposed
   plan revision idempotently (`expected_revision_number`), ticks the
   scheduler until quiescent or deadline.
5. Emits evidence to `scripts/logs/live_group_decomp/`:
   `results.json`, `server.log`, `bridge.log`, `raw/`, `live_decomp_*.db`.

## Gates (all must PASS for exit 0)

| Gate | Meaning |
| --- | --- |
| task1_classified_and_proposed | fa TASK -> planner revision proposed |
| task2_classified_and_proposed | en TASK -> planner revision proposed |
| normals_no_proposal | >=2 chatter entries produced no revision |
| no_errored_intake | zero error entries in durable events |
| approvals_ok | >=2 revisions approved via REST |
| multi_task_executions | >=1 execution with >1 decomposed task |
| analytics_ledger_written | >=2 S7 outcome rows |
| forced_toolcall_path_used | native_first_ask / escalated_retry_ok / recovered_key_repair observed |
| executions_completed | every started execution reached `completed` |

## Upstream outage protocol

Probes are cheap and safe:

```bash
python3 /home/z/my-project/scripts/glm_wait_loop.py 500 85   # exit 0 == window open
```

If the free-tier window stays closed (sustained HTTP 429 across hours),
do NOT burn the run into it; re-probe later. History shows sustained
outages of multiple hours between resets.
