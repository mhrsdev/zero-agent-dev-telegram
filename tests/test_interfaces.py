"""Interface adapter tests — covers all M13 validation gates.

Per PLAN.md M13 validation:
- Unknown and unlinked users cannot act.
- Disabled topics/channels produce no planning or execution side effects.
- Duplicate webhook/update delivery is idempotent.
- Edited or stale approval messages cannot approve a newer revision.
- Website and messaging actions observe the same durable state.
- Platform outage does not lose backend execution state.
"""

from __future__ import annotations

import pytest

from zero.app.services import build_services
from zero.config import Settings
from zero.domain.interfaces import (
    NormalizedEvent,
)
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations


@pytest.fixture
def services(test_settings: Settings):
    database = Database(test_settings)
    apply_migrations(database)
    return build_services(test_settings, database)


@pytest.fixture
def project_with_owner_and_binding(services):
    """Create a project, owner, verified Telegram identity, and enabled
    binding."""
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(
        owner_id=owner.id, name="Project A"
    )
    # Link the owner's Telegram identity (verified).
    services.identity.link_external_identity(
        user_id=owner.id,
        platform="telegram",
        external_id="7086634092",
        external_username="owner",
        verified=True,
    )
    # Create an enabled binding for a Telegram chat + topic.
    binding = services.interfaces.create_binding(
        project_id=project.id,
        actor_id=owner.id,
        platform="telegram",
        chat_id="100",
        topic_id="7",
        is_enabled=True,
    )
    return owner, project, binding


# ----------------------------------------------------------------------
# Scope management
# ----------------------------------------------------------------------


def test_create_binding_not_enabled_by_default(services) -> None:
    """Per TELEGRAM_FINDINGS: General is NOT enabled by default."""
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(
        owner_id=owner.id, name="P"
    )
    binding = services.interfaces.create_binding(
        project_id=project.id, actor_id=owner.id,
        platform="telegram", chat_id="100", topic_id=None,
    )
    assert binding.is_enabled is False  # NOT enabled by default


def test_enable_binding(services, project_with_owner_and_binding) -> None:
    owner, project, binding = project_with_owner_and_binding
    assert binding.is_enabled is True  # fixture enables it
    # Disable.
    services.interfaces.disable_binding(
        project_id=project.id, binding_id=binding.id, actor_id=owner.id
    )
    binding = services.interfaces.get_binding("telegram", "100", "7")
    assert binding.is_enabled is False
    # Re-enable.
    services.interfaces.enable_binding(
        project_id=project.id, binding_id=binding.id, actor_id=owner.id
    )
    binding = services.interfaces.get_binding("telegram", "100", "7")
    assert binding.is_enabled is True


# ----------------------------------------------------------------------
# Unknown and unlinked users cannot act
# ----------------------------------------------------------------------


def test_unlinked_user_cannot_act(services, project_with_owner_and_binding) -> None:
    """Per PLAN.md M13: 'Unknown and unlinked users cannot act.'"""
    _owner, project, _binding = project_with_owner_and_binding
    # Send an event from an unlinked Telegram user.
    event = NormalizedEvent(
        platform="telegram",
        external_event_id="update_1",
        external_actor_id="9999999999",  # not linked
        chat_id="100",
        topic_id="7",
        event_kind="message",
        content="Hello from unlinked user",
    )
    result = services.interfaces.process_inbound_event(event)
    assert result.processing_result == "ignored_unlinked"
    # No conversation event was ingested.
    events = services.plans.list_conversation_events(
        project_id=project.id, limit=10
    )
    assert len(events) == 0


# ----------------------------------------------------------------------
# Disabled topics produce no side effects
# ----------------------------------------------------------------------


def test_disabled_scope_produces_no_side_effects(
    services, project_with_owner_and_binding
) -> None:
    """Per PLAN.md M13: 'Disabled topics/channels produce no planning
    or execution side effects.'"""
    owner, project, binding = project_with_owner_and_binding
    # Disable the binding.
    services.interfaces.disable_binding(
        project_id=project.id, binding_id=binding.id, actor_id=owner.id
    )
    # Send an event from the owner (linked, verified).
    event = NormalizedEvent(
        platform="telegram",
        external_event_id="update_1",
        external_actor_id="7086634092",
        chat_id="100",
        topic_id="7",
        event_kind="message",
        content="Hello from owner in disabled scope",
    )
    result = services.interfaces.process_inbound_event(event)
    assert result.processing_result == "ignored_disabled"
    # No conversation event was ingested.
    events = services.plans.list_conversation_events(
        project_id=project.id, limit=10
    )
    assert len(events) == 0


# ----------------------------------------------------------------------
# Linked user can send messages (normal conversation doesn't execute)
# ----------------------------------------------------------------------


