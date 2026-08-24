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

import os
import subprocess
import sys
import time
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

_posix_only = pytest.mark.skipif(
    os.name == "nt",
    reason="depends on POSIX shell, setsid, or symlink privileges",
)


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
        allowed_commands=frozenset({"echo", "sh", "sleep"}),
    )
    return s


def _make_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "core.autocrlf", "false"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@test.com"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "README.md").write_text("# Test Repo\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
    )
    return repo


@_posix_only
def test_integration_git_scrubs_git_configuration_environment(
    services, tmp_path: Path, monkeypatch
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text(
        "#!/bin/sh\n"
        'printf \'%s|%s|%s\' "$GIT_CONFIG_NOSYSTEM" "$GIT_CONFIG_GLOBAL" '
        '"$GIT_TERMINAL_PROMPT"\n'
    )
    fake_git.chmod(0o700)
    monkeypatch.setenv("PATH", str(fake_bin))

    output = services.integration._git(tmp_path, "status")

    assert output == "1|/dev/null|0"


@_posix_only
def test_integration_git_rejects_output_over_limit(services, tmp_path: Path, monkeypatch) -> None:
    fake_bin = tmp_path / "large-bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text("#!/bin/sh\n/usr/bin/head -c 8388609 /dev/zero\n")
    fake_git.chmod(0o700)
    monkeypatch.setenv("PATH", str(fake_bin))

    with pytest.raises(MergeGateError, match="output exceeded"):
        services.integration._git(tmp_path, "status")


@_posix_only
def test_integration_bounded_test_kills_detached_descendants(services, tmp_path: Path) -> None:
    pid_file = tmp_path / "detached-child.pid"
    script = tmp_path / "spawn_detached.py"
    script.write_text(
        "import os, pathlib, subprocess, sys, time\n"
        'child = subprocess.Popen(["setsid", "sleep", "30"], '
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid))\n"
        "time.sleep(30)\n"
    )
    child_pid = None
    try:
        exit_code, timed_out, _stdout, _stderr = services.integration._run_bounded_test(
            sys.executable,
            (str(script), str(pid_file)),
            cwd=tmp_path,
            timeout_seconds=1,
        )
        child_pid = int(pid_file.read_text())
        for _ in range(20):
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            pytest.fail("detached combined-test descendant survived timeout cleanup")
        assert exit_code is None
        assert timed_out is True
    finally:
        if child_pid is not None:
            try:
                os.kill(child_pid, 9)
            except ProcessLookupError:
                pass


@_posix_only
def test_copy_untracked_files_rejects_symlink_to_host_file(services, tmp_path: Path) -> None:
    source = _make_repo(tmp_path, "symlink-source")
    destination = tmp_path / "symlink-destination"
    destination.mkdir()
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("host-only\n")
    os.symlink(outside, source / "leaked.txt")

    with pytest.raises(MergeGateError, match="symlink"):
        services.integration._copy_untracked_files(source, destination)

    assert not (destination / "leaked.txt").exists()


def _setup_execution_with_two_tasks(services, tmp_path):
    """Create a project, repo, approved plan, execution with 2 tasks,
    and worktrees with changes for each task."""
    from zero.app.worker_service import TaskSpec
    from zero.domain.plans import PlanRevisionContent

    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(owner_id=owner.id, name="Project A")
    repo_path = _make_repo(tmp_path)
    repo = services.worktree.register_repository(
        project_id=project.id,
        actor_id=owner.id,
        name="test-repo",
        local_path=str(repo_path),
        default_base_revision="main",
    )
    event = services.plans.ingest_conversation_event(
        project_id=project.id,
        actor_id=owner.id,
        source="web",
        origin_kind="authenticated_human",
        content="Add two features.",
    )
    plan = services.plans.create_plan(project_id=project.id, actor_id=owner.id)
    content = PlanRevisionContent(
        objective="Add two features",
        scope=(),
        constraints=(),
        acceptance_criteria=("Both features work",),
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
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        project_id=project.id,
        actor_id=owner.id,
        task_specs=[
            TaskSpec(key="A", objective="Task A"),
            TaskSpec(key="B", objective="Task B"),
        ],
    )
    tasks = services.worker.list_tasks(
        execution.id,
        project_id=execution.project_id,
        actor_id=services.identity.get_project(execution.project_id).owner_user_id,
    )
    return owner, project, repo, execution, tasks


