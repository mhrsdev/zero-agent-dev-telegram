"""RED tests for Gate D authorized API surface reachability."""

from __future__ import annotations

import importlib

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from zero.app.api import create_app
from zero.config import Settings

EXPECTED_ROUTE_PREFIXES = (
    "/projects/{project_id}/artifacts",
    "/projects/{project_id}/rag",
    "/projects/{project_id}/agent-types",
    "/projects/{project_id}/topology",
    "/projects/{project_id}/providers",
    "/projects/{project_id}/repositories",
    "/projects/{project_id}/worktrees",
    "/projects/{project_id}/integration",
    "/projects/{project_id}/interfaces",
)


def test_gate_d_api_registers_all_project_scoped_surface_routes(test_settings: Settings):
    app = create_app(test_settings)
    paths = set(app.openapi()["paths"])

    missing = [prefix for prefix in EXPECTED_ROUTE_PREFIXES if prefix not in paths]
    assert not missing, f"missing API surface routes: {missing}"


def test_new_mutation_models_reject_caller_supplied_actor_context():
    api = importlib.import_module("zero.app.api")
    model = getattr(api, "StoreArtifactRequest", None)
    assert model is not None, "artifact API request model is missing"
    assert "actor_id" not in model.model_fields
    with pytest.raises(ValidationError):
        model(kind="other", content="evidence", actor_id="spoofed")


@pytest.mark.asyncio
async def test_authenticated_viewer_cannot_read_foreign_artifact(test_settings: Settings):
    settings = test_settings.model_copy(update={"auth_required": True})
    app = create_app(settings)
    # Use the app's own database: this test is a route/actor regression, so
    # seed the composition root after construction rather than a second DB.
    app_services = app.state.services
    app_owner = app_services.identity.create_user(display_name="app-owner")
    app_viewer = app_services.identity.create_user(display_name="app-viewer")
    app_project = app_services.identity.create_project(owner_id=app_owner.id, name="owned")
    app_services.identity.add_member(
        project_id=app_project.id,
        actor_id=app_owner.id,
        member_id=app_viewer.id,
        role="viewer",
    )
    app_artifact = app_services.artifacts.store_artifact(
        project_id=app_project.id,
        actor_id=app_owner.id,
        kind="other",
        content="private",
    )
    app_token, _expires_at = app_services.auth.issue_access_token(app_viewer.id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            f"/projects/{app_project.id.value}/artifacts/{app_artifact.id.value}",
            headers={"Authorization": f"Bearer {app_token}"},
        )
    assert response.status_code in {403, 404}
