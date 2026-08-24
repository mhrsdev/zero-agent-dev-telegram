"""R5 onboarding fixes: verification reachable + auto-verify on first inbound."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from zero.app.services import build_services
from zero.config import Settings
from zero.domain.interfaces import NormalizedEvent
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations


@pytest.fixture
def services(test_settings: Settings):
    database = Database(test_settings)
    apply_migrations(database)
    return build_services(test_settings, database)


@pytest.mark.asyncio
async def test_verify_route_marks_identity_verified(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        user = (await ac.post("/users", json={"display_name": "V"})).json()
        link = await ac.post(
            f"/users/{user['id']}/external-identities",
            json={"platform": "telegram", "external_id": "9001"},
        )
        assert link.status_code == 201
        ver = await ac.post(
            f"/users/{user['id']}/external-identities/verify",
            json={"platform": "telegram", "external_id": "9001"},
        )
        assert ver.status_code == 200, ver.text
        assert ver.json()["verified"] is True


def test_auto_verify_on_first_inbound(services) -> None:
    """A linked-but-unverified owner proves possession by messaging the
    bound chat; default policy auto-verifies and processing proceeds past
    the unlinked gate (membership check still applies)."""
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(owner_id=owner.id, name="AutoV")
    services.identity.link_external_identity(
        user_id=owner.id,
        platform="telegram",
        external_id="424242",
        verified=False,
    )
    binding = services.interfaces.create_binding(
        project_id=project.id,
        actor_id=owner.id,
        platform="telegram",
        chat_id="100",
        topic_id=None,
        is_enabled=True,
    )
    assert binding.is_enabled

    event = NormalizedEvent(
        platform="telegram",
        external_event_id="u1",
        external_actor_id="424242",
        chat_id="100",
        topic_id=None,
        event_kind="message",
        content="/start",
    )
    entry = services.interfaces.process_inbound_event(event)
    assert entry.processing_result != "ignored_unlinked", (
        f"auto-verify failed: {entry.processing_detail}"
    )

    # And the identity is now durably verified.
    refreshed = services.identity._identity_repo.require_verified_external_identity(
        "telegram", "424242"
    )
    assert refreshed.verified_at is not None


def test_auto_verify_does_not_apply_to_unlinked_senders(services) -> None:
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(owner_id=owner.id, name="NoLeak")
    services.interfaces.create_binding(
        project_id=project.id,
        actor_id=owner.id,
        platform="telegram",
        chat_id="100",
        topic_id=None,
        is_enabled=True,
    )
    event = NormalizedEvent(
        platform="telegram",
        external_event_id="u2",
        external_actor_id="stranger",
        chat_id="100",
        topic_id=None,
        event_kind="message",
        content="/start",
    )
    entry = services.interfaces.process_inbound_event(event)
    assert entry.processing_result == "ignored_unlinked"
