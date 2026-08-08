"""Phase 3 HTTP boundary smoke tests for plan and execution endpoints.

Per ``zero-modular-bootstrap`` §"One executable path is a design
asset": the smoke test starts the same ASGI app intended for later
deployment, using isolated configuration and persistence.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from zero.app.api import create_app
from zero.config import Settings


async def _setup_project(client: AsyncClient) -> tuple[str, str]:
    """Create a user and project; return (user_id, project_id)."""
    user_resp = await client.post(
        "/users", json={"display_name": "Owner"}
    )
    user_id = user_resp.json()["id"]
    proj_resp = await client.post(
        "/projects", json={"owner_id": user_id, "name": "Project A"}
    )
    project_id = proj_resp.json()["id"]
    return user_id, project_id


async def _create_approved_plan(
    client: AsyncClient, user_id: str, project_id: str
) -> str:
    """Create a plan, propose a revision, approve it, and return the
    handoff_id."""
    # Ingest a conversation event.
    event_resp = await client.post(
        f"/projects/{project_id}/conversation-events",
        json={
            "actor_id": user_id,
            "source": "web",
            "origin_kind": "authenticated_human",
            "content": "Add a login page.",
        },
    )
    event_id = event_resp.json()["id"]
    # Create a plan.
    plan_resp = await client.post(
        f"/projects/{project_id}/plans",
        json={"actor_id": user_id},
    )
    plan_id = plan_resp.json()["id"]
    # Propose a revision.
    await client.post(
        f"/projects/{project_id}/plans/{plan_id}/revisions",
        json={
            "actor_id": user_id,
            "objective": "Add a login page",
            "scope": ["frontend"],
            "constraints": [],
            "acceptance_criteria": ["Login form renders"],
            "risks": [],
            "unresolved_questions": [],
            "source_event_ids": [event_id],
        },
    )
    # Approve the revision.
    approve_resp = await client.post(
        f"/projects/{project_id}/plans/{plan_id}/approve",
        json={
            "actor_id": user_id,
            "expected_revision_number": 1,
            "idempotency_key": "approval-1",
        },
    )
    return approve_resp.json()["handoff"]["id"]


@pytest.mark.asyncio
async def test_plan_lifecycle_end_to_end() -> None:
    """Full plan lifecycle: ingest event -> create plan -> propose ->
    approve -> handoff produced."""
    settings = Settings.load_for_test()
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        user_id, project_id = await _setup_project(ac)
        handoff_id = await _create_approved_plan(ac, user_id, project_id)
    assert handoff_id.startswith("ph_")


@pytest.mark.asyncio
async def test_stale_revision_returns_409() -> None:
    """Per PLAN.md M4: 'Approval of an old revision fails after edit.'
    The HTTP endpoint returns 409 Conflict with the expected and
    actual revision numbers."""
    settings = Settings.load_for_test()
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        user_id, project_id = await _setup_project(ac)
        # Ingest event.
        event_resp = await client_post_event(ac, project_id, user_id)
        event_id = event_resp.json()["id"]
        # Create plan.
        plan_resp = await ac.post(
            f"/projects/{project_id}/plans",
            json={"actor_id": user_id},
        )
        plan_id = plan_resp.json()["id"]
        # Propose revision 1.
        await ac.post(
            f"/projects/{project_id}/plans/{plan_id}/revisions",
            json={
                "actor_id": user_id,
                "objective": "V1",
                "scope": [],
                "constraints": [],
                "acceptance_criteria": ["Works"],
                "risks": [],
                "unresolved_questions": [],
                "source_event_ids": [event_id],
            },
        )
        # Propose revision 2.
        await ac.post(
            f"/projects/{project_id}/plans/{plan_id}/revisions",
            json={
                "actor_id": user_id,
                "objective": "V2",
                "scope": [],
                "constraints": [],
                "acceptance_criteria": ["Works"],
                "risks": [],
                "unresolved_questions": [],
                "source_event_ids": [event_id],
            },
        )
        # Attempt to approve revision 1 (stale).
        resp = await ac.post(
            f"/projects/{project_id}/plans/{plan_id}/approve",
            json={
                "actor_id": user_id,
                "expected_revision_number": 1,
                "idempotency_key": "stale",
            },
        )
    assert resp.status_code == 409
    body = resp.json()["detail"]
    assert body["expected_revision"] == 1
    assert body["actual_revision"] == 2


async def client_post_event(ac, project_id, user_id):
    return await ac.post(
        f"/projects/{project_id}/conversation-events",
        json={
            "actor_id": user_id,
            "source": "web",
            "origin_kind": "authenticated_human",
            "content": "Add a login page.",
        },
    )


@pytest.mark.asyncio
async def test_unauthorized_approval_returns_403() -> None:
    """Per PLAN.md M4: 'Unauthorized approval fails.'"""
    settings = Settings.load_for_test()
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        user_id, project_id = await _setup_project(ac)
        # Add a viewer (read-only).
        viewer_resp = await ac.post(
            "/users", json={"display_name": "Viewer"}
        )
        viewer_id = viewer_resp.json()["id"]
        await ac.post(
            f"/projects/{project_id}/members",
            json={"member_id": viewer_id, "role": "viewer"},
        )
        # Ingest event as owner.
        event_resp = await client_post_event(ac, project_id, user_id)
        event_id = event_resp.json()["id"]
        # Create plan as owner.
        plan_resp = await ac.post(
            f"/projects/{project_id}/plans",
            json={"actor_id": user_id},
        )
        plan_id = plan_resp.json()["id"]
        # Propose revision as owner.
        await ac.post(
            f"/projects/{project_id}/plans/{plan_id}/revisions",
            json={
                "actor_id": user_id,
                "objective": "Add a login page",
                "scope": [],
                "constraints": [],
                "acceptance_criteria": ["Works"],
                "risks": [],
                "unresolved_questions": [],
                "source_event_ids": [event_id],
            },
        )
        # Attempt to approve as viewer (unauthorized).
        resp = await ac.post(
            f"/projects/{project_id}/plans/{plan_id}/approve",
            json={
                "actor_id": viewer_id,
                "expected_revision_number": 1,
                "idempotency_key": "viewer-approval",
            },
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_execution_creation_end_to_end() -> None:
    """Create an execution from an approved handoff via HTTP."""
    settings = Settings.load_for_test()
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        user_id, project_id = await _setup_project(ac)
        handoff_id = await _create_approved_plan(ac, user_id, project_id)
        # Create an execution with two independent tasks.
        resp = await ac.post(
            f"/projects/{project_id}/handoffs/{handoff_id}/executions",
            json={
                "actor_id": user_id,
                "task_specs": [
                    {"key": "A", "objective": "Task A"},
                    {"key": "B", "objective": "Task B"},
                ],
                "dependency_specs": [],
            },
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["state"] == "pending"
    execution_id = body["id"]
    # List ready tasks: both should be ready (no dependencies).
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        ready_resp = await ac.get(
            f"/projects/{project_id}/executions/{execution_id}/ready-tasks"
        )
    assert ready_resp.status_code == 200
    ready = ready_resp.json()
    assert len(ready) == 2


@pytest.mark.asyncio
async def test_cycle_rejection_returns_400() -> None:
    """Per PLAN.md M5: 'Cycles and missing dependencies are rejected.'"""
    settings = Settings.load_for_test()
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        user_id, project_id = await _setup_project(ac)
        handoff_id = await _create_approved_plan(ac, user_id, project_id)
        # Create an execution with a cycle A -> B -> A.
        resp = await ac.post(
            f"/projects/{project_id}/handoffs/{handoff_id}/executions",
            json={
                "actor_id": user_id,
                "task_specs": [
                    {"key": "A", "objective": "Task A"},
                    {"key": "B", "objective": "Task B"},
                ],
                "dependency_specs": [
                    {"task_key": "A", "depends_on_key": "B"},
                    {"task_key": "B", "depends_on_key": "A"},
                ],
            },
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_execution_cancellation_end_to_end() -> None:
    """Cancel an execution via HTTP; verify tasks are cancelled."""
    settings = Settings.load_for_test()
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        user_id, project_id = await _setup_project(ac)
        handoff_id = await _create_approved_plan(ac, user_id, project_id)
        exec_resp = await ac.post(
            f"/projects/{project_id}/handoffs/{handoff_id}/executions",
            json={
                "actor_id": user_id,
                "task_specs": [{"key": "A", "objective": "Task A"}],
            },
        )
        execution_id = exec_resp.json()["id"]
        # Cancel.
        cancel_resp = await ac.post(
            f"/projects/{project_id}/executions/{execution_id}/cancel"
            f"?actor_id={user_id}"
        )
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["state"] == "cancelled"
    # Verify tasks are cancelled.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        tasks_resp = await ac.get(
            f"/projects/{project_id}/executions/{execution_id}/tasks"
        )
    tasks = tasks_resp.json()
    assert all(t["state"] == "cancelled" for t in tasks)


@pytest.mark.asyncio
async def test_execution_recovery_end_to_end() -> None:
    """Recover an execution after a simulated restart via HTTP."""
    settings = Settings.load_for_test()
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        user_id, project_id = await _setup_project(ac)
        handoff_id = await _create_approved_plan(ac, user_id, project_id)
        exec_resp = await ac.post(
            f"/projects/{project_id}/handoffs/{handoff_id}/executions",
            json={
                "actor_id": user_id,
                "task_specs": [{"key": "A", "objective": "Task A"}],
            },
        )
        execution_id = exec_resp.json()["id"]
        # Recover.
        recover_resp = await ac.post(
            f"/projects/{project_id}/executions/{execution_id}/recover"
            f"?actor_id={user_id}"
        )
    assert recover_resp.status_code == 200
    body = recover_resp.json()
    # With no running tasks, the execution should be paused after
    # recovery (or pending if no tasks were ever claimed).
    assert body["state"] in ("pending", "paused")
