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

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest
from pydantic import SecretStr

from zero.app import observability_service as observability_module
from zero.app.observability_service import (
    BackupService,
    MetricsService,
    scan_for_secrets,
)
from zero.app.services import build_services
from zero.config import Settings
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations


@pytest.fixture
def services():
    settings = Settings.load_for_test(
        secret_key=SecretStr("test-only-backup-key-material-0123456789")
    )
    database = Database(settings)
    apply_migrations(database)
    return build_services(settings, database)


# ----------------------------------------------------------------------
# Secret canary scan
# ----------------------------------------------------------------------


_symlink_privilege = pytest.mark.skipif(
    os.name == "nt",
    reason="creating symlinks requires elevated privileges on Windows",
)


def test_scan_for_secrets_detects_api_key() -> None:
    canary = "sk-" + ("a" * 20)
    matches = scan_for_secrets(f"my key is {canary}")
    assert len(matches) > 0


def test_scan_for_secrets_detects_bearer_token() -> None:
    matches = scan_for_secrets("Authorization: " + "Bearer " + "abc123def456")
    assert len(matches) > 0


def test_scan_for_secrets_detects_password() -> None:
    matches = scan_for_secrets("password=supersecret")
    assert len(matches) > 0


def test_scan_for_secrets_clean_text() -> None:
    matches = scan_for_secrets("This is a normal log message about users.")
    assert len(matches) == 0


def test_backup_without_configured_key_fails_closed(tmp_path) -> None:
    settings = Settings.load_for_test(database_url=f"sqlite:///{tmp_path / 'backup.db'}")
    database = Database(settings)
    apply_migrations(database)
    backup = BackupService(database)

    with pytest.raises(ValueError, match="encryption is unavailable"):
        backup.backup_to_file(str(tmp_path / "backup.bin"))


def test_backup_with_empty_configured_key_fails_closed(tmp_path) -> None:
    settings = Settings.load_for_test(database_url=f"sqlite:///{tmp_path / 'empty-key.db'}")
    database = Database(settings)
    apply_migrations(database)
    backup = BackupService(database, key_material="   ")

    with pytest.raises(ValueError, match="encryption is unavailable"):
        backup.backup_to_file(str(tmp_path / "empty-key.bin"))


def _write_encrypted_payload(backup: BackupService, path: Path, payload: object) -> None:
    token = b"ZERO-BACKUP-V1\\n" + backup._require_fernet().encrypt(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    )
    path.write_bytes(token)


def test_restore_closes_active_wal_connections_before_replacement(services, tmp_path) -> None:
    owner = services.identity.create_user(display_name="Source Owner")
    services.identity.create_project(owner_id=owner.id, name="Source Project")
    backup_path = Path(services.backup.backup_to_file(str(tmp_path / "source.zero")))

    target_path = tmp_path / "active.db"
    target = Database(Settings.load_for_test(database_url=f"sqlite:///{target_path}"))
    active = target.connect()
    active.execute("CREATE TABLE marker (value TEXT NOT NULL)")
    active.execute("INSERT INTO marker(value) VALUES ('old')")
    active.commit()

    services.backup.restore_from_file(str(backup_path), target)
    assert not (tmp_path / "active.db-wal").exists()
    assert not (tmp_path / "active.db-shm").exists()
    fresh = target.connect()
    assert fresh.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1


def test_restore_rolls_back_file_swap_when_post_replace_step_fails(
    services, tmp_path, monkeypatch
) -> None:
    owner = services.identity.create_user(display_name="Source Owner")
    services.identity.create_project(owner_id=owner.id, name="Source Project")
    backup_path = Path(services.backup.backup_to_file(str(tmp_path / "source.zero")))

    target_path = tmp_path / "rollback.db"
    target = Database(Settings.load_for_test(database_url=f"sqlite:///{target_path}"))
    conn = target.connect()
    conn.execute("CREATE TABLE marker (value TEXT NOT NULL)")
    conn.execute("INSERT INTO marker(value) VALUES ('old')")
    conn.commit()
    target.close()
    before = target_path.read_bytes()
    original_chmod = observability_module.os.chmod

    def fail_target_chmod(path, mode):
        if Path(path) == target_path:
            raise OSError("injected post-replace chmod failure")
        return original_chmod(path, mode)

    monkeypatch.setattr(observability_module.os, "chmod", fail_target_chmod)
    with pytest.raises(OSError, match="post-replace"):
        services.backup.restore_from_file(str(backup_path), target)
    assert target_path.read_bytes() == before
    check = sqlite3.connect(target_path)
    assert check.execute("SELECT value FROM marker").fetchone()[0] == "old"
    check.close()