# ----------------------------------------------------------------------
# Impact-set derivation
# ----------------------------------------------------------------------


def test_derive_impact_set_from_task_diffs(services, tmp_path) -> None:
    """Per PLAN.md M11: 'Impact-set derivation from task outputs.'"""
    owner, project, repo, execution, tasks = _setup_execution_with_two_tasks(services, tmp_path)
    task_a, task_b = tasks[0], tasks[1]
    # Create worktrees and make changes.
    wt_a = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repo.id,
        execution_id=execution.id,
        task_id=task_a.id,
        actor_id=owner.id,
    )
    (Path(wt_a.worktree_path) / "feature_a.py").write_text("print('A')")
    services.worktree.capture_diff(
        project_id=project.id, worktree_id=wt_a.id, task_id=task_a.id, actor_id=owner.id
    )
    wt_b = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repo.id,
        execution_id=execution.id,
        task_id=task_b.id,
        actor_id=owner.id,
    )
    (Path(wt_b.worktree_path) / "feature_b.py").write_text("print('B')")
    services.worktree.capture_diff(
        project_id=project.id, worktree_id=wt_b.id, task_id=task_b.id, actor_id=owner.id
    )
    # Derive the impact set.
    impact = services.integration.derive_impact_set(
        project_id=project.id,
        execution_id=execution.id,
        task_ids=(task_a.id, task_b.id),
        actor_id=owner.id,
    )
    # Both files should be in the impact set.
    paths = {e.file_path for e in impact}
    assert "feature_a.py" in paths
    assert "feature_b.py" in paths


@_posix_only
def test_automatic_combined_test_records_isolated_evidence(tmp_path) -> None:
    """A review runs its configured combined test in an isolated workspace."""
    settings = Settings.load_for_test(
        worktree_allowed_commands=("sh",),
        worktree_root=str(tmp_path / "task-worktrees"),
    )
    database = Database(settings)
    apply_migrations(database)
    services = build_services(settings, database)
    owner, project, _repo, execution, tasks = _setup_execution_with_two_tasks(services, tmp_path)
    for task, filename in zip(tasks, ("feature_a.py", "feature_b.py"), strict=True):
        worktree = services.worktree.create_worktree(
            project_id=project.id,
            repository_id=_repo.id,
            execution_id=execution.id,
            task_id=task.id,
            actor_id=owner.id,
        )
        (Path(worktree.worktree_path) / filename).write_text("value = 1\n")
        services.worktree.capture_diff(
            project_id=project.id,
            worktree_id=worktree.id,
            task_id=task.id,
            actor_id=owner.id,
        )

    review = services.integration.create_review(
        project_id=project.id,
        execution_id=execution.id,
        source_task_ids=tuple(task.id for task in tasks),
        actor_id=owner.id,
    )
    completed = services.integration.run_combined_tests(
        project_id=project.id,
        review_id=review.id,
        command="sh",
        args=("-c", "test -f feature_a.py && test -f feature_b.py"),
        actor_id=owner.id,
    )

    assert completed.state == "approved"
    assert completed.combined_test_result == "pass"
    assert completed.integration_worktree_id is not None
    evidence = services.integration.list_review_evidence(project.id, review.id)
    assert len(evidence) == 1
    assert evidence[0].exit_code == 0
    assert not Path(evidence[0].worktree_path).exists()


# ----------------------------------------------------------------------
# Compatible independent changes integrate cleanly
# ----------------------------------------------------------------------


