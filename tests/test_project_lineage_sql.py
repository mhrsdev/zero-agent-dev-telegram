"""Direct SQL regressions for project-lineage boundaries.

Application authorization is not sufficient protection for a shared SQLite
store: alternate callers can bypass service-layer checks. These tests exercise
the schema after the complete migration sequence and require both INSERT and
UPDATE operations to reject identifiers owned by another project.
"""

from __future__ import annotations

import sqlite3

import pytest

from zero.app.services import build_services
from zero.app.worker_service import TaskSpec
from zero.config import Settings
from zero.domain.plans import PlanRevisionContent
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations


@pytest.fixture
def services(test_settings: Settings):
    database = Database(test_settings)
    apply_migrations(database)
    return build_services(test_settings, database)


def _make_execution(services, name: str):
    owner = services.identity.create_user(display_name=f"{name} owner")
    project = services.identity.create_project(owner_id=owner.id, name=name)
    event = services.plans.ingest_conversation_event(
        project_id=project.id,
        actor_id=owner.id,
        source="web",
        origin_kind="authenticated_human",
        content=f"Create {name} task.",
    )
    plan = services.plans.create_plan(project_id=project.id, actor_id=owner.id)
    services.plans.propose_revision(
        plan_id=plan.id,
        project_id=project.id,
        actor_id=owner.id,
        content=PlanRevisionContent(
            objective=f"Implement {name}",
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
        idempotency_key=f"{name.lower()}-approval",
    )
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        project_id=project.id,
        actor_id=owner.id,
        task_specs=[TaskSpec(key=f"{name.lower()}-task", objective=f"Do {name}")],
    )
    return owner, project, execution


def _insert_integration_fixture(services, project_id: str, execution_id: str, suffix: str):
    conn = services.database.connect()
    conn.execute(
        "INSERT INTO repositories "
        "(id, project_id, name, local_path, default_base_revision) "
        "VALUES (?, ?, ?, ?, ?)",
        (f"repo-{suffix}", project_id, f"repo-{suffix}", f"/tmp/{suffix}", "main"),
    )
    conn.execute(
        "INSERT INTO integration_reviews "
        "(id, project_id, execution_id, source_task_ids) VALUES (?, ?, ?, ?)",
        (f"review-{suffix}", project_id, execution_id, "[]"),
    )
    conn.execute(
        "INSERT INTO integration_worktrees "
        "(id, project_id, execution_id, repository_id, worktree_path, branch_name, base_revision) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            f"integration-wt-{suffix}",
            project_id,
            execution_id,
            f"repo-{suffix}",
            f"/tmp/integration-{suffix}",
            f"integration/{suffix}",
            "main",
        ),
    )
    conn.commit()


def test_interface_event_log_rejects_cross_project_insert_and_update(services) -> None:
    owner_a, project_a, _execution_a = _make_execution(services, "Interface A")
    owner_b, project_b, _execution_b = _make_execution(services, "Interface B")
    binding_a = services.interfaces.create_binding(
        project_id=project_a.id,
        actor_id=owner_a.id,
        platform="telegram",
        chat_id="chat-a",
    )
    binding_b = services.interfaces.create_binding(
        project_id=project_b.id,
        actor_id=owner_b.id,
        platform="telegram",
        chat_id="chat-b",
    )
    conn = services.database.connect()

    with pytest.raises(sqlite3.IntegrityError, match="interface event project lineage"):
        conn.execute(
            "INSERT INTO interface_event_log "
            "(id, project_id, platform, binding_scope, binding_id, external_event_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "event-cross-project-insert",
                project_a.id.value,
                "telegram",
                binding_b.id.value,
                binding_b.id.value,
                "external-insert",
            ),
        )
    conn.rollback()

    conn.execute(
        "INSERT INTO interface_event_log "
        "(id, project_id, platform, binding_scope, binding_id, external_event_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            "event-cross-project-update",
            project_a.id.value,
            "telegram",
            binding_a.id.value,
            binding_a.id.value,
            "external-update",
        ),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="interface event project lineage"):
        conn.execute(
            "UPDATE interface_event_log SET binding_scope = ?, binding_id = ? WHERE id = ?",
            (binding_b.id.value, binding_b.id.value, "event-cross-project-update"),
        )
    conn.rollback()


