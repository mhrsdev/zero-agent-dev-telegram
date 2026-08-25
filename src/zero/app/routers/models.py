"""Shared strict request models for the per-domain routers."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StoreArtifactRequest(_StrictRequest):
    kind: str = Field(..., min_length=1, max_length=40)
    content: str = Field(..., min_length=1)
    producer: str | None = Field(default=None, max_length=200)
    provenance: str | None = None
    media_type: str = Field(default="text/plain", max_length=100)


class StoreRagDocumentRequest(_StrictRequest):
    source_type: str = Field(..., min_length=1, max_length=40)
    source_id: str = Field(..., min_length=1, max_length=200)
    title: str = Field(..., min_length=1, max_length=500)
    content: str = Field(..., min_length=1)
    state: str = Field(default="candidate", pattern="^(candidate|approved)$")


class CreateAgentTypeRequest(_StrictRequest):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    name: str = Field(..., min_length=1, max_length=200)
    responsibility: str = Field(..., min_length=1, max_length=2000)
    memory_scope: str = Field(default="", max_length=2000)
    permitted_tools: list[str] = Field(default_factory=list)
    model_policy: dict[str, str] = Field(default_factory=dict)
    context_budget_tokens: int = Field(default=100000, ge=1)
    max_concurrent_instances: int = Field(default=1, ge=1)


class AddKnowledgeRequest(_StrictRequest):
    kind: str = Field(..., min_length=1, max_length=40)
    content: str = Field(..., min_length=1)
    provenance: str | None = None
    state: str = Field(default="approved", pattern="^(candidate|approved)$")


class RegisterRepositoryRequest(_StrictRequest):
    name: str = Field(..., min_length=1, max_length=200)
    local_path: str = Field(..., min_length=1, max_length=4096)
    default_base_revision: str | None = None


class CreateInterfaceBindingRequest(_StrictRequest):
    platform: str = Field(..., pattern="^(telegram|discord|other)$")
    chat_id: str = Field(..., min_length=1, max_length=200)
    topic_id: str | None = Field(default=None, max_length=200)
    bot_token_ref: str | None = Field(default=None, max_length=200)
    is_enabled: bool = False


class CreateIntegrationReviewRequest(_StrictRequest):
    execution_id: str = Field(..., min_length=1)
    source_task_ids: list[str] = Field(..., min_length=1)


class CreateMergeProposalRequest(_StrictRequest):
    review_id: str = Field(..., min_length=1)
    execution_id: str = Field(..., min_length=1)
    source_tasks: list[str] = Field(..., min_length=1)
    source_diffs: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


class ReconcileProviderRequestRequest(_StrictRequest):
    resolution: str = Field(
        ...,
        pattern="^(confirmed_not_dispatched|confirmed_dispatched)$",
    )
    note: str = Field(default="", max_length=500)
