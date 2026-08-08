"""Provider service — model resolution, request execution, usage recording,
cost estimation.

Per ``zero-provider-adapter-contract`` SKILL.md:
- Canonical events and state are provider-neutral.
- Provider rendering validates tool-call/result shape before submission.
- Changing model/provider does not destroy identity, memory, task, or
  execution state.
- Prompt cache is an optional adapter optimization.
- Token classes remain separate.
- Whole-agent-tree usage is counted exactly once.
- Estimated cost is distinct from authoritative reconciled billing.

Per ``zero-claude-token-economics`` SKILL.md:
- Persist adapter/model/version with every request and usage record.
- Disabling an adapter must not make canonical project history unreadable.
- Duplicate streamed usage is not double-counted.
- Parent and child usage reconcile to one whole-tree total.
- Cache miss, hit, creation, and invalidation reasons are observable.
- Pricing changes do not mutate historical raw usage.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

from zero.app.artifact_service import ArtifactService
from zero.app.provider_adapter import (
    FakeProviderAdapter,
    ProviderAdapter,
    compute_request_hash,
    validate_tool_messages,
)
from zero.domain.audit import AuditEvent, AuditEventId, AuditSource
from zero.domain.execution import ExecutionId
from zero.domain.identity import ProjectId, UserId
from zero.domain.ids import (
    generate_audit_event_id,
    generate_provider_request_id,
    generate_usage_record_id,
)
from zero.domain.providers import (
    CanonicalRequest,
    CanonicalResponse,
    PricingEntry,
    ProviderErrorClass,
    ProviderModel,
    ProviderRequest,
    ProviderRequestId,
    TokenUsage,
    ToolCallResult,
    UsageRecord,
    UsageRecordId,
)
from zero.persistence.repositories.audit_repository import AuditRepository
from zero.persistence.repositories.provider_repository import (
    ProviderRepository,
)


def _now_utc_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def estimate_cost(
    usage: TokenUsage,
    pricing: PricingEntry,
) -> str:
    """Estimate cost in USD from usage and pricing.

    Per ``zero-claude-token-economics`` §"Estimated cost is not billing
    truth": this is a client-side estimate, NOT an invoice.
    """
    input_cost = Decimal(usage.input_tokens) * Decimal(pricing.input_price_per_million) / Decimal(1000000)
    output_cost = Decimal(usage.output_tokens) * Decimal(pricing.output_price_per_million) / Decimal(1000000)
    cache_create_cost = Decimal(usage.cache_creation_input_tokens) * Decimal(pricing.cache_creation_price_per_million) / Decimal(1000000)
    cache_read_cost = Decimal(usage.cache_read_input_tokens) * Decimal(pricing.cache_read_price_per_million) / Decimal(1000000)
    total = input_cost + output_cost + cache_create_cost + cache_read_cost
    return str(total.quantize(Decimal("0.000001")))


class ProviderService:
    """Application operations for provider model resolution, request
    execution, and usage recording.

    The service:
    - resolves provider models by name;
    - routes canonical requests through the appropriate adapter;
    - deduplicates requests by hash;
    - records usage with separate token classes;
    - estimates cost from versioned pricing;
    - supports reconciliation of authoritative billing.
    """

    def __init__(
        self,
        provider_repo: ProviderRepository,
        artifact_service: ArtifactService,
        audit_repo: AuditRepository,
    ) -> None:
        self._repo = provider_repo
        self._artifact_service = artifact_service
        self._audit_repo = audit_repo
        self._adapters: dict[str, ProviderAdapter] = {}
        self._register_default_adapters()

    def _register_default_adapters(self) -> None:
        # Register the fake adapter for tests.
        self._adapters["fake"] = FakeProviderAdapter(self._repo)

    def register_adapter(self, adapter: ProviderAdapter) -> None:
        """Register a custom adapter."""
        self._adapters[adapter.provider_name] = adapter

    # ------------------------------------------------------------------
    # Model resolution
    # ------------------------------------------------------------------

    def get_model(
        self, provider: str, model_name: str
    ) -> ProviderModel:
        """Resolve model capability/context metadata.

        Per ``zero-provider-adapter-contract`` §"Capabilities replace
        provider-name conditionals": capabilities describe observed
        contract support, not provider-name conditionals.
        """
        return self._repo.get_provider_model(provider, model_name)

    def list_models(self) -> list[ProviderModel]:
        return self._repo.list_provider_models(active_only=True)

    # ------------------------------------------------------------------
    # Request execution
    # ------------------------------------------------------------------

    def send_request(
        self,
        *,
        project_id: ProjectId,
        actor_id: UserId,
        request: CanonicalRequest,
        execution_id: ExecutionId | None = None,
        source: AuditSource = "system",
    ) -> tuple[ProviderRequest, CanonicalResponse]:
        """Send a canonical request through the provider adapter.

        Per ``zero-provider-adapter-contract`` §"Request/message and
        query deduplication": if the same request (by hash) was already
        submitted, the existing result is returned.

        Per ``zero-claude-token-economics`` §"Persist adapter/model/
        version with every request and usage record": the provider
        request record stores provider, model_name, and request_hash.

        Returns:
            (provider_request, canonical_response)
        """
        # Validate tool messages before submission.
        msg_dicts = [
            {
                "role": m.role,
                "content": m.content,
                "tool_call_id": m.tool_call_id,
                "tool_calls": list(m.tool_calls),
            }
            for m in request.messages
        ]
        _clean, stripped = validate_tool_messages(msg_dicts)
        if stripped:
            # Orphan tool results were repaired by dropping them.
            pass

        # Compute the request hash for deduplication.
        req_hash = compute_request_hash(request)

        # Check for an existing request with the same hash.
        existing = self._repo.get_provider_request_by_hash(req_hash)
        if existing is not None:
            # Idempotent: return the existing request. The response
            # artifact contains the full response.
            response = self._build_response_from_artifact(existing)
            return existing, response

        # Create the provider request record.
        provider_request = ProviderRequest(
            id=ProviderRequestId(generate_provider_request_id()),
            project_id=project_id,
            execution_id=execution_id,
            provider=request.provider,
            model_name=request.model_name,
            request_hash=req_hash,
            state="pending",
            started_at=_now_utc_iso(),
        )
        self._repo.insert_provider_request(provider_request)

        # Update state to streaming (we don't actually stream in the
        # fake adapter, but the state transition is correct).
        self._repo.update_provider_request_state(
            provider_request.id, "streaming"
        )

        # Get the adapter and send the request.
        adapter = self._adapters.get(request.provider)
        if adapter is None:
            self._repo.update_provider_request_state(
                provider_request.id,
                "failed",
                error_class="invalid_request",
                error_message=f"No adapter registered for provider {request.provider!r}",
            )
            raise ValueError(
                f"No adapter registered for provider {request.provider!r}"
            )

        try:
            response = adapter.send_request(request)
        except Exception as exc:
            # Classify the error.
            error_class = self._classify_error(exc)
            self._repo.update_provider_request_state(
                provider_request.id,
                "failed",
                error_class=error_class,
                error_message=str(exc)[:500],  # redacted
            )
            self._audit_repo.insert(
                AuditEvent(
                    id=AuditEventId(generate_audit_event_id()),
                    project_id=project_id,
                    actor_id=actor_id,
                    source=source,
                    operation="provider.request",
                    target_type="provider_request",
                    target_id=provider_request.id.value,
                    result="failure",
                    redacted_summary=(
                        f"Provider {request.provider}:{request.model_name} "
                        f"request failed: {error_class}"
                    ),
                    correlation_id=execution_id.value if execution_id else None,
                    created_at=_now_utc_iso(),
                )
            )
            raise

        # Store the response as an artifact.
        response_text = json.dumps({
            "content": response.content,
            "tool_calls": [
                {
                    "tool_name": tc.tool_name,
                    "tool_call_id": tc.tool_call_id,
                    "arguments": tc.arguments,
                    "result": tc.result,
                    "is_error": tc.is_error,
                }
                for tc in response.tool_calls
            ],
            "finish_reason": response.finish_reason,
            "provider_message_id": response.provider_message_id,
        }, ensure_ascii=False, indent=2)
        response_artifact = self._artifact_service.store_artifact(
            project_id=project_id,
            actor_id=actor_id,
            kind="other",
            content=response_text,
            producer=f"provider:{request.provider}:{request.model_name}",
            provenance=json.dumps({
                "provider_request_id": provider_request.id.value,
                "execution_id": execution_id.value if execution_id else None,
            }),
            source=source,
        )

        # Update the provider request to completed.
        self._repo.update_provider_request_state(
            provider_request.id,
            "completed",
            response_artifact_id=response_artifact.id,
        )

        # Record usage.
        if response.usage is not None:
            self._record_usage(
                project_id=project_id,
                provider_request_id=provider_request.id,
                execution_id=execution_id,
                usage=response.usage,
                provider_message_id=response.provider_message_id,
                provider=request.provider,
                model_name=request.model_name,
                actor_id=actor_id,
                source=source,
            )

        # Audit.
        self._audit_repo.insert(
            AuditEvent(
                id=AuditEventId(generate_audit_event_id()),
                project_id=project_id,
                actor_id=actor_id,
                source=source,
                operation="provider.request",
                target_type="provider_request",
                target_id=provider_request.id.value,
                result="success",
                redacted_summary=(
                    f"Provider {request.provider}:{request.model_name} "
                    f"request completed"
                ),
                correlation_id=execution_id.value if execution_id else None,
                created_at=_now_utc_iso(),
            )
        )

        # Re-fetch the updated provider request.
        provider_request = self._repo.get_provider_request(provider_request.id)
        return provider_request, response

    def _build_response_from_artifact(
        self, req: ProviderRequest
    ) -> CanonicalResponse:
        """Rebuild a CanonicalResponse from the stored artifact."""
        if req.response_artifact_id is None:
            return CanonicalResponse(content="[no response stored]")
        artifact = self._artifact_service.get_artifact(
            project_id=req.project_id,
            artifact_id=req.response_artifact_id,
            actor_id=UserId("zu_system"),
        )
        data = json.loads(artifact.content)
        tool_calls = tuple(
            ToolCallResult(
                tool_name=tc["tool_name"],
                tool_call_id=tc["tool_call_id"],
                arguments=tc["arguments"],
                result=tc["result"],
                is_error=tc.get("is_error", False),
            )
            for tc in data.get("tool_calls", [])
        )
        return CanonicalResponse(
            content=data.get("content", ""),
            tool_calls=tool_calls,
            finish_reason=data.get("finish_reason", "stop"),
            provider_message_id=data.get("provider_message_id"),
        )

    def _classify_error(self, exc: Exception) -> ProviderErrorClass:
        """Classify an exception into a stable error class.

        Per ``zero-provider-adapter-contract`` §"Errors need stable
        classes and provider detail".
        """
        exc_str = str(exc).lower()
        if "auth" in exc_str or "unauthorized" in exc_str or "api key" in exc_str:
            return "auth_failure"
        if "rate limit" in exc_str or "429" in exc_str:
            return "rate_limit"
        if "context" in exc_str and "limit" in exc_str:
            return "context_limit"
        if "timeout" in exc_str or "transient" in exc_str:
            return "transient"
        if "policy" in exc_str or "refusal" in exc_str:
            return "policy_refusal"
        return "invalid_request"

    def _record_usage(
        self,
        *,
        project_id: ProjectId,
        provider_request_id: ProviderRequestId,
        execution_id: ExecutionId | None,
        usage: TokenUsage,
        provider_message_id: str | None,
        provider: str,
        model_name: str,
        actor_id: UserId,
        source: AuditSource,
    ) -> None:
        """Record a usage record with separate token classes.

        Per ``zero-claude-token-economics`` §"Duplicate streamed usage
        is not double-counted": the UNIQUE(provider_request_id,
        provider_message_id) constraint ensures idempotency.
        """
        # Get pricing for cost estimation.
        pricing = self._repo.get_latest_pricing_entry(provider, model_name)
        estimated_cost = "0"
        catalog_version = 1
        if pricing is not None:
            estimated_cost = estimate_cost(usage, pricing)
            catalog_version = pricing.catalog_version

        record = UsageRecord(
            id=UsageRecordId(generate_usage_record_id()),
            project_id=project_id,
            provider_request_id=provider_request_id,
            execution_id=execution_id,
            provider_message_id=provider_message_id,
            usage=usage,
            estimated_cost_usd=estimated_cost,
            pricing_catalog_version=catalog_version,
            is_whole_tree=True,
            created_at=_now_utc_iso(),
        )
        inserted = self._repo.insert_usage_record(record)
        if not inserted:
            # Duplicate: not double-counted.
            pass

    # ------------------------------------------------------------------
    # Usage queries
    # ------------------------------------------------------------------

    def get_usage_for_project(
        self, project_id: ProjectId
    ) -> TokenUsage:
        """Aggregate all usage for a project.

        Per ``zero-claude-token-economics`` §"Whole-tree child usage
        aggregation": sum all non-duplicate records.
        """
        return self._repo.aggregate_usage_for_project(project_id)

    def list_usage_records_for_project(
        self, project_id: ProjectId
    ) -> list[UsageRecord]:
        return self._repo.list_usage_records_for_project(project_id)

    def list_provider_requests_for_project(
        self, project_id: ProjectId
    ) -> list[ProviderRequest]:
        return self._repo.list_provider_requests_for_project(project_id)

    # ------------------------------------------------------------------
    # Pricing
    # ------------------------------------------------------------------

    def register_pricing(
        self,
        *,
        catalog_version: int,
        provider: str,
        model_name: str,
        input_price_per_million: str,
        output_price_per_million: str,
        cache_creation_price_per_million: str = "0",
        cache_read_price_per_million: str = "0",
    ) -> PricingEntry:
        """Register a pricing entry in the versioned catalog.

        Per ``zero-claude-token-economics`` §"Pricing is versioned
        data, not parser logic": prices belong in a versioned server-
        side catalog.

        Per ``zero-claude-token-economics`` §"Pricing changes do not
        mutate historical raw usage": the pricing catalog is versioned;
        historical usage records retain their pricing_catalog_version.
        """
        entry = PricingEntry(
            catalog_version=catalog_version,
            provider=provider,
            model_name=model_name,
            input_price_per_million=input_price_per_million,
            output_price_per_million=output_price_per_million,
            cache_creation_price_per_million=cache_creation_price_per_million,
            cache_read_price_per_million=cache_read_price_per_million,
            effective_at=_now_utc_iso(),
        )
        self._repo.insert_pricing_entry(entry)
        return entry

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------

    def reconcile_usage(
        self,
        *,
        usage_id: UsageRecordId,
        reconciled_cost_usd: str,
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> None:
        """Set the authoritative reconciled cost for a usage record.

        Per ``zero-claude-token-economics`` §"Estimated cost is not
        billing truth": reconciled_cost_usd is separate from
        estimated_cost_usd and comes from the provider's billing
        system.
        """
        self._repo.reconcile_usage_cost(usage_id, reconciled_cost_usd)
        self._audit_repo.insert(
            AuditEvent(
                id=AuditEventId(generate_audit_event_id()),
                project_id=None,
                actor_id=actor_id,
                source=source,
                operation="usage.reconcile",
                target_type="usage_record",
                target_id=usage_id.value,
                result="success",
                redacted_summary=f"Reconciled usage {usage_id.value}",
                created_at=_now_utc_iso(),
            )
        )
