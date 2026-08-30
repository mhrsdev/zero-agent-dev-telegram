"""Round-9 regressions: RAG + knowledge HTTP routes must actually work.

GAP I/J (found live in round 9): three read routes called service
methods whose ``actor_id`` keyword argument is REQUIRED — every call
raised TypeError and surfaced as a 500 (``GET /projects/{pid}/rag``,
``GET /projects/{pid}/rag/{id}``) or a misleading 404
(``GET /projects/{pid}/agent-types/{tid}/knowledge``). None of these
routes had ever been exercised in a live run before, so the defects
survived seven fix rounds. Fixed by passing the authorized actor, and
pinned here with real ASGI round-trips.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from zero.app.api import create_app
from zero.config import Settings


@pytest.mark.asyncio
async def test_rag_list_and_get_routes_return_real_documents() -> None:
    settings = Settings.load_for_test()
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        user = (await ac.post("/users", json={"display_name": "Owner"})).json()
        project = (
            await ac.post(
                "/projects", json={"owner_id": user["id"], "name": "RAG routes"}
            )
        ).json()
        ingest = await ac.post(
            f"/projects/{project['id']}/rag",
            json={
                "source_type": "manual",
                "source_id": "round9-routes",
                "title": "Route regression dossier",
                "content": "BLUE HERON is the codename.",
                "state": "approved",
            },
        )
        assert ingest.status_code == 201, ingest.text
        doc = ingest.json()

        listed = await ac.get(f"/projects/{project['id']}/rag")
        assert listed.status_code == 200, listed.text
        assert any(item["id"] == doc["id"] for item in listed.json())

        fetched = await ac.get(f"/projects/{project['id']}/rag/{doc['id']}")
        assert fetched.status_code == 200, fetched.text
        assert "BLUE HERON" in fetched.json().get("content", "")


@pytest.mark.asyncio
async def test_duplicate_rag_ingest_returns_409_not_500() -> None:
    """GAP K (round-9 live find): re-ingesting the same (source_type,
    source_id) used to escape as an unhandled IntegrityError and surface
    as a 500. The typed domain error must map to an honest 409."""
    settings = Settings.load_for_test()
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        user = (await ac.post("/users", json={"display_name": "Owner"})).json()
        project = (
            await ac.post(
                "/projects", json={"owner_id": user["id"], "name": "RAG dedupe"}
            )
        ).json()
        payload = {
            "source_type": "manual",
            "source_id": "round9-dedupe",
            "title": "Dedupe dossier",
            "content": "Only the first ingest wins.",
            "state": "approved",
        }
        first = await ac.post(f"/projects/{project['id']}/rag", json=payload)
        assert first.status_code == 201, first.text
        second = await ac.post(f"/projects/{project['id']}/rag", json=payload)
        assert second.status_code == 409, second.text
        assert "already exists" in second.json()["detail"]


@pytest.mark.asyncio
async def test_knowledge_list_route_returns_created_records() -> None:
    settings = Settings.load_for_test()
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        user = (await ac.post("/users", json={"display_name": "Owner"})).json()
        project = (
            await ac.post(
                "/projects", json={"owner_id": user["id"], "name": "Knowledge routes"}
            )
        ).json()
        created = await ac.post(
            f"/projects/{project['id']}/agent-types",
            json={
                "name": "route-checker",
                "responsibility": "Verify knowledge list routes.",
            },
        )
        assert created.status_code == 201, created.text
        type_id = created.json()["id"]

        for fact in ("fact one BLUE HERON", "fact two KESTREL"):
            added = await ac.post(
                f"/projects/{project['id']}/agent-types/{type_id}/knowledge",
                json={"kind": "fact", "content": fact, "state": "approved"},
            )
            assert added.status_code == 201, added.text

        listed = await ac.get(
            f"/projects/{project['id']}/agent-types/{type_id}/knowledge"
        )
        assert listed.status_code == 200, listed.text
        contents = [item.get("content", "") for item in listed.json()]
        assert len(contents) == 2, contents
        assert any("BLUE HERON" in c for c in contents)
