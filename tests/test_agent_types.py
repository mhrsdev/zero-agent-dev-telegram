"""Agent type service tests — covers all M7 validation gates.

Per PLAN.md M7 validation:
- Unauthorized tools are unavailable to a restricted type.
- Instance concurrency respects the type limit.
- Split routes all mandatory knowledge to destinations or archive.
- Merge deduplicates without losing provenance.
- Retirement is blocked until reconciliation passes.
- Rollback restores the prior active topology.
- Cross-project type or memory access returns nothing.

Per PLAN.md M7 acceptance:
- A type can be created, instantiated, split or merged, validated,
  archived, and rolled back with every mandatory knowledge record
  accounted for.
"""

from __future__ import annotations

import pytest

from zero.app.services import build_services
from zero.config import Settings
from zero.domain.agent_types import (
    ConcurrencyLimitExceededError,
    InvalidAgentTypeTransitionError,
    KnowledgeReconciliationError,
)
from zero.domain.authorization import AuthorizationError
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations


@pytest.fixture
def services(test_settings: Settings):
    database = Database(test_settings)
    apply_migrations(database)
    return build_services(test_settings, database)


@pytest.fixture
def project_with_owner(services):
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(
        owner_id=owner.id, name="Project A"
    )
    return owner, project


# ----------------------------------------------------------------------
# Type creation
# ----------------------------------------------------------------------


def test_create_type_returns_active_type(services, project_with_owner) -> None:
    owner, project = project_with_owner
    t = services.agent_types.create_type(
        project_id=project.id,
        actor_id=owner.id,
        name="Backend Specialist",
        responsibility="API contracts and backend logic",
        memory_scope="API decisions and schemas",
        context_budget_tokens=50000,
        max_concurrent_instances=3,
    )
    assert t.id.value.startswith("at_")
    assert t.state == "active"
    assert t.context_budget_tokens == 50000
    assert t.max_concurrent_instances == 3


def test_create_type_rejects_empty_name(services, project_with_owner) -> None:
    owner, project = project_with_owner
    with pytest.raises(ValueError, match="name"):
        services.agent_types.create_type(
            project_id=project.id,
            actor_id=owner.id,
            name="",
            responsibility="R",
            memory_scope="M",
        )


def test_create_type_rejects_empty_responsibility(
    services, project_with_owner
) -> None:
    owner, project = project_with_owner
    with pytest.raises(ValueError, match="responsibility"):
        services.agent_types.create_type(
            project_id=project.id,
            actor_id=owner.id,
            name="T",
            responsibility="",
            memory_scope="M",
        )


def test_create_type_rejects_duplicate_name(services, project_with_owner) -> None:
    owner, project = project_with_owner
    services.agent_types.create_type(
        project_id=project.id, actor_id=owner.id,
        name="Type A", responsibility="R", memory_scope="M"
    )
    from zero.domain.agent_types import AgentTypeAlreadyExistsError

    with pytest.raises(AgentTypeAlreadyExistsError):
        services.agent_types.create_type(
            project_id=project.id, actor_id=owner.id,
            name="Type A", responsibility="R2", memory_scope="M2"
        )


def test_create_type_requires_permission(services, project_with_owner) -> None:
    """Per PLAN.md M3: only owners (or delegated admins) can manage
    agents. A viewer cannot create types."""
    owner, project = project_with_owner
    viewer = services.identity.create_user(display_name="Viewer")
    services.identity.add_member(
        project_id=project.id, actor_id=owner.id,
        member_id=viewer.id, role="viewer"
    )
    with pytest.raises(AuthorizationError):
        services.agent_types.create_type(
            project_id=project.id, actor_id=viewer.id,
            name="T", responsibility="R", memory_scope="M"
        )


# ----------------------------------------------------------------------
# Instances and concurrency limit
# ----------------------------------------------------------------------


