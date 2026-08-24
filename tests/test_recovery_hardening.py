"""Hardened recovery semantics tests.

Per the release audit (§5.2, Phase 3):
- never-dispatched ``pending`` provider requests are requeued instead of
  being converted to unknown;
- only ``streaming`` requests become unknown for reconciliation;
- active worktrees with an unexpired owning attempt lease are NOT
  interrupted (cleanup requires proof of non-ownership);
- partial compaction recovery inspects actual source/target activation
  state: durable targets are activated, missing targets are failed.
"""

from __future__ import annotations

import pytest

from zero.app.services import build_services
from zero.app.worker_service import TaskSpec
from zero.config import Settings
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations


@pytest.fixture
def services(test_settings: Settings):
    database = Database(test_settings)
    apply_migrations(database)
    return build_services(test_settings, database)


def _make_project(services, name: str):
    owner = services.identity.create_user(display_name=f"{name} owner")
    project = services.identity.create_project(owner_id=owner.id, name=name)
    return owner, project


def _approved_plan(services, owner, project, key: str):
    from zero.domain.plans import PlanRevisionContent

    event = services.plans.ingest_conversation_event(
        project_id=project.id,
        actor_id=owner.id,
        source="web",
        origin_kind="authenticated_human",
        content=f"Approved work for {key}.",
    )
    plan = services.plans.create_plan(project_id=project.id, actor_id=owner.id)
    services.plans.propose_revision(
        plan_id=plan.id,
        project_id=project.id,
        actor_id=owner.id,
        content=PlanRevisionContent(
            objective=f"Work {key}",
            scope=(),
            constraints=(),
            acceptance_criteria=("Done",),
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
        idempotency_key=f"{key}-approval",
    )
    return handoff


def _make_execution_with_task(services, owner, project, key: str):
    """Create a durable execution with one ready task (real FK chain)."""
    handoff = _approved_plan(services, owner, project, key)
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        project_id=project.id,
        actor_id=owner.id,
        task_specs=[TaskSpec(key="A", objective="Task A")],
    )
    tasks = services.worker.list_tasks(execution.id, project_id=project.id, actor_id=owner.id)
    return execution, tasks[0]


def _insert_provider_request(services, project_id, *, state: str, claim_owner="worker-1"):
    conn = services.database.connect()
    request_id = f"pr_{state}_{abs(hash((str(project_id), state))) % 10_000_000}"
    conn.execute(
        "INSERT INTO provider_requests "
        "(id, project_id, execution_id, provider, model_name, request_hash, "
        "idempotency_key, state, started_at, claim_owner, lease_expires_at) "
        "VALUES (?, ?, NULL, 'fake', 'fake-standard', 'hash', ?, ?, "
        "strftime('%Y-%m-%dT%H:%M:%fZ','now','-1 hour'), ?, "
        "strftime('%Y-%m-%dT%H:%M:%fZ','now','-30 minutes'))",
        (request_id, project_id.value, request_id, state, claim_owner),
    )
    conn.commit()
    return request_id


def test_pending_provider_requests_are_requeued_not_unknowned(services) -> None:
    _owner, project = _make_project(services, "Requeue")
    request_id = _insert_provider_request(
        services, project.id, state="pending", claim_owner="dead-worker"
    )

    result = services.recovery.recover_stale_provider_requests()
    assert result["unknown_streaming"] == []
    row = (
        services.database.connect()
        .execute("SELECT state, claim_owner FROM provider_requests WHERE id = ?", (request_id,))
        .fetchone()
    )
    assert row["state"] == "pending"
    assert row["claim_owner"] is None


def test_streaming_provider_requests_become_unknown(services) -> None:
    _owner, project = _make_project(services, "Streaming")
    request_id = _insert_provider_request(services, project.id, state="streaming")

    result = services.recovery.recover_stale_provider_requests()
    assert request_id in result["unknown_streaming"]
    row = (
        services.database.connect()
        .execute("SELECT state FROM provider_requests WHERE id = ?", (request_id,))
        .fetchone()
    )
    assert row["state"] == "unknown"


