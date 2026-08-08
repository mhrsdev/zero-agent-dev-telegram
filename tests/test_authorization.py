"""Authorization service tests — central decision path.

Per ``zero-control-plane-trust`` §"Authorization is a domain decision":
A centralized decision path does not require one giant authorization
class. It means every protected route converges on the same domain
policy instead of duplicating partial checks in controllers, bots, and
UI components.

Per ``zero-control-plane-trust`` §"UI controls are not security": UI
visibility is a usability concern, not a security control. The
decision here is authoritative.

Per PLAN.md M3 validation: "Allow and deny cases for every current
permission." and "Revocation takes effect through all implemented
interfaces."
"""

from __future__ import annotations

import pytest

from zero.app.services import build_services
from zero.config import Settings
from zero.domain.authorization import (
    ALL_PERMISSIONS,
    AuthorizationError,
    permissions_for_role,
    role_has_permission,
)
from zero.domain.identity import (
    UserId,
)
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations

# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture
def services(test_settings: Settings):
    database = Database(test_settings)
    apply_migrations(database)
    return build_services(test_settings, database)


@pytest.fixture
def project_with_members(services):
    """Create a project with an owner, a member, and a viewer."""
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(
        owner_id=owner.id, name="Project A"
    )
    member = services.identity.create_user(display_name="Member")
    viewer = services.identity.create_user(display_name="Viewer")
    services.identity.add_member(
        project_id=project.id,
        actor_id=owner.id,
        member_id=member.id,
        role="member",
    )
    services.identity.add_member(
        project_id=project.id,
        actor_id=owner.id,
        member_id=viewer.id,
        role="viewer",
    )
    return {
        "owner": owner,
        "project": project,
        "member": member,
        "viewer": viewer,
    }


# ----------------------------------------------------------------------
# Role-permission matrix unit tests
# ----------------------------------------------------------------------


def test_owner_has_all_permissions() -> None:
    """Per PLAN.md M3: minimal owner/member permission model. Owner
    has full project authority."""
    assert permissions_for_role("owner") == frozenset(ALL_PERMISSIONS)


def test_member_has_operational_permissions_but_not_admin() -> None:
    member_perms = permissions_for_role("member")
    # Member can do operational things.
    assert "plan.propose" in member_perms
    assert "plan.approve" in member_perms
    assert "execution.start" in member_perms
    # Member cannot do admin things.
    assert "member.manage" not in member_perms
    assert "tool.manage" not in member_perms
    assert "secret.manage" not in member_perms
    assert "audit.view" not in member_perms


def test_viewer_has_read_only_permissions() -> None:
    viewer_perms = permissions_for_role("viewer")
    assert "project.view" in viewer_perms
    assert "plan.propose" not in viewer_perms
    assert "plan.approve" not in viewer_perms
    assert "member.manage" not in viewer_perms


def test_role_has_permission_helper() -> None:
    assert role_has_permission("owner", "secret.manage") is True
    assert role_has_permission("member", "secret.manage") is False
    assert role_has_permission("viewer", "project.view") is True


# ----------------------------------------------------------------------
# AuthorizationService.authorize — allow cases
# ----------------------------------------------------------------------


def test_owner_is_allowed_all_permissions(services, project_with_members) -> None:
    owner = project_with_members["owner"]
    project = project_with_members["project"]
    for permission in ALL_PERMISSIONS:
        decision = services.authorization.authorize(
            actor_id=owner.id,
            project_id=project.id,
            permission=permission,
        )
        assert decision.allowed, f"Owner should be allowed {permission}"


def test_member_is_allowed_operational_permissions(
    services, project_with_members
) -> None:
    member = project_with_members["member"]
    project = project_with_members["project"]
    for permission in permissions_for_role("member"):
        decision = services.authorization.authorize(
            actor_id=member.id,
            project_id=project.id,
            permission=permission,
        )
        assert decision.allowed, f"Member should be allowed {permission}"


def test_viewer_is_allowed_read_permissions(
    services, project_with_members
) -> None:
    viewer = project_with_members["viewer"]
    project = project_with_members["project"]
    decision = services.authorization.authorize(
        actor_id=viewer.id, project_id=project.id, permission="project.view"
    )
    assert decision.allowed
    assert decision.role == "viewer"
    assert decision.reason == "allowed"


# ----------------------------------------------------------------------
# AuthorizationService.authorize — deny cases
# ----------------------------------------------------------------------


def test_viewer_denied_plan_propose(services, project_with_members) -> None:
    """Per PLAN.md M3: 'Allow and deny cases for every current permission.'"""
    viewer = project_with_members["viewer"]
    project = project_with_members["project"]
    decision = services.authorization.authorize(
        actor_id=viewer.id, project_id=project.id, permission="plan.propose"
    )
    assert not decision.allowed
    assert decision.reason == "permission_denied"
    assert decision.role == "viewer"


def test_member_denied_secret_manage(services, project_with_members) -> None:
    member = project_with_members["member"]
    project = project_with_members["project"]
    decision = services.authorization.authorize(
        actor_id=member.id, project_id=project.id, permission="secret.manage"
    )
    assert not decision.allowed
    assert decision.reason == "permission_denied"