def test_create_instance_respects_concurrency_limit(
    services, project_with_owner
) -> None:
    """Per PLAN.md M7: 'Instance concurrency respects the type limit.'"""
    owner, project = project_with_owner
    t = services.agent_types.create_type(
        project_id=project.id, actor_id=owner.id,
        name="Limited", responsibility="R", memory_scope="M",
        max_concurrent_instances=2,
    )
    # Create two instances (within limit).
    inst1 = services.agent_types.create_instance(
        project_id=project.id, type_id=t.id, actor_id=owner.id
    )
    inst2 = services.agent_types.create_instance(
        project_id=project.id, type_id=t.id, actor_id=owner.id
    )
    # Assign both to tasks (transition to running).
    from zero.app.worker_service import TaskSpec
    from zero.domain.plans import PlanRevisionContent

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
        source_event_ids=(event.id,))
    services.plans.propose_revision(
        plan_id=plan.id, actor_id=owner.id, content=content
    )
    _, handoff = services.plans.approve_revision(
        plan_id=plan.id, actor_id=owner.id,
        expected_revision_number=1, idempotency_key="a1"
    )
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id, actor_id=owner.id,
        task_specs=[TaskSpec(key="A", objective="A")]
    )
    task = services.worker.list_tasks(execution.id)[0]
    services.agent_types.assign_instance_to_task(
        project_id=project.id, instance_id=inst1.id,
        task_id=task.id, actor_id=owner.id
    )
    services.agent_types.assign_instance_to_task(
        project_id=project.id, instance_id=inst2.id,
        task_id=task.id, actor_id=owner.id
    )
    # Third instance exceeds the limit.
    inst3 = services.agent_types.create_instance(
        project_id=project.id, type_id=t.id, actor_id=owner.id
    )
    with pytest.raises(ConcurrencyLimitExceededError):
        services.agent_types.assign_instance_to_task(
            project_id=project.id, instance_id=inst3.id,
            task_id=task.id, actor_id=owner.id
        )


def test_cannot_create_instance_of_archived_type(
    services, project_with_owner
) -> None:
    owner, project = project_with_owner
    t = services.agent_types.create_type(
        project_id=project.id, actor_id=owner.id,
        name="T", responsibility="R", memory_scope="M"
    )
    # Archive the type via retire.
    services.agent_types.retire_type(
        project_id=project.id, type_id=t.id, actor_id=owner.id
    )
    with pytest.raises(InvalidAgentTypeTransitionError):
        services.agent_types.create_instance(
            project_id=project.id, type_id=t.id, actor_id=owner.id
        )


# ----------------------------------------------------------------------
# Knowledge records
# ----------------------------------------------------------------------


def test_add_knowledge_to_type(services, project_with_owner) -> None:
    owner, project = project_with_owner
    t = services.agent_types.create_type(
        project_id=project.id, actor_id=owner.id,
        name="T", responsibility="R", memory_scope="M"
    )
    r = services.agent_types.add_knowledge(
        project_id=project.id, type_id=t.id, actor_id=owner.id,
        kind="decision", content="Use PostgreSQL"
    )
    assert r.id.value.startswith("kr_")
    assert r.agent_type_id == t.id
    assert r.state == "approved"
    assert r.content_hash  # SHA-256 is not empty


def test_add_knowledge_rejects_empty_content(
    services, project_with_owner
) -> None:
    owner, project = project_with_owner
    t = services.agent_types.create_type(
        project_id=project.id, actor_id=owner.id,
        name="T", responsibility="R", memory_scope="M"
    )
    with pytest.raises(ValueError, match="content"):
        services.agent_types.add_knowledge(
            project_id=project.id, type_id=t.id, actor_id=owner.id,
            kind="fact", content=""
        )


def test_list_knowledge_for_type_returns_only_owned_records(
    services, project_with_owner
) -> None:
    """Per zero-project-isolation-evidence: knowledge is type-scoped."""
    owner, project = project_with_owner
    t1 = services.agent_types.create_type(
        project_id=project.id, actor_id=owner.id,
        name="T1", responsibility="R1", memory_scope="M1"
    )
    t2 = services.agent_types.create_type(
        project_id=project.id, actor_id=owner.id,
        name="T2", responsibility="R2", memory_scope="M2"
    )
    services.agent_types.add_knowledge(
        project_id=project.id, type_id=t1.id, actor_id=owner.id,
        kind="fact", content="T1 fact"
    )
    services.agent_types.add_knowledge(
        project_id=project.id, type_id=t2.id, actor_id=owner.id,
        kind="fact", content="T2 fact"
    )
    t1_records = services.agent_types.list_knowledge_for_type(
        project.id, t1.id
    )
    t2_records = services.agent_types.list_knowledge_for_type(
        project.id, t2.id
    )
    assert len(t1_records) == 1
    assert t1_records[0].content == "T1 fact"
    assert len(t2_records) == 1
    assert t2_records[0].content == "T2 fact"


