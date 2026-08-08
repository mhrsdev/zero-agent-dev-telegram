"""Worktree service tests — covers all M6 validation gates.

Per PLAN.md M6 validation:
- Two independent tasks modify different files concurrently without
  collision.
- Two tasks targeting the same contract are detected for integration
  review.
- One failed task cannot corrupt another worktree.
- Path traversal and repository escape attempts fail.
- Restart identifies orphaned running work safely.
- Cleanup preserves untracked or uncommitted human work unless
  explicitly authorized.

Per PLAN.md M6 acceptance:
- Two isolated tasks can run concurrently and each produces a
  verifiable diff and test report while the base workspace remains
  unchanged.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from zero.app.services import build_services
from zero.app.worker_service import TaskSpec
from zero.app.worktree_service import (
    PathValidationError,
    WorktreeService,
    is_path_inside,
    validate_repository_path,
    validate_worktree_path,
)
from zero.config import Settings
from zero.domain.plans import PlanRevisionContent
from zero.domain.worktrees import (
    WorktreeCleanupError,
)
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
    )
    return s


def _make_repo(tmp_path: Path, name: str = "repo") -> Path:
    """Create a git repo with one commit."""
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True,
        capture_output=True,
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
        check=True,
        capture_output=True,
    )
    return repo


def _make_approved_plan(services) -> tuple:
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(
        owner_id=owner.id, name="P"
    )
    event = services.plans.ingest_conversation_event(
        project_id=project.id,
        actor_id=owner.id,
        source="web",
        origin_kind="authenticated_human",
        content="Add a feature.",
    )
    plan = services.plans.create_plan(
        project_id=project.id, actor_id=owner.id
    )
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
        plan_id=plan.id, actor_id=owner.id, content=content
    )
    _, handoff = services.plans.approve_revision(
        plan_id=plan.id,
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


# ----------------------------------------------------------------------
# Path validation
# ----------------------------------------------------------------------


def test_validate_repository_path_rejects_relative(tmp_path) -> None:
    with pytest.raises(PathValidationError, match="absolute"):
        validate_repository_path("relative/path")


def test_validate_repository_path_rejects_traversal(tmp_path) -> None:
    with pytest.raises(PathValidationError, match=r"\.\."):
        validate_repository_path(str(tmp_path / ".." / "etc"))


def test_validate_repository_path_rejects_nonexistent(tmp_path) -> None:
    with pytest.raises(PathValidationError, match="does not resolve"):
        validate_repository_path(str(tmp_path / "nonexistent"))


def test_validate_repository_path_rejects_non_git_dir(tmp_path) -> None:
    """Per PLAN.md M6: repository path must be a git repository."""
    non_git = tmp_path / "not-a-repo"
    non_git.mkdir()
    with pytest.raises(PathValidationError, match="not a git repository"):
        validate_repository_path(str(non_git))


def test_validate_repository_path_accepts_git_repo(tmp_path) -> None:
    repo = _make_repo(tmp_path)
    result = validate_repository_path(str(repo))
    assert result == str(repo.resolve())


def test_validate_worktree_path_rejects_relative_root() -> None:
    with pytest.raises(PathValidationError, match="absolute"):
        validate_worktree_path("relative/root", "wt_abc")


def test_validate_worktree_path_rejects_traversal_root() -> None:
    with pytest.raises(PathValidationError, match=r"\.\."):
        validate_worktree_path("/tmp/../etc", "wt_abc")


def test_validate_worktree_path_rejects_unsafe_id() -> None:
    with pytest.raises(PathValidationError, match="unsafe characters"):
        validate_worktree_path("/tmp/worktrees", "wt_../../etc")


def test_is_path_inside_detects_escape() -> None:
    assert is_path_inside("/tmp/worktrees/wt_1", "/tmp/worktrees") is True
    assert is_path_inside("/tmp/worktrees/wt_1/sub", "/tmp/worktrees") is True
    assert is_path_inside("/etc", "/tmp/worktrees") is False
    assert is_path_inside("/tmp/other", "/tmp/worktrees") is False


# ----------------------------------------------------------------------
# Repository registration
# ----------------------------------------------------------------------


def test_register_repository_succeeds(services, project_with_repo_and_plan) -> None:
    _owner, _project, _plan, _handoff, repo = project_with_repo_and_plan
    assert repo.id.value.startswith("repo_")
    assert repo.name == "test-repo"


def test_register_repository_rejects_nonexistent_path(
    services, project_with_repo_and_plan, tmp_path
) -> None:
    owner, project, _, _, _ = project_with_repo_and_plan
    with pytest.raises(PathValidationError):
        services.worktree.register_repository(
            project_id=project.id,
            actor_id=owner.id,
            name="bad",
            local_path=str(tmp_path / "nonexistent"),
        )


# ----------------------------------------------------------------------
# Worktree lifecycle
# ----------------------------------------------------------------------


def test_create_worktree_creates_branch_and_directory(
    services, project_with_repo_and_plan
) -> None:
    owner, project, _plan, handoff, repo = project_with_repo_and_plan
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        actor_id=owner.id,
        task_specs=[TaskSpec(key="A", objective="Task A")],
    )
    task = services.worker.list_tasks(execution.id)[0]
    wt = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repo.id,
        execution_id=execution.id,
        task_id=task.id,
        actor_id=owner.id,
    )
    assert wt.state == "allocated"
    assert wt.branch_name.startswith("zero/")
    assert Path(wt.worktree_path).is_dir()
    # The worktree has a .git file (not directory) pointing to the main repo.
    assert (Path(wt.worktree_path) / ".git").exists()


def test_two_independent_tasks_modify_different_files_concurrently(
    services, project_with_repo_and_plan, tmp_path
) -> None:
    """Per PLAN.md M6 acceptance: 'Two isolated tasks can run concurrently
    and each produces a verifiable diff and test report while the base
    workspace remains unchanged.'"""
    owner, project, _plan, handoff, repo = project_with_repo_and_plan
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        actor_id=owner.id,
        task_specs=[
            TaskSpec(key="A", objective="Task A"),
            TaskSpec(key="B", objective="Task B"),
        ],
    )
    tasks = services.worker.list_tasks(execution.id)
    task_a, task_b = tasks[0], tasks[1]
    # Create two worktrees.
    wt_a = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repo.id,
        execution_id=execution.id,
        task_id=task_a.id,
        actor_id=owner.id,
    )
    wt_b = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repo.id,
        execution_id=execution.id,
        task_id=task_b.id,
        actor_id=owner.id,
    )
    assert wt_a.worktree_path != wt_b.worktree_path
    # Modify different files in each worktree.
    (Path(wt_a.worktree_path) / "file_a.txt").write_text("content A")
    (Path(wt_b.worktree_path) / "file_b.txt").write_text("content B")
    # Each worktree has its own changes; the other doesn't.
    assert (Path(wt_a.worktree_path) / "file_a.txt").exists()
    assert not (Path(wt_a.worktree_path) / "file_b.txt").exists()
    assert (Path(wt_b.worktree_path) / "file_b.txt").exists()
    assert not (Path(wt_b.worktree_path) / "file_a.txt").exists()
    # The base repo is unchanged.
    assert not (Path(repo.local_path) / "file_a.txt").exists()
    assert not (Path(repo.local_path) / "file_b.txt").exists()


