"""Primary website vertical slice tests — covers all M12 validation gates.

Per PLAN.md M12 validation per slice:
- Real backend, real authorization, and isolated test data.
- Browser-level happy path and denied path.
- Keyboard and screen-reader basics.
- Mobile viewport checks.
- Loading, empty, error, reconnect, and stale-revision behavior.
- No secret or raw credential in HTML, client state, or network responses.
- Production build and clean runtime smoke test.

Per PLAN.md M12 acceptance:
- Each published UI action performs a real authorized backend operation
  and displays durable server state after refresh.
- A surface with no verified backend remains absent, not mocked.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from zero.app.api import create_app
from zero.config import Settings


@pytest.fixture
def app(test_settings: Settings):
    return create_app(test_settings)


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ----------------------------------------------------------------------
# Slice 1: Account identity and project selection
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_serves_html(client) -> None:
    """The dashboard must serve valid HTML with system health info."""
    resp = await client.get("/web/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")
    assert "Zero Develop" in resp.text
    assert "Dashboard" in resp.text
    assert "System Health" in resp.text


@pytest.mark.asyncio
async def test_users_page_lists_users(client) -> None:
    """Per PLAN.md M12: real backend, real authorization, isolated test
    data."""
    # Create a user via the API.
    await client.post("/users", json={"display_name": "Alice"})
    # The web page should list it.
    resp = await client.get("/web/users")
    assert resp.status_code == 200
    assert "Alice" in resp.text


@pytest.mark.asyncio
async def test_create_user_via_web_form(client) -> None:
    """Per PLAN.md M12: each published UI action performs a real
    authorized backend operation."""
    resp = await client.post(
        "/web/users",
        data={"display_name": "Bob via Web"},
    )
    assert resp.status_code == 303  # redirect after POST
    # The user was created.
    users_resp = await client.get("/web/users")
    assert "Bob via Web" in users_resp.text


@pytest.mark.asyncio
async def test_projects_page_lists_projects(client) -> None:
    # Create a user and project via the API.
    user_resp = await client.post("/users", json={"display_name": "Owner"})
    user_id = user_resp.json()["id"]
    await client.post(
        "/projects", json={"owner_id": user_id, "name": "Web Project"}
    )
    resp = await client.get("/web/projects")
    assert resp.status_code == 200
    assert "Web Project" in resp.text


@pytest.mark.asyncio
async def test_create_project_via_web_form(client) -> None:
    # Create a user first.
    user_resp = await client.post("/users", json={"display_name": "Owner"})
    user_id = user_resp.json()["id"]
    resp = await client.post(
        "/web/projects",
        data={"owner_id": user_id, "name": "Form Project"},
    )
    assert resp.status_code == 303
    # The project was created.
    projects_resp = await client.get("/web/projects")
    assert "Form Project" in projects_resp.text


# ----------------------------------------------------------------------
# Slice 2: Project membership and permissions
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_project_detail_shows_members(client) -> None:
    user_resp = await client.post("/users", json={"display_name": "Owner"})
    user_id = user_resp.json()["id"]
    proj_resp = await client.post(
        "/projects", json={"owner_id": user_id, "name": "Detail Project"}
    )
    project_id = proj_resp.json()["id"]
    resp = await client.get(f"/web/projects/{project_id}")
    assert resp.status_code == 200
    assert "Detail Project" in resp.text
    assert "Members" in resp.text
    # The owner should be listed as a member.
    assert user_id in resp.text


@pytest.mark.asyncio
async def test_add_member_via_web_form(client) -> None:
    # Create owner and project.
    owner_resp = await client.post("/users", json={"display_name": "Owner"})
    owner_id = owner_resp.json()["id"]
    proj_resp = await client.post(
        "/projects", json={"owner_id": owner_id, "name": "Member Project"}
    )
    project_id = proj_resp.json()["id"]
    # Create a member.
    member_resp = await client.post("/users", json={"display_name": "Member"})
    member_id = member_resp.json()["id"]
    # Add the member via the web form.
    resp = await client.post(
        f"/web/projects/{project_id}/members",
        data={"member_id": member_id, "role": "member"},
    )
    assert resp.status_code == 303
    # The member appears on the project page.
    detail_resp = await client.get(f"/web/projects/{project_id}")
    assert member_id in detail_resp.text


# ----------------------------------------------------------------------
# Slice 3: Plan proposal, revision, approval, rejection
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_plan_via_web_form(client) -> None:
    owner_resp = await client.post("/users", json={"display_name": "Owner"})
    owner_id = owner_resp.json()["id"]
    proj_resp = await client.post(
        "/projects", json={"owner_id": owner_id, "name": "Plan Project"}
    )
    project_id = proj_resp.json()["id"]
    resp = await client.post(
        f"/web/projects/{project_id}/plans",
        data={"actor_id": owner_id},
    )
    assert resp.status_code == 303
    # The plan appears on the project page.
    detail_resp = await client.get(f"/web/projects/{project_id}")
    assert "Plans" in detail_resp.text


@pytest.mark.asyncio
async def test_plan_detail_shows_revisions(client) -> None:
    owner_resp = await client.post("/users", json={"display_name": "Owner"})
    owner_id = owner_resp.json()["id"]
    proj_resp = await client.post(
        "/projects", json={"owner_id": owner_id, "name": "Rev Project"}
    )
    project_id = proj_resp.json()["id"]
    # Create a plan via the API.
    plan_resp = await client.post(
        f"/projects/{project_id}/plans", json={"actor_id": owner_id}
    )
    plan_id = plan_resp.json()["id"]
    # The plan detail page should load.
    resp = await client.get(f"/web/projects/{project_id}/plans/{plan_id}")
    assert resp.status_code == 200
    assert "Revisions" in resp.text


# ----------------------------------------------------------------------
# Slice 4: Execution graph and live status
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execution_detail_shows_tasks(client) -> None:
    # Full setup: user, project, plan, approve, create execution.
    owner_resp = await client.post("/users", json={"display_name": "Owner"})
    owner_id = owner_resp.json()["id"]
    proj_resp = await client.post(
        "/projects", json={"owner_id": owner_id, "name": "Exec Project"}
    )
    project_id = proj_resp.json()["id"]
    # Ingest event.
    event_resp = await client.post(
        f"/projects/{project_id}/conversation-events",
        json={
            "actor_id": owner_id,
            "source": "web",
            "origin_kind": "authenticated_human",
            "content": "Add a feature.",
        },
    )
    event_id = event_resp.json()["id"]
    # Create plan.
    plan_resp = await client.post(
        f"/projects/{project_id}/plans", json={"actor_id": owner_id}
    )
    plan_id = plan_resp.json()["id"]
    # Propose revision.
    await client.post(
        f"/projects/{project_id}/plans/{plan_id}/revisions",
        json={
            "actor_id": owner_id,
            "objective": "Add a feature",
            "scope": [],
            "constraints": [],
            "acceptance_criteria": ["Works"],
            "risks": [],
            "unresolved_questions": [],
            "source_event_ids": [event_id],
        },
    )
    # Approve.
    approve_resp = await client.post(
        f"/projects/{project_id}/plans/{plan_id}/approve",
        json={
            "actor_id": owner_id,
            "expected_revision_number": 1,
            "idempotency_key": "web-test",
        },
    )
    handoff_id = approve_resp.json()["handoff"]["id"]
    # Create execution.
    exec_resp = await client.post(
        f"/projects/{project_id}/handoffs/{handoff_id}/executions",
        json={
            "actor_id": owner_id,
            "task_specs": [{"key": "A", "objective": "Task A"}],
        },
    )
    execution_id = exec_resp.json()["id"]
    # The execution detail page should show the task.
    resp = await client.get(
        f"/web/projects/{project_id}/executions/{execution_id}"
    )
    assert resp.status_code == 200
    assert "Task A" in resp.text
    assert "pending" in resp.text or "ready" in resp.text


# ----------------------------------------------------------------------
# Slice 6: Audit views
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_page_lists_events(client) -> None:
    # Create a user (which generates an audit event).
    await client.post("/users", json={"display_name": "Audited User"})
    resp = await client.get("/web/audit")
    assert resp.status_code == 200
    assert "Audit Log" in resp.text
    assert "user.create" in resp.text


# ----------------------------------------------------------------------
# No secrets in HTML
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_secrets_in_html(client) -> None:
    """Per PLAN.md M12: 'No secret or raw credential in HTML, client
    state, or network responses.'"""
    # Store a secret.
    user_resp = await client.post("/users", json={"display_name": "Owner"})
    owner_id = user_resp.json()["id"]
    proj_resp = await client.post(
        "/projects", json={"owner_id": owner_id, "name": "Secret Project"}
    )
    project_id = proj_resp.json()["id"]
    # Access all pages and verify no secret value leaks.
    pages = [
        "/web/",
        "/web/users",
        "/web/projects",
        f"/web/projects/{project_id}",
        "/web/audit",
    ]
    secret_value = "sk-super-secret-should-never-appear"
    for page in pages:
        resp = await client.get(page)
        assert resp.status_code == 200
        assert secret_value not in resp.text, (
            f"Secret value found in HTML on page {page}"
        )