def test_compatible_changes_integrate_cleanly(services, tmp_path) -> None:
    """Per PLAN.md M11: 'Compatible independent changes integrate
    cleanly.'"""
    owner, project, repo, execution, tasks = _setup_execution_with_two_tasks(services, tmp_path)
    task_a, task_b = tasks[0], tasks[1]
    # Create worktrees with non-conflicting changes.
    wt_a = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repo.id,
        execution_id=execution.id,
        task_id=task_a.id,
        actor_id=owner.id,
    )
    (Path(wt_a.worktree_path) / "file_a.py").write_text("a = 1")
    services.worktree.capture_diff(
        project_id=project.id, worktree_id=wt_a.id, task_id=task_a.id, actor_id=owner.id
    )
    wt_b = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repo.id,
        execution_id=execution.id,
        task_id=task_b.id,
        actor_id=owner.id,
    )
    (Path(wt_b.worktree_path) / "file_b.py").write_text("b = 2")
    services.worktree.capture_diff(
        project_id=project.id, worktree_id=wt_b.id, task_id=task_b.id, actor_id=owner.id
    )
    # Create an integration review.
    review = services.integration.create_review(
        project_id=project.id,
        execution_id=execution.id,
        source_task_ids=(task_a.id, task_b.id),
        actor_id=owner.id,
    )
    # No contract files were changed, so no conflicts.
    assert review.conflict_classification == "none"
    assert len(review.conflict_details) == 0
    # Record combined test result as pass.
    review = services.integration.record_combined_test_result(
        project_id=project.id,
        review_id=review.id,
        result="pass",
        actor_id=owner.id,
    )
    assert review.state == "approved"
    assert review.combined_test_result == "pass"


# ----------------------------------------------------------------------
# Conflicting schema/type/API changes are detected
# ----------------------------------------------------------------------


def test_contract_changes_are_detected(services, tmp_path) -> None:
    """Per PLAN.md M11: 'Conflicting schema/type/API changes are
    detected.'"""
    owner, project, repo, execution, tasks = _setup_execution_with_two_tasks(services, tmp_path)
    task_a = tasks[0]
    # Create a worktree with a contract file change.
    wt_a = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repo.id,
        execution_id=execution.id,
        task_id=task_a.id,
        actor_id=owner.id,
    )
    # Create a schema file (contract).
    (Path(wt_a.worktree_path) / "schema.sql").write_text("CREATE TABLE users (id INTEGER);")
    services.worktree.capture_diff(
        project_id=project.id, worktree_id=wt_a.id, task_id=task_a.id, actor_id=owner.id
    )
    # Create an integration review.
    review = services.integration.create_review(
        project_id=project.id,
        execution_id=execution.id,
        source_task_ids=(task_a.id,),
        actor_id=owner.id,
    )
    # The schema file should be in touched_contracts.
    assert "schema.sql" in review.touched_contracts
    # A conflict detail should exist for the contract change.
    assert len(review.conflict_details) >= 1
    assert review.conflict_classification in ("low_risk", "human_decision_required")


# ----------------------------------------------------------------------
# Deceptive green unit test cannot bypass failed combined tests
# ----------------------------------------------------------------------


def test_deceptive_green_test_cannot_bypass_failed_combined_tests(services, tmp_path) -> None:
    """Per PLAN.md M11: 'A deceptive green unit test cannot bypass
    failed combined tests.'"""
    owner, project, repo, execution, tasks = _setup_execution_with_two_tasks(services, tmp_path)
    task_a, task_b = tasks[0], tasks[1]
    # Create worktrees with changes.
    wt_a = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repo.id,
        execution_id=execution.id,
        task_id=task_a.id,
        actor_id=owner.id,
    )
    (Path(wt_a.worktree_path) / "schema.sql").write_text("CREATE TABLE x;")
    services.worktree.capture_diff(
        project_id=project.id, worktree_id=wt_a.id, task_id=task_a.id, actor_id=owner.id
    )
    wt_b = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repo.id,
        execution_id=execution.id,
        task_id=task_b.id,
        actor_id=owner.id,
    )
    (Path(wt_b.worktree_path) / "schema.sql").write_text("CREATE TABLE y;")
    services.worktree.capture_diff(
        project_id=project.id, worktree_id=wt_b.id, task_id=task_b.id, actor_id=owner.id
    )
    # Create an integration review.
    review = services.integration.create_review(
        project_id=project.id,
        execution_id=execution.id,
        source_task_ids=(task_a.id, task_b.id),
        actor_id=owner.id,
    )
    # Record combined test result as FAIL.
    review = services.integration.record_combined_test_result(
        project_id=project.id,
        review_id=review.id,
        result="fail",
        actor_id=owner.id,
    )
    # The review should be rejected or paused, not approved.
    assert review.state in ("rejected", "human_decision_paused")
    # Cannot create a merge proposal from a non-approved review.
    with pytest.raises(MergeGateError):
        services.integration.create_merge_proposal(
            project_id=project.id,
            review_id=review.id,
            execution_id=execution.id,
            source_tasks=(task_a.id, task_b.id),
            actor_id=owner.id,
        )


