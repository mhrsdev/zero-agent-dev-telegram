"""Integration review and merge gate tests — covers all M11 validation gates.

Per PLAN.md M11 validation:
- Compatible independent changes integrate cleanly.
- Conflicting schema/type/API changes are detected.
- A deceptive green unit test cannot bypass failed combined tests.
- Human-decision conflict pauses merge.
- Rejected integration does not update accepted memory.
- Merge provenance traces every included task and approval.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from zero.app.services import build_services
from zero.app.worktree_service import WorktreeService
from zero.config import Settings
from zero.domain.authorization import AuthorizationError
from zero.domain.integration import (
    MergeGateError,
)
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations


@pytest.fixture
def services(test_settings: Settings, tmp_path):
    database = Database(test_settings)
    apply_migrations(database)
    s = build_services(test_settings, database)
    # Override the worktree service with a tmp_path root.
    s.worktree = WorktreeService(
        s.worktree._repo,
        s.worktree._audit_repo,
        s.worktree._authz,
        worktree_root=str(tmp_path / "worktrees"),
    )
    return s


def _make_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@test.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"], check=True
    )
    (repo / "README.md").write_text("# Test Repo\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"],
        check=True, capture_output=True,
    )
    return repo


def _setup_execution_with_two_tasks(services, tmp_path):
    """Create a project, repo, approved plan, execution with 2 tasks,
    and worktrees with changes for each task."""
    from zero.app.worker_service import TaskSpec
    from zero.domain.plans import PlanRevisionContent

    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(
        owner_id=owner.id, name="Project A"
    )
    repo_path = _make_repo(tmp_path)
    repo = services.worktree.register_repository(
        project_id=project.id, actor_id=owner.id, name="test-repo",
        local_path=str(repo_path), default_base_revision="main",
    )
    event = services.plans.ingest_conversation_event(
        project_id=project.id, actor_id=owner.id, source="web",
        origin_kind="authenticated_human", content="Add two features."
    )
    plan = services.plans.create_plan(
        project_id=project.id, actor_id=owner.id
    )
    content = PlanRevisionContent(
        objective="Add two features", scope=(), constraints=(),
        acceptance_criteria=("Both features work",), risks=(),
        unresolved_questions=(), source_event_ids=(event.id,)
    )
    services.plans.propose_revision(
        plan_id=plan.id, actor_id=owner.id, content=content
    )
    _, handoff = services.plans.approve_revision(
        plan_id=plan.id, actor_id=owner.id,
        expected_revision_number=1, idempotency_key="a1"
    )
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id, actor_id=owner.id,
        task_specs=[
            TaskSpec(key="A", objective="Task A"),
            TaskSpec(key="B", objective="Task B"),
        ],
    )
    tasks = services.worker.list_tasks(execution.id)
    return owner, project, repo, execution, tasks


# ----------------------------------------------------------------------
# Impact-set derivation
# ----------------------------------------------------------------------


def test_derive_impact_set_from_task_diffs(services, tmp_path) -> None:
    """Per PLAN.md M11: 'Impact-set derivation from task outputs.'"""
    owner, project, repo, execution, tasks = _setup_execution_with_two_tasks(
        services, tmp_path
    )
    task_a, task_b = tasks[0], tasks[1]
    # Create worktrees and make changes.
    wt_a = services.worktree.create_worktree(
        project_id=project.id, repository_id=repo.id,
        execution_id=execution.id, task_id=task_a.id, actor_id=owner.id
    )
    (Path(wt_a.worktree_path) / "feature_a.py").write_text("print('A')")
    services.worktree.capture_diff(
        project_id=project.id, worktree_id=wt_a.id, task_id=task_a.id,
        actor_id=owner.id
    )
    wt_b = services.worktree.create_worktree(
        project_id=project.id, repository_id=repo.id,
        execution_id=execution.id, task_id=task_b.id, actor_id=owner.id
    )
    (Path(wt_b.worktree_path) / "feature_b.py").write_text("print('B')")
    services.worktree.capture_diff(
        project_id=project.id, worktree_id=wt_b.id, task_id=task_b.id,
        actor_id=owner.id
    )
    # Derive the impact set.
    impact = services.integration.derive_impact_set(
        execution_id=execution.id,
        task_ids=(task_a.id, task_b.id),
    )
    # Both files should be in the impact set.
    paths = {e.file_path for e in impact}
    assert "feature_a.py" in paths
    assert "feature_b.py" in paths


# ----------------------------------------------------------------------
# Compatible independent changes integrate cleanly
# ----------------------------------------------------------------------


def test_compatible_changes_integrate_cleanly(services, tmp_path) -> None:
    """Per PLAN.md M11: 'Compatible independent changes integrate
    cleanly.'"""
    owner, project, repo, execution, tasks = _setup_execution_with_two_tasks(
        services, tmp_path
    )
    task_a, task_b = tasks[0], tasks[1]
    # Create worktrees with non-conflicting changes.
    wt_a = services.worktree.create_worktree(
        project_id=project.id, repository_id=repo.id,
        execution_id=execution.id, task_id=task_a.id, actor_id=owner.id
    )
    (Path(wt_a.worktree_path) / "file_a.py").write_text("a = 1")
    services.worktree.capture_diff(
        project_id=project.id, worktree_id=wt_a.id, task_id=task_a.id,
        actor_id=owner.id
    )
    wt_b = services.worktree.create_worktree(
        project_id=project.id, repository_id=repo.id,
        execution_id=execution.id, task_id=task_b.id, actor_id=owner.id
    )
    (Path(wt_b.worktree_path) / "file_b.py").write_text("b = 2")
    services.worktree.capture_diff(
        project_id=project.id, worktree_id=wt_b.id, task_id=task_b.id,
        actor_id=owner.id
    )
    # Create an integration review.
    review = services.integration.create_review(
        project_id=project.id, execution_id=execution.id,
        source_task_ids=(task_a.id, task_b.id), actor_id=owner.id,
    )
    # No contract files were changed, so no conflicts.
    assert review.conflict_classification == "none"
    assert len(review.conflict_details) == 0
    # Record combined test result as pass.
    review = services.integration.record_combined_test_result(
        project_id=project.id, review_id=review.id,
        result="pass", actor_id=owner.id,
    )
    assert review.state == "approved"
    assert review.combined_test_result == "pass"


