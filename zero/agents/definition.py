"""Zero v2 agent definitions — ADR T-7.1.

Agent types: ``coding|testing|documentation|security|release``.

Per ADR:
    - Model policy is only a name Router understands (``effort-tier``).
    - Zero doesn't know what model backs it.
    - Tool allowlist.
    - Permissions ≤ launcher's permissions.
    - Agent belongs to one scope (personal or project), never both.

Effort-tier mapping (token-economy-design.md §6):
    - Documentation/Triage → ``zero/cheap`` | ``zero/fast``
    - Coding              → ``zero/coding``
    - Security/Release    → ``zero/best`` | ``zero/reasoning``
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from zero.core.scope import Scope

__all__ = [
    "AGENT_TYPE_TO_EFFORT_TIER",
    "EFFORT_TIERS",
    "AgentDefinition",
    "AgentType",
]


class AgentType(StrEnum):
    CODING = "coding"
    TESTING = "testing"
    DOCUMENTATION = "documentation"
    SECURITY = "security"
    RELEASE = "release"
    TRIAGE = "triage"


# Effort-tier aliases — names the Router understands.
# Zero doesn't know what model backs each alias.
EFFORT_TIERS = frozenset({
    "zero/cheap",
    "zero/fast",
    "zero/coding",
    "zero/best",
    "zero/reasoning",
})

# Default mapping per agent type.
AGENT_TYPE_TO_EFFORT_TIER: dict[AgentType, str] = {
    AgentType.DOCUMENTATION: "zero/cheap",
    AgentType.TRIAGE: "zero/fast",
    AgentType.CODING: "zero/coding",
    AgentType.TESTING: "zero/coding",
    AgentType.SECURITY: "zero/reasoning",
    AgentType.RELEASE: "zero/best",
}


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    """A reusable agent definition.

    Per T-7.1 acceptance criteria:
        - ``effort_tier`` must be in :data:`EFFORT_TIERS`.
        - ``tool_allowlist`` is a closed set — agent can ONLY use listed tools.
        - ``permissions`` is a subset of launcher's permissions (verified at
          run start).
        - ``scope`` is exactly one (personal OR project), never both.
    """

    name: str
    agent_type: AgentType
    scope: Scope
    system_prompt: str
    effort_tier: str
    tool_allowlist: frozenset[str] = field(default_factory=frozenset)
    max_turns: int = 50
    budget_usd: float = 5.0
    description: str = ""
    id: str = field(default_factory=lambda: f"agt_{uuid.uuid4().hex[:16]}")

    def __post_init__(self) -> None:
        if self.effort_tier not in EFFORT_TIERS:
            raise ValueError(
                f"effort_tier {self.effort_tier!r} not in allowed set {sorted(EFFORT_TIERS)}"
            )
        if self.max_turns <= 0 or self.max_turns > 1000:
            raise ValueError(f"max_turns must be in [1, 1000], got {self.max_turns}")
        if self.budget_usd <= 0:
            raise ValueError(f"budget_usd must be positive, got {self.budget_usd}")
        # Agent belongs to one scope (personal OR project), never both.
        # Personal agents cannot have project scope, and vice versa.
        # This is enforced by the scope field itself — no extra check needed.

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.agent_type.value,
            "scope": self.scope.retrieval_key(),
            "effort_tier": self.effort_tier,
            "tool_count": len(self.tool_allowlist),
            "max_turns": self.max_turns,
            "budget_usd": self.budget_usd,
        }