def _worktree_setup(services, owner, project, tmp_path):
    import subprocess

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo_dir)], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "T"], check=True)
    (repo_dir / "README.md").write_text("base\n")
    subprocess.run(["git", "-C", str(repo_dir), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo_dir), "commit", "-q", "-m", "init"],
        check=True,
        capture_output=True,
    )
    repo = services.worktree.register_repository(
        project_id=project.id,
        actor_id=owner.id,
        name="repo",
        local_path=str(repo_dir),
        default_base_revision="main",
    )
    execution = services.worker.create_execution_from_handoff(
        handoff_id=_approved_plan(services, owner, project, f"wt-{project.id.value}").id,
        project_id=project.id,
        actor_id=owner.id,
        task_specs=[TaskSpec(key="A", objective="Task A")],
    )
    task = services.worker.list_tasks(execution.id, project_id=project.id, actor_id=owner.id)[0]
    return repo, execution, task


def test_owned_worktree_is_not_interrupted_by_recovery(services, tmp_path) -> None:
    owner, project = _make_project(services, "OwnedWorktree")
    repo, execution, task = _worktree_setup(services, owner, project, tmp_path)

    # Claim the task: creates a running attempt holding an unexpired lease.
    services.worker.claim_task(
        execution_id=execution.id,
        task_id=task.id,
        project_id=project.id,
        actor_id=owner.id,
        lease_owner="live-worker",
        lease_duration_seconds=300,
    )
    worktree = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repo.id,
        execution_id=execution.id,
        task_id=task.id,
        actor_id=owner.id,
    )
    services.worktree.activate_worktree(
        project_id=project.id, worktree_id=worktree.id, actor_id=owner.id
    )

    recovered = services.recovery.recover_orphan_worktrees()
    assert worktree.id.value not in recovered
    state = services.worktree.get_worktree(project.id, worktree.id, actor_id=owner.id).state
    assert state == "active"


def test_expired_lease_worktree_is_interrupted_by_recovery(services, tmp_path) -> None:
    owner, project = _make_project(services, "OrphanWorktree")
    repo, execution, task = _worktree_setup(services, owner, project, tmp_path)
    worktree = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repo.id,
        execution_id=execution.id,
        task_id=task.id,
        actor_id=owner.id,
    )
    services.worktree.activate_worktree(
        project_id=project.id, worktree_id=worktree.id, actor_id=owner.id
    )
    # No running attempt owns this task: nothing proves live ownership.

    recovered = services.recovery.recover_orphan_worktrees()
    assert worktree.id.value in recovered
    state = services.worktree.get_worktree(project.id, worktree.id, actor_id=owner.id).state
    assert state == "interrupted"


def test_partial_compaction_with_durable_target_is_completed(services) -> None:
    owner, project = _make_project(services, "Compaction")
    execution, _task = _make_execution_with_task(services, owner, project, "compact")
    execution_id = execution.id.value
    conn = services.database.connect()

    # Source context version is the currently active one.
    conn.execute(
        "INSERT INTO context_versions (id, project_id, execution_id, version, active, "
        "system_message, user_prefix, plan_contract, execution_snapshot, retrieved_context, "
        "conversation_tail, compaction_summary, token_count) "
        "VALUES ('cv_source_probe_001', ?, ?, 1, 1, '', '', '', '', '[]', '', '', 10)",
        (project.id.value, execution_id),
    )
    # Target exists durably but was never activated (crash before commit).
    conn.execute(
        "INSERT INTO context_versions (id, project_id, execution_id, version, active, "
        "system_message, user_prefix, plan_contract, execution_snapshot, retrieved_context, "
        "conversation_tail, compaction_summary, token_count) "
        "VALUES ('cv_target_probe_01', ?, ?, 2, 0, '', '', '', '', '[]', '', 'summary', 5)",
        (project.id.value, execution_id),
    )
    record_id = "cr_compact_probe_001"
    conn.execute(
        "INSERT INTO compaction_records (id, project_id, execution_id, source_context_version, "
        "target_context_version, source_event_range, summary, fit_rung, state, no_thrash_count) "
        "VALUES (?, ?, ?, 1, 2, '{}', 'summary', 'verbatim', 'committed', 0)",
        (record_id, project.id.value, execution_id),
    )
    conn.commit()

    recovered = services.recovery.recover_partial_compaction()
    assert record_id in recovered
    state = conn.execute(
        "SELECT state FROM compaction_records WHERE id = ?", (record_id,)
    ).fetchone()["state"]
    target_active = conn.execute(
        "SELECT active FROM context_versions WHERE id = 'cv_target_probe_01'"
    ).fetchone()["active"]
    source_active = conn.execute(
        "SELECT active FROM context_versions WHERE id = 'cv_source_probe_001'"
    ).fetchone()["active"]
    assert state == "activated"
    assert target_active == 1
    assert source_active == 0
