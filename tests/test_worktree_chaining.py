"""Regression: dependent task worktrees must see their dependencies' work.

Real run (2026-08-28, execution exec_klkodw2yuyhzeiz5dn6a4s1i): every task
worktree branched from the bare repository default, so the "run the tests"
task's worktree contained NO test suite (earlier tasks' files lived only
in their own uncommitted worktrees) and the evidence unittest run exited
1. Two-part fix pinned here:
  1. WorktreeService.complete_worktree(succeeded=True) commits the full
     worktree state onto the task branch (evidence checkpoint);
  2. AgentRuntime._dependency_worktree_bases resolves a task's worktree
     base from its SUCCEEDED dependency worktree branches.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from zero.app.services import build_services
from zero.app.worker_service import DependencySpec, TaskSpec
from zero.app.worktree_service import WorktreeService
from zero.config import Settings
from zero.domain.plans import PlanRevisionContent
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations


@pytest.fixture
def services(test_settings: Settings, tmp_path):
    database = Database(test_settings)
    apply_migrations(database)
    s = build_services(test_settings, database)
    s.worktree = WorktreeService(
        s.worktree._repo,
        s.worktree._audit_repo,
        s.worktree._authz,
        worktree_root=str(tmp_path / "worktrees"),
        allowed_commands=frozenset({"echo", "sleep"}),
    )
    return s


def _make_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "core.autocrlf", "false"],
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@test.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "README.md").write_text("# Test Repo\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "initial"], check=True, capture_output=True)
    return repo


def _make_approved_plan(services) -> tuple:
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(owner_id=owner.id, name="P")
    event = services.plans.ingest_conversation_event(
        project_id=project.id,
        actor_id=owner.id,
        source="web",
        origin_kind="authenticated_human",
        content="Add a feature.",
    )
    plan = services.plans.create_plan(project_id=project.id, actor_id=owner.id)
    content = PlanRevisionContent(
        objective="Add a feature",
        scope=(),
        constraints=(),
        acceptance_criteria=("Works",),
        risks=(),
        unresolved_questions=(),
        source_event_ids=(event.id,),
    )
    services.plans.propose_revision(
        plan_id=plan.id, project_id=project.id, actor_id=owner.id, content=content
    )
    _, handoff = services.plans.approve_revision(
        plan_id=plan.id,
        project_id=project.id,
        actor_id=owner.id,
        expected_revision_number=1,
        idempotency_key="a1",
    )
    return owner, project, plan, handoff


@pytest.fixture
def project_with_repo_and_plan(services, tmp_path):
    owner, project, plan, handoff = _make_approved_plan(services)
    repo_path = _make_repo(tmp_path)
    repo = services.worktree.register_repository(
        project_id=project.id,
        actor_id=owner.id,
        name="test-repo",
        local_path=str(repo_path),
        default_base_revision="main",
    )
    return owner, project, plan, handoff, repo


@pytest.fixture()
def two_task_execution(services, project_with_repo_and_plan):
    """Execution with A -> B dependency and a registered repository."""
    owner, project, _plan, handoff, repo = project_with_repo_and_plan
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        project_id=project.id,
        actor_id=owner.id,
        task_specs=[
            TaskSpec(key="A", objective="Create the module"),
            TaskSpec(key="B", objective="Run the tests over A's work"),
        ],
        dependency_specs=[
            DependencySpec(task_key="B", depends_on_key="A"),
        ],
    )
    tasks = services.worker.list_tasks(
        execution.id,
        project_id=project.id,
        actor_id=owner.id,
    )
    by_key = {}
    # TaskSpec order is preserved in creation; map by objective.
    for t in tasks:
        by_key[t.objective] = t
    return services, owner, project, repo, execution, by_key


def test_succeeded_dependency_becomes_worktree_base(two_task_execution):
    services, owner, project, repo, execution, by_key = two_task_execution
    task_a = by_key["Create the module"]
    task_b = by_key["Run the tests over A's work"]

    wt_a = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repo.id,
        execution_id=execution.id,
        task_id=task_a.id,
        actor_id=owner.id,
    )
    services.worktree.activate_worktree(
        project_id=project.id, worktree_id=wt_a.id, actor_id=owner.id
    )
    (Path(wt_a.worktree_path) / "module.py").write_text("VALUE = 1\n")
    services.worktree.complete_worktree(
        project_id=project.id,
        worktree_id=wt_a.id,
        actor_id=owner.id,
        succeeded=True,
    )

    runtime = services.runtime
    base, extra = runtime._dependency_worktree_bases(
        project_id=project.id,
        execution_id=execution.id,
        task_id=task_b.id,
        actor_id=owner.id,
        source="system",
    )
    assert base == wt_a.branch_name, "B must branch from A's evidence checkpoint"
    assert extra == ()


def test_dependency_base_chain_carries_files_forward(two_task_execution):
    """End-to-end on the service boundary: B's worktree contains A's file."""
    services, owner, project, repo, execution, by_key = two_task_execution
    task_a = by_key["Create the module"]
    task_b = by_key["Run the tests over A's work"]

    wt_a = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repo.id,
        execution_id=execution.id,
        task_id=task_a.id,
        actor_id=owner.id,
    )
    services.worktree.activate_worktree(
        project_id=project.id, worktree_id=wt_a.id, actor_id=owner.id
    )
    (Path(wt_a.worktree_path) / "module.py").write_text("VALUE = 1\n")
    services.worktree.complete_worktree(
        project_id=project.id,
        worktree_id=wt_a.id,
        actor_id=owner.id,
        succeeded=True,
    )

    base, _extra = services.runtime._dependency_worktree_bases(
        project_id=project.id,
        execution_id=execution.id,
        task_id=task_b.id,
        actor_id=owner.id,
        source="system",
    )
    wt_b = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repo.id,
        execution_id=execution.id,
        task_id=task_b.id,
        actor_id=owner.id,
        base_revision=base,
    )
    assert (Path(wt_b.worktree_path) / "module.py").exists(), (
        "the dependent worktree must contain the dependency's committed files"
    )
    # And it is a clean git state on B's own branch.
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=wt_b.worktree_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert status.strip() == ""


def test_no_succeeded_dependency_falls_back_to_default(two_task_execution):
    """A task whose dependency worktree did not succeed keeps the old
    repository-default base (no fabricated state)."""
    services, owner, project, repo, execution, by_key = two_task_execution
    task_a = by_key["Create the module"]
    task_b = by_key["Run the tests over A's work"]

    wt_a = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repo.id,
        execution_id=execution.id,
        task_id=task_a.id,
        actor_id=owner.id,
    )
    services.worktree.activate_worktree(
        project_id=project.id, worktree_id=wt_a.id, actor_id=owner.id
    )
    # A FAILS: no commit, worktree state failed.
    services.worktree.complete_worktree(
        project_id=project.id,
        worktree_id=wt_a.id,
        actor_id=owner.id,
        succeeded=False,
    )
    base, extra = services.runtime._dependency_worktree_bases(
        project_id=project.id,
        execution_id=execution.id,
        task_id=task_b.id,
        actor_id=owner.id,
        source="system",
    )
    assert base is None and extra == ()
