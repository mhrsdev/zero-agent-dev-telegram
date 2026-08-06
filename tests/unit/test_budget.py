"""Unit tests for zero.agents.budget — ADR T-7.4."""
from __future__ import annotations

import pytest
from zero.agents.budget import Budget, BudgetExceededError, BudgetTracker
from zero.core.scope import Scope


@pytest.fixture
def dev_scope() -> Scope:
    return Scope.development(
        org_id="org_01HABC", workspace_id="ws_01HABC",
        project_id="prj_01HABC", group_id="grp_01HABC", topic_id=100,
    ).with_default_memory_scope()


class TestBudget:
    def test_initial_state(self) -> None:
        b = Budget(cap_usd=10.0)
        assert b.remaining_usd == 10.0
        assert b.utilization == 0.0
        assert b.is_exceeded is False
        assert b.is_warning is False

    def test_check_before_call_passes_when_under_cap(self) -> None:
        b = Budget(cap_usd=10.0)
        b.check_before_call()  # should not raise

    def test_check_before_call_raises_when_exceeded(self) -> None:
        b = Budget(cap_usd=1.0)
        b.record_spend(1.5, scope=Scope.personal(user_id="usr_01H").with_default_memory_scope())
        with pytest.raises(BudgetExceededError, match="already exceeded"):
            b.check_before_call()

    def test_warning_threshold(self, dev_scope: Scope) -> None:
        b = Budget(cap_usd=10.0, warning_threshold=0.8)
        b.record_spend(8.0, scope=dev_scope)  # 80%
        assert b.is_warning is True
        assert b.is_exceeded is False

    def test_exceeded_after_cap(self, dev_scope: Scope) -> None:
        b = Budget(cap_usd=1.0)
        b.record_spend(1.0, scope=dev_scope)
        assert b.is_exceeded is True


class TestBudgetTracker:
    def test_check_no_budget_set_passes(self, dev_scope: Scope) -> None:
        """No budget configured → no check fails (unlimited by default)."""
        tracker = BudgetTracker()
        tracker.check(scope=dev_scope)  # should not raise

    def test_check_with_project_budget(self, dev_scope: Scope) -> None:
        tracker = BudgetTracker()
        tracker.set_project_budget(dev_scope.project_id, cap_usd=5.0)  # type: ignore[arg-type]
        tracker.check(scope=dev_scope)
        tracker.record(amount_usd=4.0, scope=dev_scope)
        tracker.check(scope=dev_scope)  # still under cap
        tracker.record(amount_usd=2.0, scope=dev_scope)  # now exceeded
        with pytest.raises(BudgetExceededError):
            tracker.check(scope=dev_scope)

    def test_check_with_agent_budget(self, dev_scope: Scope) -> None:
        tracker = BudgetTracker()
        tracker.set_agent_budget("agt_01HALICE", cap_usd=1.0)
        tracker.check(scope=dev_scope, agent_def_id="agt_01HALICE")
        tracker.record(amount_usd=0.5, scope=dev_scope, agent_def_id="agt_01HALICE")
        tracker.check(scope=dev_scope, agent_def_id="agt_01HALICE")
        tracker.record(amount_usd=0.6, scope=dev_scope, agent_def_id="agt_01HALICE")
        with pytest.raises(BudgetExceededError):
            tracker.check(scope=dev_scope, agent_def_id="agt_01HALICE")

    def test_kill_switch_blocks_all(self, dev_scope: Scope) -> None:
        tracker = BudgetTracker()
        tracker.activate_kill_switch()
        with pytest.raises(BudgetExceededError, match="kill switch"):
            tracker.check(scope=dev_scope)

    def test_kill_switch_deactivate(self, dev_scope: Scope) -> None:
        tracker = BudgetTracker()
        tracker.activate_kill_switch()
        tracker.deactivate_kill_switch()
        tracker.check(scope=dev_scope)  # should not raise
