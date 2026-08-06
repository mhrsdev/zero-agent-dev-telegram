"""Unit tests for zero.agents.definition — ADR T-7.1."""
from __future__ import annotations

import pytest
from zero.agents.definition import (
    AGENT_TYPE_TO_EFFORT_TIER,
    EFFORT_TIERS,
    AgentDefinition,
    AgentType,
)
from zero.core.scope import Scope


@pytest.fixture
def dev_scope() -> Scope:
    return Scope.development(
        org_id="org_01HABC", workspace_id="ws_01HABC",
        project_id="prj_01HABC", group_id="grp_01HABC", topic_id=100,
    ).with_default_memory_scope()


class TestAgentDefinition:
    def test_basic_construction(self, dev_scope: Scope) -> None:
        a = AgentDefinition(
            name="Coding Agent",
            agent_type=AgentType.CODING,
            scope=dev_scope,
            system_prompt="You are a coding agent.",
            effort_tier="zero/coding",
            tool_allowlist=frozenset({"read_file", "write_file"}),
        )
        assert a.agent_type is AgentType.CODING
        assert a.effort_tier == "zero/coding"

    def test_invalid_effort_tier_rejected(self, dev_scope: Scope) -> None:
        with pytest.raises(ValueError, match="not in allowed set"):
            AgentDefinition(
                name="bad",
                agent_type=AgentType.CODING,
                scope=dev_scope,
                system_prompt="x",
                effort_tier="gpt-4",  # not in EFFORT_TIERS
            )

    def test_default_mapping(self, dev_scope: Scope) -> None:
        """Each agent type has a default effort tier."""
        for at in AgentType:
            tier = AGENT_TYPE_TO_EFFORT_TIER[at]
            assert tier in EFFORT_TIERS

    def test_max_turns_validation(self, dev_scope: Scope) -> None:
        with pytest.raises(ValueError, match="max_turns"):
            AgentDefinition(
                name="x", agent_type=AgentType.CODING, scope=dev_scope,
                system_prompt="x", effort_tier="zero/coding", max_turns=0,
            )
        with pytest.raises(ValueError, match="max_turns"):
            AgentDefinition(
                name="x", agent_type=AgentType.CODING, scope=dev_scope,
                system_prompt="x", effort_tier="zero/coding", max_turns=10000,
            )

    def test_budget_must_be_positive(self, dev_scope: Scope) -> None:
        with pytest.raises(ValueError, match="budget_usd"):
            AgentDefinition(
                name="x", agent_type=AgentType.CODING, scope=dev_scope,
                system_prompt="x", effort_tier="zero/coding", budget_usd=0,
            )

    def test_to_log_dict_doesnt_leak_prompt(self, dev_scope: Scope) -> None:
        a = AgentDefinition(
            name="x", agent_type=AgentType.CODING, scope=dev_scope,
            system_prompt="SECRET_SYSTEM_PROMPT",
            effort_tier="zero/coding",
        )
        d = a.to_log_dict()
        # system_prompt should NOT be in log dict
        assert "system_prompt" not in d
        assert "SECRET_SYSTEM_PROMPT" not in str(d)