def test_in_memory_restore_rolls_back_when_copy_fails(services, tmp_path, monkeypatch) -> None:
    owner = services.identity.create_user(display_name="Source Owner")
    services.identity.create_project(owner_id=owner.id, name="Source Project")
    backup_path = Path(services.backup.backup_to_file(str(tmp_path / "source.zero")))

    target = Database(Settings.load_for_test())
    conn = target.connect()
    conn.execute("CREATE TABLE marker (value TEXT NOT NULL)")
    conn.execute("INSERT INTO marker(value) VALUES ('old')")
    conn.commit()

    def fail_copy(*_args, **_kwargs):
        raise sqlite3.OperationalError("injected destination failure")

    monkeypatch.setattr(services.backup, "_backup_connection", fail_copy, raising=False)
    with pytest.raises(ValueError, match="in-memory"):
        services.backup.restore_from_file(str(backup_path), target)
    restored = target.connect()
    assert restored.execute("SELECT value FROM marker").fetchone()[0] == "old"


def test_restore_rejects_authenticated_partial_schema(services, tmp_path) -> None:
    tables = (
        "schema_migrations",
        "projects",
        "users",
        "audit_events",
        "plans",
        "plan_revisions",
        "executions",
        "tasks",
        "artifacts",
        "rag_documents",
        "merge_proposals",
    )
    sql = "\n".join(f"CREATE TABLE {table} (id TEXT PRIMARY KEY);" for table in tables) + "\n"
    # ``with sqlite3.connect(...)`` commits but does NOT close the
    # connection, so the handle survived the block and its finalizer
    # later raised ``ResourceWarning: unclosed database`` — an error
    # under the repo's warnings-as-errors policy, surfacing in whichever
    # test happened to run when the collector caught up.
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(sql)
        schema_hash = services.backup._schema_hash(conn)
    finally:
        conn.close()
    payload = {
        "format": "zero-sqlite-backup-v1",
        "created_at": "2026-01-01T00:00:00Z",
        "schema_hash": schema_hash,
        "sql_sha256": hashlib.sha256(sql.encode()).hexdigest(),
        "sql": sql,
    }
    backup_path = tmp_path / "partial.zero"
    _write_encrypted_payload(services.backup, backup_path, payload)
    target = Database(Settings.load_for_test(database_url=f"sqlite:///{tmp_path / 'target.db'}"))
    with pytest.raises(ValueError, match="verification"):
        services.backup.restore_from_file(str(backup_path), target)


def test_restore_rejects_authenticated_malformed_payload(services, tmp_path) -> None:
    backup_path = tmp_path / "malformed.zero"
    _write_encrypted_payload(services.backup, backup_path, [])
    target = Database(Settings.load_for_test(database_url=f"sqlite:///{tmp_path / 'target.db'}"))
    with pytest.raises(TypeError, match="payload"):
        services.backup.restore_from_file(str(backup_path), target)


def test_backup_rolls_back_existing_file_when_post_replace_fails(
    services, tmp_path, monkeypatch
) -> None:
    backup_path = tmp_path / "backup.zero"
    services.backup.backup_to_file(str(backup_path))
    before = backup_path.read_bytes()
    original_chmod = observability_module.os.chmod

    def fail_target_chmod(path, mode):
        if Path(path) == backup_path:
            raise OSError("injected backup chmod failure")
        return original_chmod(path, mode)

    monkeypatch.setattr(observability_module.os, "chmod", fail_target_chmod)
    with pytest.raises(OSError, match="backup chmod"):
        services.backup.backup_to_file(str(backup_path))
    assert backup_path.read_bytes() == before


def test_backup_rolls_back_existing_file_when_directory_fsync_fails(
    services, tmp_path, monkeypatch
) -> None:
    backup_path = tmp_path / "backup-fsync.zero"
    services.backup.backup_to_file(str(backup_path))
    before = backup_path.read_bytes()

    def fail_fsync(_path):
        raise OSError("injected backup directory fsync failure")

    monkeypatch.setattr(services.backup, "_fsync_directory", fail_fsync)
    with pytest.raises(OSError, match="directory fsync"):
        services.backup.backup_to_file(str(backup_path))
    assert backup_path.read_bytes() == before


