"""DB-backed TopicBindingStore + GroupPolicyStore — replaces in-memory stores.

Per ADR T-4.4 + T-4.5:
    - TopicBindings persist across restarts
    - GroupPolicy persists across restarts
    - resolve_mode looks up real Project metadata (org_id, workspace_id)
      from the dev_projects table, providing accurate scope reconstruction
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from zero.core.scope import Mode, Scope
from zero.telegram.topic_binding import (
    BindingStatus,
    GroupPolicy,
    GroupPolicyStore,
    ModeResolutionResult,
    TopicBinding,
    TopicBindingStore,
    resolve_mode as _base_resolve_mode,
)

if TYPE_CHECKING:
    from zero.db import Database

__all__ = ["DbTopicBindingStore", "DbGroupPolicyStore", "resolve_mode_with_db"]


class DbTopicBindingStore(TopicBindingStore):
    """DB-backed TopicBindingStore.

    Persists to ``normal_topic_bindings`` table (normal schema).
    Falls back to in-memory for tests without DB.
    """

    def __init__(self, db: Database | None = None) -> None:
        self._db = db
        # In-memory cache (synced with DB).
        self._cache: dict[tuple[str, int], TopicBinding] = {}

    async def upsert_async(self, binding: TopicBinding) -> TopicBinding:
        """Insert or replace a binding (DB + cache)."""
        key = (binding.group_id, binding.topic_id)
        self._cache[key] = binding
        if self._db is None:
            return binding
        # Persist to normal schema.
        normal_scope = Scope.normal(
            group_id=binding.group_id, topic_id=binding.topic_id,
        ).with_default_memory_scope()
        async with self._db.connection_for(normal_scope) as conn:
            # Ensure the group exists.
            await conn.execute(
                """INSERT OR IGNORE INTO normal_groups
                   (group_id, telegram_chat_id, is_forum, default_unconfigured_topic_mode)
                   VALUES (?, 0, 0, 'normal')""",
                (binding.group_id,),
            )
            await conn.execute(
                """INSERT OR REPLACE INTO normal_topic_bindings
                   (group_id, topic_id, mode, memory_scope_id, project_id,
                    configured_by, configured_at, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    binding.group_id,
                    binding.topic_id,
                    binding.mode,
                    binding.memory_scope_id,
                    binding.project_id,
                    binding.configured_by,
                    binding.configured_at.isoformat(),
                    binding.status.value,
                ),
            )
        return binding

    def upsert(self, binding: TopicBinding) -> TopicBinding:
        key = (binding.group_id, binding.topic_id)
        self._cache[key] = binding
        return binding

    async def get_async(self, group_id: str, topic_id: int) -> TopicBinding | None:
        """Fetch a binding by (group_id, topic_id)."""
        key = (group_id, topic_id)
        cached = self._cache.get(key)
        if cached is not None and cached.is_active:
            return cached
        if cached is not None and not cached.is_active:
            return None
        if self._db is None:
            return None

        normal_scope = Scope.normal(group_id=group_id, topic_id=topic_id).with_default_memory_scope()
        async with self._db.connection_for(normal_scope) as conn:
            row = await conn.fetchone(
                """SELECT group_id, topic_id, mode, memory_scope_id, project_id,
                          configured_by, configured_at, status
                   FROM normal_topic_bindings
                   WHERE group_id = ? AND topic_id = ? AND status = 'active'""",
                (group_id, topic_id),
            )
            if row is None:
                return None
            binding = self._row_to_binding(row)
            self._cache[key] = binding
            return binding

    def get(self, group_id: str, topic_id: int) -> TopicBinding | None:
        """Sync get (cache only — use get_async for DB)."""
        binding = self._cache.get((group_id, topic_id))
        if binding is None or not binding.is_active:
            return None
        return binding

    async def list_for_group_async(self, group_id: str) -> list[TopicBinding]:
        """List all active bindings for a group."""
        if self._db is None:
            return [
                b for (gid, _), b in self._cache.items()
                if gid == group_id and b.is_active
            ]
        normal_scope = Scope.normal(group_id=group_id, topic_id=0).with_default_memory_scope()
        async with self._db.connection_for(normal_scope) as conn:
            rows = await conn.fetchall(
                """SELECT group_id, topic_id, mode, memory_scope_id, project_id,
                          configured_by, configured_at, status
                   FROM normal_topic_bindings
                   WHERE group_id = ? AND status = 'active'
                   ORDER BY topic_id""",
                (group_id,),
            )
            return [self._row_to_binding(r) for r in rows]

    def list_for_group(self, group_id: str) -> list[TopicBinding]:
        return [
            b for (gid, _), b in self._cache.items()
            if gid == group_id and b.is_active
        ]

    async def archive_async(self, group_id: str, topic_id: int) -> bool:
        """Archive a binding."""
        key = (group_id, topic_id)
        b = self._cache.get(key)
        if b is None or not b.is_active:
            return False
        b.status = BindingStatus.ARCHIVED
        if self._db is not None:
            normal_scope = Scope.normal(group_id=group_id, topic_id=topic_id).with_default_memory_scope()
            async with self._db.connection_for(normal_scope) as conn:
                await conn.execute(
                    "UPDATE normal_topic_bindings SET status = 'archived' "
                    "WHERE group_id = ? AND topic_id = ?",
                    (group_id, topic_id),
                )
        return True

    def archive(self, group_id: str, topic_id: int) -> bool:
        b = self._cache.get((group_id, topic_id))
        if b is None or not b.is_active:
            return False
        b.status = BindingStatus.ARCHIVED
        return True

    @staticmethod
    def _row_to_binding(row: tuple[Any, ...]) -> TopicBinding:
        """Convert a DB row to TopicBinding."""
        (
            group_id,
            topic_id,
            mode,
            memory_scope_id,
            project_id,
            configured_by,
            configured_at_str,
            status_str,
        ) = row
        binding = TopicBinding(
            group_id=str(group_id),
            topic_id=int(str(topic_id)),
            mode=str(mode),  # type: ignore[arg-type]
            memory_scope_id=str(memory_scope_id),
            configured_by=str(configured_by),
            project_id=str(project_id) if project_id else None,
            status=BindingStatus(str(status_str)),
        )
        # Override the auto-generated configured_at.
        binding.configured_at = datetime.fromisoformat(str(configured_at_str))
        return binding


