"""S5 — RUN THE COMPLETE BOT + SERVER LIVE FOR 5 MINUTES (real Telegram + real LLM).

Drives the full system through the live server's own workers for exactly a
5-minute window while sampling liveness evidence every 15 seconds:

  t=0      bot outbound marker (real Telegram sendMessage to the group)
  thread   pipeline driver (s3 style, fresh TASK_KEY textcase_5min):
             gateway intake -> planner LLM -> casual non-actionable probe
             -> human approval -> handoff -> server scheduler/decomposition
  main     15s samples: /healthz, getUpdates poll cycles (log bytes),
           provider_requests delta, result_deliveries delta, execution/task
           states, notable log lines (completions / deliveries / fallbacks)
  t=300    final 5-minute evidence summary (recorded to state.json)

Idempotent + resumable: safe to re-run; the server keeps running afterwards.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
import urllib.request

sys.path.insert(0, "/home/z/my-project/scripts/realrun")
from env_common import (  # noqa: E402
    BOT_TOKEN,
    GROUP_ID,
    MODEL,
    TG_SENDER_ID,
    build_real_services,
    management_project,
    read_state,
    record,
    setup_env,
)
setup_env()

from zero.domain.interfaces import NormalizedEvent  # noqa: E402

TASK_KEY = "textcase_5min"
RUN_SECONDS = 300
LOG_PATH = "/home/z/my-project/scripts/realrun/server.log"
DB_PATH = "/home/z/my-project/zero-real-home/engine.db"

MASSIVE_TASK = (
    "Team, quick follow-up package: build `wordwrap` in this repo. Requirements:\n"
    "1. wordwrap/wrap.py with wrap_text(text, width) — greedy word wrap, "
    "never splits a word, collapses extra spaces; standard library only.\n"
    "2. wordwrap/__init__.py re-exporting wrap_text with __all__.\n"
    "3. tests/test_wrap.py with unittest coverage: empty text, word longer "
    "than width, exact width, multiple spaces, normal paragraph.\n"
    "4. Run: python3 -m unittest discover -s tests -v — must pass.\n"
    "5. Capture the final diff of all created/modified files."
)

CASUAL_CHAT = (
    "btw team I pushed the onboarding docs update, nothing urgent — also who "
    "wants coffee after standup?"
)


def bot_send(text: str) -> int | None:
    """Real outbound Telegram message from the bot (marker + liveness proof)."""
    import httpx

    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": GROUP_ID, "text": text},
            timeout=15,
        )
        data = r.json()
        return (data.get("result") or {}).get("message_id")
    except Exception as exc:  # noqa: BLE001
        print(f"    bot_send failed: {type(exc).__name__}: {exc}")
        return None


def db_counts() -> dict:
    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        pr = db.execute("SELECT count(*) FROM provider_requests").fetchone()[0]
        dl = db.execute("SELECT count(*) FROM result_deliveries").fetchone()[0]
        dl_sent = db.execute(
            "SELECT count(*) FROM result_deliveries WHERE state='sent'"
        ).fetchone()[0]
        return {"provider_requests": pr, "deliveries": dl, "deliveries_sent": dl_sent}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# pipeline driver thread (gateway -> planner -> casual probe -> approval)
# ---------------------------------------------------------------------------
def pipeline_driver(results: dict) -> None:
    try:
        settings, services = build_real_services()
        project = management_project(services)
        owner_id = project.owner_user_id

        event_id = read_state(f"{TASK_KEY}_event_id") or f"realrun-{__import__('uuid').uuid4().hex[:12]}"
        record(f"{TASK_KEY}_event_id", event_id)

        def find_revision():
            conv = services.plans.get_conversation_event_by_external(
                project_id=project.id, source="telegram",
                external_event_id=event_id, actor_id=owner_id,
            )
            if conv is None:
                return None
            return services.plans.find_revision_by_source_event(
                project_id=project.id, event_id=conv.id, actor_id=owner_id, source="telegram"
            )

        revision = find_revision()
        if revision is None:
            print("[pipe] REAL gateway intake -> planner LLM (claude-opus-5)")
            result = services.interfaces.process_inbound_event(
                NormalizedEvent(
                    platform="telegram", external_event_id=event_id,
                    external_actor_id=TG_SENDER_ID, chat_id=GROUP_ID,
                    topic_id=None, event_kind="message", content=MASSIVE_TASK,
                )
            )
            print(f"[pipe] {result.processing_result}: {result.processing_detail[:120]}")
            if result.processing_result != "processed":
                results["intake"] = result.processing_detail[:200]
                return
            revision = find_revision()
            if revision is None:
                results["intake"] = "planner judged non-actionable (unexpected)"
                return
        results["intake"] = "ok"
        record(f"{TASK_KEY}_plan", {
            "plan_id": revision.plan_id.value,
            "revision_id": revision.id.value,
            "objective": revision.content.objective,
        })
        print(f"[pipe] revision {revision.id.value} objective: {revision.content.objective[:100]}")

        if not read_state(f"{TASK_KEY}_casual_checked"):
            cas_event = f"realrun-{__import__('uuid').uuid4().hex[:12]}"
            try:
                res = services.interfaces.process_inbound_event(
                    NormalizedEvent(
                        platform="telegram", external_event_id=cas_event,
                        external_actor_id=TG_SENDER_ID, chat_id=GROUP_ID,
                        topic_id=None, event_kind="message", content=CASUAL_CHAT,
                    )
                )
                tail = res.processing_detail[-90:]
                results["casual"] = tail
                record(f"{TASK_KEY}_casual_checked", tail)
                print(f"[pipe] casual chat -> {tail}")
            except Exception as exc:  # noqa: BLE001
                results["casual"] = f"error {type(exc).__name__}"

        full = services.plans.get_plan(
            revision.plan_id, project_id=project.id, actor_id=owner_id
        )
        if full.current_state != "approved":
            approval, handoff = services.plans.approve_revision(
                plan_id=revision.plan_id, project_id=project.id, actor_id=owner_id,
                expected_revision_number=revision.revision_number,
                idempotency_key=f"realrun-approve-{event_id}",
            )
            record(f"{TASK_KEY}_handoff_id", handoff.id.value)
            results["approval"] = f"approved -> handoff {handoff.id.value}"
            print(f"[pipe] approved -> handoff {handoff.id.value} (server scheduler owns it)")
        else:
            results["approval"] = "already approved"
    except Exception as exc:  # noqa: BLE001
        results["pipeline_error"] = f"{type(exc).__name__}: {exc}"
        print(f"[pipe] ERROR {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
def main() -> int:
    t_start = time.time()
    t_start_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t_start))
    print(f"=== S5 COMPLETE 5-MINUTE LIVE RUN start={t_start_iso} ===")

    # byte offset into server.log at run start (for incremental log reading)
    import os
    log_pos = os.path.getsize(LOG_PATH) if os.path.exists(LOG_PATH) else 0

    # 0. real bot outbound marker
    mid = bot_send(
        "🟢 Zero bot 5-minute complete live run started (server pid "
        f"{open('/home/z/my-project/scripts/realrun/server.pid').read().strip()})."
    )
    print(f"[0] bot marker message_id={mid}")
    base_counts = db_counts()
    print(f"[0] baseline: {base_counts}")

    # 1. chat tool-loop round (real LLM + real wordcount tool) — quick proof
    chat_result = None
    try:
        settings, services = build_real_services()
        project = management_project(services)
        from zero.app.chat_service import ChatService, TokenBucketRateLimiter

        chat = ChatService(
            providers=services.providers,
            authorization=services.authorization,
            tools=services.tools,
            rate_limiter=TokenBucketRateLimiter(30),
        )
        turn = chat.complete(
            project_id=project.id,
            actor_id=project.owner_user_id,
            message=(
                "Use the wordcount tool to count the words in 'five minute live "
                "run works' and report the exact number."
            ),
            provider="openai-compatible",
            model_name=MODEL,
            agent_scope="main_worker",
            max_tool_rounds=3,
            source="web",
        )
        chat_result = {
            "content_head": turn.content[:160],
            "tools": [
                {"tool": t["tool_name"], "status": t.get("status")}
                for t in turn.tool_calls_executed
            ],
        }
        print(f"[1] chat tool-loop: tools={chat_result['tools']}")
    except Exception as exc:  # noqa: BLE001
        chat_result = {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}
        print(f"[1] chat tool-loop ERROR: {chat_result['error']}")

    # 2. pipeline driver thread
    pipe_results: dict = {}
    th = threading.Thread(target=pipeline_driver, args=(pipe_results,), daemon=True)
    th.start()

    # 3. monitoring loop — 15s samples for 300s
    samples: list[dict] = []
    notable: list[str] = []
    KEYWORDS = ("delivery", "Delivery", "completed", "COMPLETED", "fallback",
                "FALLBACK", "524", "ERROR", "task.", "decomposition", "handoff")

    def read_new_log(pos: int) -> tuple[int, list[str]]:
        size = os.path.getsize(LOG_PATH)
        if size == pos:
            return pos, []
        with open(LOG_PATH, "rb") as f:
            f.seek(pos)
            chunk = f.read().decode("utf-8", errors="replace")
        return size, [ln for ln in chunk.splitlines() if any(k in ln for k in KEYWORDS)]

    while time.time() - t_start < RUN_SECONDS:
        time.sleep(15 if samples else 3)
        elapsed = int(time.time() - t_start)
        try:
            with urllib.request.urlopen("http://127.0.0.1:8000/healthz", timeout=5) as r:
                health = json.loads(r.read()).get("status")
        except Exception as exc:  # noqa: BLE001
            health = f"DOWN({type(exc).__name__})"
        log_pos, new_lines = read_new_log(log_pos)
        notable.extend(f"{time.strftime('%H:%M:%S')} {ln.strip()[:160]}" for ln in new_lines)
        polls = sum(1 for ln in notable if "getUpdates" in ln)
        c = db_counts()
        sample = {
            "t": f"+{elapsed}s", "health": health,
            "poll_cycles_delta": polls,
            "provider_requests_delta": c["provider_requests"] - base_counts["provider_requests"],
            "deliveries_delta": c["deliveries"] - base_counts["deliveries"],
            "deliveries_sent_delta": c["deliveries_sent"] - base_counts["deliveries_sent"],
        }
        samples.append(sample)
        print(f"[watch +{elapsed:>3}s] health={health} polls+{polls} "
              f"llm+{sample['provider_requests_delta']} "
              f"deliveries+{sample['deliveries_sent_delta']}")

    # 4. final execution state (if handoff reached an execution)
    execution_final = None
    try:
        settings, services = build_real_services()
        project = management_project(services)
        owner_id = project.owner_user_id
        handoff_id = read_state(f"{TASK_KEY}_handoff_id")
        if handoff_id:
            from zero.domain.execution import ExecutionId
            from zero.domain.plans import PlanHandoffId

            h = services.plans.get_handoff(
                PlanHandoffId(handoff_id), project_id=project.id, actor_id=owner_id
            )
            if h.execution_id:
                execution = services.worker.get_execution(
                    ExecutionId(h.execution_id), project_id=project.id, actor_id=owner_id
                )
                tasks = services.worker.list_tasks(
                    execution.id, project_id=project.id, actor_id=owner_id
                )
                execution_final = {
                    "id": execution.id.value,
                    "state": execution.state,
                    "tasks": [
                        {"id": t.id.value[:22], "state": t.state,
                         "objective": t.objective[:70]}
                        for t in tasks
                    ],
                }
    except Exception as exc:  # noqa: BLE001
        execution_final = {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}

    final_counts = db_counts()
    summary = {
        "run_started": t_start_iso,
        "run_seconds": RUN_SECONDS,
        "server_pid": open("/home/z/my-project/scripts/realrun/server.pid").read().strip(),
        "bot_marker_message_id": mid,
        "chat_tool_loop": chat_result,
        "pipeline": pipe_results,
        "baseline_counts": base_counts,
        "final_counts": final_counts,
        "deltas": {
            "provider_requests": final_counts["provider_requests"] - base_counts["provider_requests"],
            "deliveries_sent": final_counts["deliveries_sent"] - base_counts["deliveries_sent"],
        },
        "poll_cycles_observed": sum(1 for ln in notable if "getUpdates" in ln),
        "notable_log_lines": notable[-25:],
        "execution_final": execution_final,
    }
    record(f"{TASK_KEY}_5min_summary", summary)
    print("\n=== 5-MINUTE RUN SUMMARY ===")
    print(json.dumps({k: v for k, v in summary.items()
                      if k not in ("notable_log_lines",)}, indent=2, default=str)[:2200])
    print(f"\nnotable log lines captured: {len(notable)}")
    for ln in notable[-12:]:
        print("  |", ln)
    print("=== server still running (left up) ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