def test_restore_rejects_authenticated_empty_migration_ledger(services, tmp_path) -> None:
    backup_path = Path(services.backup.backup_to_file(str(tmp_path / "source.zero")))
    raw = backup_path.read_bytes()
    prefix = b"ZERO-BACKUP-V1\\n"
    payload = json.loads(
        services.backup._require_fernet().decrypt(raw[len(prefix) :]).decode("utf-8")
    )
    payload["sql"] += "\\nDELETE FROM schema_migrations;\\n"
    payload["sql_sha256"] = hashlib.sha256(payload["sql"].encode("utf-8")).hexdigest()
    _write_encrypted_payload(services.backup, backup_path, payload)
    target = Database(Settings.load_for_test(database_url=f"sqlite:///{tmp_path / 'target.db'}"))
    with pytest.raises(ValueError, match="verification"):
        services.backup.restore_from_file(str(backup_path), target)


def test_in_memory_restore_rolls_back_when_destination_connect_fails(
    services, tmp_path, monkeypatch
) -> None:
    backup_path = Path(services.backup.backup_to_file(str(tmp_path / "source.zero")))
    target = Database(Settings.load_for_test())
    conn = target.connect()
    conn.execute("CREATE TABLE marker (value TEXT NOT NULL)")
    conn.execute("INSERT INTO marker(value) VALUES ('old')")
    conn.commit()
    original_connect = target.connect
    calls = 0

    def fail_destination_connect():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sqlite3.OperationalError("injected destination connect failure")
        return original_connect()

    monkeypatch.setattr(target, "connect", fail_destination_connect)
    with pytest.raises(ValueError, match="in-memory"):
        services.backup.restore_from_file(str(backup_path), target)
    restored = target.connect()
    assert restored.execute("SELECT value FROM marker").fetchone()[0] == "old"


def test_in_memory_restore_preserves_keyboard_interrupt(services, tmp_path, monkeypatch) -> None:
    backup_path = Path(services.backup.backup_to_file(str(tmp_path / "source.zero")))
    target = Database(Settings.load_for_test())
    conn = target.connect()
    conn.execute("CREATE TABLE marker (value TEXT NOT NULL)")
    conn.execute("INSERT INTO marker(value) VALUES ('old')")
    conn.commit()

    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(services.backup, "_backup_connection", interrupt)
    with pytest.raises(KeyboardInterrupt):
        services.backup.restore_from_file(str(backup_path), target)
    restored = target.connect()
    assert restored.execute("SELECT value FROM marker").fetchone()[0] == "old"


def test_restore_cleans_staging_file_when_chmod_fails(services, tmp_path, monkeypatch) -> None:
    backup_path = Path(services.backup.backup_to_file(str(tmp_path / "source.zero")))
    target = Database(Settings.load_for_test(database_url=f"sqlite:///{tmp_path / 'target.db'}"))
    original_chmod = observability_module.os.chmod

    def fail_staging_chmod(path, mode):
        if Path(path).name.startswith(".zero-restore-"):
            raise OSError("injected staging chmod failure")
        return original_chmod(path, mode)

    monkeypatch.setattr(observability_module.os, "chmod", fail_staging_chmod)
    with pytest.raises(OSError, match="staging chmod"):
        services.backup.restore_from_file(str(backup_path), target)
    assert list(tmp_path.glob(".zero-restore-*.db")) == []


@_symlink_privilege
def test_restore_rejects_backup_input_symlink(services, tmp_path) -> None:
    real_backup = Path(services.backup.backup_to_file(str(tmp_path / "source.zero")))
    symlink = tmp_path / "backup-link.zero"
    symlink.symlink_to(real_backup)
    target = Database(Settings.load_for_test(database_url=f"sqlite:///{tmp_path / 'target.db'}"))
    with pytest.raises(ValueError, match="symlink"):
        services.backup.restore_from_file(str(symlink), target)


@_symlink_privilege
def test_restore_rejects_symlinked_parent_directory(services, tmp_path) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    backup_path = Path(services.backup.backup_to_file(str(tmp_path / "source.zero")))
    target = Database(
        Settings.load_for_test(database_url=f"sqlite:///{linked_parent / 'target.db'}")
    )
    with pytest.raises(ValueError, match="symlink"):
        services.backup.restore_from_file(str(backup_path), target)


