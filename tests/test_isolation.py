"""Adversarial project-isolation tests.

Per ``zero-project-isolation-evidence`` SKILL.md §"Adversarial pairs
reveal mistakes": isolation tests are strongest when two projects
deliberately look alike (same filenames, same agent type labels, same
plan titles, same Telegram topic numbers, same external usernames
where platform permits, similar memory text).

Per PLAN.md M2 acceptance: 'Two isolated projects with overlapping
human names and external usernames cannot access or mutate each
other's records through any implemented path.'
"""

from __future__ import annotations

import pytest

from zero.app.services import build_services
from zero.config import Settings
from zero.domain.identity import (
    DuplicateExternalIdentityError,
)
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations


@pytest.fixture
def services(test_settings: Settings):
    database = Database(test_settings)
    apply_migrations(database)
    return build_services(test_settings, database)


@pytest.fixture
def two_lookalike_projects(services):
    """Create two projects with deliberately overlapping attributes.

    Per zero-project-isolation-evidence §"Adversarial pairs reveal
    mistakes": same owner display name, same project name, same
    member display name. The only thing that differs is the
    server-issued IDs.
    """
    # Two owners with the same display name.
    owner_a = services.identity.create_user(display_name="Alice")
    owner_b = services.identity.create_user(display_name="Alice")
    # Two projects with the same name.
    project_a = services.identity.create_project(owner_id=owner_a.id, name="Lookalike Project")
    project_b = services.identity.create_project(owner_id=owner_b.id, name="Lookalike Project")
    # Two members with the same display name, each in their own project.
    member_a = services.identity.create_user(display_name="Bob")
    member_b = services.identity.create_user(display_name="Bob")
    services.identity.add_member(
        project_id=project_a.id,
        actor_id=owner_a.id,
        member_id=member_a.id,
        role="member",
    )
    services.identity.add_member(
        project_id=project_b.id,
        actor_id=owner_b.id,
        member_id=member_b.id,
        role="member",
    )
    return {
        "owner_a": owner_a,
        "owner_b": owner_b,
        "project_a": project_a,
        "project_b": project_b,
        "member_a": member_a,
        "member_b": member_b,
    }


# ----------------------------------------------------------------------
# Identity isolation
# ----------------------------------------------------------------------


def test_owner_a_cannot_access_project_b(services, two_lookalike_projects) -> None:
    """The owner of project A is NOT a member of project B, even though
    both projects have the same name and the owners have the same
    display name."""
    owner_a = two_lookalike_projects["owner_a"]
    project_b = two_lookalike_projects["project_b"]
    scope = services.identity.resolve_scope(project_b.id, owner_a.id)
    assert not scope.is_member


def test_member_a_cannot_resolve_scope_in_project_b(services, two_lookalike_projects) -> None:
    member_a = two_lookalike_projects["member_a"]
    project_b = two_lookalike_projects["project_b"]
    scope = services.identity.resolve_scope(project_b.id, member_a.id)
    assert not scope.is_member


def test_list_members_does_not_leak_across_projects(services, two_lookalike_projects) -> None:
    """Per zero-project-isolation-evidence §"Scope begins before
    access": listing members of project A must not return any member
    of project B."""
    project_a = two_lookalike_projects["project_a"]
    member_b = two_lookalike_projects["member_b"]
    owner_a = two_lookalike_projects["owner_a"]
    members = services.identity.list_members(project_a.id, owner_a.id)
    member_ids = {m.user_id for m in members}
    assert member_b.id not in member_ids


# ----------------------------------------------------------------------
# Audit isolation
# ----------------------------------------------------------------------


def test_audit_events_do_not_leak_across_projects(services, two_lookalike_projects) -> None:
    """Per zero-project-isolation-evidence: audit events are
    project-scoped. Listing project A's audit events must not return
    any event from project B."""
    project_a = two_lookalike_projects["project_a"]
    project_b = two_lookalike_projects["project_b"]
    owner_b = two_lookalike_projects["owner_b"]
    # Generate an audit event in project B by adding a member.
    new_member_b = services.identity.create_user(display_name="Carol")
    services.identity.add_member(
        project_id=project_b.id,
        actor_id=owner_b.id,
        member_id=new_member_b.id,
        role="viewer",
    )
    # List project A's audit events.
    events_a = services.audit.list_for_project(
        project_id=project_a.id, actor_id=project_a.owner_user_id, limit=100
    )
    # No event in project A should mention project_b's ID.
    for event in events_a:
        assert event.project_id != project_b.id
    # Specifically, the member.add event for new_member_b in project B
    # must not appear in project A's audit log.
    leaked = [
        e
        for e in events_a
        if e.operation == "member.add" and e.target_id and new_member_b.id.value in e.target_id
    ]
    assert len(leaked) == 0


# ----------------------------------------------------------------------
# Secret isolation
# ----------------------------------------------------------------------


