"""Identity service tests — users, projects, memberships, external identities.

Per ``zero-control-plane-trust`` §"Identity is a link, not a name":
display names are not authority. Two users may have identical display
names; they remain distinct identities.

Per ``zero-project-isolation-evidence`` §"Adversarial pairs reveal
mistakes": isolation tests are strongest when two projects
deliberately look alike (same filenames, same agent type labels, same
plan titles, same external usernames where platform permits).
"""

from __future__ import annotations

import pytest

from zero.app.services import Services, build_services
from zero.config import Settings
from zero.domain.identity import (
    DuplicateExternalIdentityError,
    ExternalIdentityNotVerifiedError,
    MembershipAlreadyExistsError,
    MembershipNotFoundError,
    ProjectId,
    ProjectNotFoundError,
    UserId,
    UserNotFoundError,
)
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations

# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture
def services(test_settings: Settings) -> Services:
    database = Database(test_settings)
    apply_migrations(database)
    return build_services(test_settings, database)


# ----------------------------------------------------------------------
# User creation
# ----------------------------------------------------------------------


def test_create_user_returns_server_issued_id(services) -> None:
    user = services.identity.create_user(display_name="Alice")
    assert user.id.value.startswith("zu_")
    assert user.display_name == "Alice"
    assert user.status == "active"
    assert user.created_at


def test_create_user_records_audit_event(services) -> None:
    user = services.identity.create_user(display_name="Bob")
    events = services.audit.list_for_actor(actor_id=user.id, limit=10)
    assert len(events) >= 1
    create_event = next(
        (e for e in events if e.operation == "user.create"), None
    )
    assert create_event is not None
    assert create_event.target_id == user.id.value
    assert create_event.result == "success"


def test_two_users_with_same_display_name_are_distinct(services) -> None:
    """Per zero-control-plane-trust: display names are not authority.

    Two users with identical display names must remain distinct
    identities with different server-issued IDs.
    """
    user_a = services.identity.create_user(display_name="Chris")
    user_b = services.identity.create_user(display_name="Chris")
    assert user_a.id != user_b.id
    assert user_a.display_name == user_b.display_name


def test_create_user_rejects_empty_display_name(services) -> None:
    with pytest.raises(ValueError, match="display_name"):
        services.identity.create_user(display_name="")
    with pytest.raises(ValueError, match="display_name"):
        services.identity.create_user(display_name="   ")


# ----------------------------------------------------------------------
# Project creation
# ----------------------------------------------------------------------


def test_create_project_returns_server_issued_id(services) -> None:
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(
        owner_id=owner.id, name="Project A"
    )
    assert project.id.value.startswith("p_")
    assert project.name == "Project A"
    assert project.owner_user_id == owner.id


def test_create_project_automatically_adds_owner_membership(services) -> None:
    """Per zero-control-plane-trust §"Atomicity follows the business
    fact": creating a project + creating the owner membership is one
    atomic business fact.
    """
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(
        owner_id=owner.id, name="Project A"
    )
    membership = services.identity._identity_repo.get_membership(
        project.id, owner.id
    )
    assert membership is not None
    assert membership.role == "owner"


def test_create_project_records_audit_event(services) -> None:
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(
        owner_id=owner.id, name="Project A"
    )
    events = services.audit.list_for_project(project_id=project.id, actor_id=project.owner_user_id, limit=10)
    create_event = next(
        (e for e in events if e.operation == "project.create"), None
    )
    assert create_event is not None
    assert create_event.target_id == project.id.value


def test_create_project_rejects_nonexistent_owner(services) -> None:
    with pytest.raises(UserNotFoundError):
        services.identity.create_project(
            owner_id=UserId("zu_nonexistent"), name="Project X"
        )


def test_get_project_raises_for_nonexistent(services) -> None:
    with pytest.raises(ProjectNotFoundError):
        services.identity.get_project(ProjectId("p_nonexistent"))


# ----------------------------------------------------------------------
# Memberships
# ----------------------------------------------------------------------


def test_add_member_assigns_role(services) -> None:
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(
        owner_id=owner.id, name="Project A"
    )
    member = services.identity.create_user(display_name="Member")
    membership = services.identity.add_member(
        project_id=project.id,
        actor_id=owner.id,
        member_id=member.id,
        role="member",
    )
    assert membership.role == "member"
    assert membership.user_id == member.id
    assert membership.project_id == project.id


def test_add_member_rejects_duplicate(services) -> None:
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(
        owner_id=owner.id, name="Project A"
    )
    member = services.identity.create_user(display_name="Member")
    services.identity.add_member(
        project_id=project.id,
        actor_id=owner.id,
        member_id=member.id,
        role="member",
    )
    with pytest.raises(MembershipAlreadyExistsError):
        services.identity.add_member(
            project_id=project.id,
            actor_id=owner.id,
            member_id=member.id,
            role="viewer",
        )


def test_remove_member_succeeds(services) -> None:
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(
        owner_id=owner.id, name="Project A"
    )
    member = services.identity.create_user(display_name="Member")
    services.identity.add_member(
        project_id=project.id,
        actor_id=owner.id,
        member_id=member.id,
        role="member",
    )
    services.identity.remove_member(
        project_id=project.id,
        actor_id=owner.id,
        member_id=member.id,
    )
    # Membership should be gone.
    scope = services.identity.resolve_scope(project.id, member.id)
    assert not scope.is_member


