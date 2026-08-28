"""S1R — resume the in-flight real execution (durable, replay-safe)."""

from __future__ import annotations

import sys
import time

sys.path.insert(0, "/home/z/my-project/scripts/realrun")
from env_common import (  # noqa: E402
    MODEL,
    build_real_services,
    management_project,
    read_state,
    record,
    setup_env,
)

setup_env()


def main() -> int:
    settings, services = build_real_services()
    project = management_project(services)
    owner = services.identity.get_user(project.owner_user_id)

    from zero.domain.execution import ExecutionId

    handoff_id = read_state("approval", {}).get("handoff_id")
    execution = None
    if handoff_id:
        from zero.domain.plans import PlanHandoffId

        h = services.plans.get_handoff(
            PlanHandoffId(handoff_id), project_id=project.id, actor_id=owner.id
        )
        if h.execution_id:
            execution = services.worker.get_execution(
                ExecutionId(h.execution_id), project_id=project.id, actor_id=owner.id
            )
    if execution is None:
        print("no in-flight execution for the recorded handoff — run s1 first")
        return 1
    print("resuming execution:", execution.id.value, "state:", execution.state)

    # designed restart path: reclaim interrupted leases (marks the task
    # killed mid-flight back to ready, attempt 'unknown')
    execution = services.worker.recover_after_restart(
        execution_id=execution.id, project_id=project.id, actor_id=owner.id
    )
    print("after recovery:", execution.state)

    tick = 0
    while tick < 14 and execution.state not in {"completed", "failed", "cancelled"}:
        tick += 1
        t0 = time.time()
        res = services.scheduler.run_once(
            project_id=project.id,
            actor_id=owner.id,
            lease_owner="realrun-harness",
            provider="openai-compatible",
            model_name=MODEL,
        )
        execution = services.worker.get_execution(
            execution.id, project_id=project.id, actor_id=owner.id
        )
        print(
            f"tick {tick}: handoffs={res.handoffs_claimed} tasks={res.tasks_run} "
            f"deliveries={len(res.result_delivery_ids)} errors={list(res.errors)[:2]} "
            f"-> state={execution.state} ({time.time() - t0:.1f}s)"
        )
        if res.tasks_run == 0 and res.handoffs_claimed == 0 and not res.integration_review_ids:
            time.sleep(1.0)
    record("execution_state", execution.state)

    tasks = services.worker.list_tasks(execution.id, project_id=project.id, actor_id=owner.id)
    for t in tasks:
        print(f"  - [{t.state}] {t.objective[:100]}")
    record("execution_final", {
        "id": execution.id.value,
        "state": execution.state,
        "tasks": [{"id": t.id.value, "state": t.state, "objective": t.objective} for t in tasks],
    })

    print("\ndelivery drain → REAL sendMessage")
    for _ in range(3):
        drained = services.result_delivery.drain_once(project_id=project.id)
        print("drained:", drained)
        if not drained:
            break
    record("delivery_drained", str(drained))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