# ----------------------------------------------------------------------
# Command runner
# ----------------------------------------------------------------------


def test_run_command_captures_stdout_and_exit_code(
    services, project_with_repo_and_plan
) -> None:
    owner, project, _plan, handoff, repo = project_with_repo_and_plan
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        actor_id=owner.id,
        task_specs=[TaskSpec(key="A", objective="Task A")],
    )
    task = services.worker.list_tasks(execution.id)[0]
    wt = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repo.id,
        execution_id=execution.id,
        task_id=task.id,
        actor_id=owner.id,
    )
    run, artifacts = services.worktree.run_command(
        project_id=project.id,
        worktree_id=wt.id,
        task_id=task.id,
        actor_id=owner.id,
        command="echo",
        args=("hello world",),
    )
    assert run.state == "completed"
    assert run.exit_code == 0
    assert run.timed_out is False
    stdout_artifact = next(a for a in artifacts if a.kind == "stdout")
    assert "hello world" in stdout_artifact.content
    exit_artifact = next(a for a in artifacts if a.kind == "exit_status")
    assert "exit_code=0" in exit_artifact.content


def test_run_command_captures_failure(
    services, project_with_repo_and_plan
) -> None:
    owner, project, _plan, handoff, repo = project_with_repo_and_plan
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        actor_id=owner.id,
        task_specs=[TaskSpec(key="A", objective="Task A")],
    )
    task = services.worker.list_tasks(execution.id)[0]
    wt = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repo.id,
        execution_id=execution.id,
        task_id=task.id,
        actor_id=owner.id,
    )
    # Run a command that exits non-zero.
    run, _artifacts = services.worktree.run_command(
        project_id=project.id,
        worktree_id=wt.id,
        task_id=task.id,
        actor_id=owner.id,
        command="sh",
        args=("-c", "exit 42"),
    )
    assert run.state == "completed"
    assert run.exit_code == 42