def test_member_denied_audit_view(services, project_with_members) -> None:
    member = project_with_members["member"]
    project = project_with_members["project"]
    decision = services.authorization.authorize(
        actor_id=member.id, project_id=project.id, permission="audit.view"
    )
    assert not decision.allowed
    assert decision.reason == "permission_denied"


def test_non_member_denied_all_permissions(
    services, project_with_members
) -> None:
    """Per zero-project-isolation-evidence: cross-project access is
    prevented at more than one appropriate layer. A non-member is
    denied at the authorization layer."""
    non_member = services.identity.create_user(display_name="Outsider")
    project = project_with_members["project"]
    for permission in ALL_PERMISSIONS:
        decision = services.authorization.authorize(
            actor_id=non_member.id,
            project_id=project.id,
            permission=permission,
        )
        assert not decision.allowed
        assert decision.reason == "not_member"


def test_nonexistent_user_denied(services, project_with_members) -> None:
    """Per zero-control-plane-trust §"Failure shapes teach the boundary":
    a non-existent user is denied with a typed reason, not a 500."""
    project = project_with_members["project"]
    decision = services.authorization.authorize(
        actor_id=UserId("zu_nonexistent"),
        project_id=project.id,
        permission="project.view",
    )
    assert not decision.allowed
    assert decision.reason == "user_not_found"


# ----------------------------------------------------------------------
# AuthorizationService.require_permission — raises on deny
# ----------------------------------------------------------------------


def test_require_permission_raises_on_deny(services, project_with_members) -> None:
    viewer = project_with_members["viewer"]
    project = project_with_members["project"]
    with pytest.raises(AuthorizationError) as exc_info:
        services.authorization.require_permission(
            actor_id=viewer.id,
            project_id=project.id,
            permission="plan.approve",
        )
    assert exc_info.value.decision.reason == "permission_denied"


def test_require_permission_returns_decision_on_allow(
    services, project_with_members
) -> None:
    owner = project_with_members["owner"]
    project = project_with_members["project"]
    decision = services.authorization.require_permission(
        actor_id=owner.id,
        project_id=project.id,
        permission="plan.approve",
    )
    assert decision.allowed


# ----------------------------------------------------------------------
# Revocation takes effect immediately
# ----------------------------------------------------------------------


def test_revocation_takes_effect_immediately(
    services, project_with_members
) -> None:
    """Per PLAN.md M3: 'Revocation takes effect through all implemented
    interfaces.' Removing a member's membership immediately denies
    all permissions."""
    member = project_with_members["member"]
    project = project_with_members["project"]
    owner = project_with_members["owner"]

    # Member can approve plans before removal.
    decision_before = services.authorization.authorize(
        actor_id=member.id, project_id=project.id, permission="plan.approve"
    )
    assert decision_before.allowed

    # Owner removes the member.
    services.identity.remove_member(
        project_id=project.id,
        actor_id=owner.id,
        member_id=member.id,
    )

    # Member is now denied.
    decision_after = services.authorization.authorize(
        actor_id=member.id, project_id=project.id, permission="plan.approve"
    )
    assert not decision_after.allowed
    assert decision_after.reason == "not_member"


# ----------------------------------------------------------------------
# Denied decisions are audited
# ----------------------------------------------------------------------


def test_denied_decision_is_audited(services, project_with_members) -> None:
    """Per zero-control-plane-trust §"Audit is evidence": denied
    authorization is recorded as an audit event so the denial is
    observable."""
    viewer = project_with_members["viewer"]
    project = project_with_members["project"]
    services.authorization.authorize(
        actor_id=viewer.id, project_id=project.id, permission="plan.approve"
    )
    events = services.audit.list_for_project(project_id=project.id, actor_id=project.owner_user_id, limit=50)
    denial_events = [
        e
        for e in events
        if e.operation == "authz.plan.approve" and e.result == "denied"
    ]
    assert len(denial_events) >= 1
    event = denial_events[0]
    assert event.actor_id == viewer.id
    assert "permission_denied" in (event.redacted_summary or "")


# ----------------------------------------------------------------------
# Cross-project access denied
# ----------------------------------------------------------------------


def test_cross_project_access_denied(services) -> None:
    """Per PLAN.md M2 acceptance: 'Two isolated projects with overlapping
    human names and external usernames cannot access or mutate each
    other's records through any implemented path.'

    A member of project A must not be able to access project B.
    """
    # Create two projects with overlapping member names.
    owner_a = services.identity.create_user(display_name="Alice")
    owner_b = services.identity.create_user(display_name="Alice")  # same name
    services.identity.create_project(
        owner_id=owner_a.id, name="Project Alpha"
    )
    project_b = services.identity.create_project(
        owner_id=owner_b.id, name="Project Alpha"  # same name
    )
    # owner_a is NOT a member of project_b.
    decision = services.authorization.authorize(
        actor_id=owner_a.id,
        project_id=project_b.id,
        permission="project.view",
    )
    assert not decision.allowed
    assert decision.reason == "not_member"
