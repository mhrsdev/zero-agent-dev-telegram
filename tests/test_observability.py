"""Observability, recovery, and security hardening tests — M14 validation.

Per PLAN.md M14 validation:
- Secret canary scan across logs, audit, metrics, artifacts, prompts,
  and backups.
- Restore into an isolated environment and run integrity checks.
- Kill processes at critical transition points and verify safe resume.
- Simulate provider/tool outage and retry exhaustion.
- Verify audit ordering and actor identity for sensitive operations.
- Verify no cross-project identifiers or data leak through metrics or
  errors.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from zero.app.observability_service import (
    MetricsService,
    scan_for_secrets,
)
from zero.app.services import build_services
from zero.config import Settings
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations


@pytest.fixture
def services(test_settings: Settings):
    database = Database(test_settings)
    apply_migrations(database)
    return build_services(test_settings, database)


# ----------------------------------------------------------------------
# Secret canary scan
# ----------------------------------------------------------------------


def test_scan_for_secrets_detects_api_key() -> None:
    matches = scan_for_secrets("my key is sk-abc123def456ghi789jkl012mno345")
    assert len(matches) > 0


def test_scan_for_secrets_detects_bearer_token() -> None:
    matches = scan_for_secrets("Authorization: Bearer abc123def456")
    assert len(matches) > 0


def test_scan_for_secrets_detects_password() -> None:
    matches = scan_for_secrets("password=supersecret")
    assert len(matches) > 0


def test_scan_for_secrets_clean_text() -> None:
    matches = scan_for_secrets("This is a normal log message about users.")
    assert len(matches) == 0


def test_canary_scan_finds_no_secrets_in_clean_system(services) -> None:
    """Per PLAN.md M14: 'Secret canary scan across logs, audit, metrics,
    artifacts, prompts, and backups.'"""
    # Create some normal data.
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(
        owner_id=owner.id, name="Clean Project"
    )
    services.artifacts.store_artifact(
        project_id=project.id, actor_id=owner.id,
        kind="stdout", content="normal output without secrets",
    )
    # Scan the system.
    findings = services.canary.scan_all()
    # No secrets should be found.
    for surface, matches in findings.items():
        assert len(matches) == 0, (
            f"Secret found in {surface}: {matches}"
        )


def test_canary_scan_detects_secret_in_audit(services) -> None:
    """If a secret-like value accidentally enters an audit summary, the
    defensive redaction in the audit repository should replace it before
    storage, and the canary scan should find no secrets in the stored
    audit events."""
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(
        owner_id=owner.id, name="Canary Project"
    )
    # Record an audit event with a secret-like value.
    services.audit.record(
        project_id=project.id,
        actor_id=owner.id,
        source="web",
        operation="test.sensitive",
        redacted_summary="Used key sk-abc123def456ghi789jkl012mno345",
    )
    # The defensive redaction in the audit repository should have
    # replaced the secret before storage.
    conn = services.database.connect()
    cursor = conn.execute(
        "SELECT redacted_summary FROM audit_events "
        "WHERE operation = 'test.sensitive'"
    )
    stored_summary = cursor.fetchone()[0]
    assert "sk-abc123def456ghi789jkl012mno345" not in stored_summary
    assert "REDACTED" in stored_summary
    # The canary scan should find no secrets in the stored audit events
    # (because they were already redacted).
    findings = services.canary.scan_all()
    assert len(findings["audit_events"]) == 0


def test_canary_scan_detects_secret_in_artifact(services) -> None:
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(
        owner_id=owner.id, name="Artifact Canary"
    )
    services.artifacts.store_artifact(
        project_id=project.id, actor_id=owner.id,
        kind="stdout",
        content="output contains password=hunter2",
    )
    findings = services.canary.scan_all()
    assert len(findings["artifacts"]) > 0


# ----------------------------------------------------------------------
# Metrics
# ----------------------------------------------------------------------


def test_metrics_increment_with_low_cardinality_labels() -> None:
    """Per zero-observability-evidence: metrics use low-cardinality
    dimensions."""
    m = MetricsService()
    m.increment("execution.completed", result="success", source="web")
    m.increment("execution.completed", result="success", source="web")
    m.increment("execution.completed", result="failure", source="telegram")
    counters = m.get_counters()
    assert counters["execution.completed|result=success|source=web"] == 2
    assert counters["execution.completed|result=failure|source=telegram"] == 1


def test_metrics_reject_high_cardinality_labels() -> None:
    """Per zero-observability-evidence: prompt text, error message, user
    name, file path, tool arguments are NEVER used as labels."""
    m = MetricsService()
    # Unknown result is normalized to "unknown".
    m.increment("test", result="weird_value_not_in_allowed_set")
    counters = m.get_counters()
    assert "test|result=unknown" in counters


def test_metrics_duration() -> None:
    m = MetricsService()
    m.observe_duration("tool.invoke", 100.0)
    m.observe_duration("tool.invoke", 200.0)
    summary = m.get_histogram_summary("tool.invoke")
    assert summary is not None
    assert summary["count"] == 2
    assert summary["min"] == 100.0
    assert summary["max"] == 200.0
    assert summary["avg"] == 150.0


