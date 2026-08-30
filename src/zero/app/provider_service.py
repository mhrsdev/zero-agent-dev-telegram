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
import logging
import random
import re
import sqlite3
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from decimal import Decimal
from threading import Event
from typing import Any

from zero.app.clock import now_utc_iso

logger = logging.getLogger(__name__)

#: Error classes eligible for same-provider in-process retry before the
#: fallback chain is consulted. Both classes provably produced no
#: completed response, so redelivery cannot double-spend a result.
_SAME_PROVIDER_RETRYABLE_CLASSES = frozenset({"transient", "rate_limit"})


def _same_provider_backoff_seconds(exc: Exception, attempt: int) -> float:
    """Jittered exponential backoff honoring Retry-After (Hermes parity).

    Reference semantics: exponential base with uniform jitter, and an
    explicit provider ``Retry-After`` (surfaced by adapters as
    ``(retry_after=N)``) overrides the curve, capped well under the
    request lease window.
    """
    match = re.search(r"\(retry_after=(\d+)", str(exc))
    if match:
        base = min(60.0, float(match.group(1)))
    else:
        base = min(20.0, 1.0 * (2 ** max(0, attempt - 1)))
    return base + random.uniform(0.0, base * 0.5)


from zero.app.artifact_service import ArtifactService
from zero.app.authorization_service import AuthorizationService
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
    RETRIABLE_ERROR_CLASSES,
    CanonicalRequest,
    CanonicalResponse,
    CanonicalStreamEvent,
    PricingEntry,
    ProviderCancelledError,
    ProviderErrorClass,
    ProviderModel,
    ProviderModelNotFoundError,
    ProviderRequest,
    ProviderRequestId,
    ProviderRequestStateError,
    ProviderUnknownOutcomeError,
    TokenUsage,
    ToolCallResult,
    UsageRecord,
    UsageRecordId,
    coerce_tool_declarations,
)
from zero.persistence.repositories.audit_repository import AuditRepository
from zero.persistence.repositories.provider_repository import (
    ProviderRepository,
)


def request_dedup_scope(
    project_id: ProjectId,
    execution_id: ExecutionId | None,
    idempotency_key: str | None = None,
) -> str:
    """Return the logical idempotency scope for a provider request.

    Explicit idempotency keys are project-scoped and intentionally survive
    execution lookup changes.  Requests without a key remain deduplicated by
    project plus execution, which prevents replaying one task's prompt as a
    different task's result.
    """
    if idempotency_key:
        return f"{project_id.value}:idempotency:{idempotency_key}"
    if execution_id is None:
        return project_id.value
    return f"{project_id.value}:{execution_id.value}"


def estimate_cost(
    usage: TokenUsage,
    pricing: PricingEntry,
) -> str:
    """Estimate cost in USD from usage and pricing.

    Per ``zero-claude-token-economics`` §"Estimated cost is not billing
    truth": this is a client-side estimate, NOT an invoice.
    """
    input_cost = (
        Decimal(usage.input_tokens) * Decimal(pricing.input_price_per_million) / Decimal(1000000)
    )
    output_cost = (
        Decimal(usage.output_tokens) * Decimal(pricing.output_price_per_million) / Decimal(1000000)
    )
    cache_create_cost = (
        Decimal(usage.cache_creation_input_tokens)
        * Decimal(pricing.cache_creation_price_per_million)
        / Decimal(1000000)
    )
    cache_read_cost = (
        Decimal(usage.cache_read_input_tokens)
        * Decimal(pricing.cache_read_price_per_million)
        / Decimal(1000000)
    )
    total = input_cost + output_cost + cache_create_cost + cache_read_cost
    return str(total.quantize(Decimal("0.000001")))