class DbGroupPolicyStore(GroupPolicyStore):
    """DB-backed GroupPolicyStore.

    Persists to ``normal_groups`` table (default_unconfigured_topic_mode column).
    """

    def __init__(self, db: Database | None = None) -> None:
        self._db = db
        self._cache: dict[str, GroupPolicy] = {}

    async def get_or_default_async(self, group_id: str) -> GroupPolicy:
        """Get policy for a group, or create default."""
        if group_id in self._cache:
            return self._cache[group_id]
        if self._db is None:
            policy = GroupPolicy(group_id=group_id)
            self._cache[group_id] = policy
            return policy

        normal_scope = Scope.normal(group_id=group_id, topic_id=0).with_default_memory_scope()
        async with self._db.connection_for(normal_scope) as conn:
            row = await conn.fetchone(
                "SELECT group_id, default_unconfigured_topic_mode FROM normal_groups WHERE group_id = ?",
                (group_id,),
            )
            if row is None:
                # Create default.
                policy = GroupPolicy(group_id=group_id)
                await conn.execute(
                    """INSERT OR IGNORE INTO normal_groups
                       (group_id, telegram_chat_id, is_forum, default_unconfigured_topic_mode)
                       VALUES (?, 0, 0, 'normal')""",
                    (group_id,),
                )
                self._cache[group_id] = policy
                return policy
            policy = GroupPolicy(
                group_id=str(row[0]),
                default_unconfigured_topic_mode=str(row[1]),  # type: ignore[arg-type]
            )
            self._cache[group_id] = policy
            return policy

    def get_or_default(self, group_id: str) -> GroupPolicy:
        if group_id not in self._cache:
            self._cache[group_id] = GroupPolicy(group_id=group_id)
        return self._cache[group_id]

    async def set_async(self, policy: GroupPolicy) -> GroupPolicy:
        """Persist a policy."""
        self._cache[policy.group_id] = policy
        if self._db is None:
            return policy
        normal_scope = Scope.normal(group_id=policy.group_id, topic_id=0).with_default_memory_scope()
        async with self._db.connection_for(normal_scope) as conn:
            await conn.execute(
                """INSERT OR REPLACE INTO normal_groups
                   (group_id, telegram_chat_id, is_forum, default_unconfigured_topic_mode)
                   VALUES (?, 0, 0, ?)""",
                (policy.group_id, policy.default_unconfigured_topic_mode),
            )
        return policy

    def set(self, policy: GroupPolicy) -> GroupPolicy:
        self._cache[policy.group_id] = policy
        return policy


async def resolve_mode_with_db(
    *,
    is_private: bool,
    user_id: str,
    group_id: str | None = None,
    topic_id: int | None = None,
    binding_store: DbTopicBindingStore,
    policy_store: DbGroupPolicyStore,
    db: Database | None = None,
) -> ModeResolutionResult:
    """Resolve mode with real Project metadata lookup.

    Replaces derived org_id/workspace_id with real values from
    the dev_projects table.
    """
    # Use the base resolve_mode first.
    result = _base_resolve_mode(
        is_private=is_private,
        user_id=user_id,
        group_id=group_id,
        topic_id=topic_id,
        binding_store=binding_store,
        policy_store=policy_store,
    )

    # If DEVELOPMENT mode, look up real Project metadata.
    if result.mode is Mode.DEVELOPMENT and db is not None and result.binding is not None:
        project_id = result.binding.project_id
        if project_id is not None:
            dev_scope = Scope.development(
                org_id="org_system",
                workspace_id="ws_system",
                project_id=project_id,
                group_id=group_id or "grp_unknown",
                topic_id=topic_id or 0,
            ).with_default_memory_scope()
            async with db.connection_for(dev_scope) as conn:
                row = await conn.fetchone(
                    "SELECT workspace_id FROM dev_projects WHERE project_id = ?",
                    (project_id,),
                )
                if row is not None:
                    workspace_id = str(row[0])
                    # Look up org_id from workspace.
                    row2 = await conn.fetchone(
                        "SELECT org_id FROM dev_workspaces WHERE workspace_id = ?",
                        (workspace_id,),
                    )
                    org_id = str(row2[0]) if row2 is not None else f"org_for_{project_id}"
                    # Reconstruct the scope with real org/workspace.
                    real_scope = Scope.development(
                        org_id=org_id,
                        workspace_id=workspace_id,
                        project_id=project_id,
                        group_id=group_id or "grp_unknown",
                        topic_id=topic_id or 0,
                    ).with_default_memory_scope()
                    return ModeResolutionResult(
                        mode=result.mode,
                        scope=real_scope,
                        binding=result.binding,
                        policy_applied=result.policy_applied,
                        silenced=result.silenced,
                    )

    return result