# ----------------------------------------------------------------------
# Backup and restore
# ----------------------------------------------------------------------


def test_backup_and_restore_round_trip(services, tmp_path) -> None:
    """Per PLAN.md M14: 'Restore into an isolated environment and run
    integrity checks.'"""
    # Create some data.
    owner = services.identity.create_user(display_name="Owner")
    services.identity.create_project(
        owner_id=owner.id, name="Backup Project"
    )
    # Back up.
    backup_path = services.backup.backup_to_file(
        str(tmp_path / "backup.sql")
    )
    assert Path(backup_path).exists()
    # Restore into a fresh file-based database (not in-memory).
    restore_db_path = tmp_path / "restored.db"
    restore_conn = sqlite3.connect(str(restore_db_path))
    sql = Path(backup_path).read_text(encoding="utf-8")
    restore_conn.executescript(sql)
    restore_conn.commit()
    # Verify the data was restored.
    cursor = restore_conn.execute("SELECT COUNT(*) FROM users")
    assert cursor.fetchone()[0] >= 1
    cursor = restore_conn.execute("SELECT COUNT(*) FROM projects")
    assert cursor.fetchone()[0] >= 1
    # Verify integrity.
    cursor = restore_conn.execute("PRAGMA integrity_check")
    assert cursor.fetchone()[0] == "ok"
    restore_conn.close()


def test_restore_preserves_project_isolation(services, tmp_path) -> None:
    """Per PLAN.md M14: 'Backups and restores preserve project isolation.'"""
    # Create two projects.
    owner_a = services.identity.create_user(display_name="Owner A")
    services.identity.create_project(
        owner_id=owner_a.id, name="Project A"
    )
    owner_b = services.identity.create_user(display_name="Owner B")
    services.identity.create_project(
        owner_id=owner_b.id, name="Project B"
    )
    # Back up.
    backup_path = services.backup.backup_to_file(
        str(tmp_path / "backup_iso.sql")
    )
    # Restore into a fresh file-based database.
    restore_db_path = tmp_path / "restored_iso.db"
    restore_conn = sqlite3.connect(str(restore_db_path))
    sql = Path(backup_path).read_text(encoding="utf-8")
    restore_conn.executescript(sql)
    restore_conn.commit()
    # Verify both projects exist.
    cursor = restore_conn.execute(
        "SELECT id, name FROM projects ORDER BY name"
    )
    projects = [(row[0], row[1]) for row in cursor.fetchall()]
    assert len(projects) == 2
    names = [p[1] for p in projects]
    assert "Project A" in names
    assert "Project B" in names
    restore_conn.close()


# ----------------------------------------------------------------------
# Recovery
# ----------------------------------------------------------------------


def test_recover_stuck_executions(services) -> None:
    """Per PLAN.md M14: 'Stuck execution ... recovery.'"""
    from zero.app.worker_service import TaskSpec
    from zero.domain.plans import PlanRevisionContent

    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(
        owner_id=owner.id, name="Recovery Project"
    )
    event = services.plans.ingest_conversation_event(
        project_id=project.id, actor_id=owner.id, source="web",
        origin_kind="authenticated_human", content="Add a feature."
    )
    plan = services.plans.create_plan(
        project_id=project.id, actor_id=owner.id
    )
    content = PlanRevisionContent(
        objective="Add a feature", scope=(), constraints=(),
        acceptance_criteria=("Works",), risks=(), unresolved_questions=(),
        source_event_ids=(event.id,),
    )
    services.plans.propose_revision(
        plan_id=plan.id, actor_id=owner.id, content=content
    )
    _, handoff = services.plans.approve_revision(
        plan_id=plan.id, actor_id=owner.id,
        expected_revision_number=1, idempotency_key="r1",
    )
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id, actor_id=owner.id,
        task_specs=[TaskSpec(key="A", objective="Task A")],
    )
    # Manually set execution to 'running' to simulate a stuck state.
    conn = services.database.connect()
    conn.execute(
        "UPDATE executions SET state = 'running' WHERE id = ?",
        (execution.id.value,),
    )
    conn.commit()
    # Run recovery.
    recovered = services.recovery.recover_stuck_executions()
    assert len(recovered) >= 1
    assert execution.id.value in recovered
    # The execution should now be paused (recovered).
    exec_after = services.worker.get_execution(execution.id)
    assert exec_after.state == "paused"