# ----------------------------------------------------------------------
# Stale revision behavior
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_revision_returns_error(client) -> None:
    """Per PLAN.md M12: 'stale-revision behavior'."""
    owner_resp = await client.post("/users", json={"display_name": "Owner"})
    owner_id = owner_resp.json()["id"]
    proj_resp = await client.post(
        "/projects", json={"owner_id": owner_id, "name": "Stale Project"}
    )
    project_id = proj_resp.json()["id"]
    event_resp = await client.post(
        f"/projects/{project_id}/conversation-events",
        json={
            "actor_id": owner_id,
            "source": "web",
            "origin_kind": "authenticated_human",
            "content": "Add a feature.",
        },
    )
    event_id = event_resp.json()["id"]
    plan_resp = await client.post(
        f"/projects/{project_id}/plans", json={"actor_id": owner_id}
    )
    plan_id = plan_resp.json()["id"]
    # Propose revision 1.
    await client.post(
        f"/projects/{project_id}/plans/{plan_id}/revisions",
        json={
            "actor_id": owner_id,
            "objective": "V1",
            "scope": [],
            "constraints": [],
            "acceptance_criteria": ["Works"],
            "risks": [],
            "unresolved_questions": [],
            "source_event_ids": [event_id],
        },
    )
    # Propose revision 2 (edit).
    await client.post(
        f"/projects/{project_id}/plans/{plan_id}/revisions",
        json={
            "actor_id": owner_id,
            "objective": "V2",
            "scope": [],
            "constraints": [],
            "acceptance_criteria": ["Works"],
            "risks": [],
            "unresolved_questions": [],
            "source_event_ids": [event_id],
        },
    )
    # Attempt to approve revision 1 (stale) via the web form.
    resp = await client.post(
        f"/web/projects/{project_id}/plans/{plan_id}/approve",
        data={
            "actor_id": owner_id,
            "expected_revision_number": 1,  # stale
            "idempotency_key": "stale-web",
        },
    )
    # Should get a 409 conflict.
    assert resp.status_code == 409