# ----------------------------------------------------------------------
# Conflicting schema/type/API changes are detected
# ----------------------------------------------------------------------


def test_contract_changes_are_detected(services, tmp_path) -> None:
    """Per PLAN.md M11: 'Conflicting schema/type/API changes are
    detected.'"""
    owner, project, repo, execution, tasks = _setup_execution_with_two_tasks(
        services, tmp_path
    )
    task_a = tasks[0]
    # Create a worktree with a contract file change.
    wt_a = services.worktree.create_worktree(
        project_id=project.id, repository_id=repo.id,
        execution_id=execution.id, task_id=task_a.id, actor_id=owner.id
    )
    # Create a schema file (contract).
    (Path(wt_a.worktree_path) / "schema.sql").write_text(
        "CREATE TABLE users (id INTEGER);"
    )
    services.worktree.capture_diff(
        project_id=project.id, worktree_id=wt_a.id, task_id=task_a.id,
        actor_id=owner.id
    )
    # Create an integration review.
    review = services.integration.create_review(
        project_id=project.id, execution_id=execution.id,
        source_task_ids=(task_a.id,), actor_id=owner.id,
    )
    # The schema file should be in touched_contracts.
    assert "schema.sql" in review.touched_contracts
    # A conflict detail should exist for the contract change.
    assert len(review.conflict_details) >= 1
    assert review.conflict_classification in ("low_risk", "human_decision_required")