def test_remove_member_rejects_nonexistent(services) -> None:
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(
        owner_id=owner.id, name="Project A"
    )
    member = services.identity.create_user(display_name="Member")
    with pytest.raises(MembershipNotFoundError):
        services.identity.remove_member(
            project_id=project.id,
            actor_id=owner.id,
            member_id=member.id,
        )


def test_remove_member_rejects_removing_owner(services) -> None:
    """A project owner cannot be removed (would orphan the project)."""
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(
        owner_id=owner.id, name="Project A"
    )
    with pytest.raises(ValueError, match="owner"):
        services.identity.remove_member(
            project_id=project.id,
            actor_id=owner.id,
            member_id=owner.id,
        )


def test_list_members_returns_all_members(services) -> None:
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(
        owner_id=owner.id, name="Project A"
    )
    m1 = services.identity.create_user(display_name="M1")
    m2 = services.identity.create_user(display_name="M2")
    services.identity.add_member(
        project_id=project.id,
        actor_id=owner.id,
        member_id=m1.id,
        role="member",
    )
    services.identity.add_member(
        project_id=project.id,
        actor_id=owner.id,
        member_id=m2.id,
        role="viewer",
    )
    members = services.identity.list_members(project.id, owner.id)
    user_ids = {m.user_id for m in members}
    assert {owner.id, m1.id, m2.id} == user_ids


# ----------------------------------------------------------------------
# External identities
# ----------------------------------------------------------------------


def test_link_external_identity_returns_link_record(services) -> None:
    user = services.identity.create_user(display_name="Alice")
    identity = services.identity.link_external_identity(
        user_id=user.id,
        platform="telegram",
        external_id="7086634092",
        external_username="alice",
        verified=False,
    )
    assert identity.user_id == user.id
    assert identity.platform == "telegram"
    assert identity.external_id == "7086634092"
    assert identity.verified_at is None  # not yet verified


def test_link_external_identity_rejects_duplicate(services) -> None:
    """Per zero-control-plane-trust: same Telegram user ID may be linked
    to at most one Zero User per platform."""
    user_a = services.identity.create_user(display_name="Alice")
    user_b = services.identity.create_user(display_name="Bob")
    services.identity.link_external_identity(
        user_id=user_a.id,
        platform="telegram",
        external_id="7086634092",
    )
    with pytest.raises(DuplicateExternalIdentityError):
        services.identity.link_external_identity(
            user_id=user_b.id,
            platform="telegram",
            external_id="7086634092",
        )


def test_resolve_user_by_external_identity_requires_verification(
    services,
) -> None:
    """Per zero-control-plane-trust §"Identity is a link, not a name":
    only verified links can be used for authentication."""
    user = services.identity.create_user(display_name="Alice")
    services.identity.link_external_identity(
        user_id=user.id,
        platform="telegram",
        external_id="7086634092",
        verified=False,
    )
    with pytest.raises(ExternalIdentityNotVerifiedError):
        services.identity.resolve_user_by_external_identity(
            platform="telegram", external_id="7086634092"
        )


def test_verify_external_identity_enables_resolution(services) -> None:
    user = services.identity.create_user(display_name="Alice")
    services.identity.link_external_identity(
        user_id=user.id,
        platform="telegram",
        external_id="7086634092",
        verified=False,
    )
    services.identity.verify_external_identity(
        platform="telegram", external_id="7086634092"
    )
    resolved = services.identity.resolve_user_by_external_identity(
        platform="telegram", external_id="7086634092"
    )
    assert resolved.id == user.id


def test_external_username_is_not_authority(services) -> None:
    """Two users may have the same external username; they remain
    distinct because authority follows the server-issued ID, not the
    platform username.
    """
    user_a = services.identity.create_user(display_name="Alice")
    user_b = services.identity.create_user(display_name="Bob")
    # Both link Telegram accounts with the same username (rare but
    # possible: usernames can be reclaimed).
    services.identity.link_external_identity(
        user_id=user_a.id,
        platform="telegram",
        external_id="111",
        external_username="shared_name",
        verified=True,
    )
    services.identity.link_external_identity(
        user_id=user_b.id,
        platform="telegram",
        external_id="222",
        external_username="shared_name",
        verified=True,
    )
    # Resolving by external_id (the stable platform identifier) gives
    # the correct user in each case.
    assert services.identity.resolve_user_by_external_identity(
        platform="telegram", external_id="111"
    ).id == user_a.id
    assert services.identity.resolve_user_by_external_identity(
        platform="telegram", external_id="222"
    ).id == user_b.id


# ----------------------------------------------------------------------
# 64-bit Telegram ID round-trip (per Telegram findings §8)
# ----------------------------------------------------------------------


def test_external_id_preserves_64_bit_values(services) -> None:
    """Per zero-interface-adapter-model Telegram findings §8: Telegram
    documents that user and chat IDs can exceed 32 significant bits
    and fit within 52 significant bits; a signed 64-bit representation
    is safe. We store external_id as TEXT to preserve exact values.
    """
    user = services.identity.create_user(display_name="Alice")
    big_id = "9223372036854775807"  # max signed 64-bit
    services.identity.link_external_identity(
        user_id=user.id,
        platform="telegram",
        external_id=big_id,
        verified=True,
    )
    resolved = services.identity.resolve_user_by_external_identity(
        platform="telegram", external_id=big_id
    )
    assert resolved.id == user.id