def test_integration_review_evidence_rejects_cross_project_insert_and_update(services) -> None:
    _owner_a, project_a, execution_a = _make_execution(services, "Evidence A")
    _owner_b, project_b, execution_b = _make_execution(services, "Evidence B")
    _insert_integration_fixture(services, project_a.id.value, execution_a.id.value, "a")
    _insert_integration_fixture(services, project_b.id.value, execution_b.id.value, "b")
    conn = services.database.connect()

    values = (
        "evidence-cross-project-insert",
        project_a.id.value,
        "review-b",
        execution_a.id.value,
        "integration-wt-a",
        "/tmp/integration-a",
        "test",
        "pytest",
        "[]",
        0,
        "0",
        "",
        "",
        "hash-insert",
    )
    with pytest.raises(sqlite3.IntegrityError, match="integration review evidence project lineage"):
        conn.execute(
            "INSERT INTO integration_review_evidence "
            "(id, project_id, review_id, execution_id, integration_worktree_id, "
            "worktree_path, kind, command, args, exit_code, timed_out, stdout, stderr, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
    conn.rollback()

    conn.execute(
        "INSERT INTO integration_review_evidence "
        "(id, project_id, review_id, execution_id, integration_worktree_id, "
        "worktree_path, kind, command, content_hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "evidence-cross-project-update",
            project_a.id.value,
            "review-a",
            execution_a.id.value,
            "integration-wt-a",
            "/tmp/integration-a",
            "test",
            "pytest",
            "hash-update",
        ),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="integration review evidence project lineage"):
        conn.execute(
            "UPDATE integration_review_evidence SET review_id = ?, integration_worktree_id = ? "
            "WHERE id = ?",
            ("review-b", "integration-wt-b", "evidence-cross-project-update"),
        )
    conn.rollback()


def test_result_deliveries_rejects_cross_project_insert_and_update(services) -> None:
    owner_a, project_a, execution_a = _make_execution(services, "Delivery A")
    owner_b, project_b, _execution_b = _make_execution(services, "Delivery B")
    binding_a = services.interfaces.create_binding(
        project_id=project_a.id,
        actor_id=owner_a.id,
        platform="telegram",
        chat_id="delivery-a",
    )
    binding_b = services.interfaces.create_binding(
        project_id=project_b.id,
        actor_id=owner_b.id,
        platform="telegram",
        chat_id="delivery-b",
    )
    conn = services.database.connect()

    with pytest.raises(sqlite3.IntegrityError, match="result delivery project lineage"):
        conn.execute(
            "INSERT INTO result_deliveries "
            "(id, project_id, execution_id, binding_id, created_by, delivery_key, content) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "delivery-cross-project-insert",
                project_a.id.value,
                execution_a.id.value,
                binding_b.id.value,
                owner_a.id.value,
                "delivery-insert",
                "result",
            ),
        )
    conn.rollback()

    conn.execute(
        "INSERT INTO result_deliveries "
        "(id, project_id, execution_id, binding_id, created_by, delivery_key, content) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "delivery-cross-project-update",
            project_a.id.value,
            execution_a.id.value,
            binding_a.id.value,
            owner_a.id.value,
            "delivery-update",
            "result",
        ),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="result delivery project lineage"):
        conn.execute(
            "UPDATE result_deliveries SET binding_id = ? WHERE id = ?",
            (binding_b.id.value, "delivery-cross-project-update"),
        )
    conn.rollback()


def test_project_ownership_is_immutable_for_denormalized_roots(services) -> None:
    _owner_a, project_a, _execution_a = _make_execution(services, "Immutable A")
    _owner_b, project_b, _execution_b = _make_execution(services, "Immutable B")
    conn = services.database.connect()
    conn.execute(
        "INSERT INTO plans (id, project_id) VALUES (?, ?)",
        ("plan-immutable", project_a.id.value),
    )
    conn.execute(
        "INSERT INTO agent_types "
        "(id, project_id, name, responsibility, memory_scope) VALUES (?, ?, ?, ?, ?)",
        ("agent-type-immutable", project_a.id.value, "type", "work", "project"),
    )
    conn.execute(
        "INSERT INTO knowledge_records "
        "(id, project_id, kind, content, content_hash) VALUES (?, ?, ?, ?, ?)",
        ("knowledge-immutable", project_a.id.value, "fact", "fact", "hash"),
    )
    conn.execute(
        "INSERT INTO rag_documents "
        "(id, project_id, source_type, source_id, title, content, content_hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "rag-immutable",
            project_a.id.value,
            "manual",
            "source",
            "title",
            "content",
            "hash-rag",
        ),
    )
    conn.commit()

    mutations = (
        ("plans", "plan-immutable"),
        ("agent_types", "agent-type-immutable"),
        ("knowledge_records", "knowledge-immutable"),
        ("rag_documents", "rag-immutable"),
    )
    for table, row_id in mutations:
        with pytest.raises(sqlite3.IntegrityError, match="project ownership is immutable"):
            conn.execute(
                f"UPDATE {table} SET project_id = ? WHERE id = ?",
                (project_b.id.value, row_id),
            )
        conn.rollback()