# ----------------------------------------------------------------------
# Deceptive green unit test cannot bypass failed combined tests
# ----------------------------------------------------------------------


def test_deceptive_green_test_cannot_bypass_failed_combined_tests(
    services, tmp_path
) -> None:
    """Per PLAN.md M11: 'A deceptive green unit test cannot bypass
    failed combined tests.'"""
    owner, project, repo, execution, tasks = _setup_execution_with_two_tasks(
        services, tmp_path
    )
    task_a, task_b = tasks[0], tasks[1]
    # Create worktrees with changes.
    wt_a = services.worktree.create_worktree(
        project_id=project.id, repository_id=repo.id,
        execution_id=execution.id, task_id=task_a.id, actor_id=owner.id
    )
    (Path(wt_a.worktree_path) / "schema.sql").write_text("CREATE TABLE x;")
    services.worktree.capture_diff(
        project_id=project.id, worktree_id=wt_a.id, task_id=task_a.id,
        actor_id=owner.id
    )
    wt_b = services.worktree.create_worktree(
        project_id=project.id, repository_id=repo.id,
        execution_id=execution.id, task_id=task_b.id, actor_id=owner.id
    )
    (Path(wt_b.worktree_path) / "schema.sql").write_text("CREATE TABLE y;")
    services.worktree.capture_diff(
        project_id=project.id, worktree_id=wt_b.id, task_id=task_b.id,
        actor_id=owner.id
    )
    # Create an integration review.
    review = services.integration.create_review(
        project_id=project.id, execution_id=execution.id,
        source_task_ids=(task_a.id, task_b.id), actor_id=owner.id,
    )
    # Record combined test result as FAIL.
    review = services.integration.record_combined_test_result(
        project_id=project.id, review_id=review.id,
        result="fail", actor_id=owner.id,
    )
    # The review should be rejected or paused, not approved.
    assert review.state in ("rejected", "human_decision_paused")
    # Cannot create a merge proposal from a non-approved review.
    with pytest.raises(MergeGateError):
        services.integration.create_merge_proposal(
            project_id=project.id, review_id=review.id,
            execution_id=execution.id,
            source_tasks=(task_a.id, task_b.id),
            actor_id=owner.id,
        )


# ----------------------------------------------------------------------
# Human-decision conflict pauses merge
# ----------------------------------------------------------------------


def test_human_decision_conflict_pauses_merge(services, tmp_path) -> None:
    """Per PLAN.md M11: 'Human-decision conflict pauses merge.'"""
    owner, project, repo, execution, tasks = _setup_execution_with_two_tasks(
        services, tmp_path
    )
    task_a = tasks[0]
    # Create a worktree with an API contract change.
    wt_a = services.worktree.create_worktree(
        project_id=project.id, repository_id=repo.id,
        execution_id=execution.id, task_id=task_a.id, actor_id=owner.id
    )
    (Path(wt_a.worktree_path) / "api" / "types.py").parent.mkdir(
        parents=True, exist_ok=True
    )
    (Path(wt_a.worktree_path) / "api" / "types.py").write_text(
        "class User: pass"
    )
    services.worktree.capture_diff(
        project_id=project.id, worktree_id=wt_a.id, task_id=task_a.id,
        actor_id=owner.id
    )
    # Create an integration review.
    review = services.integration.create_review(
        project_id=project.id, execution_id=execution.id,
        source_task_ids=(task_a.id,), actor_id=owner.id,
    )
    # An API change should be classified as human_decision_required.
    assert review.conflict_classification == "human_decision_required"
    # Record combined test result as fail (escalates to paused).
    review = services.integration.record_combined_test_result(
        project_id=project.id, review_id=review.id,
        result="fail", actor_id=owner.id,
    )
    assert review.state == "human_decision_paused"


# ----------------------------------------------------------------------
# Merge requires explicit authority
# ----------------------------------------------------------------------


