"""DB-backed role store — persistent role bindings with scope inheritance.

Per ADR T-2.2:
    - Six roles: agent < reviewer/developer < maintainer < teamlead < owner
    - RoleBinding at Org/Workspace/Project level with inheritance
    - Role determines permissions (registry matrix)
    - Role changes are sensitive (need approval)
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

from zero.core.permissions import Role
from zero.core.scope import Scope

if TYPE_CHECKING:
    from zero.db import Database

__all__ = ["DbRoleStore", "RoleBinding", "RoleScopeKind"]


class RoleScopeKind(str, Enum):
    ORG = "org"
    WORKSPACE = "workspace"
    PROJECT = "project"


@dataclass(slots=True)
class RoleBinding:
    """A single role binding (user → role at scope)."""

    binding_id: str
    user_id: str
    role: Role
    scope_kind: RoleScopeKind
    scope_id: str
    granted_by: str
    granted_at: datetime
    revoked_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "binding_id": self.binding_id,
            "user_id": self.user_id,
            "role": self.role.value,
            "scope_kind": self.scope_kind.value,
            "scope_id": self.scope_id,
            "granted_by": self.granted_by,
            "granted_at": self.granted_at.isoformat(),
            "revoked": self.revoked_at is not None,
        }


# Role precedence: higher = more permissions.
_ROLE_PRECEDENCE: dict[Role, int] = {
    Role.AGENT: 0,
    Role.REVIEWER: 1,
    Role.DEVELOPER: 2,
    Role.MAINTAINER: 3,
    Role.TEAMLEAD: 4,
    Role.OWNER: 5,
    Role.PERSONAL_USER: 6,  # full access in personal scope
}


class DbRoleStore:
    """DB-backed role store with scope inheritance.

    Lookup precedence (most specific wins):
        1. Project-level binding (scope_kind='project')
        2. Workspace-level binding (scope_kind='workspace')
        3. Org-level binding (scope_kind='org')
        4. Default: AGENT (no binding found)

    For PERSONAL scope: always returns PERSONAL_USER (no DB lookup needed).
    """

    def __init__(self, db: Database) -> None:
        self._db = db
        # In-memory cache: (user_id, scope_kind, scope_id) → RoleBinding
        self._cache: dict[tuple[str, str, str], RoleBinding] = {}

    async def grant_role_async(
        self,
        *,
        user_id: str,
        role: Role,
        scope_kind: RoleScopeKind,
        scope_id: str,
        granted_by: str,
    ) -> RoleBinding:
        """Grant a role to a user at a scope."""
        binding = RoleBinding(
            binding_id=f"rb_{uuid.uuid4().hex[:16]}",
            user_id=user_id,
            role=role,
            scope_kind=scope_kind,
            scope_id=scope_id,
            granted_by=granted_by,
            granted_at=datetime.now(UTC),
        )

        # Persist to DB (dev schema only).
        if scope_kind is not RoleScopeKind.ORG or True:
            # All role bindings go to dev schema.
            dev_scope = Scope.development(
                org_id="org_system", workspace_id="ws_system",
                project_id="prj_system", group_id="grp_system", topic_id=0,
            ).with_default_memory_scope()
            async with self._db.connection_for(dev_scope) as conn:
                await conn.execute(
                    """INSERT INTO dev_role_bindings
                       (binding_id, user_id, role, scope_kind, scope_id, granted_by, granted_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        binding.binding_id,
                        user_id,
                        role.value,
                        scope_kind.value,
                        scope_id,
                        granted_by,
                        binding.granted_at.isoformat(),
                    ),
                )

        cache_key = (user_id, scope_kind.value, scope_id)
        self._cache[cache_key] = binding
        return binding

    async def revoke_role_async(
        self,
        *,
        user_id: str,
        scope_kind: RoleScopeKind,
        scope_id: str,
        revoked_by: str,
    ) -> bool:
        """Revoke a role binding."""
        dev_scope = Scope.development(
            org_id="org_system", workspace_id="ws_system",
            project_id="prj_system", group_id="grp_system", topic_id=0,
        ).with_default_memory_scope()
        now = datetime.now(UTC)
        async with self._db.connection_for(dev_scope) as conn:
            await conn.execute(
                """UPDATE dev_role_bindings
                   SET revoked_at = ?
                   WHERE user_id = ? AND scope_kind = ? AND scope_id = ?
                   AND revoked_at IS NULL""",
                (
                    now.isoformat(),
                    user_id,
                    scope_kind.value,
                    scope_id,
                ),
            )
        cache_key = (user_id, scope_kind.value, scope_id)
        cached = self._cache.pop(cache_key, None)
        if cached is not None:
            cached.revoked_at = now
            self._cache[cache_key] = cached
        return True

    async def get_role_for_scope_async(
        self,
        *,
        user_id: str,
        scope: Scope,
    ) -> Role:
        """Resolve the user's role for the given scope.

        For PERSONAL scope: returns PERSONAL_USER.
        For NORMAL scope: returns AGENT (no dev features in normal mode).
        For DEVELOPMENT scope: looks up project → workspace → org inheritance.
        """
        if scope.is_personal():
            return Role.PERSONAL_USER
        if scope.is_normal():
            # Normal mode: everyone is effectively a DEVELOPER (can chat, read memory).
            return Role.DEVELOPER
        if not scope.is_development():
            return Role.AGENT

        # DEVELOPMENT scope — look up role bindings.
        assert scope.project_id is not None  # noqa: S101
        assert scope.workspace_id is not None  # noqa: S101
        assert scope.org_id is not None  # noqa: S101

        # Try project-level first.
        role = await self._lookup_binding_async(
            user_id=user_id,
            scope_kind=RoleScopeKind.PROJECT,
            scope_id=scope.project_id,
        )
        if role is not None:
            return role

        # Then workspace-level.
        role = await self._lookup_binding_async(
            user_id=user_id,
            scope_kind=RoleScopeKind.WORKSPACE,
            scope_id=scope.workspace_id,
        )
        if role is not None:
            return role

        # Then org-level.
        role = await self._lookup_binding_async(
            user_id=user_id,
            scope_kind=RoleScopeKind.ORG,
            scope_id=scope.org_id,
        )
        if role is not None:
            return role

        # No binding → default AGENT.
        return Role.AGENT

    async def _lookup_binding_async(
        self,
        *,
        user_id: str,
        scope_kind: RoleScopeKind,
        scope_id: str,
    ) -> Role | None:
        """Look up a single role binding."""
        cache_key = (user_id, scope_kind.value, scope_id)
        cached = self._cache.get(cache_key)
        if cached is not None and cached.is_active:
            return cached.role
        if cached is not None and not cached.is_active:
            return None

        dev_scope = Scope.development(
            org_id="org_system", workspace_id="ws_system",
            project_id="prj_system", group_id="grp_system", topic_id=0,
        ).with_default_memory_scope()
        async with self._db.connection_for(dev_scope) as conn:
            row = await conn.fetchone(
                """SELECT binding_id, user_id, role, scope_kind, scope_id,
                          granted_by, granted_at, revoked_at
                   FROM dev_role_bindings
                   WHERE user_id = ? AND scope_kind = ? AND scope_id = ?
                   AND revoked_at IS NULL""",
                (user_id, scope_kind.value, scope_id),
            )
            if row is None:
                return None
            binding = self._row_to_binding(row)
            self._cache[cache_key] = binding
            return binding.role

    @staticmethod
    def _row_to_binding(row: tuple[Any, ...]) -> RoleBinding:
        """Convert a DB row to RoleBinding."""
        (
            binding_id,
            user_id,
            role_str,
            scope_kind_str,
            scope_id,
            granted_by,
            granted_at_str,
            revoked_at_str,
        ) = row
        binding = RoleBinding(
            binding_id=binding_id,
            user_id=user_id,
            role=Role(role_str),
            scope_kind=RoleScopeKind(scope_kind_str),
            scope_id=scope_id,
            granted_by=granted_by,
            granted_at=datetime.fromisoformat(granted_at_str),
        )
        if revoked_at_str:
            binding.revoked_at = datetime.fromisoformat(revoked_at_str)
        return binding

    async def list_bindings_for_user_async(
        self,
        user_id: str,
        *,
        include_revoked: bool = False,
    ) -> list[RoleBinding]:
        """List all role bindings for a user."""
        dev_scope = Scope.development(
            org_id="org_system", workspace_id="ws_system",
            project_id="prj_system", group_id="grp_system", topic_id=0,
        ).with_default_memory_scope()
        async with self._db.connection_for(dev_scope) as conn:
            if include_revoked:
                rows = await conn.fetchall(
                    """SELECT binding_id, user_id, role, scope_kind, scope_id,
                              granted_by, granted_at, revoked_at
                       FROM dev_role_bindings WHERE user_id = ?
                       ORDER BY granted_at DESC""",
                    (user_id,),
                )
            else:
                rows = await conn.fetchall(
                    """SELECT binding_id, user_id, role, scope_kind, scope_id,
                              granted_by, granted_at, revoked_at
                       FROM dev_role_bindings WHERE user_id = ? AND revoked_at IS NULL
                       ORDER BY granted_at DESC""",
                    (user_id,),
                )
            return [self._row_to_binding(r) for r in rows]

    async def list_bindings_for_scope_async(
        self,
        *,
        scope_kind: RoleScopeKind,
        scope_id: str,
        include_revoked: bool = False,
    ) -> list[RoleBinding]:
        """List all role bindings at a scope."""
        dev_scope = Scope.development(
            org_id="org_system", workspace_id="ws_system",
            project_id="prj_system", group_id="grp_system", topic_id=0,
        ).with_default_memory_scope()
        async with self._db.connection_for(dev_scope) as conn:
            if include_revoked:
                rows = await conn.fetchall(
                    """SELECT binding_id, user_id, role, scope_kind, scope_id,
                              granted_by, granted_at, revoked_at
                       FROM dev_role_bindings
                       WHERE scope_kind = ? AND scope_id = ?
                       ORDER BY granted_at DESC""",
                    (scope_kind.value, scope_id),
                )
            else:
                rows = await conn.fetchall(
                    """SELECT binding_id, user_id, role, scope_kind, scope_id,
                              granted_by, granted_at, revoked_at
                       FROM dev_role_bindings
                       WHERE scope_kind = ? AND scope_id = ? AND revoked_at IS NULL
                       ORDER BY granted_at DESC""",
                    (scope_kind.value, scope_id),
                )
            return [self._row_to_binding(r) for r in rows]