def test_linked_user_message_ingested_as_conversation(
    services, project_with_owner_and_binding
) -> None:
    """Per PLAN.md M13: 'Normal conversation does not become execution.'"""
    _owner, project, _binding = project_with_owner_and_binding
    event = NormalizedEvent(
        platform="telegram",
        external_event_id="update_1",
        external_actor_id="7086634092",
        chat_id="100",
        topic_id="7",
        event_kind="message",
        content="Let's add a login page.",
    )
    result = services.interfaces.process_inbound_event(event)
    assert result.processing_result == "processed"
    # A conversation event was ingested.
    events = services.plans.list_conversation_events(
        project_id=project.id, limit=10
    )
    assert len(events) == 1
    assert events[0].content == "Let's add a login page."
    # No plan was created (normal conversation doesn't execute).
    plans = services.plans.list_plans_for_project(project.id)
    assert len(plans) == 0


# ----------------------------------------------------------------------
# Duplicate event delivery is idempotent
# ----------------------------------------------------------------------


def test_duplicate_event_delivery_is_idempotent(
    services, project_with_owner_and_binding
) -> None:
    """Per PLAN.md M13: 'Duplicate webhook/update delivery is
    idempotent.'"""
    _owner, project, _binding = project_with_owner_and_binding
    event = NormalizedEvent(
        platform="telegram",
        external_event_id="update_dup_1",
        external_actor_id="7086634092",
        chat_id="100",
        topic_id="7",
        event_kind="message",
        content="Hello",
    )
    # First delivery.
    result1 = services.interfaces.process_inbound_event(event)
    assert result1.processing_result == "processed"
    # Duplicate delivery.
    result2 = services.interfaces.process_inbound_event(event)
    assert result2.processing_result == "processed"
    assert "duplicate" in (result2.processing_detail or "")
    # Only one conversation event was ingested.
    events = services.plans.list_conversation_events(
        project_id=project.id, limit=10
    )
    assert len(events) == 1


# ----------------------------------------------------------------------
# Callback tokens: stale revision defense
# ----------------------------------------------------------------------


def test_callback_approves_plan(services, project_with_owner_and_binding) -> None:
    """Per PLAN.md M13 acceptance: 'An authorized user can propose and
    approve one plan from an explicitly enabled messaging scope.'"""
    owner, project, _binding = project_with_owner_and_binding
    # Ingest a conversation event (via the interface).
    msg_event = NormalizedEvent(
        platform="telegram",
        external_event_id="update_msg_1",
        external_actor_id="7086634092",
        chat_id="100",
        topic_id="7",
        event_kind="message",
        content="Add a login page.",
    )
    services.interfaces.process_inbound_event(msg_event)
    # Create a plan and propose a revision (via the website/API).
    conv_events = services.plans.list_conversation_events(
        project_id=project.id, limit=10
    )
    plan = services.plans.create_plan(
        project_id=project.id, actor_id=owner.id
    )
    from zero.domain.plans import PlanRevisionContent
    content = PlanRevisionContent(
        objective="Add a login page", scope=(), constraints=(),
        acceptance_criteria=("Login form renders",),
        risks=(), unresolved_questions=(),
        source_event_ids=(conv_events[0].id,),
    )
    services.plans.propose_revision(
        plan_id=plan.id, actor_id=owner.id, content=content
    )
    # Create a callback token for approval.
    token = services.interfaces.create_callback_token(
        project_id=project.id, plan_id=plan.id,
        revision_number=1, action="approve", created_by=owner.id,
    )
    # Send a callback query from the owner.
    callback_event = NormalizedEvent(
        platform="telegram",
        external_event_id="update_cb_1",
        external_actor_id="7086634092",
        chat_id="100",
        topic_id="7",
        event_kind="callback_query",
        content="[approve callback]",
        callback_token=token.id.value,
    )
    result = services.interfaces.process_inbound_event(callback_event)
    assert result.processing_result == "processed"
    # The plan should now be approved.
    plan = services.plans.get_plan(plan.id)
    assert plan.current_state == "approved"


