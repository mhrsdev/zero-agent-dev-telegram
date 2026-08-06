"""Zero v2 agents package — Phase 7.

Agent definitions, Run lifecycle, Orchestrator (sub-agent context isolation),
Budget enforcement, Sandbox execution.

Per ADR 0004 / Phase R:
    - Zero is a pure HTTP consumer of Router via OpenAI protocol.
    - Zero NEVER picks models — structural test enforces this.
    - Cost is read from Router response header ``x-zero-cost-usd``,
      never computed locally.
"""
from __future__ import annotations

from zero.agents.budget import Budget, BudgetExceededError, BudgetTracker
from zero.agents.definition import EFFORT_TIERS, AgentDefinition, AgentType
from zero.agents.loop import AgentLoop, AgentLoopResult
from zero.agents.orchestrator import Orchestrator, OrchestratorError
from zero.agents.router_client import RouterClient, RouterResponse
from zero.agents.run import AgentRun, AgentRunStatus
from zero.agents.sandbox import Sandbox, SandboxError, SandboxSpec

__all__ = [
    "EFFORT_TIERS",
    "AgentDefinition",
    "AgentLoop",
    "AgentLoopResult",
    "AgentRun",
    "AgentRunStatus",
    "AgentType",
    "Budget",
    "BudgetExceededError",
    "BudgetTracker",
    "Orchestrator",
    "OrchestratorError",
    "RouterClient",
    "RouterResponse",
    "Sandbox",
    "SandboxError",
    "SandboxSpec",
]