def test_merge_requires_authorization(services, tmp_path) -> None:
    """Per PLAN.md M11: 'Merge requires explicit authority and passing
    gates.'"""
    owner, project, repo, execution, tasks = _setup_execution_with_two_tasks(
        services, tmp_path
    )
    task_a, task_b = tasks[0], tasks[1]
    # Create worktrees with non-conflicting changes.
    wt_a = services.worktree.create_worktree(
        project_id=project.id, repository_id=repo.id,
        execution_id=execution.id, task_id=task_a.id, actor_id=owner.id
    )
    (Path(wt_a.worktree_path) / "file_a.py").write_text("a = 1")
    services.worktree.capture_diff(
        project_id=project.id, worktree_id=wt_a.id, task_id=task_a.id,
        actor_id=owner.id
    )
    wt_b = services.worktree.create_worktree(
        project_id=project.id, repository_id=repo.id,
        execution_id=execution.id, task_id=task_b.id, actor_id=owner.id
    )
    (Path(wt_b.worktree_path) / "file_b.py").write_text("b = 2")
    services.worktree.capture_diff(
        project_id=project.id, worktree_id=wt_b.id, task_id=task_b.id,
        actor_id=owner.id
    )
    # Create review and pass combined tests.
    review = services.integration.create_review(
        project_id=project.id, execution_id=execution.id,
        source_task_ids=(task_a.id, task_b.id), actor_id=owner.id,
    )
    review = services.integration.record_combined_test_result(
        project_id=project.id, review_id=review.id,
        result="pass", actor_id=owner.id,
    )
    # Create a merge proposal.
    proposal = services.integration.create_merge_proposal(
        project_id=project.id, review_id=review.id,
        execution_id=execution.id,
        source_tasks=(task_a.id, task_b.id),
        actor_id=owner.id,
    )
    # A viewer cannot approve the merge.
    viewer = services.identity.create_user(display_name="Viewer")
    services.identity.add_member(
        project_id=project.id, actor_id=owner.id,
        member_id=viewer.id, role="viewer"
    )
    with pytest.raises(AuthorizationError):
        services.integration.approve_merge(
            project_id=project.id, proposal_id=proposal.id,
            actor_id=viewer.id,
        )
    # The owner can approve.
    proposal = services.integration.approve_merge(
        project_id=project.id, proposal_id=proposal.id,
        actor_id=owner.id,
    )
    assert proposal.state == "approved"
    # Execute the merge.
    proposal = services.integration.execute_merge(
        project_id=project.id, proposal_id=proposal.id,
        actor_id=owner.id,
    )
    assert proposal.state == "merged"
    assert proposal.merged_at is not None


# ----------------------------------------------------------------------
# Rejected integration does not update accepted memory
# ----------------------------------------------------------------------


def test_rejected_integration_does_not_update_memory(
    services, tmp_path
) -> None:
    """Per PLAN.md M11: 'Rejected integration does not update accepted
    memory.'"""
    owner, project, repo, execution, tasks = _setup_execution_with_two_tasks(
        services, tmp_path
    )
    task_a = tasks[0]
    # Create a worktree with a contract change.
    wt_a = services.worktree.create_worktree(
        project_id=project.id, repository_id=repo.id,
        execution_id=execution.id, task_id=task_a.id, actor_id=owner.id
    )
    (Path(wt_a.worktree_path) / "schema.sql").write_text("CREATE TABLE x;")
    services.worktree.capture_diff(
        project_id=project.id, worktree_id=wt_a.id, task_id=task_a.id,
        actor_id=owner.id
    )
    # Create a review.
    review = services.integration.create_review(
        project_id=project.id, execution_id=execution.id,
        source_task_ids=(task_a.id,), actor_id=owner.id,
    )
    # Record combined test result as fail.
    review = services.integration.record_combined_test_result(
        project_id=project.id, review_id=review.id,
        result="fail", actor_id=owner.id,
    )
    # The review is rejected or paused; no merge proposal can be created.
    if review.state == "approved":
        proposal = services.integration.create_merge_proposal(
            project_id=project.id, review_id=review.id,
            execution_id=execution.id,
            source_tasks=(task_a.id,),
            actor_id=owner.id,
        )
        services.integration.reject_merge(
            project_id=project.id, proposal_id=proposal.id,
            actor_id=owner.id,
        )
        proposal = services.integration.get_proposal(project.id, proposal.id)
        assert proposal.state == "rejected"
    else:
        # Review is rejected/paused; no merge proposal.
        with pytest.raises(MergeGateError):
            services.integration.create_merge_proposal(
                project_id=project.id, review_id=review.id,
                execution_id=execution.id,
                source_tasks=(task_a.id,),
                actor_id=owner.id,
            )
    # No RAG documents should have been ingested from the rejected
    # integration (post-integration memory/RAG update only from accepted
    # results).
    rag_docs = services.artifacts.list_rag_documents(project.id)
    assert len(rag_docs) == 0