def estimate_request_tokens(request: CanonicalRequest) -> int:
    """Pre-flight input token estimate for a canonical request (GAP 11).

    Uses the real tokenizer seam with the request's model name so GPT
    family models get exact tiktoken counts when available; other
    models keep the documented bytes÷4 approximation. Server-reported
    usage remains the only billing truth.
    """
    from zero.manage.core.tokenizer import count_tokens

    total = count_tokens(request.system_message or "", request.model_name)
    for message in request.messages:
        total += count_tokens(message.content, request.model_name)
        for _name, _call_id, arguments in message.tool_calls:
            total += count_tokens(arguments, request.model_name)
    for declaration in coerce_tool_declarations(request.tools):
        total += count_tokens(json.dumps(declaration.normalized_parameters()), request.model_name)
    return total


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
        authorization: AuthorizationService,
        *,
        include_fake: bool = False,
        metrics: Any | None = None,
        provider_max_attempts: int = 2,
    ) -> None:
        self._repo = provider_repo
        self._artifact_service = artifact_service
        self._audit_repo = audit_repo
        self._authorization = authorization
        self._metrics = metrics
        # Total dispatch attempts per request (>=1). Retries apply only
        # to provably-unanswered transient/rate-limit failures.
        self._provider_max_attempts = max(1, min(8, int(provider_max_attempts)))
        self._adapters: dict[str, ProviderAdapter] = {}
        self._fallback_chain: tuple[str, ...] = ()
        # Hermes parity (audit 2026-08-28): fallback entries are (provider,
        # model) pairs. The wizard/config's ``routing.fallback_models``
        # promise model-level failover within the primary provider — a
        # single-gateway deployment previously had a DEGENERATE chain
        # (only one adapter registered) so model outages could never
        # route anywhere. ``_fallback_models`` extends the chain with
        # same-provider alternative models before other providers.
        self._fallback_models: tuple[str, ...] = ()
        # (provider, model) -> bool: does the gateway honor the ``tools``
        # parameter natively? Probed lazily once per pair (see
        # ``tool_call_support``); the probe exists because some gateways
        # ACCEPT the tools parameter and then silently drop it.
        self._tool_call_support: dict[tuple[str, str], bool] = {}
        self._register_default_adapters(include_fake=include_fake)

    def _register_default_adapters(self, *, include_fake: bool) -> None:
        # The deterministic adapter is explicitly test-only. Production and
        # development must configure a real adapter instead of silently
        # presenting fabricated model output as a live provider.
        if include_fake:
            self._adapters["fake"] = FakeProviderAdapter(self._repo)

    def register_adapter(self, adapter: ProviderAdapter) -> None:
        """Register a custom adapter."""
        self._adapters[adapter.provider_name] = adapter

    @property
    def registered_provider_names(self) -> tuple[str, ...]:
        """Names of adapters that can serve live model requests."""
        return tuple(sorted(self._adapters))

    def tool_call_support(self, provider: str, model_name: str) -> bool:
        """Does this (provider, model) honor the native ``tools`` parameter?

        Some gateways accept the OpenAI ``tools`` parameter and then
        silently drop it: every response comes back with
        ``tool_calls: []`` while the model hallucinates tool-like text.
        One tiny probe request per (provider, model) per process decides
        the truth — a declaration-only tool the model cannot resist
        calling ("echo_check") separates the two behaviors deterministically:

        - a native ``tool_call`` in the response → native works;
        - text-only response → the gateway strips tools → callers fall
          back to the text tool protocol.

        Probe failures (gateway unreachable, unknown model) default to
        ``True`` (native): the fallback protocol must never mask a real
        outage, and the native path is the historical behavior.
        """
        key = (provider, model_name)
        cached = self._tool_call_support.get(key)
        if cached is not None:
            return cached
        # NOTE: a persisted ``native_tools`` capability is NOT trusted as
        # proof — it is resolved from model metadata, and the whole point
        # of this probe is that some gateways advertise what they then
        # silently strip. The probe runs once per (provider, model) per
        # process; when it observes stripping, the capability is removed
        # from the model row so every other surface adapts.
        supported = True
        try:
            from zero.domain.providers import (
                CanonicalMessage,
                CanonicalRequest,
                ToolDeclaration,
            )

            probe = CanonicalRequest(
                provider=provider,
                model_name=model_name,
                messages=(
                    CanonicalMessage(
                        role="user",
                        content=(
                            "Call the echo_check tool exactly now, with empty "
                            "arguments. Do not answer in words."
                        ),
                    ),
                ),
                tools=(
                    ToolDeclaration(
                        name="echo_check",
                        description=(
                            "Mandatory handshake tool. The very next action "
                            "must be a call to this tool with empty arguments."
                        ),
                        parameters={"type": "object", "properties": {}},
                    ),
                ),
                max_tokens=64,
                stream=False,
            )
            _request, response = self._adapters[provider].send_request(probe)
            supported = bool(response.tool_calls)
            if not supported:
                logger.warning(
                    "tool-call probe: gateway %s:%s strips native tools — "
                    "text-protocol tool calling enabled",
                    provider,
                    model_name,
                )
        except Exception:
            supported = True
        if not supported:
            # Record the observed truth on the model row: every other
            # surface (decomposition ladder, planner, forced tool-choice)
            # keys off the ``native_tools`` capability and will degrade
            # to the text contract without another probe round trip.
            try:
                model = self.get_model(provider, model_name)
                kept = tuple(
                    capability
                    for capability in model.capabilities
                    if capability != "native_tools"
                )
                if len(kept) != len(model.capabilities):
                    self._repo.set_provider_model_capabilities(model, kept)
            except Exception:  # noqa: BLE001 - capability bookkeeping is best-effort
                pass
        self._tool_call_support[key] = supported
        return supported

    def set_tool_call_support(self, provider: str, model_name: str, supported: bool) -> None:
        """Override the cached probe result (tests / operator knowledge)."""
        self._tool_call_support[(provider, model_name)] = bool(supported)

    def set_fallback_chain(self, chain: tuple[str, ...]) -> None:
        """Configure the ordered provider fallback chain.

        Per the release audit (Hermes parity): provider selection and
        fallback routing must be a real capability. The chain is tried
        in order when the primary attempt fails with a retryable error
        class (transient/rate_limit/auth_failure); policy refusals,
        context limits, cancellations, and unknown outcomes are never
        retried on another provider.
        """
        unknown = [name for name in chain if name not in self._adapters]
        if unknown:
            raise ValueError(f"fallback providers not registered: {unknown}")
        self._fallback_chain = tuple(chain)

    def set_fallback_models(self, models: tuple[str, ...]) -> None:
        """Configure ordered fallback MODEL names on the primary provider.

        Hermes fallback chain entries are (provider, model) pairs; the
        deployment's routing config promises alternative models on the
        same gateway. Each entry is validated against the provider's
        resolvable models lazily at dispatch time (an unknown fallback
        model is skipped, never fatal).
        """
        cleaned: list[str] = []
        for name in models:
            value = str(name).strip()
            if value and value not in cleaned:
                cleaned.append(value)
        self._fallback_models = tuple(cleaned)

    @property
    def fallback_models(self) -> tuple[str, ...]:
        """Ordered same-provider fallback model names."""
        return self._fallback_models

    #: Error classes eligible for another provider attempt.
    #: Hermes parity (audit 2026-08-28): ``auth_failure`` joins the set —
    # a bad/expired primary credential must escalate to the fallback
    # chain instead of killing the task when a healthy fallback exists.
    FALLBACK_ELIGIBLE_CLASSES: frozenset[str] = frozenset(
        {"transient", "rate_limit", "auth_failure"}
    )

    def send_request_with_fallback(
        self,
        *,
        project_id: ProjectId,
        actor_id: UserId,
        execution_id: ExecutionId | None = None,
        request: CanonicalRequest,
        cancel_event=None,
        source: AuditSource = "system",
        agent_scope: str | None = None,
        stream_observer: Callable[[dict], None] | None = None,
    ):
        """Send one request, falling back down the configured chain.

        Each attempt is its own durable provider request (its own hash
        and identity); a fallback never replays an uncertain outcome.
        The response of the first successful attempt wins; the original
        exception surfaces when every attempt fails.

        ``stream_observer`` (GAP 5) receives client-safe event dicts as
        they arrive when the request streams; durable bookkeeping is
        unchanged.
        """
        from zero.domain.providers import CanonicalRequest as _CanonicalRequest

        # Hermes parity (audit 2026-08-28): the chain is a list of
        # (provider, model) pairs. Same-provider fallback MODELS come
        # first (the deployment's routing.fallback_models contract),
        # then other registered providers with the original model.
        # Each attempt is its own durable provider request (the hash
        # includes model_name), so a fallback never replays an
        # uncertain outcome.
        chain: list[tuple[str, str]] = [(request.provider, request.model_name)]
        for model_name in self._fallback_models:
            if model_name != request.model_name and (request.provider, model_name) not in chain:
                chain.append((request.provider, model_name))
        for name in self._fallback_chain:
            if name != request.provider and all(name != p for p, _m in chain):
                chain.append((name, request.model_name))
        last_exc: Exception | None = None
        for index, (provider_name, model_name) in enumerate(chain):
            if cancel_event is not None and cancel_event.is_set():
                raise last_exc or RuntimeError("cancelled before dispatch")
            attempt_request = (
                request
                if (provider_name, model_name) == (request.provider, request.model_name)
                else _CanonicalRequest(
                    provider=provider_name,
                    model_name=model_name,
                    messages=request.messages,
                    tools=request.tools,
                    tool_choice=request.tool_choice,
                    system_message=request.system_message,
                    max_tokens=request.max_tokens,
                    temperature=request.temperature,
                    stream=False,
                )
            )
            try:
                # Unknown models on a fallback provider skip forward to
                # the next chain entry instead of aborting the chain:
                # a model the fallback cannot resolve is a routing
                # concern, not a request failure.
                try:
                    self.get_model(provider_name, attempt_request.model_name)
                except ProviderModelNotFoundError as model_exc:
                    last_exc = model_exc
                    if index == len(chain) - 1:
                        raise
                    continue
                return self.send_request(
                    project_id=project_id,
                    actor_id=actor_id,
                    execution_id=execution_id,
                    request=attempt_request,
                    cancel_event=cancel_event,
                    source=source,
                    agent_scope=agent_scope,
                    stream_observer=stream_observer,
                )
            except Exception as exc:
                last_exc = exc
                error_class = self._classify_error(exc)
                if error_class not in self.FALLBACK_ELIGIBLE_CLASSES or index == len(chain) - 1:
                    raise
        raise last_exc or RuntimeError("provider fallback chain exhausted")

    # ------------------------------------------------------------------
    # Model resolution
    # ------------------------------------------------------------------

    def get_model(self, provider: str, model_name: str) -> ProviderModel:
        """Resolve model capability/context metadata.

        Per ``zero-provider-adapter-contract`` §"Capabilities replace
        provider-name conditionals": capabilities describe observed
        contract support, not provider-name conditionals.
        """
        try:
            model = self._repo.get_provider_model(provider, model_name)
        except ProviderModelNotFoundError:
            adapter = self._adapters.get(provider)
            if adapter is None:
                raise
            model = adapter.get_model(model_name)
            try:
                self._repo.insert_provider_model(model)
            except sqlite3.IntegrityError:
                # A concurrent resolver may have registered the same model.
                model = self._repo.get_provider_model(provider, model_name)
        if not model.is_active:
            raise ProviderModelNotFoundError(f"Provider model {provider}:{model_name} is inactive")
        return model

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
        idempotency_key: str | None = None,
        permission: str = "execution.start",
        cancel_event: Event | None = None,
        lease_owner: str | None = None,
        lease_seconds: int = 300,
        source: AuditSource = "system",
        agent_scope: str | None = None,
        stream_observer: Callable[[dict], None] | None = None,
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
        self._authorization.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission=permission,  # type: ignore[arg-type]
            source=source,
        )
        if idempotency_key is not None:
            if not isinstance(idempotency_key, str) or not idempotency_key.strip():
                raise ValueError("idempotency_key must be a non-empty string when supplied")
            if len(idempotency_key) > 256:
                raise ValueError("idempotency_key exceeds the maximum length")
            idempotency_key = idempotency_key.strip()

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
        req_hash = compute_request_hash(
            request,
            scope=request_dedup_scope(project_id, execution_id, idempotency_key),
        )

        # An explicit idempotency key is a durable logical identity.
        # Reuse with a different payload is rejected rather than replayed;
        # a completed request replays from its stored artifact, and a
        # failed request is retryable under the same durable identity
        # when its error class allows it. Active and unknown outcomes are
        # not safe to duplicate without provider-specific idempotency.
        existing = (
            self._repo.get_provider_request_by_idempotency_key(project_id, idempotency_key)
            if idempotency_key is not None
            else None
        )
        if existing is not None and existing.request_hash != req_hash:
            raise ValueError("idempotency_key was already used for a different request")
        if existing is None:
            existing = self._repo.get_provider_request_by_hash(project_id, req_hash)
        if existing is not None:
            if existing.project_id != project_id:
                raise ValueError("provider request project scope mismatch")
            if existing.state == "completed":
                response = self._build_response_from_artifact(existing, actor_id)
                return existing, response

        # Resolve and validate the active model before claiming a new
        # request or reopening a failed one. Historical completed replay
        # above remains readable even if its model is later disabled.
        model = self.get_model(request.provider, request.model_name)
        if request.stream and not model.has_capability("streaming"):
            raise ValueError(
                f"provider model {request.provider}:{request.model_name} does not support streaming"
            )
        if request.tools and not model.has_capability("native_tools"):
            raise ValueError(
                f"provider model {request.provider}:{request.model_name} does not support native tools"
            )
        if request.max_tokens > model.max_output_tokens:
            raise ValueError(
                f"max_output_tokens {request.max_tokens} exceeds model limit "
                f"{model.max_output_tokens}"
            )

        if existing is not None:
            if existing.state != "failed":
                raise RuntimeError(
                    f"provider request {existing.id.value} is {existing.state}; replay is not safe"
                )
            if existing.error_class not in RETRIABLE_ERROR_CLASSES:
                raise RuntimeError(
                    f"provider request {existing.id.value} failed with a not retryable class {existing.error_class!r}"
                )
            provider_request = existing
        else:
            # Create the provider request record.
            provider_request = ProviderRequest(
                id=ProviderRequestId(generate_provider_request_id()),
                project_id=project_id,
                execution_id=execution_id,
                provider=request.provider,
                model_name=request.model_name,
                request_hash=req_hash,
                state="pending",
                idempotency_key=idempotency_key,
                started_at=now_utc_iso(),
            )
            claimed = self._repo.insert_provider_request(provider_request)
            if not claimed:
                winner = (
                    self._repo.get_provider_request_by_idempotency_key(project_id, idempotency_key)
                    if idempotency_key is not None
                    else self._repo.get_provider_request_by_hash(project_id, req_hash)
                )
                if winner is None:
                    raise RuntimeError("provider request claim disappeared")
                if winner.project_id != project_id:
                    raise ValueError("provider request project scope mismatch")
                if idempotency_key is not None and winner.request_hash != req_hash:
                    raise ValueError("idempotency_key was already used for a different request")
                if winner.state == "completed":
                    response = self._build_response_from_artifact(winner, actor_id)
                    return winner, response
                if winner.state == "failed":
                    if winner.error_class not in RETRIABLE_ERROR_CLASSES:
                        raise RuntimeError(
                            f"provider request {winner.id.value} failed with a not retryable class "
                            f"{winner.error_class!r}"
                        )
                    provider_request = winner
                else:
                    raise RuntimeError(
                        f"provider request {winner.id.value} is {winner.state}; replay is not safe"
                    )

            # The actual active claim is fenced separately from the logical
            # idempotency insert, so stale workers cannot finalize a retry.

        provider_request = self._repo.claim_provider_request(
            provider_request.id,
            claim_owner=lease_owner or f"provider:{actor_id.value}",
            lease_seconds=lease_seconds,
        )
        claim_token = provider_request.claim_token
        if not claim_token:  # pragma: no cover - repository invariant
            raise RuntimeError("provider request claim did not return a fencing token")

        # Get the adapter and send the request.
        adapter = self._adapters.get(request.provider)
        if adapter is None:
            self._repo.update_provider_request_state(
                provider_request.id,
                "failed",
                error_class="invalid_request",
                error_message=f"No adapter registered for provider {request.provider!r}",
                claim_token=claim_token,
            )
            raise ValueError(f"No adapter registered for provider {request.provider!r}")

        provider_started = time.monotonic()
        attempts_allowed = max(1, int(self._provider_max_attempts))
        attempts_used = 0
        while True:
            try:
                if cancel_event is not None and cancel_event.is_set():
                    raise ProviderCancelledError("provider request cancelled before dispatch")
                if request.stream:
                    events = adapter.send_request_stream(request, cancel_event=cancel_event)
                    if stream_observer is not None:
                        events = self._tap_stream(events, stream_observer)
                    response = self._collect_stream(
                        events,
                        cancel_event=cancel_event,
                        heartbeat=lambda: self._repo.heartbeat_provider_request(
                            provider_request.id,
                            claim_token=claim_token,
                            lease_seconds=lease_seconds,
                        ),
                    )
                else:
                    response = (
                        adapter.send_request(request, cancel_event=cancel_event)
                        if cancel_event is not None
                        else adapter.send_request(request)
                    )
                break
            except Exception as exc:
                error_class = self._classify_error(exc)
                # Same-provider bounded retry for transient/rate-limit
                # failures (Hermes conversation_loop parity): these
                # classes never reached a completed response, so an
                # in-process redelivery cannot double-spend a result.
                if (
                    error_class in _SAME_PROVIDER_RETRYABLE_CLASSES
                    and attempts_used + 1 < attempts_allowed
                    and not (cancel_event is not None and cancel_event.is_set())
                ):
                    attempts_used += 1
                    delay = _same_provider_backoff_seconds(exc, attempts_used)
                    logger.debug(
                        "provider %s transient failure (%s); retry %d/%d in %.2fs",
                        request.provider,
                        error_class,
                        attempts_used + 1,
                        attempts_allowed,
                        delay,
                    )
                    time.sleep(delay)
                    continue
                # Classify the error.
                error_class = self._classify_error(exc)
                terminal_state = (
                    "unknown"
                    if error_class == "unknown_outcome"
                    else "cancelled"
                    if error_class == "cancelled"
                    else "failed"
                )
                self._repo.update_provider_request_state(
                    provider_request.id,
                    terminal_state,
                    error_class=error_class,
                    error_message="provider request failed",
                    claim_token=claim_token,
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
                        created_at=now_utc_iso(),
                    )
                )
                if self._metrics is not None:
                    metric_result = (
                        "cancelled"
                        if error_class == "cancelled"
                        else "error"
                        if error_class == "unknown_outcome"
                        else "failure"
                    )
                    self._metrics.increment(
                        "provider_requests_total", result=metric_result, source=source
                    )
                    self._metrics.observe_duration(
                        "provider_request_duration_ms",
                        (time.monotonic() - provider_started) * 1000,
                    )
                raise

        # Store the response as an artifact.
        response_text = json.dumps(
            {
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
                "usage": (
                    {
                        "input_tokens": response.usage.input_tokens,
                        "output_tokens": response.usage.output_tokens,
                        "cache_creation_input_tokens": response.usage.cache_creation_input_tokens,
                        "cache_read_input_tokens": response.usage.cache_read_input_tokens,
                    }
                    if response.usage is not None
                    else None
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
        try:
            with self._repo.database.transaction():
                response_artifact = self._artifact_service.store_artifact(
                    project_id=project_id,
                    actor_id=actor_id,
                    kind="other",
                    content=response_text,
                    producer=f"provider:{request.provider}:{request.model_name}",
                    provenance=json.dumps(
                        {
                            "provider_request_id": provider_request.id.value,
                            "execution_id": execution_id.value if execution_id else None,
                        }
                    ),
                    source=source,
                    commit=False,
                )

                self._repo.update_provider_request_state(
                    provider_request.id,
                    "completed",
                    response_artifact_id=response_artifact.id,
                    claim_token=claim_token,
                    commit=False,
                )

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
                        commit=False,
                        is_whole_tree=(agent_scope is None or agent_scope != "sub_agent_type"),
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
                        result="success",
                        redacted_summary=(
                            f"Provider {request.provider}:{request.model_name} request completed"
                        ),
                        correlation_id=execution_id.value if execution_id else None,
                        created_at=now_utc_iso(),
                    ),
                    commit=False,
                )
        except Exception as exc:
            # The provider response was received, but the durable artifact /
            # usage / audit transaction did not commit.  This is an unknown
            # outcome, not a retryable definite failure: a later worker must
            # not replay the provider call without an explicit reconciliation
            # decision.  Finalize the request outside the failed transaction
            # while the claim token still fences stale workers.
            try:
                self._repo.update_provider_request_state(
                    provider_request.id,
                    "unknown",
                    error_class="unknown_outcome",
                    error_message="provider response finalization failed",
                    claim_token=claim_token,
                )
            except Exception as state_exc:
                raise ProviderRequestStateError(
                    "provider response finalization failed and state could not be finalized"
                ) from state_exc
            if isinstance(exc, (OSError, sqlite3.Error)):
                raise ProviderRequestStateError("provider response finalization failed") from exc
            raise

        # Re-fetch the updated provider request and bind the stored raw
        # response artifact onto the canonical response.
        response = replace(response, raw_response_artifact_id=response_artifact.id)
        provider_request = self._repo.get_provider_request(provider_request.id)
        if self._metrics is not None:
            self._metrics.increment("provider_requests_total", result="success", source=source)
            self._metrics.observe_duration(
                "provider_request_duration_ms",
                (time.monotonic() - provider_started) * 1000,
            )
        return provider_request, response

    @staticmethod
    def _tap_stream(
        events: Iterator[CanonicalStreamEvent],
        observer: Callable[[dict], None],
    ) -> Iterator[CanonicalStreamEvent]:
        """Forward client-safe event dicts while passing events through.

        Per GAP 5: usage events stay internal (accounting only); text
        deltas, resolved tool calls, and the terminal marker are
        forwarded. Observer failures never break the provider stream.
        """

        def _safe_observe(payload: dict) -> None:
            try:
                observer(payload)
            except Exception:
                logger.debug("stream observer raised; event dropped", exc_info=True)

        for event in events:
            if event.kind == "text_delta":
                _safe_observe({"type": "text_delta", "text": event.text})
            elif event.kind == "tool_call_delta" and event.tool_call is not None:
                call = event.tool_call
                if call.tool_call_id:
                    arguments: Any = call.arguments
                    try:
                        arguments = json.loads(call.arguments)
                    except (TypeError, ValueError):
                        arguments = call.arguments
                    _safe_observe(
                        {
                            "type": "tool_call",
                            "name": call.tool_name,
                            "arguments": arguments,
                        }
                    )
            elif event.kind == "message_end":
                _safe_observe({"type": "done", "finish_reason": event.finish_reason or "stop"})
            yield event

    def _collect_stream(
        self,
        events: Iterator[CanonicalStreamEvent],
        *,
        cancel_event: Event | None = None,
        heartbeat: Callable[[], bool] | None = None,
    ) -> CanonicalResponse:
        text_parts: list[str] = []
        call_names: dict[str, str] = {}
        call_arguments: dict[str, str] = {}
        pending_name: str | None = None
        usage: TokenUsage | None = None
        finish_reason = "stop"
        provider_message_id: str | None = None
        for event in events:
            if cancel_event is not None and cancel_event.is_set():
                raise ProviderCancelledError("provider stream cancelled")
            if heartbeat is not None and not heartbeat():
                raise ProviderRequestStateError("provider request lease was lost")
            if event.provider_message_id:
                provider_message_id = event.provider_message_id
            if event.kind == "text_delta":
                text_parts.append(event.text)
            elif event.kind == "tool_call_delta" and event.tool_call is not None:
                call = event.tool_call
                if not call.tool_call_id:
                    # Some providers emit the function name in its own
                    # delta before the identifier arrives; buffer it and
                    # attach it to the next id-bearing delta instead of
                    # silently dropping the whole call.
                    if call.tool_name:
                        pending_name = call.tool_name
                    continue
                effective_name = call.tool_name or pending_name
                call_names[call.tool_call_id] = effective_name or call_names.get(
                    call.tool_call_id, ""
                )
                pending_name = None
                call_arguments[call.tool_call_id] = (
                    call_arguments.get(call.tool_call_id, "") + call.arguments
                )
            elif event.kind == "usage":
                usage = event.usage
            elif event.kind == "message_end" and event.finish_reason:
                finish_reason = event.finish_reason
        tool_calls = tuple(
            ToolCallResult(
                tool_name=call_names[call_id],
                tool_call_id=call_id,
                arguments=call_arguments.get(call_id, "{}"),
                result="",
            )
            for call_id in call_names
        )
        return CanonicalResponse(
            content="".join(text_parts),
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
            provider_message_id=provider_message_id,
        )

    def _build_response_from_artifact(
        self, req: ProviderRequest, actor_id: UserId
    ) -> CanonicalResponse:
        """Rebuild a CanonicalResponse from the stored artifact."""
        if req.response_artifact_id is None:
            raise ProviderRequestStateError(
                f"completed provider request {req.id.value} has no response artifact"
            )
        artifact = self._artifact_service.get_artifact(
            project_id=req.project_id,
            artifact_id=req.response_artifact_id,
            actor_id=actor_id,
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
        usage_data = data.get("usage")
        usage = TokenUsage(**usage_data) if usage_data is not None else None
        if usage is None:
            usage_records = self._repo.list_usage_records_for_request(req.id)
            if usage_records:
                usage = usage_records[-1].usage
        return CanonicalResponse(
            content=data.get("content", ""),
            tool_calls=tool_calls,
            finish_reason=data.get("finish_reason", "stop"),
            provider_message_id=data.get("provider_message_id"),
            usage=usage,
        )

    def _classify_error(self, exc: Exception) -> ProviderErrorClass:
        """Classify an exception into a stable error class.

        Per ``zero-provider-adapter-contract`` §"Errors need stable
        classes and provider detail".
        """
        exc_str = str(exc).lower()
        if isinstance(exc, ProviderCancelledError):
            return "cancelled"
        # Real-run fix: a stream that ended without its terminal marker
        # never produced a completed response — the runtime consumed no
        # result (partial deltas are discarded when the collect path
        # raises), so an in-process redelivery cannot double-spend a
        # result. Same argument as the 5xx/edge-timeout classes: the
        # provider may have completed the generation server-side, but
        # operationally the request is retryable. Observed twice in the
        # r7 real run (gateway dropped the SSE body mid-generation).
        # Checked BEFORE the ProviderUnknownOutcomeError isinstance
        # branch, which would otherwise short-circuit to unknown.
        if "ended without a terminal message marker" in exc_str:
            return "transient"
        if isinstance(exc, ProviderUnknownOutcomeError):
            return "unknown_outcome"
        if isinstance(exc, ProviderRequestStateError) and "lease was lost" in exc_str:
            # The stream may have completed provider-side; only operator
            # reconciliation can know. Never auto-retry this.
            return "unknown_outcome"
        if "auth" in exc_str or "unauthorized" in exc_str or "api key" in exc_str:
            return "auth_failure"
        if "rate limit" in exc_str or "429" in exc_str:
            return "rate_limit"
        if "context" in exc_str and "limit" in exc_str:
            return "context_limit"
        if (
            "timeout" in exc_str
            or "transient" in exc_str
            or "connection" in exc_str
            or "network" in exc_str
            or "temporarily unavailable" in exc_str
            or "service unavailable" in exc_str
        ):
            return "transient"
        if "provider http request failed with status " in exc_str:
            try:
                status = int(exc_str.rsplit(" ", 1)[-1])
            except ValueError:
                status = 0
            if 500 <= status <= 599:
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
        commit: bool = True,
        is_whole_tree: bool = True,
    ) -> None:
        """Record a usage record with separate token classes.

        Per ``zero-claude-token-economics`` §"Duplicate streamed usage
        is not double-counted": the UNIQUE(provider_request_id,
        provider_message_id) constraint ensures idempotency.

        ``is_whole_tree`` is computed from the requesting scope, not
        asserted: only non-sub-agent requests count the whole usage
        tree exactly once.
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
            is_whole_tree=is_whole_tree,
            created_at=now_utc_iso(),
        )
        inserted = self._repo.insert_usage_record(record, commit=commit)
        if not inserted:
            # Duplicate: not double-counted.
            pass

    # ------------------------------------------------------------------
    # Usage queries
    # ------------------------------------------------------------------

    def get_usage_for_project(
        self,
        project_id: ProjectId,
        *,
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> TokenUsage:
        """Aggregate all usage for a project."""
        self._authorization.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="cost.view",
            source=source,
        )
        return self._repo.aggregate_usage_for_project(project_id)

    def list_usage_records_for_project(
        self,
        project_id: ProjectId,
        *,
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> list[UsageRecord]:
        self._authorization.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="cost.view",
            source=source,
        )
        return self._repo.list_usage_records_for_project(project_id)

    def list_provider_requests_for_project(
        self,
        project_id: ProjectId,
        *,
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> list[ProviderRequest]:
        self._authorization.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="cost.view",
            source=source,
        )
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
            effective_at=now_utc_iso(),
        )
        self._repo.insert_pricing_entry(entry)
        return entry

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------

    def reconcile_usage(
        self,
        *,
        project_id: ProjectId,
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
        self._authorization.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="cost.view",
            source=source,
        )
        self._repo.reconcile_usage_cost(
            project_id,
            usage_id,
            reconciled_cost_usd,
        )
        self._audit_repo.insert(
            AuditEvent(
                id=AuditEventId(generate_audit_event_id()),
                project_id=project_id,
                actor_id=actor_id,
                source=source,
                operation="usage.reconcile",
                target_type="usage_record",
                target_id=usage_id.value,
                result="success",
                redacted_summary=f"Reconciled usage {usage_id.value}",
                created_at=now_utc_iso(),
            )
        )

    # ------------------------------------------------------------------
    # Unknown-outcome reconciliation (operator workflow)
    # ------------------------------------------------------------------

    def list_unknown_requests(self, project_id: ProjectId):
        """List provider requests whose external outcome is unknown."""
        return [
            request
            for request in self._repo.list_provider_requests_for_project(project_id)
            if request.state == "unknown"
        ]

    def reconcile_provider_request(
        self,
        *,
        project_id: ProjectId,
        request_id: ProviderRequestId,
        actor_id: UserId,
        resolution: str,
        note: str = "",
        source: AuditSource = "web",
    ) -> None:
        """Record an operator's reconciliation decision for one unknown.

        Per ``zero-recovery-consistency`` §"External operations may be
        uncertain": an unknown outcome is never replayed automatically.
        Reconciliation requires an explicit operator decision:

        - ``confirmed_not_dispatched``: verified the provider never
          accepted the request. The request becomes ``failed`` with a
          reconciled error class so normal retry paths may proceed.
        - ``confirmed_dispatched``: verified the provider processed it
          but no response artifact exists. The request stays ``unknown``
          (no safe completion is possible) and the reconciliation note
          and audit record preserve the investigation.
        """
        if resolution not in {"confirmed_not_dispatched", "confirmed_dispatched"}:
            raise ValueError("resolution must be confirmed_not_dispatched or confirmed_dispatched")
        self._authorization.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="execution.start",
            source=source,
        )
        conn = self._repo.database.connect()
        row = conn.execute(
            "SELECT id FROM provider_requests WHERE id = ? AND project_id = ? AND state = 'unknown'",
            (request_id.value, project_id.value),
        ).fetchone()
        if row is None:
            raise ValueError("unknown provider request not found in this project")
        if resolution == "confirmed_not_dispatched":
            conn.execute(
                "UPDATE provider_requests SET state = 'failed', "
                "error_class = 'reconciled_not_dispatched', "
                "error_message = ? WHERE id = ?",
                (note[:500] or "operator reconciled: provider did not accept", request_id.value),
            )
        else:
            conn.execute(
                "UPDATE provider_requests SET error_message = ? WHERE id = ?",
                (
                    note[:500] or "operator reconciled: provider accepted; no response artifact",
                    request_id.value,
                ),
            )
        conn.commit()
        self._audit_repo.insert(
            AuditEvent(
                id=AuditEventId(generate_audit_event_id()),
                project_id=project_id,
                actor_id=actor_id,
                source=source,
                operation="provider.reconcile",
                target_type="provider_request",
                target_id=request_id.value,
                result="success",
                redacted_summary=f"Reconciled unknown provider request ({resolution})",
                correlation_id=None,
                created_at=now_utc_iso(),
            )
        )
