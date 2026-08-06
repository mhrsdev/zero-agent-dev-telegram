"""CronJobTool — manage scheduled tasks (cron jobs).

Actions: create, list, delete, run.

Per ADR T-7.3: this tool is BLOCKED for sub-agents (only the top-level
agent can schedule cron jobs). This is enforced by the Orchestrator's
``DELEGATE_BLOCKED_TOOLS`` set.

Schedules are stored in-memory (per-process). Scope-isolated — jobs can
only be listed/deleted/run by their owning scope. In a multi-process
deployment, use an external scheduler (systemd timers, k8s CronJobs).
"""
from __future__ import annotations

import uuid
from typing import Any

from zero.tools.base import Tool, ToolContext, ToolSpec

__all__ = ["CronJobTool", "CRONJOB_SCHEMA", "register"]


CRONJOB_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["create", "list", "delete", "run"]},
        "name": {"type": "string", "description": "Cron job name (for create/delete)"},
        "schedule": {
            "type": "string",
            "description": "Cron expression (e.g. '0 9 * * *' = daily at 9am) or 'every:Nd' / 'every:Nh' / 'every:Nm'",
        },
        "task": {"type": "string", "description": "Task description (for create/run)"},
        "job_id": {"type": "string", "description": "Job ID (for delete)"},
    },
    "required": ["action"],
}

# In-memory cron job registry (persisted to DB in enterprise version).
# Each job is a dict with: job_id, name, schedule, task, scope_key, created_by.
_cron_jobs: dict[str, dict[str, Any]] = {}


class CronJobTool(Tool):
    """Manage scheduled tasks (cron jobs)."""

    spec = ToolSpec(
        name="cronjob",
        description="Manage scheduled tasks (create, list, delete, run).",
        parameters_schema=CRONJOB_SCHEMA,
        required_permissions=frozenset({"cron.create"}),
        approval_level="standard",
    )

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> str:
        action = str(args["action"])

        if action == "create":
            name = str(args.get("name", ""))
            schedule = str(args.get("schedule", ""))
            task = str(args.get("task", ""))
            if not name or not schedule or not task:
                return "[TOOL_ERROR] create requires name, schedule, and task"
            job_id = f"cron_{uuid.uuid4().hex[:12]}"
            _cron_jobs[job_id] = {
                "job_id": job_id,
                "name": name,
                "schedule": schedule,
                "task": task,
                "scope_key": ctx.scope.retrieval_key(),
                "created_by": ctx.actor_id,
            }
            return f"✅ Created cron job {job_id}: {name} ({schedule})"

        if action == "list":
            scope_key = ctx.scope.retrieval_key()
            jobs = [j for j in _cron_jobs.values() if j["scope_key"] == scope_key]
            if not jobs:
                return "(no cron jobs)"
            lines = [f"  • {j['job_id']}: {j['name']} ({j['schedule']}) — {j['task'][:50]}" for j in jobs]
            return "Cron jobs:\n" + "\n".join(lines)

        if action == "delete":
            job_id = str(args.get("job_id", ""))
            if job_id not in _cron_jobs:
                return f"[TOOL_ERROR] job {job_id!r} not found"
            job = _cron_jobs.pop(job_id)
            if job["scope_key"] != ctx.scope.retrieval_key():
                # Restore — don't delete cross-scope.
                _cron_jobs[job_id] = job
                return "[TOOL_ERROR] cannot delete job from different scope"
            return f"✅ Deleted cron job {job_id}"

        if action == "run":
            job_id = str(args.get("job_id", ""))
            job = _cron_jobs.get(job_id)
            if job is None:
                return f"[TOOL_ERROR] job {job_id!r} not found"
            if job["scope_key"] != ctx.scope.retrieval_key():
                return "[TOOL_ERROR] cannot run job from different scope"
            # In production, this would enqueue the task on the async job queue
            # (zero.core.jobs) and return immediately. For now, we return the
            # task description so the caller can execute it.
            return f"Would run cron job {job_id}: {job['task']}"

        return f"[TOOL_ERROR] unknown action {action!r}"


def register() -> None:
    """Register the CronJobTool with the global tool registry."""
    from zero.tools.builtin_tools._helpers import register_tool  # noqa: PLC0415

    register_tool(CronJobTool())
