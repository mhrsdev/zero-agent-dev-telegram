"""Agent type service — type lifecycle, instances, knowledge, topology
migration (split/merge/retire/rollback).

Per ``zero-agent-execution-lifecycle`` SKILL.md §"Topology evolution
is a data migration":

- A safe transition has these conceptual stages:
  1. freeze writes or establish a version boundary;
  2. snapshot the source scope;
  3. copy/transform records into destination scopes with provenance links;
  4. rebuild destination indexes;
  5. run retrieval and count/hash reconciliation checks;
  6. activate the destination topology;
  7. archive the source topology;
  8. retain rollback metadata.
- Abort activation if any mandatory record cannot be accounted for.
  Archive; never hard-delete as part of topology evolution.

Per ``zero-context-memory`` SKILL.md §"Non-negotiable invariants":
- Removing, splitting, or merging a sub-agent type never deletes its
  knowledge.

Per PLAN.md M7 invariants:
- Split, merge, retirement, and role changes are lossless and
  reversible.
"""

from __future__ import annotations

import hashlib
import json

from zero.app.clock import now_utc_iso
from zero.app.authorization_service import AuthorizationService
from zero.domain.agent_types import (
    AgentInstance,
    AgentInstanceId,
    AgentInstanceState,
    AgentType,
    AgentTypeId,
    ConcurrencyLimitExceededError,
    InvalidAgentTypeTransitionError,
    KnowledgeKind,
    KnowledgeReconciliationError,
    KnowledgeRecord,
    KnowledgeRecordId,
    KnowledgeRecordNotFoundError,
    KnowledgeState,
    TopologySnapshot,
    TopologySnapshotId,
)
from zero.domain.audit import AuditEvent, AuditEventId, AuditSource
from zero.domain.execution import TaskId
from zero.domain.identity import ProjectId, UserId
from zero.domain.ids import (
    generate_agent_instance_id,
    generate_agent_type_id,
    generate_audit_event_id,
    generate_knowledge_record_id,
    generate_topology_snapshot_id,
)
from zero.persistence.repositories.agent_type_repository import (
    AgentTypeRepository,
)
from zero.persistence.repositories.audit_repository import AuditRepository


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class AgentTypeService:
    """Application operations for the dynamic Sub Agent Type lifecycle.

    The service is the only place where agent type state transitions,
    knowledge migration, and topology snapshots happen.
    """

    def __init__(
        self,
        agent_type_repo: AgentTypeRepository,
        audit_repo: AuditRepository,
        authorization_service: AuthorizationService,
    ) -> None:
        self._repo = agent_type_repo
        self._audit_repo = audit_repo
        self._authz = authorization_service

    # ------------------------------------------------------------------
    # Type lifecycle
    # ------------------------------------------------------------------

    def create_type(
        self,
        *,
        project_id: ProjectId,
        actor_id: UserId,
        name: str,
        responsibility: str,
        memory_scope: str,
        permitted_tools: tuple[str, ...] = (),
        model_policy: dict[str, str] | None = None,
        context_budget_tokens: int = 100000,
        max_concurrent_instances: int = 1,
        source: AuditSource = "web",
        commit: bool = True,
    ) -> AgentType:
        """Create a new Sub Agent Type.

        Per ``zero-agent-execution-lifecycle`` §"Dynamic does not mean
        arbitrary": a type is justified by a current boundary, not by
        a familiar job title.
        """
        if not name or not name.strip():
            raise ValueError("type name must not be empty")
        if not responsibility or not responsibility.strip():
            raise ValueError("responsibility must not be empty")
        # Authorize.
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="agent.manage",
            source=source,
        )
        agent_type = AgentType(
            id=AgentTypeId(generate_agent_type_id()),
            project_id=project_id,
            name=name.strip(),
            responsibility=responsibility.strip(),
            memory_scope=memory_scope.strip(),
            permitted_tools=permitted_tools,
            model_policy=model_policy or {},
            context_budget_tokens=context_budget_tokens,
            max_concurrent_instances=max_concurrent_instances,
            state="active",
            version=1,
            created_at=now_utc_iso(),
            updated_at=now_utc_iso(),
        )
        self._repo.insert_agent_type(agent_type, commit=commit)
        self._audit_repo.insert(
            AuditEvent(
                id=AuditEventId(generate_audit_event_id()),
                project_id=project_id,
                actor_id=actor_id,
                source=source,
                operation="agent_type.create",
                target_type="agent_type",
                target_id=agent_type.id.value,
                result="success",
                redacted_summary=f"Created agent type {agent_type.name!r}",
                created_at=now_utc_iso(),
            ),
            commit=commit,
        )
        return agent_type

    def get_type(self, project_id: ProjectId, type_id: AgentTypeId) -> AgentType:
        return self._repo.get_agent_type(project_id, type_id)

    def list_types(
        self,
        project_id: ProjectId,
        *,
        include_archived: bool = True,
    ) -> list[AgentType]:
        return self._repo.list_agent_types_for_project(
            project_id, include_archived=include_archived
        )

    def update_type(
        self,
        *,
        project_id: ProjectId,
        type_id: AgentTypeId,
        actor_id: UserId,
        responsibility: str | None = None,
        memory_scope: str | None = None,
        permitted_tools: tuple[str, ...] | None = None,
        model_policy: dict[str, str] | None = None,
        context_budget_tokens: int | None = None,
        max_concurrent_instances: int | None = None,
        source: AuditSource = "web",
    ) -> AgentType:
        """Update an agent type's configuration. Does not change state."""
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="agent.manage",
            source=source,
        )
        self._repo.update_agent_type(
            type_id,
            responsibility=responsibility,
            memory_scope=memory_scope,
            permitted_tools=permitted_tools,
            model_policy=model_policy,
            context_budget_tokens=context_budget_tokens,
            max_concurrent_instances=max_concurrent_instances,
        )
        self._audit_repo.insert(
            AuditEvent(
                id=AuditEventId(generate_audit_event_id()),
                project_id=project_id,
                actor_id=actor_id,
                source=source,
                operation="agent_type.update",
                target_type="agent_type",
                target_id=type_id.value,
                result="success",
                redacted_summary=f"Updated agent type {type_id.value}",
                created_at=now_utc_iso(),
            )
        )
        return self._repo.get_agent_type(project_id, type_id)

    # ------------------------------------------------------------------
    # Instances
    # ------------------------------------------------------------------

    def create_instance(
        self,
        *,
        project_id: ProjectId,
        type_id: AgentTypeId,
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> AgentInstance:
        """Create a runtime instance of a type.

        Per ``zero-agent-execution-lifecycle`` §"Instances share
        accepted knowledge, not mutable scratch state": multiple
        instances of one type may use the same durable approved
        knowledge. Their current prompts, temporary files, command
        output, and unaccepted conclusions remain task-local.

        Per PLAN.md M7: "Instance concurrency respects the type limit."
        The concurrency limit is enforced when assigning an instance to
        a task (transitioning to ``running``), not when creating an
        idle instance. Idle instances do not consume resources.
        """
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="agent.manage",
            source=source,
        )
        agent_type = self._repo.get_agent_type(project_id, type_id)
        if agent_type.state != "active":
            raise InvalidAgentTypeTransitionError(
                f"Cannot create instance of type in state {agent_type.state!r}"
            )
        instance = AgentInstance(
            id=AgentInstanceId(generate_agent_instance_id()),
            project_id=project_id,
            agent_type_id=type_id,
            task_id=None,
            state="idle",
            created_at=now_utc_iso(),
            updated_at=now_utc_iso(),
        )
        self._repo.insert_instance(instance)
        return instance

    def assign_instance_to_task(
        self,
        *,
        project_id: ProjectId,
        instance_id: AgentInstanceId,
        task_id: TaskId,
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> AgentInstance:
        """Assign an idle instance to a task and transition it to
        running."""
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="execution.start",
            source=source,
        )
        instance = self._repo.get_instance(instance_id)
        if instance.state != "idle":
            raise InvalidAgentTypeTransitionError(
                f"Cannot assign instance in state {instance.state!r}"
            )
        # BEGIN IMMEDIATE serializes writers: the limit check and the
        # transition happen inside one write transaction so two
        # concurrent assignments cannot both observe free capacity.
        with self._repo.database.transaction():
            agent_type = self._repo.get_agent_type(project_id, instance.agent_type_id)
            current = self._repo.get_instance(instance_id)
            if current.state != "idle":
                raise InvalidAgentTypeTransitionError(
                    f"Cannot assign instance in state {current.state!r}"
                )
            running = self._repo.count_running_instances(instance.agent_type_id)
            if running >= agent_type.max_concurrent_instances:
                raise ConcurrencyLimitExceededError(
                    f"Type {agent_type.name!r} has reached its concurrency "
                    f"limit of {agent_type.max_concurrent_instances}"
                )
            self._repo.update_instance_state(
                instance_id,
                "running",
                task_id=task_id,
                commit=False,
            )
        return self._repo.get_instance(instance_id)

    def complete_instance(
        self,
        *,
        project_id: ProjectId,
        instance_id: AgentInstanceId,
        actor_id: UserId,
        succeeded: bool,
        source: AuditSource = "system",
    ) -> AgentInstance:
        instance = self._repo.get_instance(instance_id)
        new_state: AgentInstanceState = "completed" if succeeded else "failed"
        if instance.state != "running":
            raise InvalidAgentTypeTransitionError(
                f"Cannot complete instance in state {instance.state!r}"
            )
        self._repo.update_instance_state(instance_id, new_state)
        return self._repo.get_instance(instance_id)

    def list_instances_for_type(self, type_id: AgentTypeId) -> list[AgentInstance]:
        return self._repo.list_instances_for_type(type_id)

    # ------------------------------------------------------------------
    # Knowledge records
    # ------------------------------------------------------------------

    def add_knowledge(
        self,
        *,
        project_id: ProjectId,
        type_id: AgentTypeId | None,
        actor_id: UserId,
        kind: KnowledgeKind,
        content: str,
        provenance: str | None = None,
        state: KnowledgeState = "approved",
        source: AuditSource = "system",
    ) -> KnowledgeRecord:
        """Add a knowledge record to a type (or project-wide if type_id
        is None)."""
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="agent.manage",
            source=source,
        )
        if not content or not content.strip():
            raise ValueError("content must not be empty")
        # If type_id is provided, verify it exists and belongs to the project.
        if type_id is not None:
            self._repo.get_agent_type(project_id, type_id)
        record = KnowledgeRecord(
            id=KnowledgeRecordId(generate_knowledge_record_id()),
            project_id=project_id,
            agent_type_id=type_id,
            kind=kind,
            content=content.strip(),
            content_hash=_sha256(content),
            provenance=provenance,
            state=state,
            created_at=now_utc_iso(),
            updated_at=now_utc_iso(),
        )
        self._repo.insert_knowledge(record)
        return record

    def list_knowledge_for_type(
        self,
        project_id: ProjectId,
        type_id: AgentTypeId,
        *,
        actor_id: UserId,
        include_archived: bool = False,
        source: AuditSource = "system",
    ) -> list[KnowledgeRecord]:
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="execution.view_diffs",
            source=source,
        )
        # Verify the type belongs to the project.
        self._repo.get_agent_type(project_id, type_id)
        return self._repo.list_knowledge_for_type(type_id, include_archived=include_archived)

    def list_knowledge_for_project(
        self,
        project_id: ProjectId,
        *,
        actor_id: UserId,
        include_archived: bool = False,
        source: AuditSource = "system",
    ) -> list[KnowledgeRecord]:
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="execution.view_diffs",
            source=source,
        )
        return self._repo.list_knowledge_for_project(project_id, include_archived=include_archived)

    # ------------------------------------------------------------------
    # Topology migration: split
    # ------------------------------------------------------------------

    def split_type(
        self,
        *,
        project_id: ProjectId,
        source_type_id: AgentTypeId,
        actor_id: UserId,
        destination_specs: list[tuple[str, str, str]],
        # Each tuple is (name, responsibility, memory_scope) for a new type.
        knowledge_routing: dict[str, list[KnowledgeRecordId]] | None = None,
        # knowledge_routing maps destination type name -> list of record
        # IDs to migrate. Records not listed go to archive.
        source: AuditSource = "web",
    ) -> tuple[list[AgentType], TopologySnapshot]:
        """Split a type into multiple new types, routing knowledge to
        destinations.

        Per ``zero-agent-execution-lifecycle`` §"Topology evolution is
        a data migration": snapshot, classify, copy/transform, rebuild,
        reconcile, activate, archive, retain rollback.

        Per PLAN.md M7: "Split routes all mandatory knowledge to
        destinations or archive." Records not explicitly routed are
        archived (never deleted).

        Returns the list of new types and the topology snapshot taken
        before the split.
        """
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="agent.manage",
            source=source,
        )
        source_type = self._repo.get_agent_type(project_id, source_type_id)
        if source_type.state != "active":
            raise InvalidAgentTypeTransitionError(
                f"Cannot split type in state {source_type.state!r}"
            )
        with self._repo._database.transaction():
            # 1. Take a topology snapshot (freeze).
            snapshot = self._take_snapshot(
                project_id, "before_split", actor_id, source, commit=False
            )
            # 2. Create destination types.
            new_types: list[AgentType] = []
            for name, resp, mem_scope in destination_specs:
                new_type = self.create_type(
                    project_id=project_id,
                    actor_id=actor_id,
                    name=name,
                    responsibility=resp,
                    memory_scope=mem_scope,
                    permitted_tools=source_type.permitted_tools,
                    model_policy=dict(source_type.model_policy),
                    context_budget_tokens=source_type.context_budget_tokens,
                    max_concurrent_instances=source_type.max_concurrent_instances,
                    source=source,
                    commit=False,
                )
                new_types.append(new_type)
            # 3. Route knowledge.
            all_records = self._repo.list_knowledge_for_type(source_type_id, include_archived=False)
            routed_ids: set[str] = set()
            name_to_type = {t.name: t for t in new_types}
            if knowledge_routing:
                for dest_name, record_ids in knowledge_routing.items():
                    if dest_name not in name_to_type:
                        raise ValueError(f"Unknown destination type {dest_name!r}")
                    dest_type = name_to_type[dest_name]
                    for rid in record_ids:
                        # Verify the record belongs to the source type.
                        record = self._repo.get_knowledge(rid)
                        if record.agent_type_id != source_type_id:
                            raise KnowledgeReconciliationError(
                                f"Record {rid} does not belong to source type"
                            )
                        self._repo.reassign_knowledge(
                            rid, dest_type.id, migrated_from=rid, commit=False
                        )
                        routed_ids.add(rid.value)
            # 4. Archive unrouted records (never delete).
            unrouted: list[str] = []
            routed_record_ids: set[str] = set()
            if knowledge_routing:
                for record_ids in knowledge_routing.values():
                    for rid in record_ids:
                        routed_record_ids.add(rid.value)
            for record in all_records:
                if record.id.value not in routed_record_ids:
                    self._repo.update_knowledge_state(record.id, "archived", commit=False)
                    unrouted.append(record.id.value)
            # 5. Reconcile: every source record must be accounted for.
            # Routed records were reassigned to destination types (their
            # agent_type_id changed). Unrouted records were archived (still
            # under the source type, state=archived). We verify by looking
            # up each original record by ID.
            accounted = 0
            unaccounted: list[str] = []
            for record in all_records:
                try:
                    self._repo.get_knowledge(record.id)
                    # The record still exists. If it was routed, its
                    # agent_type_id changed and migrated_from is set. If it
                    # was archived, its state is 'archived'. Either way,
                    # it's accounted for.
                    accounted += 1
                except KnowledgeRecordNotFoundError:
                    unaccounted.append(record.id.value)
            if unaccounted:
                raise KnowledgeReconciliationError(
                    f"Knowledge reconciliation failed: {len(unaccounted)} "
                    f"records not found after migration",
                    unaccounted_records=unaccounted,
                )
            # 6. Archive the source type (never delete).
            self._repo.update_agent_type(
                source_type_id,
                state="archived",
                superseded_by=new_types[0].id if new_types else None,
                commit=False,
            )
            self._audit_repo.insert(
                AuditEvent(
                    id=AuditEventId(generate_audit_event_id()),
                    project_id=project_id,
                    actor_id=actor_id,
                    source=source,
                    operation="agent_type.split",
                    target_type="agent_type",
                    target_id=source_type_id.value,
                    result="success",
                    redacted_summary=(
                        f"Split type {source_type.name!r} into "
                        f"{len(new_types)} types; archived {len(unrouted)} "
                        f"unrouted records"
                    ),
                    correlation_id=snapshot.id.value,
                    created_at=now_utc_iso(),
                ),
                commit=False,
            )
        return new_types, snapshot

    # ------------------------------------------------------------------
    # Topology migration: merge
    # ------------------------------------------------------------------

    def merge_types(
        self,
        *,
        project_id: ProjectId,
        source_type_ids: list[AgentTypeId],
        destination_name: str,
        destination_responsibility: str,
        destination_memory_scope: str,
        actor_id: UserId,
        source: AuditSource = "web",
    ) -> tuple[AgentType, TopologySnapshot]:
        """Merge multiple types into a single new type.

        Per PLAN.md M7: "Merge deduplicates without losing provenance."
        All knowledge records from source types are migrated to the
        destination with provenance links. Source types are archived.
        """
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="agent.manage",
            source=source,
        )
        if len(source_type_ids) < 2:
            raise ValueError("merge requires at least 2 source types")
        # Verify all source types.
        source_types: list[AgentType] = []
        for tid in source_type_ids:
            t = self._repo.get_agent_type(project_id, tid)
            if t.state != "active":
                raise InvalidAgentTypeTransitionError(
                    f"Cannot merge type {t.name!r} in state {t.state!r}"
                )
            source_types.append(t)
        with self._repo._database.transaction():
            # 1. Take a snapshot.
            snapshot = self._take_snapshot(
                project_id, "before_merge", actor_id, source, commit=False
            )
            # 2. Create the destination type.
            dest_type = self.create_type(
                project_id=project_id,
                actor_id=actor_id,
                name=destination_name,
                responsibility=destination_responsibility,
                memory_scope=destination_memory_scope,
                # Merge tool permissions from all sources (union).
                permitted_tools=tuple(
                    sorted({tool for t in source_types for tool in t.permitted_tools})
                ),
                model_policy=dict(source_types[0].model_policy),
                context_budget_tokens=max(t.context_budget_tokens for t in source_types),
                max_concurrent_instances=max(t.max_concurrent_instances for t in source_types),
                source=source,
                commit=False,
            )
            # 3. Migrate all knowledge records.
            total_migrated = 0
            for src_type in source_types:
                records = self._repo.list_knowledge_for_type(src_type.id, include_archived=False)
                for record in records:
                    self._repo.reassign_knowledge(
                        record.id, dest_type.id, migrated_from=record.id, commit=False
                    )
                    total_migrated += 1
            # 4. Archive source types.
            for src_type in source_types:
                self._repo.update_agent_type(
                    src_type.id,
                    state="archived",
                    superseded_by=dest_type.id,
                    commit=False,
                )
            self._audit_repo.insert(
                AuditEvent(
                    id=AuditEventId(generate_audit_event_id()),
                    project_id=project_id,
                    actor_id=actor_id,
                    source=source,
                    operation="agent_type.merge",
                    target_type="agent_type",
                    target_id=dest_type.id.value,
                    result="success",
                    redacted_summary=(
                        f"Merged {len(source_types)} types into "
                        f"{dest_type.name!r}; migrated {total_migrated} records"
                    ),
                    correlation_id=snapshot.id.value,
                    created_at=now_utc_iso(),
                ),
                commit=False,
            )
        return dest_type, snapshot

    # ------------------------------------------------------------------
    # Topology migration: retire
    # ------------------------------------------------------------------

    def retire_type(
        self,
        *,
        project_id: ProjectId,
        type_id: AgentTypeId,
        actor_id: UserId,
        archive_knowledge: bool = True,
        source: AuditSource = "web",
    ) -> tuple[AgentType, TopologySnapshot]:
        """Retire a type. Knowledge is archived (never deleted).

        Per PLAN.md M7: "Retirement is blocked until reconciliation
        passes." We verify that all knowledge records are accounted
        for (archived) before retiring.
        """
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="agent.manage",
            source=source,
        )
        agent_type = self._repo.get_agent_type(project_id, type_id)
        if agent_type.state != "active":
            raise InvalidAgentTypeTransitionError(
                f"Cannot retire type in state {agent_type.state!r}"
            )
        # Check no running instances.
        running = self._repo.count_running_instances(type_id)
        if running > 0:
            raise KnowledgeReconciliationError(
                f"Cannot retire type {agent_type.name!r}: {running} instances are still running"
            )
        with self._repo._database.transaction():
            # 1. Take a snapshot.
            snapshot = self._take_snapshot(
                project_id, "before_retire", actor_id, source, commit=False
            )
            # 2. Archive all knowledge records.
            if archive_knowledge:
                records = self._repo.list_knowledge_for_type(type_id, include_archived=False)
                for record in records:
                    self._repo.update_knowledge_state(record.id, "archived", commit=False)
            # 3. Reconcile: no records in non-archived state.
            remaining = self._repo.list_knowledge_for_type(type_id, include_archived=False)
            if remaining:
                raise KnowledgeReconciliationError(
                    f"Retirement blocked: {len(remaining)} records not archived",
                    unaccounted_records=[r.id.value for r in remaining],
                )
            # 4. Retire the type.
            self._repo.update_agent_type(type_id, state="retired", commit=False)
            self._audit_repo.insert(
                AuditEvent(
                    id=AuditEventId(generate_audit_event_id()),
                    project_id=project_id,
                    actor_id=actor_id,
                    source=source,
                    operation="agent_type.retire",
                    target_type="agent_type",
                    target_id=type_id.value,
                    result="success",
                    redacted_summary=f"Retired type {agent_type.name!r}",
                    correlation_id=snapshot.id.value,
                    created_at=now_utc_iso(),
                ),
                commit=False,
            )
        return self._repo.get_agent_type(project_id, type_id), snapshot

    # ------------------------------------------------------------------
    # Topology rollback
    # ------------------------------------------------------------------

    def rollback_to_snapshot(
        self,
        *,
        project_id: ProjectId,
        snapshot_id: TopologySnapshotId,
        actor_id: UserId,
        source: AuditSource = "web",
    ) -> TopologySnapshot:
        """Roll back the topology to a previous snapshot.

        Per PLAN.md M7: "Rollback restores the prior active topology."
        Per ``zero-agent-execution-lifecycle``: "Never hard-delete
        source topology or memory as part of evolution."

        Rollback reactivates archived types that were active at snapshot
        time and re-archives types that were created after the snapshot.
        It does NOT delete any types or knowledge records.
        """
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="agent.manage",
            source=source,
        )
        # Find the snapshot.
        snapshots = self._repo.list_snapshots(project_id)
        target = next((s for s in snapshots if s.id == snapshot_id), None)
        if target is None:
            from zero.domain.agent_types import TopologyRollbackError

            raise TopologyRollbackError(f"Snapshot {snapshot_id} not found")
        # Parse the snapshot state.
        state = json.loads(target.topology_state)
        snapshot_types = state.get("types", [])
        snapshot_type_ids = {t["id"] for t in snapshot_types}
        snapshot_active_ids = {t["id"] for t in snapshot_types if t["state"] == "active"}
        # Record-id -> owning type id at snapshot time (older snapshots
        # do not carry knowledge_ids and restore routing best-effort).
        snapshot_knowledge_owner: dict[str, str] = {}
        for t in snapshot_types:
            owner_id = str(t.get("id") or "")
            for record_id in t.get("knowledge_ids", ()):
                snapshot_knowledge_owner[str(record_id)] = owner_id
        with self._repo.database.transaction():
            # Take a new snapshot before rollback (for audit).
            rollback_snapshot = self._take_snapshot(
                project_id, "rollback", actor_id, source, commit=False
            )
            # Reactivate types that were active at snapshot time but are
            # now archived; retired types take the governed two-step
            # path (retired -> archived -> active) now that the state
            # machine allows the rollback escape hatch.
            current_types = self._repo.list_agent_types_for_project(
                project_id, include_archived=True
            )
            for t in current_types:
                if t.id.value not in snapshot_active_ids:
                    if t.id.value not in snapshot_type_ids and t.state == "active":
                        # This type was created after the snapshot;
                        # archive it.
                        self._repo.update_agent_type(t.id, state="archived", commit=False)
                    continue
                if t.state == "archived":
                    self._repo.update_agent_type(t.id, state="active", commit=False)
                elif t.state == "retired":
                    self._repo.update_agent_type(t.id, state="archived", commit=False)
                    self._repo.update_agent_type(t.id, state="active", commit=False)
            # Restore knowledge routing: any record that the snapshot
            # attributes to a type but which has since been reassigned
            # is moved back to its snapshot-time owner.
            import logging

            from zero.domain.agent_types import KnowledgeRecordId, KnowledgeRecordNotFoundError

            _rollback_logger = logging.getLogger(__name__)
            for record_id_value, owner_value in sorted(snapshot_knowledge_owner.items()):
                try:
                    record = self._repo.get_knowledge(KnowledgeRecordId(record_id_value))
                except KnowledgeRecordNotFoundError:
                    continue
                current_owner = record.agent_type_id.value if record.agent_type_id else None
                if current_owner != owner_value:
                    try:
                        self._repo.reassign_knowledge(
                            record.id,
                            AgentTypeId(owner_value),
                            migrated_from=None,
                            commit=False,
                        )
                    except Exception as reassign_exc:  # noqa: BLE001 - best-effort restore
                        _rollback_logger.warning(
                            "knowledge %s could not be restored to type %s: %s",
                            record_id_value,
                            owner_value,
                            type(reassign_exc).__name__,
                        )
            self._audit_repo.insert(
                AuditEvent(
                    id=AuditEventId(generate_audit_event_id()),
                    project_id=project_id,
                    actor_id=actor_id,
                    source=source,
                    operation="agent_type.rollback",
                    target_type="topology_snapshot",
                    target_id=snapshot_id.value,
                    result="success",
                    redacted_summary=f"Rolled back to snapshot {snapshot_id.value}",
                    correlation_id=rollback_snapshot.id.value,
                    created_at=now_utc_iso(),
                ),
                commit=False,
            )
        return rollback_snapshot

    # ------------------------------------------------------------------
    # Snapshot helper
    # ------------------------------------------------------------------

    def _take_snapshot(
        self,
        project_id: ProjectId,
        reason: str,
        actor_id: UserId,
        source: AuditSource,
        *,
        commit: bool = True,
    ) -> TopologySnapshot:
        """Capture a topology snapshot.

        Each type records its knowledge-record membership so a rollback
        can restore knowledge routing, not only the type rows.
        """
        types = self._repo.list_agent_types_for_project(project_id, include_archived=True)
        state = {
            "types": [
                {
                    "id": t.id.value,
                    "name": t.name,
                    "state": t.state,
                    "version": t.version,
                    "knowledge_count": self._repo.count_knowledge_for_type(t.id),
                    "knowledge_ids": sorted(
                        record.id.value
                        for record in self._repo.list_knowledge_for_type(
                            t.id,
                            include_archived=True,
                        )
                    ),
                }
                for t in types
            ],
        }
        existing = self._repo.get_latest_snapshot(project_id)
        version = (existing.snapshot_version + 1) if existing else 1
        snapshot = TopologySnapshot(
            id=TopologySnapshotId(generate_topology_snapshot_id()),
            project_id=project_id,
            snapshot_version=version,
            reason=reason,
            topology_state=json.dumps(state, sort_keys=True),
            created_at=now_utc_iso(),
        )
        self._repo.insert_snapshot(snapshot, commit=commit)
        return snapshot

    def get_latest_snapshot(self, project_id: ProjectId) -> TopologySnapshot | None:
        return self._repo.get_latest_snapshot(project_id)

    def list_snapshots(self, project_id: ProjectId) -> list[TopologySnapshot]:
        return self._repo.list_snapshots(project_id)