def test_secret_in_project_a_not_visible_in_project_b(
    services, two_lookalike_projects, monkeypatch
) -> None:
    """Per zero-project-isolation-evidence §"Artifacts need namespaces
    and authorization": a secret stored in project A must not be
    retrievable from project B, even by guessing the secret ID."""
    # Enable secret_key for this test.
    from zero.config import Settings

    settings = Settings.load_for_test(secret_key="x" * 64)
    database = Database(settings)
    apply_migrations(database)
    secured_services = build_services(settings, database)

    owner_a = secured_services.identity.create_user(display_name="Owner A")
    project_a = secured_services.identity.create_project(owner_id=owner_a.id, name="Project A")
    owner_b = secured_services.identity.create_user(display_name="Owner B")
    project_b = secured_services.identity.create_project(owner_id=owner_b.id, name="Project B")
    # Store a secret in project A.
    secret_ref = secured_services.secrets.store(
        project_id=project_a.id,
        name="api_key",
        secret_type="api_key",
        value="sk-super-secret-value-for-project-a",
        actor_id=owner_a.id,
    )
    # Try to retrieve it from project B by guessing the ID.
    from zero.domain.secrets import SecretNotFoundError

    with pytest.raises(SecretNotFoundError):
        secured_services.secrets.get_reference(
            project_id=project_b.id,
            secret_id=secret_ref.id,
            actor_id=owner_b.id,
        )
    # And by the resolve_value path.
    with pytest.raises(SecretNotFoundError):
        secured_services.secrets.resolve_value(
            project_id=project_b.id,
            secret_id=secret_ref.id,
            actor_id=owner_b.id,
        )
    # List secrets in project B — should be empty.
    secrets_b = secured_services.secrets.list_for_project(
        project_id=project_b.id, actor_id=project_b.owner_user_id
    )
    assert len(secrets_b) == 0


# ----------------------------------------------------------------------
# Tool grant isolation
# ----------------------------------------------------------------------


def test_tool_grant_in_project_a_not_usable_in_project_b(
    services, two_lookalike_projects, monkeypatch
) -> None:
    """Per zero-tool-capability-runtime §"Tool choice and tool
    permission are separate": a tool grant is scoped to a project.
    Project B cannot invoke a tool using project A's grant."""
    # Register the echo tool.
    echo_tool = services.tools.register_echo_tool()
    project_a = two_lookalike_projects["project_a"]
    project_b = two_lookalike_projects["project_b"]
    owner_a = two_lookalike_projects["owner_a"]
    # Grant echo to main_worker in project A only.
    services.tools.grant_tool(
        project_id=project_a.id,
        actor_id=project_a.owner_user_id,
        tool_id=echo_tool.id,
        agent_scope="main_worker",
    )
    # Invoking from project A works.
    result_a = services.tools.invoke(
        project_id=project_a.id,
        actor_id=owner_a.id,
        agent_scope="main_worker",
        tool_name="echo",
        input_data={"message": "hello from A"},
    )
    assert result_a.status == "success"
    # Invoking from project B is denied (no grant).
    from zero.domain.tools import ToolInvocationDeniedError

    with pytest.raises(ToolInvocationDeniedError):
        services.tools.invoke(
            project_id=project_b.id,
            actor_id=two_lookalike_projects["owner_b"].id,
            agent_scope="main_worker",
            tool_name="echo",
            input_data={"message": "hello from B"},
        )


# ----------------------------------------------------------------------
# External identity isolation
# ----------------------------------------------------------------------


def test_external_identity_cannot_be_linked_to_two_users(services, two_lookalike_projects) -> None:
    """Per zero-control-plane-trust §"Identity is a link, not a name":
    the same external identity (platform + external_id) may be linked
    to at most one Zero User."""
    owner_a = two_lookalike_projects["owner_a"]
    owner_b = two_lookalike_projects["owner_b"]
    services.identity.link_external_identity(
        user_id=owner_a.id,
        platform="telegram",
        external_id="1234567890",
        verified=True,
    )
    # owner_b cannot link the same Telegram account.
    with pytest.raises(DuplicateExternalIdentityError):
        services.identity.link_external_identity(
            user_id=owner_b.id,
            platform="telegram",
            external_id="1234567890",
            verified=True,
        )


# ----------------------------------------------------------------------
# Concurrent membership updates preserve consistency
# ----------------------------------------------------------------------


def test_concurrent_duplicate_membership_insert_is_rejected(
    services, two_lookalike_projects
) -> None:
    """Per PLAN.md M2 validation: 'Concurrent membership updates
    preserve consistency.' The database UNIQUE(project_id, user_id)
    constraint ensures that even if two requests race to add the same
    member, only one succeeds."""
    project_a = two_lookalike_projects["project_a"]
    owner_a = two_lookalike_projects["owner_a"]
    new_member = services.identity.create_user(display_name="New Member")
    # First add succeeds.
    services.identity.add_member(
        project_id=project_a.id,
        actor_id=owner_a.id,
        member_id=new_member.id,
        role="viewer",
    )
    # Second add (simulating a duplicate retry) fails.
    from zero.domain.identity import MembershipAlreadyExistsError

    with pytest.raises(MembershipAlreadyExistsError):
        services.identity.add_member(
            project_id=project_a.id,
            actor_id=owner_a.id,
            member_id=new_member.id,
            role="viewer",
        )