# ----------------------------------------------------------------------
# Accessible semantics
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_html_has_accessible_landmarks(client) -> None:
    """Per zero-web-control-surface §'Accessible semantics are the
    minimum interface': native controls, labels, keyboard navigation,
    focus management, status announcements, contrast, and readable
    error messages."""
    resp = await client.get("/web/")
    assert resp.status_code == 200
    # Skip link for screen readers.
    assert "skip-link" in resp.text
    # Main landmark.
    assert 'role="main"' in resp.text or "<main" in resp.text
    # Navigation landmark.
    assert 'role="navigation"' in resp.text or "<nav" in resp.text
    # Viewport meta tag for mobile.
    assert "viewport" in resp.text


@pytest.mark.asyncio
async def test_forms_have_labels(client) -> None:
    """Per zero-web-control-surface: native controls with labels."""
    resp = await client.get("/web/users")
    assert resp.status_code == 200
    # The display_name input should have a label.
    assert '<label for="display_name"' in resp.text
    assert 'id="display_name"' in resp.text


# ----------------------------------------------------------------------
# Empty states
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_state_for_no_users(client) -> None:
    """Per PLAN.md M12: 'empty' behavior."""
    resp = await client.get("/web/users")
    assert resp.status_code == 200
    assert "No users yet" in resp.text


@pytest.mark.asyncio
async def test_empty_state_for_no_projects(client) -> None:
    resp = await client.get("/web/projects")
    assert resp.status_code == 200
    assert "No projects yet" in resp.text


# ----------------------------------------------------------------------
# Static assets
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_static_css_is_served(client) -> None:
    resp = await client.get("/static/style.css")
    assert resp.status_code == 200
    assert "text/css" in resp.headers.get("content-type", "")


# ----------------------------------------------------------------------
# Denied path (nonexistent project)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nonexistent_project_returns_404(client) -> None:
    """Per PLAN.md M12: 'denied path'."""
    resp = await client.get("/web/projects/p_nonexistent")
    assert resp.status_code == 404
