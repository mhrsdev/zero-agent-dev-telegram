"""S3 — REAL full pipeline driven by the LIVE SERVER's own workers.

Unlike s1 (manual scheduler ticks), this script only:
  1. sends a NEW massive task through the real gateway (policy gate ->
     identity -> durable conversation event -> planner LLM);
  2. verifies the planner's task decomposition (titles/objectives);
  3. additionally probes the non-actionable path (casual chat);
  4. approves the revision (human approval step);
  5. MONITORS while the live uvicorn server's scheduler worker runs the
     agent runtime (real LLM + worktree tools) and the delivery worker
     sends real Telegram messages.

Idempotent + resumable: re-run to continue monitoring.
"""

from __future__ import annotations

import sys
import time
import uuid

sys.path.insert(0, "/home/z/my-project/scripts/realrun")
from env_common import (  # noqa: E402
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

TASK_KEY = sys.argv[1] if len(sys.argv) > 1 else "textcase"  # state key namespace

MASSIVE_TASK = (
    "Team, new package for this repo: build `textcase`. Requirements:\n"
    "1. textcase/convert.py with functions: to_snake_case(text), "
    "to_camel_case(text), to_kebab_case(text) — input may contain spaces, "
    "hyphens or underscores; only the standard library.\n"
    "2. textcase/__init__.py re-exporting the three functions with __all__.\n"
    "3. tests/test_convert.py with unittest coverage: mixed separators, "
    "already-converted inputs, empty string, single word, ALL CAPS input.\n"
    "4. Run: python3 -m unittest discover -s tests -v — must pass with 0 failures.\n"
    "5. Update README.md with a Short Usage section for textcase.\n"
    "6. Use the delegate tool so a sub-agent reviews the word-splitting "
    "rules before you finish.\n"
    "7. Capture the final diff of all created/modified files."
)

CASUAL_CHAT = (
    "hey team, what's everyone working on this week? I finally finished "
    "the onboarding docs and I'm thinking about the next team offsite."
)


def main() -> int:
    settings, services = build_real_services()
    project = management_project(services)
    owner_id = project.owner_user_id
    t0 = time.time()
    deadline = 7.5 * 60

    # -- state -----------------------------------------------------------------
    event_id = read_state(f"{TASK_KEY}_event_id")
    if not event_id:
        event_id = f"realrun-{uuid.uuid4().hex[:12]}"
        record(f"{TASK_KEY}_event_id", event_id)
    print(f"event: {event_id}")

    def find_revision():
        conv = services.plans.get_conversation_event_by_external(
            project_id=project.id,
            source="telegram",
            external_event_id=event_id,
            actor_id=owner_id,
        )
        if conv is None:
            return None
        return services.plans.find_revision_by_source_event(
            project_id=project.id, event_id=conv.id, actor_id=owner_id, source="telegram"
        )

    # -- 1+2. gateway intake + planner LLM (once) ------------------------------
    revision = find_revision()
    if revision is None:
        print("[1] REAL gateway intake -> planner LLM (claude-opus-5)")
        result = services.interfaces.process_inbound_event(
            NormalizedEvent(
                platform="telegram",
                external_event_id=event_id,
                external_actor_id=TG_SENDER_ID,
                chat_id=GROUP_ID,
                topic_id=None,
                event_kind="message",
                content=MASSIVE_TASK,
            )
        )
        detail = result.processing_detail
        print(f"    {result.processing_result}: {detail[:140]}")
        if result.processing_result != "processed":
            return 1
        revision = find_revision()
        if revision is None:
            print("    planner judged non-actionable (unexpected for the big task)")
            return 1
    print(f"[2] revision {revision.id.value} plan={revision.plan_id.value}")
    print("    objective:", revision.content.objective[:150])
    record(f"{TASK_KEY}_plan", {
        "plan_id": revision.plan_id.value,
        "revision_id": revision.id.value,
        "objective": revision.content.objective,
    })

    # -- 3. non-actionable path (real planner says no) -------------------------
    if not read_state(f"{TASK_KEY}_casual_checked"):
        cas_event = f"realrun-{uuid.uuid4().hex[:12]}"
        record(f"{TASK_KEY}_casual_event", cas_event)
        try:
            res = services.interfaces.process_inbound_event(
                NormalizedEvent(
                    platform="telegram",
                    external_event_id=cas_event,
                    external_actor_id=TG_SENDER_ID,
                    chat_id=GROUP_ID,
                    topic_id=None,
                    event_kind="message",
                    content=CASUAL_CHAT,
                )
            )
            print(f"[3] casual chat -> {res.processing_detail[-80:]}")
            record(f"{TASK_KEY}_casual_checked", str(res.processing_detail))
        except Exception as exc:  # noqa: BLE001
            record(f"{TASK_KEY}_casual_checked", f"error {type(exc).__name__}")
            print(f"[3] casual chat errored: {type(exc).__name__}")

    # -- 4. human approval (idempotent) ----------------------------------------
    full = services.plans.get_plan(revision.plan_id, project_id=project.id, actor_id=owner_id)
    handoff_id = read_state(f"{TASK_KEY}_handoff_id")
    if full.current_state != "approved":
        approval, handoff = services.plans.approve_revision(
            plan_id=revision.plan_id,
            project_id=project.id,
            actor_id=owner_id,
            expected_revision_number=revision.revision_number,
            idempotency_key=f"realrun-approve-{event_id}",
        )
        handoff_id = handoff.id.value
        record(f"{TASK_KEY}_handoff_id", handoff_id)
        print(f"[4] approved -> handoff {handoff_id} (server scheduler takes over)")
    else:
        print(f"[4] already approved (handoff {handoff_id})")

    # -- 5. monitor the LIVE SERVER's workers ----------------------------------
    from zero.domain.execution import ExecutionId
    from zero.domain.plans import PlanHandoffId

    print("[5] monitoring server-owned scheduler/delivery workers ...")
    last_report = ""
    while time.time() - t0 < deadline:
        h = services.plans.get_handoff(
            PlanHandoffId(handoff_id), project_id=project.id, actor_id=owner_id
        )
        if h.execution_id is None:
            time.sleep(5)
            continue
        execution = services.worker.get_execution(
            ExecutionId(h.execution_id), project_id=project.id, actor_id=owner_id
        )
        tasks = services.worker.list_tasks(
            execution.id, project_id=project.id, actor_id=owner_id
        )
        states = [t.state for t in tasks]
        report = f"{execution.state}: " + " ".join(s[0].upper() for s in states)
        if report != last_report:
            print(f"    {time.strftime('%H:%M:%S')} {report}")
            last_report = report
        if execution.state in {"completed", "failed", "cancelled"}:
            record(f"{TASK_KEY}_execution", {
                "id": execution.id.value,
                "state": execution.state,
                "tasks": [
                    {"id": t.id.value, "state": t.state, "objective": t.objective}
                    for t in tasks
                ],
            })
            break
        time.sleep(10)

    # -- 6. deliveries + evidence ----------------------------------------------
    import sqlite3

    db = sqlite3.connect(f"file:/home/z/my-project/zero-real-home/engine.db?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    deliveries = db.execute(
        "SELECT id, state, external_message_id, substr(content,1,60) head "
        "FROM result_deliveries WHERE project_id=? ORDER BY created_at DESC LIMIT 6",
        (project.id.value,),
    ).fetchall()
    print("[6] recent deliveries:")
    for d in deliveries:
        print(f"    {d['state']:9s} msg={d['external_message_id']} {d['head']!r}")
    record(
        f"{TASK_KEY}_deliveries",
        [dict(d) for d in deliveries],
    )
    audit = services.audit.list_for_project(project_id=project.id, actor_id=owner_id, limit=500)
    tool_ops = [e for e in audit if e.operation == "tool.invocation" and e.result == "success"]
    counts: dict[str, int] = {}
    for e in tool_ops:
        counts[e.target_id] = counts.get(e.target_id, 0) + 1
    print(f"[7] audited tool invocations: {counts}")
    usage = services.providers.get_usage_for_project(project.id, actor_id=owner_id)
    print(f"    usage: in={usage.input_tokens} out={usage.output_tokens}")
    record(f"{TASK_KEY}_tool_invocations", counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