def test_canary_scan_finds_no_secrets_in_clean_system(services) -> None:
    """Per PLAN.md M14: 'Secret canary scan across logs, audit, metrics,
    artifacts, prompts, and backups.'"""
    # Create some normal data.
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(owner_id=owner.id, name="Clean Project")
    services.artifacts.store_artifact(
        project_id=project.id,
        actor_id=owner.id,
        kind="stdout",
        content="normal output without secrets",
    )
    # Scan the system.
    findings = services.canary.scan_all()
    # No secrets should be found.
    for surface, matches in findings.items():
        assert len(matches) == 0, f"Secret found in {surface}: {matches}"


def test_canary_scan_detects_secret_in_audit(services) -> None:
    """If a secret-like value accidentally enters an audit summary, the
    defensive redaction in the audit repository should replace it before
    storage, and the canary scan should find no secrets in the stored
    audit events."""
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(owner_id=owner.id, name="Canary Project")
    # Record an audit event with a secret-like value.
    canary = "sk-" + ("a" * 20)
    services.audit.record(
        project_id=project.id,
        actor_id=owner.id,
        source="web",
        operation="test.sensitive",
        redacted_summary=f"Used key {canary}",
    )
    # The defensive redaction in the audit repository should have
    # replaced the secret before storage.
    conn = services.database.connect()
    cursor = conn.execute(
        "SELECT redacted_summary FROM audit_events WHERE operation = 'test.sensitive'"
    )
    stored_summary = cursor.fetchone()[0]
    assert canary not in stored_summary
    assert "REDACTED" in stored_summary
    # The canary scan should find no secrets in the stored audit events
    # (because they were already redacted).
    findings = services.canary.scan_all()
    assert len(findings["audit_events"]) == 0


