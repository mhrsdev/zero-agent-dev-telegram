"""Reconcile the wedged r7 execution: operator confirms the ambiguous
provider request produced no consumed result, then unblocks the task."""

from __future__ import annotations

import sys

sys.path.insert(0, "/home/z/my-project/scripts/realrun")
from env_common import (  # noqa: E402
    build_real_services,
    management_project,
    setup_env,
)

setup_env()


def main() -> int:
    settings, services = build_real_services()
    project = management_project(services)
    owner_id = project.owner_user_id

    execution_id = "exec_5d04qrg6ksgf7r02ud87hbhy"
    from zero.domain.execution import ExecutionId

    execution = services.worker.get_execution(
        ExecutionId(execution_id), project_id=project.id, actor_id=owner_id
    )
    print("execution state:", execution.state)
    tasks = services.worker.list_tasks(
        ExecutionId(execution_id), project_id=project.id, actor_id=owner_id
    )
    blocked = [t for t in tasks if t.state == "blocked" and t.blocker_reason and "unknown" in t.blocker_reason]
    if not blocked:
        print("no unknown-outcome blocked tasks; nothing to reconcile")
        return 0
    for task in blocked:
        print("reconciling:", task.id.value, "-", task.objective[:70])
        reconciled = services.worker.reconcile_blocked_task(
            execution_id=ExecutionId(execution_id),
            project_id=project.id,
            task_id=task.id,
            actor_id=owner_id,
            source="web",
        )
        print("  ->", reconciled.state, reconciled.blocker_reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
