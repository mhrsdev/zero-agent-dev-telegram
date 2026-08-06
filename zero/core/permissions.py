"""Zero v2 permission system — ADR T-1.9.

Registry-based permissions with default-deny semantics. Every denial creates
an audit record.

Six roles per ADR 0002 §4:
    agent < reviewer/developer < maintainer < teamlead < owner

A separate ``personal`` role for Personal mode (always full access since it's
the user's own data).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from zero.core.audit import ActorType, AuditResult
from zero.core.scope import Scope

__all__ = [
    "Permission",
    "PermissionContext",
    "PermissionDenied",
    "PermissionRegistry",
    "Role",
    "global_registry",
    "has_permission",
    "register_permission",
    "require",
]


# ---------------------------------------------------------------------- roles

class Role(StrEnum):
    """The six first-class roles per ADR 0002 §4."""

    AGENT = "agent"          # lowest — no write in real env
    REVIEWER = "reviewer"
    DEVELOPER = "developer"
    MAINTAINER = "maintainer"
    TEAMLEAD = "teamlead"
    OWNER = "owner"

    # Special role for PERSONAL mode — full access to own data
    PERSONAL_USER = "personal_user"

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, Role):
            return NotImplemented
        order = [
            Role.AGENT, Role.REVIEWER, Role.DEVELOPER,
            Role.MAINTAINER, Role.TEAMLEAD, Role.OWNER,
            Role.PERSONAL_USER,  # treated as full-access in personal scope
        ]
        return order.index(self) >= order.index(other)

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, Role):
            return NotImplemented
        return self >= other and self != other

    def __le__(self, other: object) -> bool:
        if not isinstance(other, Role):
            return NotImplemented
        return other >= self

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Role):
            return NotImplemented
        return other > self


# ---------------------------------------------------------------------- permissions

# Each permission is just a string. We use a registry to track them.
Permission = str


class PermissionDenied(Exception):
    """Raised when a permission check fails."""

    def __init__(
        self,
        permission: Permission,
        actor_id: str,
        scope: Scope,
        reason: str = "role insufficient",
    ) -> None:
        self.permission = permission
        self.actor_id = actor_id
        self.scope = scope
        self.reason = reason
        super().__init__(
            f"permission denied: {permission!r} for actor {actor_id!r} "
            f"in scope {scope.retrieval_key()!r} ({reason})"
        )


# ---------------------------------------------------------------------- permission predicates

# Default role-to-permission matrix. Each permission lists the minimum role
# required (higher roles inherit). PERSONAL_USER has full access to personal
# scope regardless of matrix.

_DEFAULT_MATRIX: dict[Permission, Role] = {
    # tenancy
    "org.create": Role.PERSONAL_USER,  # any user can create an org
    "org.delete": Role.OWNER,
    "workspace.create": Role.MAINTAINER,
    "project.create": Role.MAINTAINER,
    "project.archive": Role.OWNER,

    # team
    "user.invite": Role.MAINTAINER,
    "user.role_change": Role.OWNER,
    "user.remove": Role.OWNER,

    # tasks
    "task.create": Role.DEVELOPER,
    "task.update": Role.DEVELOPER,
    "task.delete": Role.MAINTAINER,
    "task.claim": Role.DEVELOPER,
    "task.release": Role.DEVELOPER,

    # memory
    "memory.write": Role.DEVELOPER,
    "memory.invalidate": Role.MAINTAINER,
    "memory.promote_fact": Role.MAINTAINER,  # T-6.4 acceptance criterion

    # agents
    "agent.run": Role.DEVELOPER,
    "agent.run.coding": Role.DEVELOPER,
    "agent.run.security": Role.MAINTAINER,
    "agent.run.release": Role.MAINTAINER,

    # approvals
    "approval.request": Role.DEVELOPER,
    "approval.approve": Role.MAINTAINER,  # cannot self-approve (separate check)

    # github
    "github.connect": Role.MAINTAINER,
    "github.merge_pr": Role.MAINTAINER,  # requires approval separately

    # sandbox
    "sandbox.exec": Role.DEVELOPER,

    # personal
    "personal.memory.read": Role.PERSONAL_USER,
    "personal.memory.write": Role.PERSONAL_USER,
    "personal.memory.delete": Role.PERSONAL_USER,
}


@dataclass
class PermissionContext:
    """Who is asking, in what scope, with what role."""

    actor_id: str
    actor_type: ActorType
    scope: Scope
    role: Role

    # Ownership check: optional, used by `update_own_task` style permissions
    owns_resource: bool = False

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "actor_id": self.actor_id,
            "actor_type": self.actor_type.value,
            "scope": self.scope.retrieval_key(),
            "role": self.role.value,
            "owns_resource": self.owns_resource,
        }


# ---------------------------------------------------------------------- registry

class PermissionRegistry:
    """Registry of permissions and their required roles."""

    def __init__(self) -> None:
        self._matrix: dict[Permission, Role] = dict(_DEFAULT_MATRIX)
        self._custom_checkers: dict[
            Permission, Callable[[PermissionContext], bool]
        ] = {}

    def register(
        self,
        permission: Permission,
        required_role: Role,
    ) -> None:
        """Register a permission with its minimum required role."""
        self._matrix[permission] = required_role

    def register_checker(
        self,
        permission: Permission,
        checker: Callable[[PermissionContext], bool],
    ) -> None:
        """Register a custom checker for a permission.

        The checker receives a :class:`PermissionContext` and returns True if
        access should be granted. The role check is still applied first.
        """
        self._custom_checkers[permission] = checker

    def check(self, permission: Permission, ctx: PermissionContext) -> bool:
        """Check whether ``ctx`` grants ``permission``. Default-deny."""
        required = self._matrix.get(permission)
        if required is None:
            return False  # unknown permission → default deny

        # PERSONAL_USER has full access in PERSONAL scope only.
        if ctx.scope.is_personal() and ctx.role is Role.PERSONAL_USER:
            return True

        # Role check.
        if ctx.role < required:
            return False

        # Custom checker (e.g. ownership).
        checker = self._custom_checkers.get(permission)
        if checker is not None and not checker(ctx):
            return False

        return True

    def list_permissions(self) -> list[tuple[Permission, Role]]:
        """Return all registered permissions and their required roles."""
        return sorted(self._matrix.items())


# ---------------------------------------------------------------------- global registry

global_registry = PermissionRegistry()


def register_permission(permission: Permission, required_role: Role) -> None:
    """Register a permission in the global registry."""
    global_registry.register(permission, required_role)


def has_permission(permission: Permission, ctx: PermissionContext) -> bool:
    """Check permission without raising. Default-deny."""
    return global_registry.check(permission, ctx)


def require(permission: Permission, ctx: PermissionContext) -> None:
    """Raise :class:`PermissionDenied` if ``ctx`` lacks ``permission``."""
    if not global_registry.check(permission, ctx):
        raise PermissionDenied(permission, ctx.actor_id, ctx.scope)


# ---------------------------------------------------------------------- async helper

class AuditHook(Protocol):
    async def __call__(
        self,
        *,
        actor_id: str,
        actor_type: ActorType,
        action: str,
        scope: Scope,
        result: AuditResult,
        target_type: str | None = None,
        target_id: str | None = None,
    ) -> None: ...


async def require_and_audit(
    permission: Permission,
    ctx: PermissionContext,
    *,
    action: str,
    audit_hook: AuditHook | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
) -> None:
    """Check permission; if denied, write audit entry and raise."""
    if global_registry.check(permission, ctx):
        return
    if audit_hook is not None:
        await audit_hook(
            actor_id=ctx.actor_id,
            actor_type=ctx.actor_type,
            action=action,
            scope=ctx.scope,
            result=AuditResult.DENIED,
            target_type=target_type,
            target_id=target_id,
        )
    raise PermissionDenied(permission, ctx.actor_id, ctx.scope)
