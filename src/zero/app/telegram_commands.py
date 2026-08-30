"""Dynamic Telegram slash commands (Hermes slash-command parity, gaps F+G).

The gap this closes: the Telegram surface answered only ``/start`` and
``/help``, so an operator had NO way to ask the running engine "who are
you talking to, what is running, what needs my attention" from the chat
itself. Hermes exposes a rich command surface; Zero now answers:

- ``/status`` — engine identity, routed provider/model, worker loop
  counters, recent worker errors, pending result deliveries, enabled
  bindings, and PENDING TOOL APPROVALS (gap G: manual-mode approvals
  used to be invisible in chat);
- ``/tasks`` — the most recent executions with per-task durable state;
- ``/model`` — the provider/model pair the planner, the chat bridge,
  and the scheduler tick are pinned to;
- ``/approvals`` — pending tool-approval requests with their ids, so an
  operator can resolve them from the web surface (or a future button
  flow) without opening the dashboard first.

Every reply is built from DURABLE state (the same service boundaries the
API uses), is bounded in length, and NEVER raises: a command reply is
presentation, and any failure degrades to an honest one-line error.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_MAX_TASK_LINES = 12
_MAX_ERROR_LINES = 3
_MAX_APPROVAL_LINES = 8


class TelegramCommandBook:
    """Build dynamic command replies from durable application state."""

    def __init__(self, services: Any, *, worker_status: Any = None) -> None:
        self._services = services
        self._worker_status = worker_status  # callable -> dict, optional

    # ------------------------------------------------------------------
    # Entry
    # ------------------------------------------------------------------

    def reply_for(
        self,
        first_token: str,
        *,
        project_id: Any,
        actor_id: Any,
        source: str = "telegram",
    ) -> str | None:
        """Return the reply text for a dynamic command, or None."""
        handlers = {
            "/status": self.status_reply,
            "/tasks": self.tasks_reply,
            "/model": self.model_reply,
            "/approvals": self.approvals_reply,
        }
        handler = handlers.get(first_token.lower())
        if handler is None:
            return None
        try:
            return handler(project_id=project_id, actor_id=actor_id, source=source)
        except Exception as exc:  # noqa: BLE001 - commands never crash intake
            logger.warning(
                "command %s failed: %s: %s",
                first_token,
                type(exc).__name__,
                str(exc)[:160],
            )
            return f"/{first_token.lstrip('/')} is unavailable right now ({type(exc).__name__})."

    # ------------------------------------------------------------------
    # /status
    # ------------------------------------------------------------------

    def status_reply(self, *, project_id, actor_id, source: str = "telegram") -> str:
        services = self._services
        lines: list[str] = ["**Zero engine status**"]

        provider, model = None, None
        scheduler = getattr(services, "scheduler", None)
        if scheduler is not None:
            try:
                provider, model = scheduler.tick_routing_override()
            except Exception:  # noqa: BLE001
                provider, model = None, None
        if model:
            lines.append(f"Model: `{model}`" + (f" (provider `{provider}`)" if provider else ""))

        status = self._worker_status() if callable(self._worker_status) else None
        if isinstance(status, dict):
            lines.append(
                "Workers: running={running}, scheduler_ticks={scheduler_ticks}, "
                "delivery_drains={delivery_drains}, polling_iterations={polling_iterations}".format(
                    **{key: status.get(key, "—") for key in (
                        "running", "scheduler_ticks", "delivery_drains", "polling_iterations"
                    )}
                )
            )
            errors = status.get("last_errors") or []
            recent = list(errors)[-_MAX_ERROR_LINES:]
            if recent:
                lines.append("Recent worker errors:")
                for err in recent:
                    lines.append(f"• {str(err)[:160]}")

        pending = services.result_delivery.list_pending(project_id)
        if pending:
            lines.append(f"Pending result deliveries: {len(pending)}")
        else:
            lines.append("Pending result deliveries: 0")

        approvals = self._pending_approvals(project_id)
        if approvals:
            lines.append(f"⚠️ Pending tool approvals: {len(approvals)} — send /approvals")
        else:
            lines.append("Pending tool approvals: 0")

        try:
            bindings = services.result_delivery.list_enabled_bindings(project_id)
            tg = [b for b in bindings if b.platform == "telegram"]
            lines.append(f"Enabled Telegram bindings: {len(tg)}")
        except Exception:  # noqa: BLE001
            pass
        return "\n".join(lines)[:3800]

    # ------------------------------------------------------------------
    # /tasks
    # ------------------------------------------------------------------

    def tasks_reply(self, *, project_id, actor_id, source: str = "telegram") -> str:
        services = self._services
        # Keyword-only API (live-run fix: positional project_id raised
        # TypeError and the command degraded to an error reply).
        executions = services.worker.list_project_executions(
            project_id=project_id, actor_id=actor_id, source=source
        )
        if not executions:
            return "No executions yet. Approve a plan and the worker will run it."
        lines: list[str] = ["**Recent executions**"]
        for execution in executions[:5]:
            state = execution.state
            marker = {"completed": "✅", "failed": "⚠️", "running": "🔧", "cancelled": "🚫"}.get(
                state, "⏳"
            )
            lines.append(f"{marker} {execution.id.value[:18]}… — {state}")
            try:
                tasks = services.worker.list_tasks(
                    execution.id, project_id=project_id, actor_id=actor_id, source=source
                )
            except Exception:  # noqa: BLE001
                tasks = []
            for task in tasks[:_MAX_TASK_LINES]:
                objective = (task.objective or "").strip().replace("\n", " ")[:90]
                tmarker = {
                    "completed": "✅",
                    "failed": "⚠️",
                    "running": "🔧",
                    "cancelled": "🚫",
                }.get(task.state, "⏳")
                lines.append(f"  {tmarker} {objective or task.id.value[:14]}")
        return "\n".join(lines)[:3800]

    # ------------------------------------------------------------------
    # /model
    # ------------------------------------------------------------------

    def model_reply(self, *, project_id, actor_id, source: str = "telegram") -> str:
        services = self._services
        provider, model = None, None
        scheduler = getattr(services, "scheduler", None)
        if scheduler is not None:
            try:
                provider, model = scheduler.tick_routing_override()
            except Exception:  # noqa: BLE001
                provider, model = None, None
        if not model:
            return (
                "No model routing is configured yet — the planner falls back "
                "to the environment default. Set `routing.primary_model` in "
                "the management config."
            )
        lines = [f"Primary model: `{model}`", f"Provider: `{provider or 'openai-compatible'}`"]
        try:
            names = services.providers.registered_provider_names
            if names:
                lines.append(f"Registered providers: {', '.join(names)}")
        except Exception:  # noqa: BLE001
            pass
        lines.append("This model serves the planner, the scheduler tick (tasks + decomposition), and chat.")
        return "\n".join(lines)[:1500]

    # ------------------------------------------------------------------
    # /approvals (gap G)
    # ------------------------------------------------------------------

    def approvals_reply(self, *, project_id, actor_id, source: str = "telegram") -> str:
        approvals = self._pending_approvals(project_id)
        if not approvals:
            return "No pending tool approvals. Manual mode queues calls here when ZERO_TOOL_APPROVAL_MODE=manual."
        lines = ["**Pending tool approvals**"]
        for request in approvals[:_MAX_APPROVAL_LINES]:
            tool = getattr(request, "tool_name", "?")
            request_id = getattr(request, "id", "?")
            lines.append(f"• `{tool}` — id `{str(request_id)[:26]}`")
        more = len(approvals) - _MAX_APPROVAL_LINES
        if more > 0:
            lines.append(f"…and {more} more")
        lines.append("Resolve them via the web surface: /web → Tool approvals.")
        return "\n".join(lines)[:3800]

    def _pending_approvals(self, project_id) -> list:
        gate = getattr(self._services, "approval_gate", None)
        if gate is None:
            return []
        try:
            return gate.list_pending(project_id=str(project_id.value))
        except Exception:  # noqa: BLE001
            return []


__all__ = ["TelegramCommandBook"]
