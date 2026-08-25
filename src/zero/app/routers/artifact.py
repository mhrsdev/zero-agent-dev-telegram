"""Artifact routes extracted from app.api."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request, status

from zero.app.routers.deps import authorized_actor
from zero.app.routers.models import StoreArtifactRequest, StoreRagDocumentRequest
from zero.app.services import Services
from zero.domain.identity import (
    ProjectId,
)


def _artifact_payload(artifact: Any, *, include_content: bool = False) -> dict[str, Any]:
    payload = {
        "id": artifact.id.value,
        "project_id": artifact.project_id.value,
        "content_hash": artifact.content_hash,
        "kind": artifact.kind,
        "media_type": artifact.media_type,
        "size_bytes": artifact.size_bytes,
        "producer": artifact.producer,
        "provenance": artifact.provenance,
        "created_at": artifact.created_at,
    }
    if include_content:
        payload["content"] = artifact.content
    return payload


def _rag_payload(document: Any, *, include_content: bool = False) -> dict[str, Any]:
    payload = {
        "id": document.id.value,
        "project_id": document.project_id.value,
        "source_type": document.source_type,
        "source_id": document.source_id,
        "title": document.title,
        "content_hash": document.content_hash,
        "state": document.state,
        "superseded_by": document.superseded_by.value if document.superseded_by else None,
        "index_version": document.index_version,
        "created_at": document.created_at,
        "updated_at": document.updated_at,
    }
    if include_content:
        payload["content"] = document.content
    return payload


def register_artifact_routes(app: FastAPI, services: Services) -> None:
    @app.get("/projects/{project_id}/artifacts", tags=["artifacts"])
    def list_artifacts(
        request: Request, project_id: str, kind: str | None = None
    ) -> list[dict[str, Any]]:

        actor = authorized_actor(request, services, project_id, "project.view")
        artifacts = services.artifacts.list_artifacts(
            project_id=ProjectId(project_id),
            actor_id=actor,
            kind=kind,  # type: ignore[arg-type]
        )
        return [_artifact_payload(item) for item in artifacts]

    @app.post(
        "/projects/{project_id}/artifacts",
        tags=["artifacts"],
        status_code=status.HTTP_201_CREATED,
    )
    def store_artifact(
        request: Request, project_id: str, req: StoreArtifactRequest
    ) -> dict[str, Any]:

        actor = authorized_actor(request, services, project_id, "agent.manage")
        try:
            artifact = services.artifacts.store_artifact(
                project_id=ProjectId(project_id),
                actor_id=actor,
                kind=req.kind,  # type: ignore[arg-type]
                content=req.content,
                producer=req.producer,
                provenance=req.provenance,
                media_type=req.media_type,
                source="web",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="request failed") from exc
        return _artifact_payload(artifact)

    @app.get("/projects/{project_id}/artifacts/{artifact_id}", tags=["artifacts"])
    def get_artifact(request: Request, project_id: str, artifact_id: str) -> dict[str, Any]:
        from zero.domain.artifacts import ArtifactId

        actor = authorized_actor(request, services, project_id, "agent.manage")
        try:
            artifact = services.artifacts.get_artifact(
                project_id=ProjectId(project_id),
                artifact_id=ArtifactId(artifact_id),
                actor_id=actor,
                source="web",
            )
        except Exception as exc:
            raise HTTPException(status_code=404, detail="Artifact not found") from exc
        return _artifact_payload(artifact, include_content=True)

    @app.get("/projects/{project_id}/rag", tags=["rag"])
    def list_rag_documents(
        request: Request, project_id: str, state: str | None = None
    ) -> list[dict[str, Any]]:

        authorized_actor(request, services, project_id, "project.view")
        documents = services.artifacts.list_rag_documents(
            ProjectId(project_id),
            state=state,  # type: ignore[arg-type]
        )
        return [_rag_payload(item) for item in documents]

    @app.post(
        "/projects/{project_id}/rag",
        tags=["rag"],
        status_code=status.HTTP_201_CREATED,
    )
    def ingest_rag_document(
        request: Request, project_id: str, req: StoreRagDocumentRequest
    ) -> dict[str, Any]:

        actor = authorized_actor(request, services, project_id, "agent.manage")
        try:
            document = services.artifacts.ingest_rag_document(
                project_id=ProjectId(project_id),
                actor_id=actor,
                source_type=req.source_type,  # type: ignore[arg-type]
                source_id=req.source_id,
                title=req.title,
                content=req.content,
                state=req.state,  # type: ignore[arg-type]
                source="web",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="request failed") from exc
        return _rag_payload(document)

    @app.get("/projects/{project_id}/rag/{doc_id}", tags=["rag"])
    def get_rag_document(request: Request, project_id: str, doc_id: str) -> dict[str, Any]:
        from zero.domain.artifacts import RagDocumentId

        authorized_actor(request, services, project_id, "project.view")
        try:
            document = services.artifacts.get_rag_document(
                ProjectId(project_id), RagDocumentId(doc_id)
            )
        except Exception as exc:
            raise HTTPException(status_code=404, detail="RAG document not found") from exc
        return _rag_payload(document, include_content=True)

    @app.post("/projects/{project_id}/rag/search", tags=["rag"])
    def search_rag(
        request: Request, project_id: str, query: str, limit: int = 20
    ) -> list[dict[str, Any]]:

        authorized_actor(request, services, project_id, "project.view")
        if not 1 <= limit <= 100:
            raise HTTPException(status_code=400, detail="limit must be between 1 and 100")
        results = services.artifacts.search_rag(
            project_id=ProjectId(project_id), query=query, limit=limit
        )
        return [{"document": _rag_payload(document), "score": score} for document, score in results]

    @app.post("/projects/{project_id}/rag/rebuild", tags=["rag"])
    def rebuild_rag(request: Request, project_id: str) -> dict[str, Any]:

        actor = authorized_actor(request, services, project_id, "agent.manage")
        count = services.artifacts.rebuild_rag_index(
            project_id=ProjectId(project_id), actor_id=actor, source="web"
        )
        return {"project_id": project_id, "indexed_documents": count}