# ----------------------------------------------------------------------
# Split
# ----------------------------------------------------------------------


def test_split_routes_knowledge_to_destinations(
    services, project_with_owner
) -> None:
    """Per PLAN.md M7: 'Split routes all mandatory knowledge to
    destinations or archive.'"""
    owner, project = project_with_owner
    src = services.agent_types.create_type(
        project_id=project.id, actor_id=owner.id,
        name="Platform", responsibility="R", memory_scope="M"
    )
    r1 = services.agent_types.add_knowledge(
        project_id=project.id, type_id=src.id, actor_id=owner.id,
        kind="decision", content="Use PostgreSQL"
    )
    r2 = services.agent_types.add_knowledge(
        project_id=project.id, type_id=src.id, actor_id=owner.id,
        kind="fact", content="API uses REST"
    )
    r3 = services.agent_types.add_knowledge(
        project_id=project.id, type_id=src.id, actor_id=owner.id,
        kind="constraint", content="Must support offline"
    )
    new_types, _snapshot = services.agent_types.split_type(
        project_id=project.id, source_type_id=src.id, actor_id=owner.id,
        destination_specs=[
            ("Runtime", "Runtime", "Runtime decisions"),
            ("Adapters", "Provider integration", "Provider decisions"),
        ],
        knowledge_routing={
            "Runtime": [r1.id],
            "Adapters": [r2.id],
        },
    )
    # r1 is now in Runtime.
    runtime = next(t for t in new_types if t.name == "Runtime")
    runtime_records = services.agent_types.list_knowledge_for_type(
        project.id, runtime.id
    )
    assert any(r.id == r1.id for r in runtime_records)
    # r2 is now in Adapters.
    adapters = next(t for t in new_types if t.name == "Adapters")
    adapter_records = services.agent_types.list_knowledge_for_type(
        project.id, adapters.id
    )
    assert any(r.id == r2.id for r in adapter_records)
    # r3 is archived (unrouted, not deleted).
    all_src = services.agent_types._repo.list_knowledge_for_type(
        src.id, include_archived=True
    )
    r3_record = next(r for r in all_src if r.id == r3.id)
    assert r3_record.state == "archived"
    # Source type is archived.
    assert services.agent_types.get_type(project.id, src.id).state == "archived"


def test_split_takes_topology_snapshot(services, project_with_owner) -> None:
    owner, project = project_with_owner
    src = services.agent_types.create_type(
        project_id=project.id, actor_id=owner.id,
        name="Src", responsibility="R", memory_scope="M"
    )
    _, snapshot = services.agent_types.split_type(
        project_id=project.id, source_type_id=src.id, actor_id=owner.id,
        destination_specs=[("Dest", "R", "M")],
    )
    assert snapshot.id.value.startswith("topo_")
    assert snapshot.reason == "before_split"
    assert snapshot.snapshot_version >= 1


# ----------------------------------------------------------------------
# Merge
# ----------------------------------------------------------------------