# ----------------------------------------------------------------------
# Human-decision conflict pauses merge
# ----------------------------------------------------------------------


def test_human_decision_conflict_pauses_merge(services, tmp_path) -> None:
    """Per PLAN.md M11: 'Human-decision conflict pauses merge.'"""
    owner, project, repo, execution, tasks = _setup_execution_with_two_tasks(services, tmp_path)
    task_a = tasks[0]
    # Create a worktree with an API contract change.
    wt_a = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repo.id,
        execution_id=execution.id,
        task_id=task_a.id,
        actor_id=owner.id,
    )
    (Path(wt_a.worktree_path) / "api" / "types.py").parent.mkdir(parents=True, exist_ok=True)
    (Path(wt_a.worktree_path) / "api" / "types.py").write_text("class User: pass")
    services.worktree.capture_diff(
        project_id=project.id, worktree_id=wt_a.id, task_id=task_a.id, actor_id=owner.id
    )
    # Create an integration review.
    review = services.integration.create_review(
        project_id=project.id,
        execution_id=execution.id,
        source_task_ids=(task_a.id,),
        actor_id=owner.id,
    )
    # An API change should be classified as human_decision_required.
    assert review.conflict_classification == "human_decision_required"
    # Record combined test result as fail (escalates to paused).
    review = services.integration.record_combined_test_result(
        project_id=project.id,
        review_id=review.id,
        result="fail",
        actor_id=owner.id,
    )
    assert review.state == "human_decision_paused"


# ----------------------------------------------------------------------
# Merge requires explicit authority
# ----------------------------------------------------------------------


def test_merge_requires_authorization(services, tmp_path) -> None:
    """Per PLAN.md M11: 'Merge requires explicit authority and passing
    gates.'"""
    owner, project, repo, execution, tasks = _setup_execution_with_two_tasks(services, tmp_path)
    task_a, task_b = tasks[0], tasks[1]
    # Create worktrees with non-conflicting changes.
    wt_a = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repo.id,
        execution_id=execution.id,
        task_id=task_a.id,
        actor_id=owner.id,
    )
    (Path(wt_a.worktree_path) / "file_a.py").write_text("a = 1")
    services.worktree.capture_diff(
        project_id=project.id, worktree_id=wt_a.id, task_id=task_a.id, actor_id=owner.id
    )
    wt_b = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repo.id,
        execution_id=execution.id,
        task_id=task_b.id,
        actor_id=owner.id,
    )
    (Path(wt_b.worktree_path) / "file_b.py").write_text("b = 2")
    services.worktree.capture_diff(
        project_id=project.id, worktree_id=wt_b.id, task_id=task_b.id, actor_id=owner.id
    )
    # Create review and pass combined tests.
    review = services.integration.create_review(
        project_id=project.id,
        execution_id=execution.id,
        source_task_ids=(task_a.id, task_b.id),
        actor_id=owner.id,
    )
    review = services.integration.record_combined_test_result(
        project_id=project.id,
        review_id=review.id,
        result="pass",
        actor_id=owner.id,
    )
    # Create a merge proposal.
    proposal = services.integration.create_merge_proposal(
        project_id=project.id,
        review_id=review.id,
        execution_id=execution.id,
        source_tasks=(task_a.id, task_b.id),
        actor_id=owner.id,
    )
    # A viewer cannot approve the merge.
    viewer = services.identity.create_user(display_name="Viewer")
    services.identity.add_member(
        project_id=project.id, actor_id=owner.id, member_id=viewer.id, role="viewer"
    )
    with pytest.raises(AuthorizationError):
        services.integration.approve_merge(
            project_id=project.id,
            proposal_id=proposal.id,
            actor_id=viewer.id,
        )
    # The owner can approve.
    proposal = services.integration.approve_merge(
        project_id=project.id,
        proposal_id=proposal.id,
        actor_id=owner.id,
    )
    assert proposal.state == "approved"
    # Execute the merge.
    proposal = services.integration.execute_merge(
        project_id=project.id,
        proposal_id=proposal.id,
        actor_id=owner.id,
    )
    assert proposal.state == "merged"
    assert proposal.merged_at is not None


# ----------------------------------------------------------------------
# Rejected integration does not update accepted memory
# ----------------------------------------------------------------------


