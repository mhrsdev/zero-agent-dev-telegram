"""Authorization domain types.

Per ``zero-control-plane-trust`` §"Authorization is a domain decision":
Authorization answers more than "is this user logged in?" It combines:

- authenticated actor;
- project membership and ownership;
- operation type;
- target resource and current state;
- source interface when policy cares about it;
- agent role/type and delegated capability;
- plan or execution revision;
- explicit limits such as budget or rate.

A centralized decision path does not require one giant authorization
class. It means every protected route converges on the same domain
policy instead of duplicating partial checks in controllers, bots, and
UI components.

Per ``zero-control-plane-trust`` §"UI controls are not security": the
authorization decision is made server-side, before any protected read
or mutation. UI visibility is a usability concern, not a security
control.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from zero.domain.identity import ProjectId, ProjectRole, UserId

# ----------------------------------------------------------------------
# Permissions
# ----------------------------------------------------------------------

Permission = Literal[
    # Project visibility
    "project.view",
    # Plan lifecycle (M4)
    "plan.propose",
    "plan.edit",
    "plan.approve",
    "plan.reject",
    # Execution lifecycle (M5+M6)
    "execution.start",
    "execution.stop",
    "execution.view_diffs",
    # Integration (M11)
    "integration.authorize_merge",
    # Agent management (M7)
    "agent.manage",
    # Model/provider policy (M10)
    "model.change",
    # Tools (M3)
    "tool.manage",
    # Secrets (M3)
    "secret.manage",
    # Members (M2)
    "member.manage",
    # Cost and audit visibility
    "cost.view",
    "audit.view",
]

#: All permissions recognized by the system. Used for validation and
#: for tests that need to enumerate the matrix.
ALL_PERMISSIONS: tuple[Permission, ...] = (
    "project.view",
    "plan.propose",
    "plan.edit",
    "plan.approve",
    "plan.reject",
    "execution.start",
    "execution.stop",
    "execution.view_diffs",
    "integration.authorize_merge",
    "agent.manage",
    "model.change",
    "tool.manage",
    "secret.manage",
    "member.manage",
    "cost.view",
    "audit.view",
)

# ----------------------------------------------------------------------
# Role → permission matrix
# ----------------------------------------------------------------------

#: Permissions granted to each role. This is the authoritative matrix;
#: any change here is a security-relevant decision and must be
#: reviewed.
#:
#: - ``owner``: full project authority. Can do everything, including
#:   managing members, tools, secrets, and audit.
#: - ``member``: can propose, edit, approve plans; start/stop
#:   executions; view diffs; authorize merges; manage agents; change
#:   models. Cannot manage members, tools, secrets, or view audit.
#: - ``viewer``: read-only. Can view the project and view diffs but
#:   cannot mutate anything.
ROLE_PERMISSIONS: dict[ProjectRole, frozenset[Permission]] = {
    "owner": frozenset(ALL_PERMISSIONS),
    "member": frozenset(
        {
            "project.view",
            "plan.propose",
            "plan.edit",
            "plan.approve",
            "plan.reject",
            "execution.start",
            "execution.stop",
            "execution.view_diffs",
            "integration.authorize_merge",
            "agent.manage",
            "model.change",
            "cost.view",
        }
    ),
    "viewer": frozenset({"project.view", "execution.view_diffs", "cost.view"}),
}


def permissions_for_role(role: ProjectRole) -> frozenset[Permission]:
    """Return the set of permissions granted to a role."""
    return ROLE_PERMISSIONS.get(role, frozenset())


def role_has_permission(role: ProjectRole, permission: Permission) -> bool:
    """Return True if the role grants the permission."""
    return permission in permissions_for_role(role)


# ----------------------------------------------------------------------
# Authorization decision
# ----------------------------------------------------------------------

AuthorizationReason = Literal[
    "allowed",
    "not_member",
    "permission_denied",
    "suspended_user",
    "deleted_user",
    "project_not_found",
    "user_not_found",
    "no_actor",
]


@dataclass(frozen=True)
class AuthorizationDecision:
    """The result of an authorization check.

    Attributes:
        allowed: True if the action is permitted.
        actor_id: the user who requested the action.
        project_id: the project the action targets.
        permission: the permission that was checked.
        role: the actor's role in this project, or ``None`` if the
            actor is not a member.
        reason: a typed reason for the decision. ``"allowed"`` when
            permitted; otherwise one of the denied reasons.
    """

    allowed: bool
    actor_id: UserId | None
    project_id: ProjectId
    permission: Permission
    role: ProjectRole | None
    reason: AuthorizationReason

    @classmethod
    def allow(
        cls,
        *,
        actor_id: UserId,
        project_id: ProjectId,
        permission: Permission,
        role: ProjectRole,
    ) -> AuthorizationDecision:
        return cls(
            allowed=True,
            actor_id=actor_id,
            project_id=project_id,
            permission=permission,
            role=role,
            reason="allowed",
        )

    @classmethod
    def deny(
        cls,
        *,
        actor_id: UserId | None,
        project_id: ProjectId,
        permission: Permission,
        role: ProjectRole | None,
        reason: AuthorizationReason,
    ) -> AuthorizationDecision:
        assert reason != "allowed", "Use AuthorizationDecision.allow for allowed decisions"
        return cls(
            allowed=False,
            actor_id=actor_id,
            project_id=project_id,
            permission=permission,
            role=role,
            reason=reason,
        )


# ----------------------------------------------------------------------
# Typed failures
# ----------------------------------------------------------------------


class AuthorizationError(RuntimeError):
    """Raised when an operation is attempted without authorization.

    This is a typed domain failure (per ``zero-control-plane-trust``
    §"Failure shapes teach the boundary"). Callers should catch it at
    the API boundary and translate it into the appropriate HTTP status
    or platform response.
    """

    def __init__(self, decision: AuthorizationDecision) -> None:
        self.decision = decision
        super().__init__(
            f"Authorization denied: actor={decision.actor_id}, "
            f"project={decision.project_id}, permission={decision.permission}, "
            f"reason={decision.reason}"
        )