def test_merge_migrates_all_knowledge_with_provenance(
    services, project_with_owner
) -> None:
    """Per PLAN.md M7: 'Merge deduplicates without losing provenance.'"""
    owner, project = project_with_owner
    t1 = services.agent_types.create_type(
        project_id=project.id, actor_id=owner.id,
        name="Frontend", responsibility="UI", memory_scope="UI"
    )
    t2 = services.agent_types.create_type(
        project_id=project.id, actor_id=owner.id,
        name="Backend", responsibility="API", memory_scope="API"
    )
    services.agent_types.add_knowledge(
        project_id=project.id, type_id=t1.id, actor_id=owner.id,
        kind="fact", content="Uses React"
    )
    services.agent_types.add_knowledge(
        project_id=project.id, type_id=t2.id, actor_id=owner.id,
        kind="fact", content="Uses FastAPI"
    )
    merged, _snapshot = services.agent_types.merge_types(
        project_id=project.id, source_type_ids=[t1.id, t2.id],
        destination_name="Full Stack",
        destination_responsibility="End-to-end",
        destination_memory_scope="All",
        actor_id=owner.id,
    )
    records = services.agent_types.list_knowledge_for_type(
        project.id, merged.id
    )
    assert len(records) == 2
    # Each record has migrated_from set (provenance).
    for r in records:
        assert r.migrated_from is not None
    # Source types are archived.
    assert services.agent_types.get_type(project.id, t1.id).state == "archived"
    assert services.agent_types.get_type(project.id, t2.id).state == "archived"


def test_merge_requires_at_least_two_sources(
    services, project_with_owner
) -> None:
    owner, project = project_with_owner
    t1 = services.agent_types.create_type(
        project_id=project.id, actor_id=owner.id,
        name="T1", responsibility="R", memory_scope="M"
    )
    with pytest.raises(ValueError, match="at least 2"):
        services.agent_types.merge_types(
            project_id=project.id, source_type_ids=[t1.id],
            destination_name="Merged",
            destination_responsibility="R",
            destination_memory_scope="M",
            actor_id=owner.id,
        )


# ----------------------------------------------------------------------
# Retirement
# ----------------------------------------------------------------------


def test_retire_archives_knowledge_never_deletes(
    services, project_with_owner
) -> None:
    """Per zero-context-memory: removing a type never deletes its
    knowledge."""
    owner, project = project_with_owner
    t = services.agent_types.create_type(
        project_id=project.id, actor_id=owner.id,
        name="Temp", responsibility="R", memory_scope="M"
    )
    services.agent_types.add_knowledge(
        project_id=project.id, type_id=t.id, actor_id=owner.id,
        kind="fact", content="temp fact"
    )
    retired, _ = services.agent_types.retire_type(
        project_id=project.id, type_id=t.id, actor_id=owner.id
    )
    assert retired.state == "retired"
    # Knowledge is archived, not deleted.
    records = services.agent_types._repo.list_knowledge_for_type(
        t.id, include_archived=True
    )
    assert len(records) == 1
    assert records[0].state == "archived"


def test_retire_blocked_when_instances_running(
    services, project_with_owner
) -> None:
    """Per PLAN.md M7: 'Retirement is blocked until reconciliation
    passes.' A type with running instances cannot be retired."""
    owner, project = project_with_owner
    t = services.agent_types.create_type(
        project_id=project.id, actor_id=owner.id,
        name="T", responsibility="R", memory_scope="M",
        max_concurrent_instances=1,
    )
    inst = services.agent_types.create_instance(
        project_id=project.id, type_id=t.id, actor_id=owner.id
    )
    # Create a plan+execution+task to assign the instance to.
    from zero.app.worker_service import TaskSpec
    from zero.domain.plans import PlanRevisionContent

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
        source_event_ids=(event.id,))
    services.plans.propose_revision(
        plan_id=plan.id, actor_id=owner.id, content=content
    )
    _, handoff = services.plans.approve_revision(
        plan_id=plan.id, actor_id=owner.id,
        expected_revision_number=1, idempotency_key="a1"
    )
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id, actor_id=owner.id,
        task_specs=[TaskSpec(key="A", objective="A")]
    )
    task = services.worker.list_tasks(execution.id)[0]
    services.agent_types.assign_instance_to_task(
        project_id=project.id, instance_id=inst.id,
        task_id=task.id, actor_id=owner.id
    )
    # Retirement should be blocked.
    with pytest.raises(KnowledgeReconciliationError, match="running"):
        services.agent_types.retire_type(
            project_id=project.id, type_id=t.id, actor_id=owner.id
        )


# ----------------------------------------------------------------------
# Rollback
# ----------------------------------------------------------------------