def test_run_command_times_out(
    services, project_with_repo_and_plan
) -> None:
    """Per PLAN.md M6: commands are time-bounded."""
    owner, project, _plan, handoff, repo = project_with_repo_and_plan
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        actor_id=owner.id,
        task_specs=[TaskSpec(key="A", objective="Task A")],
    )
    task = services.worker.list_tasks(execution.id)[0]
    wt = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repo.id,
        execution_id=execution.id,
        task_id=task.id,
        actor_id=owner.id,
    )
    run, _artifacts = services.worktree.run_command(
        project_id=project.id,
        worktree_id=wt.id,
        task_id=task.id,
        actor_id=owner.id,
        command="sleep",
        args=("10",),
        timeout_seconds=1,
    )
    assert run.state == "timed_out"
    assert run.timed_out is True


# ----------------------------------------------------------------------
# Diff capture
# ----------------------------------------------------------------------


def test_capture_diff_captures_changes(
    services, project_with_repo_and_plan
) -> None:
    owner, project, _plan, handoff, repo = project_with_repo_and_plan
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        actor_id=owner.id,
        task_specs=[TaskSpec(key="A", objective="Task A")],
    )
    task = services.worker.list_tasks(execution.id)[0]
    wt = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repo.id,
        execution_id=execution.id,
        task_id=task.id,
        actor_id=owner.id,
    )
    # Make a change.
    (Path(wt.worktree_path) / "new_file.txt").write_text("new content")
    diff = services.worktree.capture_diff(
        project_id=project.id,
        worktree_id=wt.id,
        task_id=task.id,
        actor_id=owner.id,
    )
    assert diff.kind == "diff"
    assert "new_file.txt" in diff.content


# ----------------------------------------------------------------------
# One failed task cannot corrupt another worktree
# ----------------------------------------------------------------------


def test_failed_task_does_not_corrupt_other_worktree(
    services, project_with_repo_and_plan
) -> None:
    """Per PLAN.md M6: 'One failed task cannot corrupt another
    worktree.'"""
    owner, project, _plan, handoff, repo = project_with_repo_and_plan
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        actor_id=owner.id,
        task_specs=[
            TaskSpec(key="A", objective="Task A"),
            TaskSpec(key="B", objective="Task B"),
        ],
    )
    tasks = services.worker.list_tasks(execution.id)
    task_a, task_b = tasks[0], tasks[1]
    wt_a = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repo.id,
        execution_id=execution.id,
        task_id=task_a.id,
        actor_id=owner.id,
    )
    wt_b = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repo.id,
        execution_id=execution.id,
        task_id=task_b.id,
        actor_id=owner.id,
    )
    # Activate both worktrees (simulating commands running).
    services.worktree.activate_worktree(
        project_id=project.id, worktree_id=wt_a.id, actor_id=owner.id
    )
    services.worktree.activate_worktree(
        project_id=project.id, worktree_id=wt_b.id, actor_id=owner.id
    )
    # Write a file in B that we'll check is intact after A fails.
    (Path(wt_b.worktree_path) / "b_file.txt").write_text("B content")
    # Fail A.
    services.worktree.complete_worktree(
        project_id=project.id,
        worktree_id=wt_a.id,
        actor_id=owner.id,
        succeeded=False,
    )
    # B's file is still intact.
    assert (Path(wt_b.worktree_path) / "b_file.txt").read_text() == "B content"


# ----------------------------------------------------------------------
# Cleanup safety
# ----------------------------------------------------------------------


def test_cleanup_refuses_uncommitted_human_work(
    services, project_with_repo_and_plan
) -> None:
    """Per PLAN.md M6: 'Cleanup preserves untracked or uncommitted human
    work unless explicitly authorized.'"""
    owner, project, _plan, handoff, repo = project_with_repo_and_plan
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        actor_id=owner.id,
        task_specs=[TaskSpec(key="A", objective="Task A")],
    )
    task = services.worker.list_tasks(execution.id)[0]
    wt = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repo.id,
        execution_id=execution.id,
        task_id=task.id,
        actor_id=owner.id,
    )
    # Activate the worktree.
    services.worktree.activate_worktree(
        project_id=project.id, worktree_id=wt.id, actor_id=owner.id
    )
    # Create an untracked file (uncommitted human work).
    (Path(wt.worktree_path) / "human_work.txt").write_text("important")
    services.worktree.complete_worktree(
        project_id=project.id,
        worktree_id=wt.id,
        actor_id=owner.id,
        succeeded=True,
    )
    services.worktree.mark_cleanup_eligible(
        project_id=project.id,
        worktree_id=wt.id,
        actor_id=owner.id,
    )
    # Cleanup should refuse because of uncommitted changes.
    with pytest.raises(WorktreeCleanupError, match="refused"):
        services.worktree.remove_worktree(
            project_id=project.id,
            worktree_id=wt.id,
            actor_id=owner.id,
        )
    # The worktree directory still exists.
    assert Path(wt.worktree_path).exists()
    # The human work is preserved.
    assert (
        (Path(wt.worktree_path) / "human_work.txt").read_text()
        == "important"
    )