def test_canary_scan_detects_secret_in_artifact(services) -> None:
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(owner_id=owner.id, name="Artifact Canary")
    services.artifacts.store_artifact(
        project_id=project.id,
        actor_id=owner.id,
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


def test_recovery_marks_stale_provider_claims_unknown(services) -> None:
    owner = services.identity.create_user(display_name="Recovery provider owner")
    project = services.identity.create_project(owner_id=owner.id, name="Recovery provider project")
    from zero.domain.providers import CanonicalMessage, CanonicalRequest

    services.providers.send_request(
        project_id=project.id,
        actor_id=owner.id,
        request=CanonicalRequest(
            provider="fake",
            model_name="fake-standard",
            messages=(CanonicalMessage(role="user", content="stale"),),
        ),
        idempotency_key="stale-provider",
    )
    conn = services.database.connect()
    conn.execute(
        "UPDATE provider_requests SET state = 'streaming', "
        "started_at = '2000-01-01T00:00:00.000Z' "
        "WHERE project_id = ? AND idempotency_key = ?",
        (project.id.value, "stale-provider"),
    )
    conn.commit()

    recovered = services.recovery.recover_stale_provider_requests(max_age_seconds=1)

    assert recovered
    row = services.providers._repo.get_provider_request_by_idempotency_key(
        project.id, "stale-provider"
    )
    assert row is not None
    assert row.state == "unknown"
    assert row.error_class == "unknown_outcome"


def test_backup_and_restore_round_trip(services, tmp_path) -> None:
    """Per PLAN.md M14: 'Restore into an isolated environment and run
    integrity checks.'"""
    # Create some data.
    owner = services.identity.create_user(display_name="Owner")
    services.identity.create_project(owner_id=owner.id, name="Backup Project")
    # Back up.
    backup_path = services.backup.backup_to_file(str(tmp_path / "backup.sql"))
    assert Path(backup_path).exists()
    # Restore through the authenticated service into a fresh file database.
    restore_db_path = tmp_path / "restored.db"
    restored = Database(Settings.load_for_test(database_url=f"sqlite:///{restore_db_path}"))
    result = services.backup.restore_from_file(backup_path, restored)
    assert result["integrity_check"] == "pass"
    restore_conn = restored.connect()
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
    services.identity.create_project(owner_id=owner_a.id, name="Project A")
    owner_b = services.identity.create_user(display_name="Owner B")
    services.identity.create_project(owner_id=owner_b.id, name="Project B")
    # Back up.
    backup_path = services.backup.backup_to_file(str(tmp_path / "backup_iso.sql"))
    # Restore through the authenticated service into a fresh file database.
    restore_db_path = tmp_path / "restored_iso.db"
    restored = Database(Settings.load_for_test(database_url=f"sqlite:///{restore_db_path}"))
    result = services.backup.restore_from_file(backup_path, restored)
    assert result["integrity_check"] == "pass"
    restore_conn = restored.connect()
    # Verify both projects exist.
    cursor = restore_conn.execute("SELECT id, name FROM projects ORDER BY name")
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
    project = services.identity.create_project(owner_id=owner.id, name="Recovery Project")
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
        idempotency_key="r1",
    )
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        project_id=project.id,
        actor_id=owner.id,
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
    # Ready work is schedulable after recovery.
    exec_after = services.worker.get_execution(
        execution.id,
        project_id=execution.project_id,
        actor_id=services.identity.get_project(execution.project_id).owner_user_id,
    )
    assert exec_after.state == "running"


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
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "core.autocrlf", "false"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "t@t.com"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    (repo / "README.md").write_text("# Test")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "core.autocrlf", "false"],
        check=True,
        capture_output=True,
    )
    # Override worktree root.
    from zero.app.worktree_service import WorktreeService

    services.worktree = WorktreeService(
        services.worktree._repo,
        services.worktree._audit_repo,
        services.worktree._authz,
        worktree_root=str(tmp_path / "worktrees"),
        allowed_commands=frozenset({"echo", "sh", "sleep"}),
    )
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(owner_id=owner.id, name="WT Recovery")
    repo_rec = services.worktree.register_repository(
        project_id=project.id,
        actor_id=owner.id,
        name="r",
        local_path=str(repo),
        default_base_revision="main",
    )
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
        idempotency_key="r2",
    )
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        project_id=project.id,
        actor_id=owner.id,
        task_specs=[TaskSpec(key="A", objective="Task A")],
    )
    task = services.worker.list_tasks(
        execution.id,
        project_id=execution.project_id,
        actor_id=services.identity.get_project(execution.project_id).owner_user_id,
    )[0]
    wt = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repo_rec.id,
        execution_id=execution.id,
        task_id=task.id,
        actor_id=owner.id,
    )
    # Activate the worktree (simulate running).
    services.worktree.activate_worktree(project_id=project.id, worktree_id=wt.id, actor_id=owner.id)
    # Run recovery.
    recovered = services.recovery.recover_orphan_worktrees()
    assert len(recovered) >= 1
    assert wt.id.value in recovered
    # The worktree should now be interrupted.
    wt_after = services.worktree.get_worktree(project.id, wt.id, actor_id=owner.id)
    assert wt_after.state == "interrupted"


def test_recover_partial_compaction(services) -> None:
    """Per PLAN.md M14: 'Partial compaction ... recovery.'"""
    from zero.app.worker_service import TaskSpec
    from zero.domain.plans import PlanRevisionContent

    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(owner_id=owner.id, name="Compaction Recovery")
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
        idempotency_key="r3",
    )
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        project_id=project.id,
        actor_id=owner.id,
        task_specs=[TaskSpec(key="A", objective="Task A")],
    )
    # Insert a compaction record in a partial state.
    conn = services.database.connect()
    conn.execute(
        "INSERT INTO compaction_records "
        "(id, project_id, execution_id, source_context_version, "
        "target_context_version, source_event_range, summary, fit_rung, "
        "state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "comp_test_partial",
            project.id.value,
            execution.id.value,
            1,
            2,
            "{}",
            "partial summary",
            "verbatim",
            "pre_flush",
        ),
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
    project_a = services.identity.create_project(owner_id=owner_a.id, name="Metrics Project A")
    owner_b = services.identity.create_user(display_name="Owner B")
    project_b = services.identity.create_project(owner_id=owner_b.id, name="Metrics Project B")
    # Increment metrics for project A.
    services.metrics.increment("execution.completed", result="success")
    # Metrics should not contain project IDs.
    counters = services.metrics.get_counters()
    for key in counters:
        assert project_a.id.value not in key
        assert project_b.id.value not in key
        assert "Metrics Project A" not in key
        assert "Metrics Project B" not in key
