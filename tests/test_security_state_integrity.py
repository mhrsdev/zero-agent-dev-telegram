"""Focused RED regressions for the F-03/F-04/F-06 remediation slice."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from zero.app.services import build_services
from zero.app.worker_service import TaskSpec
from zero.app.worktree_service import WorktreeService, validate_repository_path
from zero.config import Settings
from zero.domain.authorization import AuthorizationError
from zero.domain.execution import (
    ExecutionNotFoundError,
    LeaseOwnershipError,
    MissingEvidenceError,
)
from zero.domain.plans import PlanRevisionContent
from zero.domain.worktrees import PathValidationError
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations


def _build_services(test_settings: Settings, tmp_path: Path):
    database = Database(Settings.load_for_test(database_url=f"sqlite:///{tmp_path / 'zero.db'}"))
    apply_migrations(database)
    services = build_services(test_settings, database)
    services.worktree = WorktreeService(
        services.worktree._repo,
        services.worktree._audit_repo,
        services.worktree._authz,
        worktree_root=str(tmp_path / "worktrees"),
        allowed_commands=frozenset({"echo", "sh", "sleep"}),
    )
    return services


def _make_project(services):
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(owner_id=owner.id, name="Project")
    viewer = services.identity.create_user(display_name="Viewer")
    services.identity.add_member(
        project_id=project.id,
        actor_id=owner.id,
        member_id=viewer.id,
        role="viewer",
    )
    return owner, project, viewer


def _make_execution(services, owner, project, *, expected_evidence=()):
    event = services.plans.ingest_conversation_event(
        project_id=project.id,
        actor_id=owner.id,
        source="web",
        origin_kind="authenticated_human",
        content="Implement the change.",
    )
    plan = services.plans.create_plan(project_id=project.id, actor_id=owner.id)
    services.plans.propose_revision(
        plan_id=plan.id,
        project_id=project.id,
        actor_id=owner.id,
        content=PlanRevisionContent(
            objective="Implement the change",
            scope=(),
            constraints=(),
            acceptance_criteria=("The change works",),
            risks=(),
            unresolved_questions=(),
            source_event_ids=(event.id,),
        ),
    )
    _, handoff = services.plans.approve_revision(
        plan_id=plan.id,
        project_id=project.id,
        actor_id=owner.id,
        expected_revision_number=1,
        idempotency_key="security-regression",
    )
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        project_id=project.id,
        actor_id=owner.id,
        task_specs=[
            TaskSpec(
                key="task",
                objective="Run the task",
                expected_evidence=expected_evidence,
            )
        ],
    )
    return execution, services.worker.list_tasks(
        execution.id,
        project_id=execution.project_id,
        actor_id=services.identity.get_project(execution.project_id).owner_user_id,
    )[0]


def _make_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
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
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        check=True,
    )
    (repo / "README.md").write_text("# test\n")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
    )
    return repo


_posix_shell_only = pytest.mark.skipif(
    os.name == "nt",
    reason="depends on POSIX shell executables or symlink privileges",
)


def test_direct_sql_cannot_cross_execution_or_dependency_projects(
    test_settings,
    tmp_path,
) -> None:
    services = _build_services(test_settings, tmp_path)
    owner, project_a, _viewer = _make_project(services)
    project_b = services.identity.create_project(owner_id=owner.id, name="Project B")
    execution_a, task_a = _make_execution(services, owner, project_a)
    _execution_b, task_b = _make_execution(services, owner, project_b)
    conn = services.database.connect()

    with pytest.raises(sqlite3.IntegrityError, match="execution project lineage"):
        conn.execute(
            "UPDATE executions SET project_id = ? WHERE id = ?",
            (project_b.id.value, execution_a.id.value),
        )
    conn.rollback()

    with pytest.raises(sqlite3.IntegrityError, match="dependency project lineage"):
        conn.execute(
            "INSERT INTO task_dependencies (task_id, depends_on_task_id) VALUES (?, ?)",
            (task_a.id.value, task_b.id.value),
        )
    conn.rollback()


def test_direct_sql_cannot_cross_provider_request_execution_projects(
    test_settings,
    tmp_path,
) -> None:
    services = _build_services(test_settings, tmp_path)
    owner, project_a, _viewer = _make_project(services)
    project_b = services.identity.create_project(owner_id=owner.id, name="Provider Project B")
    execution_a, _task_a = _make_execution(services, owner, project_a)
    conn = services.database.connect()

    with pytest.raises(sqlite3.IntegrityError, match="provider request execution project lineage"):
        conn.execute(
            "INSERT INTO provider_requests "
            "(id, project_id, execution_id, provider, model_name, request_hash, state) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "preq_lineage_test",
                project_b.id.value,
                execution_a.id.value,
                "fake",
                "fake-standard",
                "lineage-hash",
                "pending",
            ),
        )
    conn.rollback()


def test_viewer_cannot_write_artifacts_or_agent_knowledge(test_settings, tmp_path):
    services = _build_services(test_settings, tmp_path)
    owner, project, viewer = _make_project(services)

    with pytest.raises(AuthorizationError):
        services.artifacts.store_artifact(
            project_id=project.id,
            actor_id=viewer.id,
            kind="other",
            content="viewer write must be rejected",
        )

    agent_type = services.agent_types.create_type(
        project_id=project.id,
        actor_id=owner.id,
        name="Worker",
        responsibility="Do work",
        memory_scope="Project facts",
    )
    with pytest.raises(AuthorizationError):
        services.agent_types.add_knowledge(
            project_id=project.id,
            type_id=agent_type.id,
            actor_id=viewer.id,
            kind="fact",
            content="viewer write must be rejected",
        )


def test_worktree_command_requires_authorization_and_allowlist(test_settings, tmp_path):
    services = _build_services(test_settings, tmp_path)
    owner, project, viewer = _make_project(services)
    execution, task = _make_execution(services, owner, project)
    repo = services.worktree.register_repository(
        project_id=project.id,
        actor_id=owner.id,
        name="repo",
        local_path=str(_make_git_repo(tmp_path)),
        default_base_revision="main",
    )
    worktree = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repo.id,
        execution_id=execution.id,
        task_id=task.id,
        actor_id=owner.id,
    )

    with pytest.raises(AuthorizationError):
        services.worktree.run_command(
            project_id=project.id,
            worktree_id=worktree.id,
            task_id=task.id,
            actor_id=viewer.id,
            command="echo",
            args=("viewer must not execute",),
        )

    with pytest.raises(Exception, match="allow|policy|permitted"):
        services.worktree.run_command(
            project_id=project.id,
            worktree_id=worktree.id,
            task_id=task.id,
            actor_id=owner.id,
            command="python3",
            args=("-c", "print('arbitrary host execution')"),
        )


def test_worktree_lineage_is_validated_before_git_side_effect(test_settings, tmp_path, monkeypatch):
    services = _build_services(test_settings, tmp_path)
    owner, project_a, _viewer = _make_project(services)
    project_b = services.identity.create_project(owner_id=owner.id, name="Other project")
    execution_b, task_b = _make_execution(services, owner, project_b)
    repo_a = services.worktree.register_repository(
        project_id=project_a.id,
        actor_id=owner.id,
        name="repo-a",
        local_path=str(_make_git_repo(tmp_path)),
        default_base_revision="main",
    )
    git_calls = []
    monkeypatch.setattr(
        services.worktree,
        "_git_worktree_add",
        lambda *args: git_calls.append(args),
    )

    with pytest.raises(ExecutionNotFoundError):
        services.worktree.create_worktree(
            project_id=project_a.id,
            repository_id=repo_a.id,
            execution_id=execution_b.id,
            task_id=task_b.id,
            actor_id=owner.id,
        )

    assert git_calls == []


def test_viewer_cannot_recover_worktrees(test_settings, tmp_path):
    services = _build_services(test_settings, tmp_path)
    owner, project, viewer = _make_project(services)
    execution, task = _make_execution(services, owner, project)
    repo = services.worktree.register_repository(
        project_id=project.id,
        actor_id=owner.id,
        name="repo",
        local_path=str(_make_git_repo(tmp_path)),
        default_base_revision="main",
    )
    worktree = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repo.id,
        execution_id=execution.id,
        task_id=task.id,
        actor_id=owner.id,
    )
    services.worktree.activate_worktree(
        project_id=project.id,
        worktree_id=worktree.id,
        actor_id=owner.id,
    )

    with pytest.raises(AuthorizationError):
        services.worktree.recover_worktrees_after_restart(
            project_id=project.id,
            actor_id=viewer.id,
        )

    assert services.worktree._repo.get_worktree(project.id, worktree.id).state == "active"


@_posix_shell_only
def test_repository_symlink_is_rejected(tmp_path):
    real_repo = _make_git_repo(tmp_path)
    link = tmp_path / "repo-link"
    link.symlink_to(real_repo, target_is_directory=True)
    with pytest.raises(PathValidationError, match="symlink"):
        validate_repository_path(str(link))


@_posix_shell_only
def test_command_output_is_bounded_and_timeout_kills_process_group(test_settings, tmp_path):
    services = _build_services(test_settings, tmp_path)
    owner, project, _viewer = _make_project(services)
    execution, task = _make_execution(services, owner, project)
    repo = services.worktree.register_repository(
        project_id=project.id,
        actor_id=owner.id,
        name="repo",
        local_path=str(_make_git_repo(tmp_path)),
        default_base_revision="main",
    )
    worktree = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repo.id,
        execution_id=execution.id,
        task_id=task.id,
        actor_id=owner.id,
    )

    _run, artifacts = services.worktree.run_command(
        project_id=project.id,
        worktree_id=worktree.id,
        task_id=task.id,
        actor_id=owner.id,
        command="echo",
        args=("x" * 8000,) * 13,
    )
    stdout = next(item for item in artifacts if item.kind == "stdout")
    assert len(stdout.content.encode("utf-8")) <= 64 * 1024
    assert "truncated" in stdout.content

    pid_file = Path(worktree.worktree_path) / "child.pid"
    run, _ = services.worktree.run_command(
        project_id=project.id,
        worktree_id=worktree.id,
        task_id=task.id,
        actor_id=owner.id,
        command="sh",
        args=("-c", f"sleep 30 & echo $! > {pid_file.name}; wait"),
        timeout_seconds=1,
    )
    assert run.state == "timed_out"
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not pid_file.exists():
        time.sleep(0.02)
    assert pid_file.exists()
    child_pid = int(pid_file.read_text().strip())
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def test_attempt_id_must_belong_to_task(test_settings, tmp_path):
    services = _build_services(test_settings, tmp_path)
    owner, project, _viewer = _make_project(services)

    # Recreate the graph with two tasks so both attempts are valid and distinct.
    event = services.plans.ingest_conversation_event(
        project_id=project.id,
        actor_id=owner.id,
        source="web",
        origin_kind="authenticated_human",
        content="Second graph.",
    )
    plan = services.plans.create_plan(project_id=project.id, actor_id=owner.id)
    services.plans.propose_revision(
        plan_id=plan.id,
        project_id=project.id,
        actor_id=owner.id,
        content=PlanRevisionContent(
            objective="Second graph",
            scope=(),
            constraints=(),
            acceptance_criteria=("Works",),
            risks=(),
            unresolved_questions=(),
            source_event_ids=(event.id,),
        ),
    )
    _, handoff = services.plans.approve_revision(
        plan_id=plan.id,
        project_id=project.id,
        actor_id=owner.id,
        expected_revision_number=1,
        idempotency_key="attempt-mismatch",
    )
    graph = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        project_id=project.id,
        actor_id=owner.id,
        task_specs=[TaskSpec(key="A", objective="A"), TaskSpec(key="B", objective="B")],
    )
    task_a, task_b = services.worker.list_tasks(
        graph.id,
        project_id=graph.project_id,
        actor_id=services.identity.get_project(graph.project_id).owner_user_id,
    )
    attempt_a = services.worker.claim_task(
        execution_id=graph.id,
        task_id=task_a.id,
        lease_owner="worker-a",
        project_id=graph.project_id,
        actor_id=services.identity.get_project(graph.project_id).owner_user_id,
    )
    services.worker.claim_task(
        execution_id=graph.id,
        task_id=task_b.id,
        lease_owner="worker-b",
        project_id=graph.project_id,
        actor_id=services.identity.get_project(graph.project_id).owner_user_id,
    )
    with pytest.raises(Exception, match="attempt"):
        services.worker.complete_task(
            execution_id=graph.id,
            task_id=task_b.id,
            attempt_id=attempt_a.id,
            actor_id=owner.id,
            project_id=graph.project_id,
        )


def test_completion_requires_expected_evidence(test_settings, tmp_path):
    services = _build_services(test_settings, tmp_path)
    owner, project, _viewer = _make_project(services)
    execution, task = _make_execution(services, owner, project, expected_evidence=("tests:passed",))
    attempt = services.worker.claim_task(
        execution_id=execution.id,
        task_id=task.id,
        lease_owner="worker",
        project_id=execution.project_id,
        actor_id=services.identity.get_project(execution.project_id).owner_user_id,
    )
    with pytest.raises(Exception, match="evidence"):
        services.worker.complete_task(
            execution_id=execution.id,
            task_id=task.id,
            attempt_id=attempt.id,
            actor_id=owner.id,
            lease_owner="worker",
            project_id=execution.project_id,
        )


def test_completion_rejects_labels_without_durable_evidence(test_settings, tmp_path):
    services = _build_services(test_settings, tmp_path)
    owner, project, _viewer = _make_project(services)
    execution, task = _make_execution(
        services,
        owner,
        project,
        expected_evidence=("tests:passed",),
    )
    attempt = services.worker.claim_task(
        execution_id=execution.id,
        task_id=task.id,
        lease_owner="worker",
        project_id=execution.project_id,
        actor_id=services.identity.get_project(execution.project_id).owner_user_id,
    )

    with pytest.raises(MissingEvidenceError):
        services.worker.complete_task(
            execution_id=execution.id,
            task_id=task.id,
            attempt_id=attempt.id,
            actor_id=owner.id,
            lease_owner="worker",
            evidence=("tests:passed",),
            project_id=execution.project_id,
        )

    assert services.worker._execution_repo.get_task(task.id).state == "running"


def test_completion_rejects_evidence_from_wrong_attempt(test_settings, tmp_path):
    services = _build_services(test_settings, tmp_path)
    owner, project, _viewer = _make_project(services)
    execution, task = _make_execution(
        services,
        owner,
        project,
        expected_evidence=("tests:passed",),
    )
    attempt = services.worker.claim_task(
        execution_id=execution.id,
        task_id=task.id,
        lease_owner="worker",
        project_id=execution.project_id,
        actor_id=services.identity.get_project(execution.project_id).owner_user_id,
    )
    artifact = services.artifacts.store_artifact(
        project_id=project.id,
        actor_id=owner.id,
        kind="test_report",
        content="passed",
        producer="security-test",
        provenance=json.dumps(
            {
                "execution_id": execution.id.value,
                "task_id": task.id.value,
                "attempt_id": "att_wrong_attempt",
            }
        ),
    )

    with pytest.raises(MissingEvidenceError):
        services.worker.complete_task(
            execution_id=execution.id,
            task_id=task.id,
            attempt_id=attempt.id,
            actor_id=owner.id,
            lease_owner="worker",
            evidence=("tests:passed",),
            evidence_artifact_ids=(artifact.id,),
            project_id=execution.project_id,
        )


def test_live_lease_is_not_recovered_as_unknown(test_settings, tmp_path):
    services = _build_services(test_settings, tmp_path)
    owner, project, _viewer = _make_project(services)
    execution, task = _make_execution(services, owner, project)
    attempt = services.worker.claim_task(
        execution_id=execution.id,
        task_id=task.id,
        lease_owner="live-worker",
        lease_duration_seconds=300,
        project_id=execution.project_id,
        actor_id=services.identity.get_project(execution.project_id).owner_user_id,
    )
    recovered = services.worker.recover_after_restart(
        execution_id=execution.id, actor_id=owner.id, project_id=execution.project_id
    )
    assert recovered.state == "running"
    assert (
        services.worker.get_execution(
            execution.id,
            project_id=execution.project_id,
            actor_id=services.identity.get_project(execution.project_id).owner_user_id,
        ).state
        == "running"
    )
    assert (
        services.worker.list_attempts(
            task.id,
            project_id=task.project_id,
            actor_id=services.identity.get_project(task.project_id).owner_user_id,
        )[0].id
        == attempt.id
    )
    assert (
        services.worker.list_attempts(
            task.id,
            project_id=task.project_id,
            actor_id=services.identity.get_project(task.project_id).owner_user_id,
        )[0].state
        == "running"
    )


def test_completion_is_fenced_if_lease_expires_after_validation(
    test_settings, tmp_path, monkeypatch
):
    services = _build_services(test_settings, tmp_path)
    owner, project, _viewer = _make_project(services)
    execution, task = _make_execution(services, owner, project)
    attempt = services.worker.claim_task(
        execution_id=execution.id,
        task_id=task.id,
        lease_owner="worker",
        project_id=execution.project_id,
        actor_id=services.identity.get_project(execution.project_id).owner_user_id,
    )

    original_validate = services.worker._validate_attempt_identity

    def validate_then_expire(*, task, attempt_id, lease_owner):
        validated = original_validate(
            task=task,
            attempt_id=attempt_id,
            lease_owner=lease_owner,
        )
        services.database.connect().execute(
            "UPDATE task_attempts SET lease_expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00.000000Z", attempt_id.value),
        )
        return validated

    monkeypatch.setattr(services.worker, "_validate_attempt_identity", validate_then_expire)

    with pytest.raises(LeaseOwnershipError):
        services.worker.complete_task(
            execution_id=execution.id,
            task_id=task.id,
            attempt_id=attempt.id,
            actor_id=owner.id,
            lease_owner="worker",
            project_id=execution.project_id,
        )

    assert services.worker._execution_repo.get_task(task.id).state == "running"
    assert services.worker._execution_repo.get_attempt(attempt.id).state == "running"


def test_completion_requires_current_lease_owner(test_settings, tmp_path):
    services = _build_services(test_settings, tmp_path)
    owner, project, _viewer = _make_project(services)
    execution, task = _make_execution(services, owner, project)
    attempt = services.worker.claim_task(
        execution_id=execution.id,
        task_id=task.id,
        lease_owner="worker-a",
        project_id=execution.project_id,
        actor_id=services.identity.get_project(execution.project_id).owner_user_id,
    )
    with pytest.raises(Exception, match="lease"):
        services.worker.complete_task(
            execution_id=execution.id,
            task_id=task.id,
            attempt_id=attempt.id,
            actor_id=owner.id,
            lease_owner="worker-b",
            project_id=execution.project_id,
        )


def test_viewer_cannot_read_worktree_dependent_data(test_settings, tmp_path):
    services = _build_services(test_settings, tmp_path)
    owner, project, _viewer = _make_project(services)
    outsider = services.identity.create_user(display_name="outsider")
    execution, task = _make_execution(services, owner, project)
    repo = services.worktree.register_repository(
        project_id=project.id,
        actor_id=owner.id,
        name="repo",
        local_path=str(_make_git_repo(tmp_path)),
        default_base_revision="main",
    )
    worktree = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repo.id,
        execution_id=execution.id,
        task_id=task.id,
        actor_id=owner.id,
    )
    _run, _artifacts = services.worktree.run_command(
        project_id=project.id,
        worktree_id=worktree.id,
        task_id=task.id,
        actor_id=owner.id,
        command="echo",
        args=("private",),
    )

    with pytest.raises(AuthorizationError):
        services.worktree.get_worktree_for_task(
            project_id=project.id,
            task_id=task.id,
            actor_id=outsider.id,
        )
    with pytest.raises(AuthorizationError):
        services.worktree.list_artifacts_for_task(
            project_id=project.id,
            task_id=task.id,
            actor_id=outsider.id,
        )
    with pytest.raises(AuthorizationError):
        services.worktree.list_command_runs_for_worktree(
            project_id=project.id,
            worktree_id=worktree.id,
            actor_id=outsider.id,
        )
