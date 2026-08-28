"""S1 — the massive real gateway E2E (resumable advance-loop).

Every phase is idempotent and durable:
  1. gateway intake (fixed external_event_id → duplicate-safe replay)
  2. planner (find_revision_by_source_event idempotency)
  3. approval (fixed idempotency_key)
  4. scheduler ticks (lease-fenced, replay-safe)
  5. delivery drain (durable queue)

Just re-run this script; it advances the SAME pipeline each time and
prints REALRUN COMPLETE when the execution finished and results were
delivered to the real Telegram group.
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
from zero.domain.interfaces import NormalizedEvent  # noqa: E402

setup_env()

DEADLINE = 8.5 * 60  # stay under the tool's 10-minute cap

MASSIVE_TASK = (
    "Team, let's build the `textkit` package in this repo. Requirements:\n"
    "1. Create textkit/__init__.py exporting the public API.\n"
    "2. Create textkit/stats.py with functions: word_count(text), "
    "char_count(text), is_palindrome(text) (ignore case/spaces/punctuation).\n"
    "3. Create textkit/cli.py with a tiny main() that prints stats for a file argument.\n"
    "4. Create tests/test_stats.py with unittest tests covering all three functions "
    "including edge cases (empty string, mixed case palindrome).\n"
    "5. Write README.md documenting usage.\n"
    "6. Run the tests with: python3 -m unittest discover -s tests -v  — they MUST pass.\n"
    "7. Capture the final diff.\n"
    "8. Use the delegate tool to have a sub-agent review your implementation plan "
    "before finishing. Follow the repo conventions in NOTES.md."
)


def main() -> int:
    settings, services = build_real_services()
    project = management_project(services)
    owner = services.identity.get_user(project.owner_user_id)
    provider = "openai-compatible"
    t_start = time.time()

    def out_of_time() -> bool:
        return time.time() - t_start > DEADLINE

    event_id = read_state("gateway_event_id")
    if not event_id:
        event_id = f"realrun-{uuid.uuid4().hex[:12]}"
        record("gateway_event_id", event_id)
    print(f"event: {event_id}")

    # -- 1+2. gateway intake + planner (duplicate-safe, replans until a
    # revision exists; the durable conversation event is reused)
    rev = services.plans.find_revision_by_source_event
    conv = services.plans.get_conversation_event_by_external(
        project_id=project.id,
        source="telegram",
        external_event_id=event_id,
        actor_id=owner.id,
    )
    revision = None
    if conv is not None:
        revision = services.plans.find_revision_by_source_event(
            project_id=project.id, event_id=conv.id, actor_id=owner.id, source="telegram"
        )
    if revision is None:
        print("[1] process_inbound_event (real gateway + planner LLM)")
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
        print(f"    {result.processing_result}: {result.processing_detail[:120]}")
        if result.processing_result != "processed":
            record("gateway_event", {"result": result.processing_result})
            return 1
        conv = services.plans.get_conversation_event_by_external(
            project_id=project.id,
            source="telegram",
            external_event_id=event_id,
            actor_id=owner.id,
        )
        revision = services.plans.find_revision_by_source_event(
            project_id=project.id, event_id=conv.id, actor_id=owner.id, source="telegram"
        )
        if revision is None:
            print("    planner judged it non-actionable — resend later or refine the ask")
            return 1
    print(f"[2] revision {revision.id.value} (plan {revision.plan_id.value})")
    print("    objective:", revision.content.objective[:140])
    record("plan", {
        "plan_id": revision.plan_id.value,
        "revision_id": revision.id.value,
        "objective": revision.content.objective,
    })

    # -- 3. approval (idempotent) --------------------------------------------
    full = services.plans.get_plan(revision.plan_id, project_id=project.id, actor_id=owner.id)
    handoff_id = read_state("approval_handoff_id")
    if full.current_state != "approved":
        approval, handoff = services.plans.approve_revision(
            plan_id=revision.plan_id,
            project_id=project.id,
            actor_id=owner.id,
            expected_revision_number=revision.revision_number,
            idempotency_key=f"realrun-approve-{event_id}",
        )
        handoff_id = handoff.id.value
        record("approval_handoff_id", handoff_id)
        print(f"[3] approved → handoff {handoff_id}")
    else:
        print(f"[3] already approved (handoff {handoff_id})")

    # -- 4. scheduler ticks until done or out of time -------------------------
    print("[4] scheduler ticks (LLM decomposer + agent runtime tool loop)")
    from zero.domain.execution import ExecutionId
    from zero.domain.plans import PlanHandoffId

    tick = 0
    while not out_of_time():
        h = services.plans.get_handoff(
            PlanHandoffId(handoff_id), project_id=project.id, actor_id=owner.id
        )
        if h.execution_id is None:
            t0 = time.time()
            res = services.scheduler.run_once(
                project_id=project.id,
                actor_id=owner.id,
                lease_owner="realrun-harness",
                provider=provider,
                model_name=MODEL,
            )
            tick += 1
            print(f"    tick {tick}: claimed handoff ({time.time() - t0:.1f}s)")
            if res.handoffs_claimed == 0 and tick > 3:
                print("    handoff not claimable yet — rerun")
                return 1
            continue
        execution = services.worker.get_execution(
            ExecutionId(h.execution_id), project_id=project.id, actor_id=owner.id
        )
        if execution.state in {"completed", "failed", "cancelled"}:
            record("execution_state", execution.state)
            print(f"[4] execution {execution.id.value} → {execution.state}")
            break
        t0 = time.time()
        res = services.scheduler.run_once(
            project_id=project.id,
            actor_id=owner.id,
            lease_owner="realrun-harness",
            provider=provider,
            model_name=MODEL,
        )
        tick += 1
        execution = services.worker.get_execution(
            ExecutionId(h.execution_id), project_id=project.id, actor_id=owner.id
        )
        print(
            f"    tick {tick}: tasks={res.tasks_run} deliveries={len(res.result_delivery_ids)} "
            f"errors={list(res.errors)[:1]} -> {execution.state} ({time.time() - t0:.1f}s)"
        )
        if res.tasks_run == 0 and not res.integration_review_ids:
            time.sleep(1.0)
    record("scheduler_ticks", tick)
    if out_of_time():
        print("    out of time — RERUN to continue (durable)")
        return 0

    # -- 5. final task view + deliveries --------------------------------------
    h = services.plans.get_handoff(
        PlanHandoffId(handoff_id), project_id=project.id, actor_id=owner.id
    )
    execution = services.worker.get_execution(
        ExecutionId(h.execution_id), project_id=project.id, actor_id=owner.id
    )
    tasks = services.worker.list_tasks(
        execution.id, project_id=project.id, actor_id=owner.id
    )
    for t in tasks:
        print(f"      - [{t.state}] {t.objective[:95]}")
    record("execution_final", {
        "id": execution.id.value,
        "state": execution.state,
        "tasks": [{"id": t.id.value, "state": t.state, "objective": t.objective} for t in tasks],
    })
    wts = services.worktree.list_worktrees_for_project(project.id, actor_id=owner.id)
    record("worktrees", [w.worktree_path for w in wts])

    print("[5] delivery drain → REAL sendMessage")
    drained_total = 0
    for _ in range(5):
        drained = services.result_delivery.drain_once(project_id=project.id)
        if not drained:
            break
        drained_total += int(drained or 0)
        if out_of_time():
            break
    print("    drained:", drained_total)
    record("delivery_drained", drained_total)

    # -- 6. evidence -----------------------------------------------------------
    audit = services.audit.list_for_project(project_id=project.id, actor_id=owner.id, limit=400)
    tool_ops = [e for e in audit if e.operation == "tool.invocation" and e.result == "success"]
    tool_names_used: dict[str, int] = {}
    for e in tool_ops:
        tool_names_used[e.target_id] = tool_names_used.get(e.target_id, 0) + 1
    usage = services.providers.get_usage_for_project(project.id, actor_id=owner.id)
    print(f"[6] audited tool invocations: {tool_names_used}")
    print(f"    usage: in={usage.input_tokens} out={usage.output_tokens}")
    record("tool_invocations", tool_names_used)
    record("usage", {"in": usage.input_tokens, "out": usage.output_tokens})
    print("\nREALRUN COMPLETE" if execution.state == "completed" else f"\nEXECUTION {execution.state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