def test_stale_callback_cannot_approve_newer_revision(
    services, project_with_owner_and_binding
) -> None:
    """Per PLAN.md M13: 'Edited or stale approval messages cannot
    approve a newer revision.'"""
    owner, project, _binding = project_with_owner_and_binding
    # Ingest a conversation event.
    msg_event = NormalizedEvent(
        platform="telegram",
        external_event_id="update_msg_2",
        external_actor_id="7086634092",
        chat_id="100",
        topic_id="7",
        event_kind="message",
        content="Add a login page.",
    )
    services.interfaces.process_inbound_event(msg_event)
    conv_events = services.plans.list_conversation_events(
        project_id=project.id, limit=10
    )
    plan = services.plans.create_plan(
        project_id=project.id, actor_id=owner.id
    )
    from zero.domain.plans import PlanRevisionContent
    content = PlanRevisionContent(
        objective="V1", scope=(), constraints=(),
        acceptance_criteria=("Works",),
        risks=(), unresolved_questions=(),
        source_event_ids=(conv_events[0].id,),
    )
    services.plans.propose_revision(
        plan_id=plan.id, actor_id=owner.id, content=content
    )
    # Create a callback token for revision 1.
    token = services.interfaces.create_callback_token(
        project_id=project.id, plan_id=plan.id,
        revision_number=1, action="approve", created_by=owner.id,
    )
    # Edit: propose revision 2.
    content2 = PlanRevisionContent(
        objective="V2", scope=(), constraints=(),
        acceptance_criteria=("Works better",),
        risks=(), unresolved_questions=(),
        source_event_ids=(conv_events[0].id,),
    )
    services.plans.propose_revision(
        plan_id=plan.id, actor_id=owner.id, content=content2
    )
    # Now try to use the callback for revision 1 (stale).
    callback_event = NormalizedEvent(
        platform="telegram",
        external_event_id="update_cb_stale",
        external_actor_id="7086634092",
        chat_id="100",
        topic_id="7",
        event_kind="callback_query",
        content="[approve stale callback]",
        callback_token=token.id.value,
    )
    result = services.interfaces.process_inbound_event(callback_event)
    assert result.processing_result == "denied"
    assert "stale" in (result.processing_detail or "").lower()
    # The plan is still in 'proposed' state (not approved).
    plan = services.plans.get_plan(plan.id)
    assert plan.current_state == "proposed"


def test_callback_token_used_twice_is_idempotent(
    services, project_with_owner_and_binding
) -> None:
    """Per PLAN.md M13: 'Duplicate webhook/update delivery is
    idempotent.' A duplicate callback should not approve twice."""
    owner, project, _binding = project_with_owner_and_binding
    # Setup: ingest event, create plan, propose, create token.
    msg_event = NormalizedEvent(
        platform="telegram",
        external_event_id="update_msg_3",
        external_actor_id="7086634092",
        chat_id="100",
        topic_id="7",
        event_kind="message",
        content="Add a feature.",
    )
    services.interfaces.process_inbound_event(msg_event)
    conv_events = services.plans.list_conversation_events(
        project_id=project.id, limit=10
    )
    plan = services.plans.create_plan(
        project_id=project.id, actor_id=owner.id
    )
    from zero.domain.plans import PlanRevisionContent
    content = PlanRevisionContent(
        objective="Add a feature", scope=(), constraints=(),
        acceptance_criteria=("Works",),
        risks=(), unresolved_questions=(),
        source_event_ids=(conv_events[0].id,),
    )
    services.plans.propose_revision(
        plan_id=plan.id, actor_id=owner.id, content=content
    )
    token = services.interfaces.create_callback_token(
        project_id=project.id, plan_id=plan.id,
        revision_number=1, action="approve", created_by=owner.id,
    )
    # First callback.
    cb1 = NormalizedEvent(
        platform="telegram", external_event_id="update_cb_2a",
        external_actor_id="7086634092", chat_id="100", topic_id="7",
        event_kind="callback_query", content="[approve]",
        callback_token=token.id.value,
    )
    result1 = services.interfaces.process_inbound_event(cb1)
    assert result1.processing_result == "processed"
    # Duplicate callback (different update_id, same token).
    cb2 = NormalizedEvent(
        platform="telegram", external_event_id="update_cb_2b",
        external_actor_id="7086634092", chat_id="100", topic_id="7",
        event_kind="callback_query", content="[approve]",
        callback_token=token.id.value,
    )
    result2 = services.interfaces.process_inbound_event(cb2)
    assert result2.processing_result == "processed"
    assert "already used" in (result2.processing_detail or "")
    # Only one handoff exists (plan was approved once).
    handoffs = services.plans.list_handoffs_for_project(project.id)
    assert len(handoffs) == 1


# ----------------------------------------------------------------------
# Website and messaging observe same durable state
# ----------------------------------------------------------------------


def test_website_and_messaging_observe_same_state(
    services, project_with_owner_and_binding
) -> None:
    """Per PLAN.md M13: 'Website and messaging actions observe the
    same durable state.'"""
    _owner, project, _binding = project_with_owner_and_binding
    # Ingest a message via the Telegram adapter.
    msg_event = NormalizedEvent(
        platform="telegram",
        external_event_id="update_same_1",
        external_actor_id="7086634092",
        chat_id="100",
        topic_id="7",
        event_kind="message",
        content="Shared message.",
    )
    services.interfaces.process_inbound_event(msg_event)
    # The website (via the plan service) sees the same conversation event.
    events = services.plans.list_conversation_events(
        project_id=project.id, limit=10
    )
    assert len(events) == 1
    assert events[0].content == "Shared message."
    assert events[0].source == "telegram"


