"""ApprovalRequestTool — request user approval for a high-risk action.

Sends a Telegram message with inline keyboard buttons:
    [Approve] [Reject] [Edit] [Request Changes]

Blocks the agent loop until the user responds or the timeout expires.

Per ADR T-8.1: requester cannot self-approve (enforced in
:class:`zero.security.approval.ApprovalResolver`).

Per ADR T-7.3: this tool is BLOCKED for sub-agents (only the top-level
agent can request approvals). This is enforced by the Orchestrator's
``DELEGATE_BLOCKED_TOOLS`` set.

Dependencies (approval store + send_keyboard callback) are injected via
:func:`set_approval_request_deps` (called by
:class:`zero.agents.runner.ZeroAgentRunner.setup()`).
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any

from zero.tools.base import Tool, ToolContext, ToolSpec

__all__ = [
    "ApprovalRequestTool",
    "APPROVAL_REQUEST_SCHEMA",
    "set_approval_request_deps",
    "register",
]


APPROVAL_REQUEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "description": "Action being approved (e.g. 'file.write', 'shell.exec')"},
        "params": {
            "type": "object",
            "description": "Action parameters (e.g. {'path': '/etc/passwd', 'content': '...'})",
        },
        "description": {"type": "string", "description": "Human-readable description of the action"},
        "timeout_seconds": {"type": "integer", "default": 300},
    },
    "required": ["action", "description"],
}

# Global approval store + send_message callback (for sending the inline keyboard).
_approval_store_ref: Any = None
_approval_send_keyboard: Any = None


def set_approval_request_deps(store: Any, send_keyboard: Any) -> None:
    """Inject the approval store + send_keyboard callback (called by runner).

    Args:
        store: A :class:`zero.stores.approval_store.DbApprovalStore` instance.
        send_keyboard: An async callback ``(approval_id, description, ctx) -> None``
            that sends a Telegram message with the 4-button inline keyboard.
    """
    global _approval_store_ref, _approval_send_keyboard
    _approval_store_ref = store
    _approval_send_keyboard = send_keyboard


class ApprovalRequestTool(Tool):
    """Request user approval for a high-risk action."""

    spec = ToolSpec(
        name="approval_request",
        description="Request user approval for a high-risk action. Blocks until resolved.",
        parameters_schema=APPROVAL_REQUEST_SCHEMA,
        required_permissions=frozenset(),
        approval_level="none",  # the tool itself IS the approval flow
    )

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> str:
        from zero.security.approval import ApprovalRequest  # noqa: PLC0415

        if _approval_store_ref is None:
            return "[TOOL_ERROR] approval store not configured"

        action = str(args["action"])
        params = dict(args.get("params", {}))
        description = str(args.get("description", action))
        timeout = int(args.get("timeout_seconds", 300))

        req = ApprovalRequest(
            requester_id=ctx.actor_id,
            action=action,
            scope=ctx.scope,
            params=params,
        )

        # Persist the approval request.
        await _approval_store_ref.create_async(req)

        # Send the inline keyboard to the user (if callback installed).
        if _approval_send_keyboard is not None:
            try:
                await _approval_send_keyboard(req.id, description, ctx)
            except Exception as e:
                return f"[TOOL_ERROR] failed to send approval keyboard: {e}"

        # Poll for resolution (with timeout).
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            updated = await _approval_store_ref.get_async(req.id)
            if updated is None:
                return "[TOOL_ERROR] approval request disappeared"
            if updated.status.value != "pending":
                if updated.status.value == "approved":
                    return f"✅ Approved by {updated.approver_id}"
                if updated.status.value == "rejected":
                    return f"❌ Rejected by {updated.approver_id}"
                if updated.status.value == "edited":
                    return f"✏️ Edited by {updated.approver_id}: {updated.edited_params}"
                if updated.status.value == "changes_requested":
                    return f"📝 Changes requested by {updated.approver_id}: {updated.resolution_note}"
                return f"Approval status: {updated.status.value}"
            await asyncio.sleep(1.0)

        return "[APPROVAL_TIMEOUT] user did not respond in time"


def register() -> None:
    """Register the ApprovalRequestTool with the global tool registry."""
    from zero.tools.builtin_tools._helpers import register_tool  # noqa: PLC0415

    register_tool(ApprovalRequestTool())
