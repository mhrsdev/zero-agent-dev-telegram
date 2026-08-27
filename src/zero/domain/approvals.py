"""Per-call tool approval domain (GAP 8b/G2, Hermes parity).

A tool invocation may require an explicit human decision before the
runtime executes it. The request/decision pair is one durable row:
``decision IS NULL`` while the verdict is pending.

Grains (Hermes approval semantics, reduced to what is enforceable
across processes):

- ``once``: this exact call only (default for resolutions).
- ``session``: same execution for the lifetime of the server process.
- ``always``: same project + tool + canonical argument shape,
  durably stored so restarts keep honoring it.
"""

from __future__ import annotations

from dataclasses import dataclass

ApprovalVerdictState = str  # "allowed" | "denied" | "pending"
ToolApprovalGrain = str  # "once" | "session" | "always"


@dataclass(frozen=True)
class ToolApprovalRequest:
    """One durable pending-or-decided tool approval row."""

    id: str
    project_id: str
    execution_id: str | None
    tool_name: str
    args_hash: str
    grain: ToolApprovalGrain
    decision: str | None  # None while pending; "allow"/"deny" after resolve
    decided_by_user_id: str | None
    reason: str | None
    created_at: str
    resolved_at: str | None


@dataclass(frozen=True)
class ApprovalVerdict:
    """What the runtime should do with a gated tool call."""

    state: ApprovalVerdictState
    request: ToolApprovalRequest | None = None
    cause: str | None = None  # "hardline" | "rule" | "standing_allow" | ...