# ----------------------------------------------------------------------
# Platform outage does not lose backend state
# ----------------------------------------------------------------------


def test_platform_outage_does_not_lose_backend_state(
    services, project_with_owner_and_binding
) -> None:
    """Per PLAN.md M13: 'Platform outage does not lose backend
    execution state.'"""
    owner, project, binding = project_with_owner_and_binding
    # Ingest a message.
    msg_event = NormalizedEvent(
        platform="telegram",
        external_event_id="update_outage_1",
        external_actor_id="7086634092",
        chat_id="100",
        topic_id="7",
        event_kind="message",
        content="Before outage.",
    )
    services.interfaces.process_inbound_event(msg_event)
    # Simulate platform outage: disable the binding.
    services.interfaces.disable_binding(
        project_id=project.id, binding_id=binding.id, actor_id=owner.id
    )
    # The conversation event is still in the backend.
    events = services.plans.list_conversation_events(
        project_id=project.id, limit=10
    )
    assert len(events) == 1
    assert events[0].content == "Before outage."
    # Re-enable the binding.
    services.interfaces.enable_binding(
        project_id=project.id, binding_id=binding.id, actor_id=owner.id
    )
    # Can process new events again.
    msg_event2 = NormalizedEvent(
        platform="telegram",
        external_event_id="update_outage_2",
        external_actor_id="7086634092",
        chat_id="100",
        topic_id="7",
        event_kind="message",
        content="After outage.",
    )
    result = services.interfaces.process_inbound_event(msg_event2)
    assert result.processing_result == "processed"


# ----------------------------------------------------------------------
# 64-bit Telegram ID round-trip
# ----------------------------------------------------------------------


def test_64bit_telegram_id_preserved(services, project_with_owner_and_binding) -> None:
    """Per TELEGRAM_FINDINGS §8: Telegram IDs can exceed 32 significant
    bits; stored as TEXT for exact preservation."""
    owner, project, _binding = project_with_owner_and_binding
    # Link a user with a very large Telegram ID.
    user2 = services.identity.create_user(display_name="Big ID User")
    big_id = "9223372036854775807"  # max signed 64-bit
    services.identity.link_external_identity(
        user_id=user2.id, platform="telegram",
        external_id=big_id, verified=True,
    )
    # Add user2 as a project member.
    services.identity.add_member(
        project_id=project.id, actor_id=owner.id,
        member_id=user2.id, role="member",
    )
    # Send an event from the big-ID user.
    event = NormalizedEvent(
        platform="telegram",
        external_event_id="update_big_id",
        external_actor_id=big_id,
        chat_id="100",
        topic_id="7",
        event_kind="message",
        content="Hello from big ID user.",
    )
    result = services.interfaces.process_inbound_event(event)
    assert result.processing_result == "processed"
    assert result.resolved_user_id == user2.id


# ----------------------------------------------------------------------
# Cross-project isolation
# ----------------------------------------------------------------------


def test_interface_events_isolated_across_projects(services) -> None:
    """Per zero-project-isolation-evidence: interface events are
    project-scoped."""
    # Project A with enabled binding.
    owner_a = services.identity.create_user(display_name="Owner A")
    project_a = services.identity.create_project(
        owner_id=owner_a.id, name="Project A"
    )
    services.identity.link_external_identity(
        user_id=owner_a.id, platform="telegram",
        external_id="111", verified=True,
    )
    services.interfaces.create_binding(
        project_id=project_a.id, actor_id=owner_a.id,
        platform="telegram", chat_id="200", topic_id=None,
        is_enabled=True,
    )
    # Project B with enabled binding in a different chat.
    owner_b = services.identity.create_user(display_name="Owner B")
    project_b = services.identity.create_project(
        owner_id=owner_b.id, name="Project B"
    )
    services.identity.link_external_identity(
        user_id=owner_b.id, platform="telegram",
        external_id="222", verified=True,
    )
    services.interfaces.create_binding(
        project_id=project_b.id, actor_id=owner_b.id,
        platform="telegram", chat_id="300", topic_id=None,
        is_enabled=True,
    )
    # Send a message in project A's chat.
    event_a = NormalizedEvent(
        platform="telegram", external_event_id="update_iso_a",
        external_actor_id="111", chat_id="200", topic_id=None,
        event_kind="message", content="Project A message.",
    )
    services.interfaces.process_inbound_event(event_a)
    # Project B's conversation events should be empty.
    events_b = services.plans.list_conversation_events(
        project_id=project_b.id, limit=10
    )
    assert len(events_b) == 0
    # Project A has the event.
    events_a = services.plans.list_conversation_events(
        project_id=project_a.id, limit=10
    )
    assert len(events_a) == 1