# ----------------------------------------------------------------------
# Merge provenance traces every included task and approval
# ----------------------------------------------------------------------


def test_merge_provenance_traces_tasks_and_approval(
    services, tmp_path
) -> None:
    """Per PLAN.md M11: 'Merge provenance traces every included task
    and approval.'"""
    owner, project, repo, execution, tasks = _setup_execution_with_two_tasks(
        services, tmp_path
    )
    task_a, task_b = tasks[0], tasks[1]
    # Create worktrees with non-conflicting changes.
    wt_a = services.worktree.create_worktree(
        project_id=project.id, repository_id=repo.id,
        execution_id=execution.id, task_id=task_a.id, actor_id=owner.id
    )
    (Path(wt_a.worktree_path) / "file_a.py").write_text("a = 1")
    services.worktree.capture_diff(
        project_id=project.id, worktree_id=wt_a.id, task_id=task_a.id,
        actor_id=owner.id
    )
    wt_b = services.worktree.create_worktree(
        project_id=project.id, repository_id=repo.id,
        execution_id=execution.id, task_id=task_b.id, actor_id=owner.id
    )
    (Path(wt_b.worktree_path) / "file_b.py").write_text("b = 2")
    services.worktree.capture_diff(
        project_id=project.id, worktree_id=wt_b.id, task_id=task_b.id,
        actor_id=owner.id
    )
    # Create review and pass combined tests.
    review = services.integration.create_review(
        project_id=project.id, execution_id=execution.id,
        source_task_ids=(task_a.id, task_b.id), actor_id=owner.id,
    )
    review = services.integration.record_combined_test_result(
        project_id=project.id, review_id=review.id,
        result="pass", actor_id=owner.id,
    )
    # Create and approve a merge proposal.
    proposal = services.integration.create_merge_proposal(
        project_id=project.id, review_id=review.id,
        execution_id=execution.id,
        source_tasks=(task_a.id, task_b.id),
        actor_id=owner.id,
    )
    proposal = services.integration.approve_merge(
        project_id=project.id, proposal_id=proposal.id,
        actor_id=owner.id,
    )
    proposal = services.integration.execute_merge(
        project_id=project.id, proposal_id=proposal.id,
        actor_id=owner.id,
    )
    # The proposal traces the source tasks.
    assert task_a.id in proposal.source_tasks
    assert task_b.id in proposal.source_tasks
    # The proposal traces the approver.
    assert proposal.approved_by == owner.id
    # The proposal traces the merge time.
    assert proposal.merged_at is not None
    # Audit events exist for the merge.
    audit_events = services.audit.list_for_project(
        project_id=project.id, actor_id=project.owner_user_id, limit=100
    )
    merge_events = [
        e for e in audit_events
        if e.operation in ("merge.propose", "merge.approve", "merge.execute")
    ]
    assert len(merge_events) >= 3