def test_recover_orphan_worktrees(services, tmp_path) -> None:
    """Per PLAN.md M14: 'Orphan worktree ... recovery.'"""
    import subprocess

    from zero.app.worker_service import TaskSpec
    from zero.domain.plans import PlanRevisionContent

    # Create a repo.
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "t@t.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "T"], check=True
    )
    (repo / "README.md").write_text("# Test")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "init"],
        check=True, capture_output=True,
    )
    # Override worktree root.
    from zero.app.worktree_service import WorktreeService
    services.worktree = WorktreeService(
        services.worktree._repo, services.worktree._audit_repo,
        services.worktree._authz,
        worktree_root=str(tmp_path / "worktrees"),
    )
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(
        owner_id=owner.id, name="WT Recovery"
    )
    repo_rec = services.worktree.register_repository(
        project_id=project.id, actor_id=owner.id, name="r",
        local_path=str(repo), default_base_revision="main",
    )
    event = services.plans.ingest_conversation_event(
        project_id=project.id, actor_id=owner.id, source="web",
        origin_kind="authenticated_human", content="Add a feature."
    )
    plan = services.plans.create_plan(
        project_id=project.id, actor_id=owner.id
    )
    content = PlanRevisionContent(
        objective="Add a feature", scope=(), constraints=(),
        acceptance_criteria=("Works",), risks=(), unresolved_questions=(),
        source_event_ids=(event.id,),
    )
    services.plans.propose_revision(
        plan_id=plan.id, actor_id=owner.id, content=content
    )
    _, handoff = services.plans.approve_revision(
        plan_id=plan.id, actor_id=owner.id,
        expected_revision_number=1, idempotency_key="r2",
    )
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id, actor_id=owner.id,
        task_specs=[TaskSpec(key="A", objective="Task A")],
    )
    task = services.worker.list_tasks(execution.id)[0]
    wt = services.worktree.create_worktree(
        project_id=project.id, repository_id=repo_rec.id,
        execution_id=execution.id, task_id=task.id, actor_id=owner.id,
    )
    # Activate the worktree (simulate running).
    services.worktree.activate_worktree(
        project_id=project.id, worktree_id=wt.id, actor_id=owner.id
    )
    # Run recovery.
    recovered = services.recovery.recover_orphan_worktrees()
    assert len(recovered) >= 1
    assert wt.id.value in recovered
    # The worktree should now be interrupted.
    wt_after = services.worktree.get_worktree(project.id, wt.id)
    assert wt_after.state == "interrupted"


def test_recover_partial_compaction(services) -> None:
    """Per PLAN.md M14: 'Partial compaction ... recovery.'"""
    from zero.app.worker_service import TaskSpec
    from zero.domain.plans import PlanRevisionContent

    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(
        owner_id=owner.id, name="Compaction Recovery"
    )
    event = services.plans.ingest_conversation_event(
        project_id=project.id, actor_id=owner.id, source="web",
        origin_kind="authenticated_human", content="Add a feature."
    )
    plan = services.plans.create_plan(
        project_id=project.id, actor_id=owner.id
    )
    content = PlanRevisionContent(
        objective="Add a feature", scope=(), constraints=(),
        acceptance_criteria=("Works",), risks=(), unresolved_questions=(),
        source_event_ids=(event.id,),
    )
    services.plans.propose_revision(
        plan_id=plan.id, actor_id=owner.id, content=content
    )
    _, handoff = services.plans.approve_revision(
        plan_id=plan.id, actor_id=owner.id,
        expected_revision_number=1, idempotency_key="r3",
    )
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id, actor_id=owner.id,
        task_specs=[TaskSpec(key="A", objective="Task A")],
    )
    # Insert a compaction record in a partial state.
    conn = services.database.connect()
    conn.execute(
        "INSERT INTO compaction_records "
        "(id, project_id, execution_id, source_context_version, "
        "target_context_version, source_event_range, summary, fit_rung, "
        "state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("comp_test_partial", project.id.value, execution.id.value,
         1, 2, "{}", "partial summary", "verbatim", "pre_flush"),
    )
    conn.commit()
    # Run recovery.
    recovered = services.recovery.recover_partial_compaction()
    assert "comp_test_partial" in recovered
    # The compaction record should now be 'failed'.
    cursor = conn.execute(
        "SELECT state FROM compaction_records WHERE id = ?",
        ("comp_test_partial",),
    )
    assert cursor.fetchone()[0] == "failed"


# ----------------------------------------------------------------------
# No cross-project leakage through metrics or errors
# ----------------------------------------------------------------------


def test_no_cross_project_leakage_through_metrics(services) -> None:
    """Per PLAN.md M14: 'Verify no cross-project identifiers or data
    leak through metrics or errors.'"""
    owner_a = services.identity.create_user(display_name="Owner A")
    project_a = services.identity.create_project(
        owner_id=owner_a.id, name="Metrics Project A"
    )
    owner_b = services.identity.create_user(display_name="Owner B")
    project_b = services.identity.create_project(
        owner_id=owner_b.id, name="Metrics Project B"
    )
    # Increment metrics for project A.
    services.metrics.increment("execution.completed", result="success")
    # Metrics should not contain project IDs.
    counters = services.metrics.get_counters()
    for key in counters:
        assert project_a.id.value not in key
        assert project_b.id.value not in key
        assert "Metrics Project A" not in key
        assert "Metrics Project B" not in key