def test_rejected_integration_does_not_update_memory(services, tmp_path) -> None:
    """Per PLAN.md M11: 'Rejected integration does not update accepted
    memory.'"""
    owner, project, repo, execution, tasks = _setup_execution_with_two_tasks(services, tmp_path)
    task_a = tasks[0]
    # Create a worktree with a contract change.
    wt_a = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repo.id,
        execution_id=execution.id,
        task_id=task_a.id,
        actor_id=owner.id,
    )
    (Path(wt_a.worktree_path) / "schema.sql").write_text("CREATE TABLE x;")
    services.worktree.capture_diff(
        project_id=project.id, worktree_id=wt_a.id, task_id=task_a.id, actor_id=owner.id
    )
    # Create a review.
    review = services.integration.create_review(
        project_id=project.id,
        execution_id=execution.id,
        source_task_ids=(task_a.id,),
        actor_id=owner.id,
    )
    # Record combined test result as fail.
    review = services.integration.record_combined_test_result(
        project_id=project.id,
        review_id=review.id,
        result="fail",
        actor_id=owner.id,
    )
    # The review is rejected or paused; no merge proposal can be created.
    if review.state == "approved":
        proposal = services.integration.create_merge_proposal(
            project_id=project.id,
            review_id=review.id,
            execution_id=execution.id,
            source_tasks=(task_a.id,),
            actor_id=owner.id,
        )
        services.integration.reject_merge(
            project_id=project.id,
            proposal_id=proposal.id,
            actor_id=owner.id,
        )
        proposal = services.integration.get_proposal(project.id, proposal.id)
        assert proposal.state == "rejected"
    else:
        # Review is rejected/paused; no merge proposal.
        with pytest.raises(MergeGateError):
            services.integration.create_merge_proposal(
                project_id=project.id,
                review_id=review.id,
                execution_id=execution.id,
                source_tasks=(task_a.id,),
                actor_id=owner.id,
            )
    # No RAG documents should have been ingested from the rejected
    # integration (post-integration memory/RAG update only from accepted
    # results).
    rag_docs = services.artifacts.list_rag_documents(project.id, actor_id=owner.id)
    assert len(rag_docs) == 0


# ----------------------------------------------------------------------
# Merge provenance traces every included task and approval
# ----------------------------------------------------------------------


def test_merge_provenance_traces_tasks_and_approval(services, tmp_path) -> None:
    """Per PLAN.md M11: 'Merge provenance traces every included task
    and approval.'"""
    owner, project, repo, execution, tasks = _setup_execution_with_two_tasks(services, tmp_path)
    task_a, task_b = tasks[0], tasks[1]
    # Create worktrees with non-conflicting changes.
    wt_a = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repo.id,
        execution_id=execution.id,
        task_id=task_a.id,
        actor_id=owner.id,
    )
    (Path(wt_a.worktree_path) / "file_a.py").write_text("a = 1")
    services.worktree.capture_diff(
        project_id=project.id, worktree_id=wt_a.id, task_id=task_a.id, actor_id=owner.id
    )
    wt_b = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repo.id,
        execution_id=execution.id,
        task_id=task_b.id,
        actor_id=owner.id,
    )
    (Path(wt_b.worktree_path) / "file_b.py").write_text("b = 2")
    services.worktree.capture_diff(
        project_id=project.id, worktree_id=wt_b.id, task_id=task_b.id, actor_id=owner.id
    )
    # Create review and pass combined tests.
    review = services.integration.create_review(
        project_id=project.id,
        execution_id=execution.id,
        source_task_ids=(task_a.id, task_b.id),
        actor_id=owner.id,
    )
    review = services.integration.record_combined_test_result(
        project_id=project.id,
        review_id=review.id,
        result="pass",
        actor_id=owner.id,
    )
    # Create and approve a merge proposal.
    proposal = services.integration.create_merge_proposal(
        project_id=project.id,
        review_id=review.id,
        execution_id=execution.id,
        source_tasks=(task_a.id, task_b.id),
        actor_id=owner.id,
    )
    proposal = services.integration.approve_merge(
        project_id=project.id,
        proposal_id=proposal.id,
        actor_id=owner.id,
    )
    proposal = services.integration.execute_merge(
        project_id=project.id,
        proposal_id=proposal.id,
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
        e
        for e in audit_events
        if e.operation in ("merge.propose", "merge.approve", "merge.execute")
    ]
    assert len(merge_events) >= 3
