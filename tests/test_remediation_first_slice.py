"""Focused RED regressions for the integration/backup remediation slice."""

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

import pytest
from pydantic import SecretStr

from tests.test_integration import _setup_execution_with_two_tasks
from zero.app.services import build_services
from zero.app.worktree_service import WorktreeService
from zero.config import Settings
from zero.domain.authorization import AuthorizationError
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations


@pytest.fixture
def services(tmp_path: Path):
    settings = Settings.load_for_test(
        secret_key=SecretStr("test-only-backup-key-material-0123456789")
    )
    database = Database(settings)
    apply_migrations(database)
    services = build_services(settings, database)
    services.worktree = WorktreeService(
        services.worktree._repo,
        services.worktree._audit_repo,
        services.worktree._authz,
        worktree_root=str(tmp_path / "worktrees"),
    )
    return services


def _two_task_changes(services, tmp_path: Path, *, same_path: bool = True):
    owner, project, repo, execution, tasks = _setup_execution_with_two_tasks(services, tmp_path)
    first, second = tasks
    for index, (task, content) in enumerate(
        ((first, "CREATE TABLE first (id INTEGER);"), (second, "CREATE TABLE second (id INTEGER);"))
    ):
        worktree = services.worktree.create_worktree(
            project_id=project.id,
            repository_id=repo.id,
            execution_id=execution.id,
            task_id=task.id,
            actor_id=owner.id,
        )
        (
            Path(worktree.worktree_path) / ("schema.sql" if same_path else f"schema_{index}.sql")
        ).write_text(content, encoding="utf-8")
        services.worktree.capture_diff(
            project_id=project.id,
            worktree_id=worktree.id,
            task_id=task.id,
            actor_id=owner.id,
        )
    return owner, project, repo, execution, first, second


def test_collision_provenance_preserves_every_source_task(services, tmp_path: Path):
    owner, project, _repo, execution, first, second = _two_task_changes(services, tmp_path)

    review = services.integration.create_review(
        project_id=project.id,
        execution_id=execution.id,
        source_task_ids=(first.id, second.id),
        actor_id=owner.id,
    )

    collision = next(
        detail for detail in review.conflict_details if "schema.sql" in detail.description
    )
    assert set(collision.source_tasks) == {first.id.value, second.id.value}
    entries = [entry for entry in review.impact_set if entry.file_path == "schema.sql"]
    assert {entry.task_id for entry in entries} == {first.id.value, second.id.value}
    assert all(entry.execution_id == execution.id.value for entry in entries)
    assert all(entry.worktree_id for entry in entries)
    assert all(entry.artifact_id for entry in entries)
    assert all(entry.base_revision for entry in entries)
    assert all(entry.content_hash for entry in entries)


def test_execute_merge_authorizes_actor_and_updates_real_target_ref(services, tmp_path: Path):
    owner, project, repo, execution, first, second = _two_task_changes(
        services, tmp_path, same_path=False
    )
    review = services.integration.create_review(
        project_id=project.id,
        execution_id=execution.id,
        source_task_ids=(first.id, second.id),
        actor_id=owner.id,
    )
    review = services.integration.record_combined_test_result(
        project_id=project.id,
        review_id=review.id,
        result="pass",
        actor_id=owner.id,
    )
    proposal = services.integration.create_merge_proposal(
        project_id=project.id,
        review_id=review.id,
        execution_id=execution.id,
        source_tasks=(first.id, second.id),
        actor_id=owner.id,
    )
    services.integration.approve_merge(
        project_id=project.id,
        proposal_id=proposal.id,
        actor_id=owner.id,
    )
    before = subprocess.check_output(
        ["git", "-C", repo.local_path, "rev-parse", "main"], text=True
    ).strip()
    viewer = services.identity.create_user(display_name="Viewer")
    services.identity.add_member(
        project_id=project.id,
        actor_id=owner.id,
        member_id=viewer.id,
        role="viewer",
    )

    with pytest.raises(AuthorizationError):
        services.integration.execute_merge(
            project_id=project.id,
            proposal_id=proposal.id,
            actor_id=viewer.id,
        )
    assert (
        subprocess.check_output(
            ["git", "-C", repo.local_path, "rev-parse", "main"], text=True
        ).strip()
        == before
    )

    merged = services.integration.execute_merge(
        project_id=project.id,
        proposal_id=proposal.id,
        actor_id=owner.id,
    )
    after = subprocess.check_output(
        ["git", "-C", repo.local_path, "rev-parse", "main"], text=True
    ).strip()
    assert merged.state == "merged"
    assert after != before
    assert merged.target_revision == after
    assert merged.rollback_revision == before
    assert (Path(repo.local_path) / "schema_0.sql").exists()
    assert (Path(repo.local_path) / "schema_1.sql").exists()


def test_backup_is_authenticated_encrypted_atomic_and_restore_isolated(services, tmp_path: Path):
    owner = services.identity.create_user(display_name="Owner")
    services.identity.create_project(owner_id=owner.id, name="Encrypted Backup")
    backup_path = Path(services.backup.backup_to_file(str(tmp_path / "backup.zero")))

    raw = backup_path.read_bytes()
    assert b"BEGIN TRANSACTION" not in raw
    assert b"CREATE TABLE" not in raw
    if os.name != "nt":
        assert os.stat(backup_path).st_mode & 0o777 == 0o600

    restored_path = tmp_path / "restored.db"
    restored = Database(Settings.load_for_test(database_url=f"sqlite:///{restored_path}"))
    result = services.backup.restore_from_file(str(backup_path), restored)
    assert result["integrity_check"] == "pass"
    conn = restored.connect()
    assert conn.execute("SELECT name FROM projects WHERE name = 'Encrypted Backup'").fetchone()

    target_path = tmp_path / "untouched.db"
    target_conn = sqlite3.connect(target_path)
    target_conn.execute("CREATE TABLE marker (value TEXT)")
    target_conn.execute("INSERT INTO marker VALUES ('keep')")
    target_conn.commit()
    target_conn.close()
    before = target_path.read_bytes()
    tampered = tmp_path / "tampered.zero"
    tampered.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
    target = Database(Settings.load_for_test(database_url=f"sqlite:///{target_path}"))
    with pytest.raises(ValueError, match="authentication|decrypt|integrity"):
        services.backup.restore_from_file(str(tampered), target)
    assert target_path.read_bytes() == before
    check = sqlite3.connect(target_path)
    assert check.execute("SELECT value FROM marker").fetchone()[0] == "keep"
    check.close()