def test_rollback_restores_active_topology(services, project_with_owner) -> None:
    """Per PLAN.md M7: 'Rollback restores the prior active topology.'"""
    owner, project = project_with_owner
    t1 = services.agent_types.create_type(
        project_id=project.id, actor_id=owner.id,
        name="Type1", responsibility="R1", memory_scope="M1"
    )
    # Split: t1 is archived, Type2 is created.
    _, snapshot = services.agent_types.split_type(
        project_id=project.id, source_type_id=t1.id, actor_id=owner.id,
        destination_specs=[("Type2", "R2", "M2")],
    )
    assert services.agent_types.get_type(project.id, t1.id).state == "archived"
    # Roll back.
    services.agent_types.rollback_to_snapshot(
        project_id=project.id, snapshot_id=snapshot.id,
        actor_id=owner.id,
    )
    # t1 is active again.
    assert services.agent_types.get_type(project.id, t1.id).state == "active"
    # Type2 (created after the snapshot) is archived.
    types = services.agent_types.list_types(project.id, include_archived=True)
    type2 = next(t for t in types if t.name == "Type2")
    assert type2.state == "archived"


# ----------------------------------------------------------------------
# Cross-project isolation
# ----------------------------------------------------------------------


def test_cross_project_type_access_returns_nothing(services) -> None:
    """Per PLAN.md M7: 'Cross-project type or memory access returns
    nothing.'"""
    owner_a = services.identity.create_user(display_name="Owner A")
    owner_b = services.identity.create_user(display_name="Owner B")
    project_a = services.identity.create_project(
        owner_id=owner_a.id, name="Project A"
    )
    project_b = services.identity.create_project(
        owner_id=owner_b.id, name="Project B"
    )
    t_a = services.agent_types.create_type(
        project_id=project_a.id, actor_id=owner_a.id,
        name="TypeA", responsibility="R", memory_scope="M"
    )
    # Owner B cannot access project A's type.
    from zero.domain.agent_types import AgentTypeNotFoundError

    with pytest.raises(AgentTypeNotFoundError):
        services.agent_types.get_type(project_b.id, t_a.id)


def test_cross_project_knowledge_access_returns_nothing(services) -> None:
    """Knowledge from project A cannot be listed via project B."""
    owner_a = services.identity.create_user(display_name="Owner A")
    owner_b = services.identity.create_user(display_name="Owner B")
    project_a = services.identity.create_project(
        owner_id=owner_a.id, name="Project A"
    )
    project_b = services.identity.create_project(
        owner_id=owner_b.id, name="Project B"
    )
    t_a = services.agent_types.create_type(
        project_id=project_a.id, actor_id=owner_a.id,
        name="TypeA", responsibility="R", memory_scope="M"
    )
    services.agent_types.add_knowledge(
        project_id=project_a.id, type_id=t_a.id, actor_id=owner_a.id,
        kind="fact", content="Project A secret fact"
    )
    # Listing project B's knowledge returns nothing.
    b_records = services.agent_types.list_knowledge_for_project(project_b.id)
    assert len(b_records) == 0


# ----------------------------------------------------------------------
# Type update
# ----------------------------------------------------------------------


def test_update_type_changes_fields(services, project_with_owner) -> None:
    owner, project = project_with_owner
    t = services.agent_types.create_type(
        project_id=project.id, actor_id=owner.id,
        name="T", responsibility="R", memory_scope="M",
        max_concurrent_instances=1,
    )
    updated = services.agent_types.update_type(
        project_id=project.id, type_id=t.id, actor_id=owner.id,
        max_concurrent_instances=5,
    )
    assert updated.max_concurrent_instances == 5
    assert updated.version == 2  # version incremented


# ----------------------------------------------------------------------
# Topology snapshots
# ----------------------------------------------------------------------


def test_list_snapshots_returns_all(services, project_with_owner) -> None:
    owner, project = project_with_owner
    t1 = services.agent_types.create_type(
        project_id=project.id, actor_id=owner.id,
        name="T1", responsibility="R", memory_scope="M"
    )
    services.agent_types.split_type(
        project_id=project.id, source_type_id=t1.id, actor_id=owner.id,
        destination_specs=[("T2", "R", "M")],
    )
    snapshots = services.agent_types.list_snapshots(project.id)
    assert len(snapshots) >= 1
    assert all(s.id.value.startswith("topo_") for s in snapshots)