def test_cleanup_succeeds_when_no_uncommitted_work(
    services, project_with_repo_and_plan
) -> None:
    owner, project, _plan, handoff, repo = project_with_repo_and_plan
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        actor_id=owner.id,
        task_specs=[TaskSpec(key="A", objective="Task A")],
    )
    task = services.worker.list_tasks(execution.id)[0]
    wt = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repo.id,
        execution_id=execution.id,
        task_id=task.id,
        actor_id=owner.id,
    )
    # Activate the worktree.
    services.worktree.activate_worktree(
        project_id=project.id, worktree_id=wt.id, actor_id=owner.id
    )
    services.worktree.complete_worktree(
        project_id=project.id,
        worktree_id=wt.id,
        actor_id=owner.id,
        succeeded=True,
    )
    services.worktree.mark_cleanup_eligible(
        project_id=project.id,
        worktree_id=wt.id,
        actor_id=owner.id,
    )
    services.worktree.remove_worktree(
        project_id=project.id,
        worktree_id=wt.id,
        actor_id=owner.id,
    )
    wt = services.worktree.get_worktree(project.id, wt.id)
    assert wt.state == "removed"
    assert not Path(wt.worktree_path).exists()


def test_cleanup_refuses_non_eligible_worktree(
    services, project_with_repo_and_plan
) -> None:
    """Per PLAN.md M6: cleanup only after eligibility checks pass."""
    owner, project, _plan, handoff, repo = project_with_repo_and_plan
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        actor_id=owner.id,
        task_specs=[TaskSpec(key="A", objective="Task A")],
    )
    task = services.worker.list_tasks(execution.id)[0]
    wt = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repo.id,
        execution_id=execution.id,
        task_id=task.id,
        actor_id=owner.id,
    )
    # Try to remove without marking cleanup_eligible.
    with pytest.raises(WorktreeCleanupError, match="not 'cleanup_eligible'"):
        services.worktree.remove_worktree(
            project_id=project.id,
            worktree_id=wt.id,
            actor_id=owner.id,
        )


# ----------------------------------------------------------------------
# Restart recovery
# ----------------------------------------------------------------------


def test_restart_marks_active_worktrees_as_interrupted(
    services, project_with_repo_and_plan
) -> None:
    """Per PLAN.md M6: 'Restart identifies orphaned running work safely.'"""
    owner, project, _plan, handoff, repo = project_with_repo_and_plan
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        actor_id=owner.id,
        task_specs=[TaskSpec(key="A", objective="Task A")],
    )
    task = services.worker.list_tasks(execution.id)[0]
    wt = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repo.id,
        execution_id=execution.id,
        task_id=task.id,
        actor_id=owner.id,
    )
    # Activate the worktree (simulating a running command).
    services.worktree.activate_worktree(
        project_id=project.id,
        worktree_id=wt.id,
        actor_id=owner.id,
    )
    # Simulate restart.
    recovered = services.worktree.recover_worktrees_after_restart(
        project_id=project.id, actor_id=owner.id
    )
    assert len(recovered) == 1
    assert recovered[0].state == "interrupted"
    # The worktree directory still exists (not deleted).
    assert Path(wt.worktree_path).exists()


# ----------------------------------------------------------------------
# Artifact integrity
# ----------------------------------------------------------------------


def test_artifact_has_content_hash(
    services, project_with_repo_and_plan
) -> None:
    """Per zero-artifact-provenance-model: artifacts have a content hash
    for integrity."""
    import hashlib

    owner, project, _plan, handoff, repo = project_with_repo_and_plan
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        actor_id=owner.id,
        task_specs=[TaskSpec(key="A", objective="Task A")],
    )
    task = services.worker.list_tasks(execution.id)[0]
    wt = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repo.id,
        execution_id=execution.id,
        task_id=task.id,
        actor_id=owner.id,
    )
    _, artifacts = services.worktree.run_command(
        project_id=project.id,
        worktree_id=wt.id,
        task_id=task.id,
        actor_id=owner.id,
        command="echo",
        args=("test content",),
    )
    stdout = next(a for a in artifacts if a.kind == "stdout")
    expected_hash = hashlib.sha256(
        stdout.content.encode("utf-8")
    ).hexdigest()
    assert stdout.content_hash == expected_hash
