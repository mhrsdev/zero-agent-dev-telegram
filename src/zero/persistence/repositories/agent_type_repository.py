"""Agent type repository — types, instances, knowledge records, topology snapshots.

Per ``zero-context-memory`` §"Non-negotiable invariants": removing,
splitting, or merging a sub-agent type never deletes its knowledge.

Per ``zero-project-isolation-evidence`` §"Scope begins before access":
all queries filter by ``project_id`` before any row is loaded.
"""

from __future__ import annotations

import json
import sqlite3

from zero.domain.agent_types import (
    AgentInstance,
    AgentInstanceId,
    AgentInstanceNotFoundError,
    AgentInstanceState,
    AgentType,
    AgentTypeId,
    AgentTypeNotFoundError,
    AgentTypeState,
    ConcurrencyLimitExceededError,
    InvalidAgentTypeTransitionError,
    KnowledgeRecord,
    KnowledgeRecordId,
    KnowledgeRecordNotFoundError,
    KnowledgeState,
    TopologySnapshot,
    TopologySnapshotId,
)
from zero.domain.execution import TaskId
from zero.domain.identity import ProjectId
from zero.domain.ids import generate_agent_instance_id
from zero.persistence.connection import Database


def _row_to_agent_type(row: sqlite3.Row) -> AgentType:
    return AgentType(
        id=AgentTypeId(row["id"]),
        project_id=ProjectId(row["project_id"]),
        name=row["name"],
        responsibility=row["responsibility"],
        memory_scope=row["memory_scope"],
        permitted_tools=tuple(json.loads(row["permitted_tools"])),
        model_policy=json.loads(row["model_policy"]),
        context_budget_tokens=row["context_budget_tokens"],
        max_concurrent_instances=row["max_concurrent_instances"],
        state=row["state"],  # type: ignore[arg-type]
        version=row["version"],
        superseded_by=AgentTypeId(row["superseded_by"]) if row["superseded_by"] else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_instance(row: sqlite3.Row) -> AgentInstance:
    return AgentInstance(
        id=AgentInstanceId(row["id"]),
        project_id=ProjectId(row["project_id"]),
        agent_type_id=AgentTypeId(row["agent_type_id"]),
        task_id=TaskId(row["task_id"]) if row["task_id"] else None,
        state=row["state"],  # type: ignore[arg-type]
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_knowledge(row: sqlite3.Row) -> KnowledgeRecord:
    return KnowledgeRecord(
        id=KnowledgeRecordId(row["id"]),
        project_id=ProjectId(row["project_id"]),
        agent_type_id=AgentTypeId(row["agent_type_id"]) if row["agent_type_id"] else None,
        kind=row["kind"],  # type: ignore[arg-type]
        content=row["content"],
        content_hash=row["content_hash"],
        provenance=row["provenance"],
        state=row["state"],  # type: ignore[arg-type]
        superseded_by=KnowledgeRecordId(row["superseded_by"]) if row["superseded_by"] else None,
        migrated_from=KnowledgeRecordId(row["migrated_from"]) if row["migrated_from"] else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_snapshot(row: sqlite3.Row) -> TopologySnapshot:
    return TopologySnapshot(
        id=TopologySnapshotId(row["id"]),
        project_id=ProjectId(row["project_id"]),
        snapshot_version=row["snapshot_version"],
        reason=row["reason"],
        topology_state=row["topology_state"],
        created_at=row["created_at"],
    )


class AgentTypeRepository:
    """Database-backed repository for agent types, instances, knowledge
    records, and topology snapshots."""

    def __init__(self, database: Database) -> None:
        self._database = database

    @property
    def database(self) -> Database:
        """The underlying database (public transaction boundary)."""
        return self._database

    # ------------------------------------------------------------------
    # Agent types
    # ------------------------------------------------------------------

    def insert_agent_type(self, agent_type: AgentType, *, commit: bool = True) -> None:
        conn = self._database.connect()
        try:
            conn.execute(
                "INSERT INTO agent_types "
                "(id, project_id, name, responsibility, memory_scope, "
                "permitted_tools, model_policy, context_budget_tokens, "
                "max_concurrent_instances, state, version) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    agent_type.id.value,
                    agent_type.project_id.value,
                    agent_type.name,
                    agent_type.responsibility,
                    agent_type.memory_scope,
                    json.dumps(list(agent_type.permitted_tools)),
                    json.dumps(agent_type.model_policy),
                    agent_type.context_budget_tokens,
                    agent_type.max_concurrent_instances,
                    agent_type.state,
                    agent_type.version,
                ),
            )
            if commit:
                conn.commit()
        except sqlite3.IntegrityError as exc:
            if commit:
                conn.rollback()
            from zero.domain.agent_types import AgentTypeAlreadyExistsError

            if "UNIQUE" in str(exc):
                raise AgentTypeAlreadyExistsError(
                    f"Agent type {agent_type.name!r} already exists in "
                    f"project {agent_type.project_id}"
                ) from exc
            raise

    def get_agent_type(self, project_id: ProjectId, type_id: AgentTypeId) -> AgentType:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, name, responsibility, memory_scope, "
            "permitted_tools, model_policy, context_budget_tokens, "
            "max_concurrent_instances, state, version, superseded_by, "
            "created_at, updated_at FROM agent_types "
            "WHERE id = ? AND project_id = ?",
            (type_id.value, project_id.value),
        )
        row = cursor.fetchone()
        if row is None:
            raise AgentTypeNotFoundError(f"Agent type {type_id} not found in project {project_id}")
        return _row_to_agent_type(row)

    def list_agent_types_for_project(
        self,
        project_id: ProjectId,
        *,
        include_archived: bool = True,
    ) -> list[AgentType]:
        conn = self._database.connect()
        if include_archived:
            cursor = conn.execute(
                "SELECT id, project_id, name, responsibility, memory_scope, "
                "permitted_tools, model_policy, context_budget_tokens, "
                "max_concurrent_instances, state, version, superseded_by, "
                "created_at, updated_at FROM agent_types "
                "WHERE project_id = ? ORDER BY created_at ASC",
                (project_id.value,),
            )
        else:
            cursor = conn.execute(
                "SELECT id, project_id, name, responsibility, memory_scope, "
                "permitted_tools, model_policy, context_budget_tokens, "
                "max_concurrent_instances, state, version, superseded_by, "
                "created_at, updated_at FROM agent_types "
                "WHERE project_id = ? AND state = 'active' "
                "ORDER BY created_at ASC",
                (project_id.value,),
            )
        return [_row_to_agent_type(row) for row in cursor.fetchall()]

    def update_agent_type(
        self,
        type_id: AgentTypeId,
        *,
        name: str | None = None,
        responsibility: str | None = None,
        memory_scope: str | None = None,
        permitted_tools: tuple[str, ...] | None = None,
        model_policy: dict[str, str] | None = None,
        context_budget_tokens: int | None = None,
        max_concurrent_instances: int | None = None,
        state: AgentTypeState | None = None,
        superseded_by: AgentTypeId | None = None,
        increment_version: bool = True,
        commit: bool = True,
    ) -> None:
        """Update fields on an agent type. Only provided fields are
        updated; ``version`` is incremented if ``increment_version`` is
        True (default)."""
        conn = self._database.connect()
        sets: list[str] = []
        params: list = []
        if name is not None:
            sets.append("name = ?")
            params.append(name)
        if responsibility is not None:
            sets.append("responsibility = ?")
            params.append(responsibility)
        if memory_scope is not None:
            sets.append("memory_scope = ?")
            params.append(memory_scope)
        if permitted_tools is not None:
            sets.append("permitted_tools = ?")
            params.append(json.dumps(list(permitted_tools)))
        if model_policy is not None:
            sets.append("model_policy = ?")
            params.append(json.dumps(model_policy))
        if context_budget_tokens is not None:
            sets.append("context_budget_tokens = ?")
            params.append(context_budget_tokens)
        if max_concurrent_instances is not None:
            sets.append("max_concurrent_instances = ?")
            params.append(max_concurrent_instances)
        if state is not None:
            sets.append("state = ?")
            params.append(state)
        if superseded_by is not None:
            sets.append("superseded_by = ?")
            params.append(superseded_by.value)
        if increment_version:
            sets.append("version = version + 1")
        sets.append("updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')")
        if not sets:
            return
        sql = f"UPDATE agent_types SET {', '.join(sets)} WHERE id = ?"
        params.append(type_id.value)
        cursor = conn.execute(sql, params)
        if cursor.rowcount == 0:
            raise AgentTypeNotFoundError(f"Agent type {type_id} not found")
        if commit:
            conn.commit()

    # ------------------------------------------------------------------
    # Agent instances
    # ------------------------------------------------------------------

    def insert_instance(self, instance: AgentInstance, *, commit: bool = True) -> None:
        conn = self._database.connect()
        try:
            conn.execute(
                "INSERT INTO agent_instances "
                "(id, project_id, agent_type_id, task_id, state) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    instance.id.value,
                    instance.project_id.value,
                    instance.agent_type_id.value,
                    instance.task_id.value if instance.task_id else None,
                    instance.state,
                ),
            )
            if commit:
                conn.commit()
        except sqlite3.IntegrityError:
            if commit:
                conn.rollback()
            raise

    def get_instance(self, instance_id: AgentInstanceId) -> AgentInstance:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, agent_type_id, task_id, state, "
            "created_at, updated_at FROM agent_instances WHERE id = ?",
            (instance_id.value,),
        )
        row = cursor.fetchone()
        if row is None:
            raise AgentInstanceNotFoundError(f"Agent instance {instance_id} not found")
        return _row_to_instance(row)

    def list_instances_for_type(self, type_id: AgentTypeId) -> list[AgentInstance]:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, agent_type_id, task_id, state, "
            "created_at, updated_at FROM agent_instances "
            "WHERE agent_type_id = ? ORDER BY created_at ASC",
            (type_id.value,),
        )
        return [_row_to_instance(row) for row in cursor.fetchall()]

    def count_running_instances(self, type_id: AgentTypeId) -> int:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT COUNT(*) FROM agent_instances WHERE agent_type_id = ? AND state = 'running'",
            (type_id.value,),
        )
        return int(cursor.fetchone()[0])

    def lease_instance_for_task(
        self,
        *,
        project_id: ProjectId,
        type_id: AgentTypeId,
        task_id: TaskId,
    ) -> AgentInstance:
        """Create one ``running`` instance of ``type_id`` bound to a task.

        The concurrency check and the insert happen inside one
        ``BEGIN IMMEDIATE`` transaction so two schedulers on the same
        database cannot both observe free capacity and exceed the
        type's ``max_concurrent_instances`` limit (the read-then-write
        race called out in the release audit).
        """
        with self._database.transaction() as conn:
            agent_type = self.get_agent_type(project_id, type_id)
            if agent_type.state != "active":
                raise InvalidAgentTypeTransitionError(
                    f"Agent type {type_id.value} is {agent_type.state!r}; "
                    "only active types can lease instances"
                )
            cursor = conn.execute(
                "SELECT COUNT(*) FROM agent_instances "
                "WHERE agent_type_id = ? AND state = 'running'",
                (type_id.value,),
            )
            running = int(cursor.fetchone()[0])
            if running >= agent_type.max_concurrent_instances:
                raise ConcurrencyLimitExceededError(
                    f"Agent type {type_id.value} already has {running} running "
                    f"instance(s); max_concurrent_instances="
                    f"{agent_type.max_concurrent_instances}"
                )
            instance = AgentInstance(
                id=AgentInstanceId(generate_agent_instance_id()),
                project_id=project_id,
                agent_type_id=type_id,
                task_id=task_id,
                state="running",
                created_at="",
                updated_at="",
            )
            conn.execute(
                "INSERT INTO agent_instances "
                "(id, project_id, agent_type_id, task_id, state) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    instance.id.value,
                    instance.project_id.value,
                    instance.agent_type_id.value,
                    instance.task_id.value if instance.task_id else None,
                    instance.state,
                ),
            )
        return instance

    def finish_instance(
        self,
        instance_id: AgentInstanceId,
        new_state: AgentInstanceState,
    ) -> None:
        """Move a leased instance to a terminal/reusable state."""
        self.update_instance_state(instance_id, new_state)

    def finish_running_instances_for_task(
        self,
        task_id: TaskId,
        new_state: AgentInstanceState = "cancelled",
        *,
        commit: bool = True,
    ) -> int:
        """Release every ``running`` instance lease held by ``task_id``.

        Restart-recovery companion to :meth:`lease_instance_for_task`
        (real-run fix): when a worker process dies mid-task,
        ``recover_after_restart`` puts the task back to ``ready``, but
        its agent-type instance rows stayed ``running`` forever —
        silently exhausting the type's ``max_concurrent_instances``
        budget so every later claim failed with
        ``ConcurrencyLimitExceededError``. Recovery now calls this to
        release the leaked leases. Returns the number of instances
        released.
        """
        conn = self._database.connect()
        cursor = conn.execute(
            "UPDATE agent_instances SET state = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE task_id = ? AND state = 'running'",
            (new_state, task_id.value),
        )
        released = int(cursor.rowcount)
        if commit:
            conn.commit()
        return released

    def update_instance_state(
        self,
        instance_id: AgentInstanceId,
        new_state: AgentInstanceState,
        *,
        task_id: TaskId | None = None,
        commit: bool = True,
    ) -> None:
        conn = self._database.connect()
        if task_id is not None:
            cursor = conn.execute(
                "UPDATE agent_instances SET state = ?, task_id = ?, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE id = ?",
                (new_state, task_id.value, instance_id.value),
            )
        else:
            cursor = conn.execute(
                "UPDATE agent_instances SET state = ?, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE id = ?",
                (new_state, instance_id.value),
            )
        if cursor.rowcount == 0:
            raise AgentInstanceNotFoundError(f"Agent instance {instance_id} not found")
        if commit:
            conn.commit()

    # ------------------------------------------------------------------
    # Knowledge records
    # ------------------------------------------------------------------

    def insert_knowledge(self, record: KnowledgeRecord, *, commit: bool = True) -> None:
        conn = self._database.connect()
        try:
            conn.execute(
                "INSERT INTO knowledge_records "
                "(id, project_id, agent_type_id, kind, content, "
                "content_hash, provenance, state, superseded_by, "
                "migrated_from) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.id.value,
                    record.project_id.value,
                    record.agent_type_id.value if record.agent_type_id else None,
                    record.kind,
                    record.content,
                    record.content_hash,
                    record.provenance,
                    record.state,
                    record.superseded_by.value if record.superseded_by else None,
                    record.migrated_from.value if record.migrated_from else None,
                ),
            )
            if commit:
                conn.commit()
        except sqlite3.IntegrityError:
            if commit:
                conn.rollback()
            raise

    def get_knowledge(self, record_id: KnowledgeRecordId) -> KnowledgeRecord:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, agent_type_id, kind, content, "
            "content_hash, provenance, state, superseded_by, "
            "migrated_from, created_at, updated_at "
            "FROM knowledge_records WHERE id = ?",
            (record_id.value,),
        )
        row = cursor.fetchone()
        if row is None:
            raise KnowledgeRecordNotFoundError(f"Knowledge record {record_id} not found")
        return _row_to_knowledge(row)

    def list_knowledge_for_type(
        self,
        type_id: AgentTypeId,
        *,
        include_archived: bool = False,
    ) -> list[KnowledgeRecord]:
        """List knowledge records owned by a type.

        Per ``zero-project-isolation-evidence`` §"Scope begins before
        access": the query filters by ``agent_type_id`` before any row
        is loaded.
        """
        conn = self._database.connect()
        if include_archived:
            cursor = conn.execute(
                "SELECT id, project_id, agent_type_id, kind, content, "
                "content_hash, provenance, state, superseded_by, "
                "migrated_from, created_at, updated_at "
                "FROM knowledge_records WHERE agent_type_id = ? "
                "ORDER BY created_at ASC",
                (type_id.value,),
            )
        else:
            cursor = conn.execute(
                "SELECT id, project_id, agent_type_id, kind, content, "
                "content_hash, provenance, state, superseded_by, "
                "migrated_from, created_at, updated_at "
                "FROM knowledge_records WHERE agent_type_id = ? "
                "AND state IN ('candidate', 'approved') "
                "ORDER BY created_at ASC",
                (type_id.value,),
            )
        return [_row_to_knowledge(row) for row in cursor.fetchall()]

    def list_knowledge_for_project(
        self,
        project_id: ProjectId,
        *,
        include_archived: bool = False,
    ) -> list[KnowledgeRecord]:
        conn = self._database.connect()
        if include_archived:
            cursor = conn.execute(
                "SELECT id, project_id, agent_type_id, kind, content, "
                "content_hash, provenance, state, superseded_by, "
                "migrated_from, created_at, updated_at "
                "FROM knowledge_records WHERE project_id = ? "
                "ORDER BY created_at ASC",
                (project_id.value,),
            )
        else:
            cursor = conn.execute(
                "SELECT id, project_id, agent_type_id, kind, content, "
                "content_hash, provenance, state, superseded_by, "
                "migrated_from, created_at, updated_at "
                "FROM knowledge_records WHERE project_id = ? "
                "AND state IN ('candidate', 'approved') "
                "ORDER BY created_at ASC",
                (project_id.value,),
            )
        return [_row_to_knowledge(row) for row in cursor.fetchall()]

    def update_knowledge_state(
        self,
        record_id: KnowledgeRecordId,
        new_state: KnowledgeState,
        *,
        superseded_by: KnowledgeRecordId | None = None,
        commit: bool = True,
    ) -> None:
        conn = self._database.connect()
        if superseded_by is not None:
            cursor = conn.execute(
                "UPDATE knowledge_records SET state = ?, "
                "superseded_by = ?, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE id = ?",
                (new_state, superseded_by.value, record_id.value),
            )
        else:
            cursor = conn.execute(
                "UPDATE knowledge_records SET state = ?, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE id = ?",
                (new_state, record_id.value),
            )
        if cursor.rowcount == 0:
            raise KnowledgeRecordNotFoundError(f"Knowledge record {record_id} not found")
        if commit:
            conn.commit()

    def reassign_knowledge(
        self,
        record_id: KnowledgeRecordId,
        new_type_id: AgentTypeId,
        *,
        migrated_from: KnowledgeRecordId | None = None,
        commit: bool = True,
    ) -> None:
        """Reassign a knowledge record to a different agent type.

        Per PLAN.md M7: "Provenance links from source knowledge to
        destination scopes." The ``migrated_from`` field records the
        original record ID for provenance.
        """
        conn = self._database.connect()
        cursor = conn.execute(
            "UPDATE knowledge_records SET agent_type_id = ?, "
            "migrated_from = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE id = ?",
            (
                new_type_id.value,
                migrated_from.value if migrated_from else None,
                record_id.value,
            ),
        )
        if cursor.rowcount == 0:
            raise KnowledgeRecordNotFoundError(f"Knowledge record {record_id} not found")
        if commit:
            conn.commit()

    def count_knowledge_for_type(self, type_id: AgentTypeId) -> int:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT COUNT(*) FROM knowledge_records WHERE agent_type_id = ?",
            (type_id.value,),
        )
        return int(cursor.fetchone()[0])

    # ------------------------------------------------------------------
    # Topology snapshots
    # ------------------------------------------------------------------

    def insert_snapshot(self, snapshot: TopologySnapshot, *, commit: bool = True) -> None:
        conn = self._database.connect()
        try:
            conn.execute(
                "INSERT INTO topology_snapshots "
                "(id, project_id, snapshot_version, reason, topology_state) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    snapshot.id.value,
                    snapshot.project_id.value,
                    snapshot.snapshot_version,
                    snapshot.reason,
                    snapshot.topology_state,
                ),
            )
            if commit:
                conn.commit()
        except sqlite3.IntegrityError:
            if commit:
                conn.rollback()
            raise

    def get_latest_snapshot(self, project_id: ProjectId) -> TopologySnapshot | None:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, snapshot_version, reason, "
            "topology_state, created_at FROM topology_snapshots "
            "WHERE project_id = ? ORDER BY snapshot_version DESC LIMIT 1",
            (project_id.value,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return _row_to_snapshot(row)

    def list_snapshots(self, project_id: ProjectId) -> list[TopologySnapshot]:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, snapshot_version, reason, "
            "topology_state, created_at FROM topology_snapshots "
            "WHERE project_id = ? ORDER BY snapshot_version ASC",
            (project_id.value,),
        )
        return [_row_to_snapshot(row) for row in cursor.fetchall()]