def test_conversation_event_cannot_move_across_projects(services) -> None:
    """Regression for the live audit probe: UPDATE conversation_events SET
    project_id = <other project> must be rejected by the schema."""
    owner_a = services.identity.create_user(display_name="A")
    project_a = services.identity.create_project(owner_id=owner_a.id, name="Lineage A")
    owner_b = services.identity.create_user(display_name="B")
    project_b = services.identity.create_project(owner_id=owner_b.id, name="Lineage B")

    event = services.plans.ingest_conversation_event(
        project_id=project_a.id,
        actor_id=owner_a.id,
        source="web",
        origin_kind="authenticated_human",
        content="Bound to project A forever.",
    )
    conn = services.database.connect()
    with pytest.raises(sqlite3.IntegrityError, match="project_id is immutable"):
        conn.execute(
            "UPDATE conversation_events SET project_id = ? WHERE id = ?",
            (project_b.id.value, event.id.value),
        )


def test_remaining_scoped_tables_reject_cross_project_updates(services, tmp_path) -> None:
    owner = services.identity.create_user(display_name="Owner")
    project_a = services.identity.create_project(owner_id=owner.id, name="Scope A")
    project_b = services.identity.create_project(owner_id=owner.id, name="Scope B")

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    import subprocess

    subprocess.run(["git", "init", "-q", str(repo_dir)], check=True, capture_output=True)
    repository = services.worktree.register_repository(
        project_id=project_a.id,
        actor_id=owner.id,
        name="repo",
        local_path=str(repo_dir),
    )
    # A secret reference row inserted directly: the trigger boundary is
    # what is under test, not encryption.
    conn = services.database.connect()
    secret_id = "sr_lineage_probe_0001"
    conn.execute(
        "INSERT INTO secret_references (id, project_id, name, secret_type, encrypted_value) "
        "VALUES (?, ?, 'cred', 'token', 'x')",
        (secret_id, project_a.id.value),
    )
    agent_type = services.agent_types.create_type(
        project_id=project_a.id,
        actor_id=owner.id,
        name="scoped",
        responsibility="scope",
        memory_scope="",
    )
    from zero.domain.agent_types import TopologySnapshot, TopologySnapshotId
    from zero.domain.ids import generate_topology_snapshot_id

    snapshot = TopologySnapshot(
        id=TopologySnapshotId(generate_topology_snapshot_id()),
        project_id=project_a.id,
        snapshot_version=1,
        reason="lineage-test",
        topology_state="{}",
    )
    services.agent_types._repo.insert_snapshot(snapshot)
    tool = services.tools.register_echo_tool()
    grant = services.tools.grant_tool(
        project_id=project_a.id,
        actor_id=owner.id,
        tool_id=tool.id,
        agent_scope="main_worker",
    )

    cases = [
        ("UPDATE repositories SET project_id = ?", repository.id.value),
        ("UPDATE secret_references SET project_id = ?", secret_id),
        ("UPDATE topology_snapshots SET project_id = ?", snapshot.id.value),
        ("UPDATE tool_grants SET project_id = ?", grant.id.value),
    ]
    for statement, target_id in cases:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(statement + " WHERE id = ?", (project_b.id.value, target_id))
    del agent_type

    # Membership rows are keyed by (project_id, user_id).
    member = services.identity.create_user(display_name="Member")
    services.identity.add_member(
        project_id=project_a.id,
        actor_id=owner.id,
        member_id=member.id,
        role="member",
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE project_memberships SET project_id = ? WHERE project_id = ? AND user_id = ?",
            (project_b.id.value, project_a.id.value, member.id.value),
        )

    # Interface bindings.
    binding = services.interfaces.create_binding(
        project_id=project_a.id,
        actor_id=owner.id,
        platform="telegram",
        chat_id="42",
        topic_id=None,
        bot_token_ref=None,
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE interface_bindings SET project_id = ? WHERE id = ?",
            (project_b.id.value, binding.id.value),
        )
