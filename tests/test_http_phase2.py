"""Phase 2 HTTP boundary smoke tests.

These tests go through the real ASGI app (via httpx's ASGI transport)
to prove that the new identity, authorization, secret, tool, and audit
endpoints work end-to-end through the HTTP boundary.

Per ``zero-modular-bootstrap`` §"One executable path is a design
asset": the smoke test starts the same ASGI app intended for later
deployment, using isolated configuration and persistence.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from zero.app.api import create_app
from zero.config import Settings


@pytest.mark.asyncio
async def test_create_user_endpoint() -> None:
    settings = Settings.load_for_test()
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.post(
            "/users", json={"display_name": "HTTP Alice"}
        )
    assert response.status_code == 201
    body = response.json()
    assert body["id"].startswith("zu_")
    assert body["display_name"] == "HTTP Alice"


@pytest.mark.asyncio
async def test_create_project_endpoint() -> None:
    settings = Settings.load_for_test()
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Create a user first.
        user_resp = await ac.post(
            "/users", json={"display_name": "Owner"}
        )
        user_id = user_resp.json()["id"]
        # Create a project.
        proj_resp = await ac.post(
            "/projects",
            json={"owner_id": user_id, "name": "HTTP Project"},
        )
    assert proj_resp.status_code == 201
    body = proj_resp.json()
    assert body["id"].startswith("p_")
    assert body["owner_user_id"] == user_id


@pytest.mark.asyncio
async def test_authorize_endpoint_returns_decision() -> None:
    settings = Settings.load_for_test()
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Create user and project.
        user_resp = await ac.post("/users", json={"display_name": "Owner"})
        user_id = user_resp.json()["id"]
        proj_resp = await ac.post(
            "/projects", json={"owner_id": user_id, "name": "Project A"}
        )
        project_id = proj_resp.json()["id"]
        # Check authorization: owner should be allowed project.view.
        authz_resp = await ac.post(
            f"/projects/{project_id}/authorize?actor_id={user_id}&permission=project.view"
        )
    assert authz_resp.status_code == 200
    body = authz_resp.json()
    assert body["allowed"] is True
    assert body["role"] == "owner"
    assert body["reason"] == "allowed"


@pytest.mark.asyncio
async def test_authorize_endpoint_denies_non_member() -> None:
    settings = Settings.load_for_test()
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Create owner and project.
        owner_resp = await ac.post("/users", json={"display_name": "Owner"})
        owner_id = owner_resp.json()["id"]
        proj_resp = await ac.post(
            "/projects", json={"owner_id": owner_id, "name": "Project A"}
        )
        project_id = proj_resp.json()["id"]
        # Create a non-member.
        non_member_resp = await ac.post(
            "/users", json={"display_name": "Outsider"}
        )
        non_member_id = non_member_resp.json()["id"]
        # Check authorization: non-member should be denied.
        authz_resp = await ac.post(
            f"/projects/{project_id}/authorize?actor_id={non_member_id}&permission=project.view"
        )
    assert authz_resp.status_code == 200
    body = authz_resp.json()
    assert body["allowed"] is False
    assert body["reason"] == "not_member"


@pytest.mark.asyncio
async def test_audit_endpoint_returns_events() -> None:
    settings = Settings.load_for_test()
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        user_resp = await ac.post("/users", json={"display_name": "Owner"})
        user_id = user_resp.json()["id"]
        proj_resp = await ac.post(
            "/projects", json={"owner_id": user_id, "name": "Project A"}
        )
        project_id = proj_resp.json()["id"]
        # List audit events for the project.
        audit_resp = await ac.get(f"/projects/{project_id}/audit")
    assert audit_resp.status_code == 200
    events = audit_resp.json()
    # At least the project.create event should be present.
    assert any(e["operation"] == "project.create" for e in events)


@pytest.mark.asyncio
async def test_tool_invocation_endpoint() -> None:
    settings = Settings.load_for_test()
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Setup: user, project.
        user_resp = await ac.post("/users", json={"display_name": "Owner"})
        user_id = user_resp.json()["id"]
        proj_resp = await ac.post(
            "/projects", json={"owner_id": user_id, "name": "Project A"}
        )
        project_id = proj_resp.json()["id"]
        # Register the echo tool. We need to do this through the service
        # because the API doesn't expose tool registration (only
        # invocation). In production, tool registration is an admin
        # operation; for this test we reach into app.state.
        app.state.services.tools.register_echo_tool()
        tool = app.state.services.tools.get_tool_by_name("echo")
        # Grant the tool to main_worker in this project.
        grant_resp = await ac.post(
            f"/projects/{project_id}/tool-grants",
            json={
                "tool_id": tool.id.value,
                "agent_scope": "main_worker",
            },
        )
        assert grant_resp.status_code == 201
        # Invoke the tool.
        invoke_resp = await ac.post(
            f"/projects/{project_id}/tool-invocations",
            json={
                "tool_name": "echo",
                "input_data": {"message": "hello from HTTP"},
                "agent_scope": "main_worker",
            },
        )
    assert invoke_resp.status_code == 200
    body = invoke_resp.json()
    assert body["status"] == "success"
    assert body["output"]["echoed"] == "hello from HTTP"
    assert body["output"]["length"] == 15


@pytest.mark.asyncio
async def test_tool_invocation_without_grant_returns_403() -> None:
    settings = Settings.load_for_test()
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        user_resp = await ac.post("/users", json={"display_name": "Owner"})
        user_id = user_resp.json()["id"]
        proj_resp = await ac.post(
            "/projects", json={"owner_id": user_id, "name": "Project A"}
        )
        project_id = proj_resp.json()["id"]
        app.state.services.tools.register_echo_tool()
        # No grant created.
        invoke_resp = await ac.post(
            f"/projects/{project_id}/tool-invocations",
            json={
                "tool_name": "echo",
                "input_data": {"message": "should be denied"},
                "agent_scope": "main_worker",
            },
        )
    assert invoke_resp.status_code == 403


@pytest.mark.asyncio
async def test_secret_storage_endpoint_never_returns_value() -> None:
    """Per zero-control-plane-trust §"Secrets are usable without being
    visible": the HTTP endpoint for storing a secret returns only
    metadata, never the value."""
    settings = Settings.load_for_test(secret_key="x" * 64)
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        user_resp = await ac.post("/users", json={"display_name": "Owner"})
        user_id = user_resp.json()["id"]
        proj_resp = await ac.post(
            "/projects", json={"owner_id": user_id, "name": "Project A"}
        )
        project_id = proj_resp.json()["id"]
        secret_value = "sk-never-leak-me-http-12345"
        store_resp = await ac.post(
            f"/projects/{project_id}/secrets",
            json={
                "name": "api_key",
                "secret_type": "api_key",
                "value": secret_value,
            },
        )
    assert store_resp.status_code == 201
    body = store_resp.json()
    # The response must not contain the value.
    assert secret_value not in store_resp.text
    assert "value" not in body
    assert body["name"] == "api_key"
    assert body["id"].startswith("sec_")
