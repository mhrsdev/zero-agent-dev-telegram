"""Provider adapter contract and deterministic fake adapter.

Per ``zero-provider-adapter-contract`` SKILL.md:

- A provider adapter translates an external model runtime into Zero's
  canonical execution vocabulary.
- Canonical meaning precedes provider mapping.
- Capabilities replace provider-name conditionals.
- Persistent provider context is an optimization.
- Streaming is event processing, not string concatenation.
- Tool calls cross two trust boundaries.
- Usage has scope and authority.
- Errors need stable classes and provider detail.
- Cancellation has provider and local meanings.

Per PLAN.md M10 deliverables:
- Minimal provider contract based on real current needs.
- One real provider adapter plus one deterministic fake used only for
  tests.
- Model capability/context metadata resolution.
- Usage normalization across input, output, cache creation, cache read.
- Request/message and query deduplication.
- Whole-tree child usage aggregation.
- Versioned pricing/estimate path and separate reconciliation path.
- Provider error classification, retry boundaries, and circuit behavior
  only where required.
"""

from __future__ import annotations

import hashlib
import json
import re
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from threading import Event
from typing import Any, ClassVar
from urllib.parse import urlsplit

import httpx

from zero.domain.ids import (
    generate_provider_model_id,
)
from zero.domain.providers import (
    CanonicalMessage,
    CanonicalRequest,
    CanonicalResponse,
    CanonicalStreamEvent,
    InvalidProviderRequestError,
    ProviderError,
    ProviderModel,
    ProviderModelId,
    ProviderModelNotFoundError,
    ProviderUnknownOutcomeError,
    TokenUsage,
    ToolCallResult,
    ToolDeclaration,
    coerce_tool_declarations,
    normalize_tool_choice,
)
from zero.persistence.repositories.provider_repository import (
    ProviderRepository,
)


def _now_utc_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _render_tools(
    tools: Sequence[ToolDeclaration | str | Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Render canonical tool declarations as OpenAI function tools.

    Declarations carry the registry's real input schema so the model can
    emit well-typed arguments; a bare name falls back to an empty object
    schema rather than being dropped. Duplicate names are dropped (some
    gateways hard-fail on duplicates).
    """
    rendered: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for declaration in coerce_tool_declarations(tools):
        if not declaration.name:
            raise ProviderError("tool declaration requires a name")
        if declaration.name in seen_names:
            continue
        seen_names.add(declaration.name)
        rendered.append(
            {
                "type": "function",
                "function": {
                    "name": declaration.name,
                    "description": declaration.description or f"Zero capability {declaration.name}",
                    "parameters": declaration.normalized_parameters(),
                },
            }
        )
    return rendered


def _rate_limit_detail(response: httpx.Response) -> str:
    """Surface Retry-After from a 429 so the retry layer can honor it."""
    retry_after = response.headers.get("retry-after")
    return f" (retry_after={retry_after})" if retry_after else ""


_EDGE_403_EMPTY_BODY = (
    "provider gateway edge protection blocked the request "
    "(transient CDN edge 403 with an empty body; identical "
    "requests succeed on retry)"
)
_EDGE_403_CHALLENGE_BODY = (
    "provider gateway edge protection blocked the request "
    "(transient CDN edge 403 with a non-JSON challenge body; "
    "identical requests succeed on retry)"
)


def _read_response_body(response: Any) -> str:
    """Best-effort read of an error response body.

    Inside an ``httpx`` streaming context the body is not fetched
    unless ``read()`` is called; every other response shape exposes
    ``.text`` directly. Reading a challenge page is safe — it never
    carries credentials (error bodies are never logged either).
    """
    try:
        read = getattr(response, "read", None)
        if callable(read):
            read()
    except Exception:  # noqa: BLE001 - body read must never mask the status
        pass
    try:
        return str(getattr(response, "text", "") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _header_value(response: Any, name: str) -> str:
    """Header lookup tolerant of both httpx responses and test doubles."""
    try:
        return str(response.headers.get(name) or "")
    except AttributeError:
        return ""


def _auth_status_error(response: Any, status_code: int) -> ProviderError:
    """Classify a 401/403 response into the exception to raise.

    Hermes classification principle (``bedrock_adapter.classify_
    bedrock_error``): classify by body pattern, retry transient
    classes, fail fast only on definitive rejections.

    The operator's gateway sits behind Cloudflare, which intermittently
    answers 403 with either an EMPTY body plus CF edge headers, or a
    NON-JSON challenge/block page — identical requests succeed seconds
    later on both shapes (proven live: chat got 403→403→200 within 4s).
    Those are TRANSIENT edge blocks. A JSON error object is a definitive
    application-level rejection → auth_failure (fail fast, escalate to
    the fallback chain). 401 is always definitive.
    """
    if status_code == 403:
        body = _read_response_body(response)
        if not body:
            server = _header_value(response, "server")
            cf_ray = _header_value(response, "cf-ray")
            if "cloudflare" in server.lower() or cf_ray:
                return ProviderError(_EDGE_403_EMPTY_BODY)
        elif not body.startswith("{"):
            return ProviderError(_EDGE_403_CHALLENGE_BODY)
    return ProviderError(f"provider auth failed with status {status_code}")


def _render_openai_tool_choice(value) -> dict[str, Any] | str:
    """Render a canonical tool_choice for OpenAI-compatible endpoints.

    Modes pass through; a forced function becomes the nested
    ``{"type": "function", "function": {"name": ...}}`` wire shape.
    """
    normalized = normalize_tool_choice(value)
    if normalized is None:
        raise InvalidProviderRequestError("tool_choice is None")
    if isinstance(normalized, str):
        return normalized
    return {"type": "function", "function": {"name": normalized["name"]}}


def _apply_tool_choice(
    payload: dict[str, Any], request: CanonicalRequest, *, protocol: str
) -> None:
    """Attach the normalized tool_choice to an outgoing payload when set.

    Silent no-op when no tools were rendered (a choice without tools is
    meaningless and some gateways 400 on it), loud failure when the
    value itself is malformed.
    """
    if request.tool_choice is None or "tools" not in payload:
        return
    if protocol == "openai-compatible":
        payload["tool_choice"] = _render_openai_tool_choice(request.tool_choice)
    elif protocol == "anthropic":
        choice = normalize_tool_choice(request.tool_choice)
        if choice is None:  # pragma: no cover - guarded above
            return
        if choice == "auto":
            payload["tool_choice"] = {"type": "auto"}
        elif choice in ("required", "any"):
            payload["tool_choice"] = {"type": "any"}
        elif choice == "none":
            raise InvalidProviderRequestError(
                "anthropic has no 'none' tool_choice; drop the tool declarations instead"
            )
        else:
            payload["tool_choice"] = {"type": "tool", "name": choice["name"]}
    else:  # pragma: no cover - internal protocol registry only
        raise ValueError(f"unknown provider protocol {protocol!r}")


def _hashable_tool_choice(req: CanonicalRequest) -> Any:
    """Deterministic JSON-ready canonical view of ``tool_choice``."""
    normalized = normalize_tool_choice(req.tool_choice)
    if normalized is None:
        return None
    if isinstance(normalized, str):
        return normalized
    return {"name": normalized["name"], "type": "function"}


def compute_request_hash(
    req: CanonicalRequest,
    *,
    scope: str | None = None,
) -> str:
    """Compute a deterministic hash of a canonical request for
    deduplication.

    Per ``zero-claude-token-economics`` §"Request/message and query
    deduplication": if the same request is submitted twice, the second
    is a no-op.
    """
    request_payload: dict[str, Any] = {
        "scope": scope,
        "provider": req.provider,
        "model_name": req.model_name,
        "messages": [
            {
                "role": m.role,
                "content": m.content,
                "tool_call_id": m.tool_call_id,
                "tool_calls": list(m.tool_calls),
            }
            for m in req.messages
        ],
        "max_tokens": req.max_tokens,
        "temperature": req.temperature,
        "tools": [
            {
                "name": declaration.name,
                "description": declaration.description,
                "parameters": declaration.normalized_parameters(),
            }
            for declaration in coerce_tool_declarations(req.tools)
        ],
        "system_message": req.system_message,
        "stream": req.stream,
    }
    # Only present when set, so hashes of choice-free requests stay
    # byte-identical across the introduction of this field.
    if req.tool_choice is not None:
        request_payload["tool_choice"] = _hashable_tool_choice(req)
    # Multimodal parts change the wire payload, so they must change the
    # dedup hash too — but only when present, keeping every legacy
    # text-only hash byte-stable across this field's introduction.
    if any(m.content_parts for m in req.messages):
        request_payload["messages"] = [
            {
                "role": m.role,
                "content": m.content,
                "tool_call_id": m.tool_call_id,
                "tool_calls": list(m.tool_calls),
                "content_parts": (
                    [dict(part) for part in m.content_parts] if m.content_parts else None
                ),
            }
            for m in req.messages
        ]
    payload = json.dumps(
        request_payload,
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------
# Tool message validation (per zero-context-memory §sanitize_tool_pairs)
# ----------------------------------------------------------------------


def _message_to_dict(
    message: CanonicalMessage | Mapping[str, Any],
) -> dict[str, Any]:
    """Translate canonical messages and mapping payloads to one shape."""
    if isinstance(message, CanonicalMessage):
        tool_calls = [
            {
                "name": tool_name,
                "id": tool_call_id,
                "arguments": arguments,
            }
            for tool_name, tool_call_id, arguments in message.tool_calls
        ]
        return {
            "role": message.role,
            "content": message.content,
            "tool_call_id": message.tool_call_id,
            "tool_calls": tool_calls,
            # Multimodal parts ride along untouched; only the
            # OpenAI-compatible renderer consumes them (Hermes parity:
            # images reach the model as image_url data-URL parts).
            "content_parts": (
                tuple(dict(part) for part in message.content_parts)
                if message.content_parts
                else None
            ),
        }

    normalized = dict(message)
    calls: list[dict[str, Any]] = []
    for call in normalized.get("tool_calls") or []:
        if isinstance(call, Mapping):
            calls.append(dict(call))
        elif isinstance(call, Sequence) and len(call) == 3:
            calls.append(
                {
                    "name": call[0],
                    "id": call[1],
                    "arguments": call[2],
                }
            )
    normalized["tool_calls"] = calls
    return normalized


def validate_tool_messages(
    messages: Sequence[CanonicalMessage | Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Validate tool-call/result pairing in a message list.

    Per ``zero-context-memory`` §"sanitize_tool_pairs": drop orphan
    tool results while preserving declared/in-flight tool calls.

    Per ``zero-provider-adapter-contract`` §"Provider rendering
    validates tool-call/result shape before submission": malformed or
    orphaned tool messages are rejected or safely repaired without
    inventing success.

    Returns:
        (clean_messages, stripped_tool_call_ids) — the clean message
        list with orphan tool results removed, and the IDs of the
        stripped tool results.
    """
    seen_call_ids: set[str] = set()
    clean: list[dict[str, Any]] = []
    stripped: list[str] = []
    for raw_message in messages:
        msg = _message_to_dict(raw_message)
        role = msg.get("role")
        if role == "assistant":
            for call in msg.get("tool_calls") or []:
                call_id = str(call.get("id") or call.get("tool_call_id") or "")
                if call_id:
                    seen_call_ids.add(call_id)
            clean.append(dict(msg))
        elif role == "tool":
            call_id = str(msg.get("tool_call_id") or "")
            if call_id and call_id in seen_call_ids:
                clean.append(dict(msg))
            else:
                stripped.append(call_id)
        else:
            clean.append(dict(msg))
    return clean, stripped


# ----------------------------------------------------------------------
# Provider adapter contract
# ----------------------------------------------------------------------


class ProviderAdapter(ABC):
    """Abstract base class for provider adapters.

    Per ``zero-provider-adapter-contract``: an adapter translates an
    external model runtime into Zero's canonical execution vocabulary.
    """

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """The provider name (e.g. "openai", "anthropic", "fake")."""

    @abstractmethod
    def get_model(self, model_name: str) -> ProviderModel:
        """Resolve model capability/context metadata."""

    @abstractmethod
    def send_request(
        self,
        request: CanonicalRequest,
        *,
        cancel_event: Event | None = None,
    ) -> CanonicalResponse:
        """Send a canonical request to the provider and return the
        canonical response.

        Per ``zero-provider-adapter-contract`` §"Canonical meaning
        precedes provider mapping": the adapter maps the canonical
        request onto the provider's wire format and maps the
        provider's response back to the canonical vocabulary.

        Per ``zero-provider-adapter-contract`` §"Tool calls cross two
        trust boundaries": provider tool-call arguments are model
        output. The adapter parses them into a canonical request; Zero's
        capability runtime performs authorization and validation.
        """

    def send_request_stream(
        self,
        request: CanonicalRequest,
        *,
        cancel_event: Event | None = None,
    ) -> Iterator[CanonicalStreamEvent]:
        """Yield canonical events; adapters may override for true SSE.

        Real-run fix: the default must emit ``tool_call_delta`` events
        for the wrapped response's tool calls. Dropping them made every
        default-streamed response look like plain text — the runtimes'
        tool loop saw no delegation call and sub-agent tests silently
        degraded (the parent "answered" instead of delegating).
        """
        if cancel_event is not None and cancel_event.is_set():
            from zero.domain.providers import ProviderCancelledError

            raise ProviderCancelledError("provider request cancelled before dispatch")
        response = self.send_request(request)
        if response.content:
            yield CanonicalStreamEvent(kind="text_delta", text=response.content)
        for call in response.tool_calls:
            yield CanonicalStreamEvent(kind="tool_call_delta", tool_call=call)
        if response.usage is not None:
            yield CanonicalStreamEvent(kind="usage", usage=response.usage)
        yield CanonicalStreamEvent(
            kind="message_end",
            finish_reason=response.finish_reason,
        )


# ----------------------------------------------------------------------
# OpenAI-compatible HTTP adapter
# ----------------------------------------------------------------------


class OpenAICompatibleProviderAdapter(ProviderAdapter):
    """Small, real HTTP adapter for OpenAI-compatible chat endpoints.

    The adapter deliberately accepts an injected :class:`httpx.Client` so
    request/response behavior can be tested over a real HTTP transport seam
    without using a live credential. The client is not exposed to the
    application core and provider errors never include response bodies or
    authorization headers.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: float = 60.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("provider API key must be configured")
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("provider base URL must be an absolute HTTP(S) URL")
        if parsed.query or parsed.fragment:
            raise ValueError("provider base URL must not contain query or fragment")
        if timeout_seconds <= 0:
            raise ValueError("provider timeout must be positive")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout_seconds)

    #: Known-model context/output limits (input context window and max
    #: output tokens). Unknown models fall back to deliberately
    #: conservative defaults instead of fabricating flagship-class
    #: numbers, so compaction budgets fail safe rather than overflow.
    _MODEL_CATALOG: ClassVar[dict[str, tuple[int, int]]] = {
        "gpt-4o": (128_000, 16_384),
        "gpt-4o-mini": (128_000, 16_384),
        "gpt-4-turbo": (128_000, 4_096),
        "gpt-4": (8_192, 8_192),
        "gpt-3.5-turbo": (16_385, 4_096),
        "o1": (200_000, 100_000),
        "o1-mini": (128_000, 65_536),
        "o3-mini": (200_000, 100_000),
    }
    _DEFAULT_CONTEXT_WINDOW: ClassVar[int] = 32_768
    _DEFAULT_MAX_OUTPUT_TOKENS: ClassVar[int] = 4_096

    @property
    def provider_name(self) -> str:
        return "openai-compatible"

    def get_model(self, model_name: str) -> ProviderModel:
        # Exact-name match first; otherwise a prefix match on known
        # dated variants (e.g. "gpt-4o-2024-08-06"); otherwise the
        # conservative default.
        context_window, max_output = self._MODEL_CATALOG.get(
            model_name,
            next(
                (
                    limits
                    for known, limits in sorted(
                        self._MODEL_CATALOG.items(), key=lambda item: -len(item[0])
                    )
                    if model_name.startswith(known + "-")
                ),
                (self._DEFAULT_CONTEXT_WINDOW, self._DEFAULT_MAX_OUTPUT_TOKENS),
            ),
        )
        return ProviderModel(
            id=ProviderModelId(generate_provider_model_id()),
            provider=self.provider_name,
            model_name=model_name,
            context_window=context_window,
            max_output_tokens=max_output,
            capabilities=(
                # Cancellation is honored at dispatch boundaries and
                # between stream lines; in-flight abort of a single HTTP
                # request is NOT claimed.
                "streaming",
                "native_tools",
                "server_reported_usage",
                "cancellation",
            ),
            is_active=True,
            created_at=_now_utc_iso(),
        )

    def send_request(
        self,
        request: CanonicalRequest,
        *,
        cancel_event: Event | None = None,
    ) -> CanonicalResponse:
        if cancel_event is not None and cancel_event.is_set():
            from zero.domain.providers import ProviderCancelledError

            raise ProviderCancelledError("provider request cancelled before dispatch")
        clean_messages, _stripped = validate_tool_messages(request.messages)
        messages = self._render_messages(request, clean_messages)
        payload: dict[str, Any] = {
            "model": request.model_name,
            "messages": messages,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        if request.tools:
            payload["tools"] = _render_tools(request.tools)
            _apply_tool_choice(payload, request, protocol="openai-compatible")

        try:
            response = self._client.post(
                f"{self._base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        except httpx.ConnectError as exc:
            raise ProviderError("provider connection failed") from exc
        except httpx.RequestError as exc:
            raise ProviderUnknownOutcomeError("provider request outcome is unknown") from exc

        if response.status_code >= 400:
            # Do not include the provider body: error bodies commonly echo
            # credential fragments or request content.
            # Bug fix (Hermes-parity audit, 2026-08-28): 401/403 must carry
            # an auth-flavored message so ``ProviderService._classify_error``
            # maps them to ``auth_failure`` (fail fast, escalate to the
            # fallback chain) instead of ``invalid_request``. The
            # Anthropic adapter already raised auth-flavored errors; this
            # adapter's generic message made a bad/expired primary API key
            # look like a request defect, silently skipping fallback.
            if response.status_code in (401, 403):
                # Shared classifier (round 6): empty CF edge body AND
                # non-JSON challenge pages → transient edge block;
                # JSON rejection → auth failure. See _auth_status_error.
                raise _auth_status_error(response, response.status_code)
            detail = _rate_limit_detail(response) if response.status_code == 429 else ""
            raise ProviderError(
                f"provider HTTP request failed with status {response.status_code}{detail}"
            )

        try:
            data = response.json()
        except ValueError as exc:
            # SSE-only gateway tolerance (live fix, round 5 — Hermes
            # anthropic_adapter documents the same gateway class):
            # api.justwoker.icu answers every chat/completions request
            # that declares `tools` with text/event-stream chunks EVEN
            # WHEN the request did not ask for streaming. The planner
            # (toolless) gets JSON; chat turns with granted tools got
            # SSE and died with "provider returned invalid JSON" on
            # every conversational reply. Aggregate the chunks instead
            # of failing.
            body_text = ""
            try:
                body_text = response.text or ""
            except Exception:  # noqa: BLE001
                body_text = ""
            content_type = ""
            try:
                content_type = str(response.headers.get("content-type") or "").lower()
            except AttributeError:
                content_type = ""
            if "text/event-stream" in content_type or body_text.lstrip().startswith("data:"):
                return self._aggregate_sse_body(body_text)
            raise ProviderError("provider returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise ProviderError("provider returned an invalid response object")
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderError("provider response did not contain a choice")
        choice = choices[0]
        message = choice.get("message") if isinstance(choice, dict) else None
        if not isinstance(message, dict):
            raise ProviderError("provider response did not contain a message")

        tool_calls: list[ToolCallResult] = []
        for raw_call in message.get("tool_calls") or []:
            if not isinstance(raw_call, Mapping):
                raise ProviderError("provider returned a malformed tool call")
            function = raw_call.get("function")
            if not isinstance(function, Mapping):
                raise ProviderError("provider returned a malformed tool function")
            call_id = str(raw_call.get("id") or "")
            tool_name = str(function.get("name") or "")
            arguments = function.get("arguments", "{}")
            if not call_id or not tool_name or not isinstance(arguments, str):
                raise ProviderError("provider returned an incomplete tool call")
            tool_calls.append(
                ToolCallResult(
                    tool_name=tool_name,
                    tool_call_id=call_id,
                    arguments=arguments,
                    result="",
                )
            )

        raw_usage = data.get("usage")
        usage = self._normalize_usage(raw_usage)
        content = message.get("content")
        if content is None:
            content = ""
        if not isinstance(content, str):
            raise ProviderError("provider returned a non-text message content")
        return CanonicalResponse(
            content=content,
            tool_calls=tuple(tool_calls),
            finish_reason=str(choice.get("finish_reason") or "stop"),
            usage=usage,
            provider_message_id=(str(data["id"]) if data.get("id") is not None else None),
        )

    def send_request_stream(
        self,
        request: CanonicalRequest,
        *,
        cancel_event: Event | None = None,
    ) -> Iterator[CanonicalStreamEvent]:
        """Consume OpenAI-compatible SSE with outcome-aware errors.

        Real-run fix: a transport failure BEFORE any stream event was
        consumed is a pure connection failure — no response data ever
        existed, so there is nothing to double-deliver and the request
        is safely retryable (same class as ConnectError). Only a break
        AFTER data started flowing stays ``unknown_outcome``: the
        provider may have completed the generation, and the project's
        durable-state philosophy reserves that case for operator
        reconciliation. A gateway blip at second ~28 (r7 run: HTTP
        RemoteProtocolError before the first SSE event) previously
        wedged the whole execution as ``paused`` pending human
        reconciliation; it now retries like any transient failure.
        """
        saw_any_event = False
        try:
            for event in self._openai_stream_events(request, cancel_event=cancel_event):
                saw_any_event = True
                yield event
        except httpx.ConnectError as exc:
            raise ProviderError("provider connection failed") from exc
        except httpx.RequestError as exc:
            if not saw_any_event:
                raise ProviderError(
                    "provider stream connection failed before any stream data"
                ) from exc
            raise ProviderUnknownOutcomeError(
                "provider request outcome is unknown"
            ) from exc

    def _openai_stream_events(
        self,
        request: CanonicalRequest,
        *,
        cancel_event: Event | None = None,
    ) -> Iterator[CanonicalStreamEvent]:
        """Raw SSE consumption (transport errors propagate to the wrapper)."""
        clean_messages, _stripped = validate_tool_messages(request.messages)
        payload: dict[str, Any] = {
            "model": request.model_name,
            "messages": self._render_messages(request, clean_messages),
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "stream": True,
        }
        if request.tools:
            payload["tools"] = _render_tools(request.tools)
            _apply_tool_choice(payload, request, protocol="openai-compatible")
        with self._client.stream(
            "POST",
            f"{self._base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        ) as response:
            if response.status_code >= 400:
                # Live-run fix (round 6): task execution streams, and
                # this branch used to raise the generic status message
                # for EVERY error — a transient CDN-edge 403 was then
                # classified invalid_request and killed the task with
                # no retry (observed live: ready task failed in 21ms
                # while identical requests succeeded seconds later).
                # Classify exactly like the non-stream path: 401/403
                # via the shared auth/edge classifier, 429 with
                # Retry-After, 503/529 as temporary unavailability.
                status_code = response.status_code
                if status_code in (401, 403):
                    raise _auth_status_error(response, status_code)
                if status_code == 429:
                    detail = _rate_limit_detail(response)
                    raise ProviderError(
                        f"provider rate limit hit with status {status_code}{detail}"
                    )
                if status_code in {503, 529}:
                    raise ProviderError(
                        f"provider temporarily unavailable ({status_code})"
                    )
                raise ProviderError(
                    f"provider HTTP request failed with status {status_code}"
                )
            tool_call_ids_by_index: dict[int, str] = {}
            saw_message_end = False
            saw_any_data = False
            for raw_line in response.iter_lines():
                if cancel_event is not None and cancel_event.is_set():
                    from zero.domain.providers import ProviderCancelledError

                    raise ProviderCancelledError("provider stream cancelled")
                line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
                line = line.strip()
                if not line or not line.startswith("data:"):
                    continue
                data_text = line[5:].strip()
                if data_text == "[DONE]":
                    if not saw_message_end:
                        raise ProviderUnknownOutcomeError(
                            "provider stream ended without a terminal message marker"
                        )
                    return
                try:
                    data = json.loads(data_text)
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ProviderError("provider returned invalid stream JSON") from exc
                if not isinstance(data, Mapping):
                    raise ProviderError("provider returned an invalid stream event")
                # A parsed provider event proves data was flowing; a later
                # break is mid-generation (unknown), not pre-response
                # (retryable transport failure).
                saw_any_data = True
                provider_message_id = str(data["id"]) if data.get("id") is not None else None
                usage = self._normalize_usage(data.get("usage"))
                if data.get("usage") is not None:
                    yield CanonicalStreamEvent(
                        kind="usage",
                        usage=usage,
                        provider_message_id=provider_message_id,
                    )
                choices = data.get("choices")
                if not isinstance(choices, list) or not choices:
                    continue
                choice = choices[0]
                if not isinstance(choice, Mapping):
                    continue
                delta = choice.get("delta")
                if isinstance(delta, Mapping):
                    text = delta.get("content")
                    if isinstance(text, str) and text:
                        yield CanonicalStreamEvent(
                            kind="text_delta",
                            text=text,
                            provider_message_id=provider_message_id,
                        )
                    for raw_call in delta.get("tool_calls") or []:
                        if not isinstance(raw_call, Mapping):
                            continue
                        function = raw_call.get("function")
                        if not isinstance(function, Mapping):
                            continue
                        raw_index = raw_call.get("index")
                        try:
                            call_index = int(raw_index)
                        except (TypeError, ValueError):
                            call_index = None
                        call_id = str(raw_call.get("id") or "")
                        if call_index is not None and call_id:
                            tool_call_ids_by_index[call_index] = call_id
                        elif call_index is not None:
                            call_id = tool_call_ids_by_index.get(call_index, "")
                        yield CanonicalStreamEvent(
                            kind="tool_call_delta",
                            tool_call=ToolCallResult(
                                tool_name=str(function.get("name") or ""),
                                tool_call_id=call_id,
                                arguments=str(function.get("arguments") or ""),
                                result="",
                            ),
                            provider_message_id=provider_message_id,
                        )
                finish_reason = choice.get("finish_reason")
                if finish_reason:
                    saw_message_end = True
                    yield CanonicalStreamEvent(
                        kind="message_end",
                        finish_reason=str(finish_reason),
                        provider_message_id=provider_message_id,
                    )
            if not saw_message_end:
                if not saw_any_data:
                    # EOF (or transport break) before a single provider
                    # event: no response data ever existed, so this is a
                    # retryable transport failure, not an unknown outcome.
                    raise ProviderError(
                        "provider stream connection failed before any stream data"
                    )
                raise ProviderUnknownOutcomeError(
                    "provider stream ended without a terminal message marker"
                )

    @staticmethod
    def _render_messages(
        request: CanonicalRequest,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        rendered: list[dict[str, Any]] = []
        if request.system_message:
            rendered.append({"role": "system", "content": request.system_message})
        for message in messages:
            item: dict[str, Any] = {
                "role": message.get("role"),
                "content": message.get("content", ""),
            }
            # Multimodal parts (Hermes parity, round 5 gap 5): when a
            # message carries OpenAI-compatible content parts (text +
            # image_url data URLs), they REPLACE the plain string content
            # on the wire — verified live against the operator's gateway
            # (claude-opus-5 answered a color probe about a real PNG).
            parts = message.get("content_parts")
            if parts:
                item["content"] = [dict(part) for part in parts]
            if message.get("role") == "tool":
                item["tool_call_id"] = message.get("tool_call_id")
            if message.get("role") == "assistant" and message.get("tool_calls"):
                item["tool_calls"] = [
                    {
                        "id": str(call.get("id") or ""),
                        "type": "function",
                        "function": {
                            "name": str(call.get("name") or ""),
                            "arguments": str(call.get("arguments") or "{}"),
                        },
                    }
                    for call in message["tool_calls"]
                ]
            rendered.append(item)
        return rendered

    @staticmethod
    def _aggregate_sse_body(body_text: str) -> CanonicalResponse:
        """Aggregate a forced-SSE chat/completions body into one response.

        Live fix (round 5): the operator's gateway streams SSE chunks for
        every tool-declaring request, even non-streaming ones. Hermes
        parity (``create_anthropic_message`` docstring): some compatible
        gateways are effectively SSE-only; the caller must aggregate
        instead of crashing on the content type.

        Chunk grammar (OpenAI-compatible deltas):
        - ``data: {chunk}`` lines; ``data: [DONE]`` terminates;
        - ``choices[0].delta.content`` appends to the text;
        - ``choices[0].delta.tool_calls`` merge by index (id, name,
          argument fragments concatenate in arrival order);
        - the last non-null ``usage`` wins; ``finish_reason`` comes from
          the last chunk that carries one.
        """
        content_parts: list[str] = []
        finish_reason = "stop"
        usage: TokenUsage | None = None
        provider_message_id: str | None = None
        tool_calls_by_index: dict[int, dict[str, str]] = {}

        for line in body_text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                chunk = json.loads(payload)
            except ValueError:
                continue
            if not isinstance(chunk, dict):
                continue
            if chunk.get("id") and provider_message_id is None:
                provider_message_id = str(chunk["id"])
            raw_chunk_usage = chunk.get("usage")
            if isinstance(raw_chunk_usage, Mapping) and raw_chunk_usage:
                usage = OpenAICompatibleProviderAdapter._normalize_usage(raw_chunk_usage)
            choices = chunk.get("choices")
            if not isinstance(choices, list) or not choices:
                continue
            choice = choices[0] if isinstance(choices[0], dict) else {}
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                delta = {}
            delta_content = delta.get("content")
            if isinstance(delta_content, str) and delta_content:
                content_parts.append(delta_content)
            for raw_call in delta.get("tool_calls") or []:
                if not isinstance(raw_call, Mapping):
                    continue
                try:
                    index = int(raw_call.get("index") or 0)
                except (TypeError, ValueError):
                    index = 0
                entry = tool_calls_by_index.setdefault(
                    index, {"id": "", "name": "", "arguments": ""}
                )
                call_id = raw_call.get("id")
                if isinstance(call_id, str) and call_id and not entry["id"]:
                    entry["id"] = call_id
                function = raw_call.get("function")
                if isinstance(function, Mapping):
                    name = function.get("name")
                    if isinstance(name, str) and name and not entry["name"]:
                        entry["name"] = name
                    arguments = function.get("arguments")
                    if isinstance(arguments, str):
                        entry["arguments"] += arguments
            if choice.get("finish_reason"):
                finish_reason = str(choice["finish_reason"])

        tool_calls = tuple(
            ToolCallResult(
                tool_name=entry["name"],
                tool_call_id=entry["id"] or f"stream_{index}",
                arguments=entry["arguments"] or "{}",
                result="",
            )
            for index, entry in sorted(tool_calls_by_index.items())
            if entry["name"]
        )
        return CanonicalResponse(
            content="".join(content_parts),
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage if usage is not None else TokenUsage(),
            provider_message_id=provider_message_id,
        )

    @staticmethod
    def _normalize_usage(raw_usage: Any) -> TokenUsage:
        if not isinstance(raw_usage, Mapping):
            return TokenUsage()
        prompt = max(0, int(raw_usage.get("prompt_tokens", 0) or 0))
        completion = max(0, int(raw_usage.get("completion_tokens", 0) or 0))
        details = raw_usage.get("prompt_tokens_details")
        cached = 0
        if isinstance(details, Mapping):
            cached = max(0, int(details.get("cached_tokens", 0) or 0))
        cached = min(cached, prompt)
        creation = max(
            0,
            int(raw_usage.get("cache_creation_input_tokens", 0) or 0),
        )
        return TokenUsage(
            input_tokens=prompt - cached,
            output_tokens=completion,
            cache_creation_input_tokens=creation,
            cache_read_input_tokens=cached,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


# ----------------------------------------------------------------------
# Anthropic Messages adapter (wire spec per Hermes anthropic_adapter)
# ----------------------------------------------------------------------


_ANTHROPIC_STOP_REASON_MAP = {
    "end_turn": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "stop_sequence": "stop",
    "refusal": "content_filter",
    "model_context_window_exceeded": "length",
}

#: Tool IDs must satisfy [a-zA-Z0-9_-] on the Anthropic wire.
_TOOL_ID_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def _sanitize_tool_id(tool_id: str) -> str:
    sanitized = _TOOL_ID_SAFE.sub("_", tool_id or "")
    return sanitized or "tool_0"


def _strip_root_schema_unions(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Anthropic rejects top-level oneOf/allOf/anyOf; coerce to object."""
    cleaned = {
        key: value for key, value in schema.items() if key not in {"oneOf", "allOf", "anyOf"}
    }
    if "type" not in cleaned:
        cleaned["type"] = "object"
    if cleaned.get("type") == "object" and not isinstance(cleaned.get("properties"), dict):
        cleaned["properties"] = {}
    return cleaned


class AnthropicMessagesProviderAdapter(ProviderAdapter):
    """Real HTTP adapter for the Anthropic Messages API.

    Wire contract mirrors the audited reference implementation
    (Hermes ``agent/anthropic_adapter.py``):

    - system prompt travels as a top-level ``system`` block list;
    - assistant tool calls become ``tool_use`` blocks and tool results
      become adjacent-user ``tool_result`` blocks with consecutive-role
      merging, leading-user enforcement, orphan-pair stripping, and
      non-whitespace placeholders everywhere;
    - ``max_tokens`` is mandatory; stop reasons map to the canonical
      finish vocabulary; usage classes are read directly (Anthropic
      reports cache tokens disjointly — no subtraction);
    - prompt caching marks the system block and the last tool;
    - an empty content payload is only legitimate for end_turn/refusal.
    """

    _MODEL_CATALOG: ClassVar[dict[str, tuple[int, int]]] = {
        "claude-opus-4": (200_000, 32_000),
        "claude-sonnet-4": (200_000, 64_000),
        "claude-3-7-sonnet": (200_000, 64_000),
        "claude-3-5-sonnet": (200_000, 8_192),
        "claude-3-5-haiku": (200_000, 8_192),
        "claude-3-opus": (200_000, 4_096),
    }
    _DEFAULT_CONTEXT_WINDOW: ClassVar[int] = 32_768
    _DEFAULT_MAX_OUTPUT_TOKENS: ClassVar[int] = 4_096

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.anthropic.com",
        timeout_seconds: float = 60.0,
        client: httpx.Client | None = None,
        anthropic_version: str = "2023-06-01",
    ) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("provider API key must be configured")
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("provider base URL must be an absolute HTTP(S) URL")
        if parsed.query or parsed.fragment:
            raise ValueError("provider base URL must not contain query or fragment")
        if timeout_seconds <= 0:
            raise ValueError("provider timeout must be positive")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._anthropic_version = anthropic_version
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout_seconds)

    @property
    def provider_name(self) -> str:
        return "anthropic"

    def get_model(self, model_name: str) -> ProviderModel:
        context_window, max_output = self._MODEL_CATALOG.get(
            model_name,
            next(
                (
                    limits
                    for known, limits in sorted(
                        self._MODEL_CATALOG.items(), key=lambda item: -len(item[0])
                    )
                    if model_name.startswith((known + "-", known))
                ),
                (self._DEFAULT_CONTEXT_WINDOW, self._DEFAULT_MAX_OUTPUT_TOKENS),
            ),
        )
        return ProviderModel(
            id=ProviderModelId(generate_provider_model_id()),
            provider=self.provider_name,
            model_name=model_name,
            context_window=context_window,
            max_output_tokens=max_output,
            capabilities=(
                "streaming",
                "native_tools",
                "server_reported_usage",
                "cancellation",
                "prompt_caching",
            ),
            is_active=True,
            created_at=_now_utc_iso(),
        )

    # -- request rendering ------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": self._anthropic_version,
            "Content-Type": "application/json",
        }

    def _endpoint(self) -> str:
        return f"{self._base_url}/v1/messages"

    @staticmethod
    def _render_tools(
        tools: Sequence[ToolDeclaration | str | Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        rendered: list[dict[str, Any]] = []
        seen_names: set[str] = set()
        for declaration in coerce_tool_declarations(tools):
            if not declaration.name or declaration.name in seen_names:
                continue
            seen_names.add(declaration.name)
            entry: dict[str, Any] = {
                "name": declaration.name,
                "description": declaration.description or f"Zero capability {declaration.name}",
                "input_schema": _strip_root_schema_unions(declaration.normalized_parameters()),
            }
            rendered.append(entry)
        if rendered:
            # Cache the full tools array across turns via a breakpoint on
            # the final tool.
            rendered[-1]["cache_control"] = {"type": "ephemeral"}
        return rendered

    @staticmethod
    def _text_block(text: str, fallback: str) -> dict[str, Any]:
        stripped = text.strip()
        return {"type": "text", "text": stripped if stripped else fallback}

    @classmethod
    def _assistant_blocks(
        cls,
        message: Mapping[str, Any],
        *,
        seen_ids: set[str],
        pending_map: dict[str, str],
    ) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            blocks.append({"type": "text", "text": content})
        elif isinstance(content, Sequence) and not isinstance(content, str):
            for part in content:
                if isinstance(part, Mapping) and part.get("type") == "text":
                    text = str(part.get("text") or "")
                    if text.strip():
                        blocks.append({"type": "text", "text": text})
        for call in message.get("tool_calls") or []:
            name = str(call.get("name") or "")
            raw_id = str(call.get("id") or "")
            final_id = _sanitize_tool_id(raw_id)
            if final_id in seen_ids:
                # Distinct raw ids can collapse under sanitization;
                # suffix later duplicates so every wire id is unique.
                suffix = 1
                while f"{final_id}_{suffix}" in seen_ids:
                    suffix += 1
                final_id = f"{final_id}_{suffix}"
            seen_ids.add(final_id)
            pending_map[raw_id] = final_id
            raw_arguments = call.get("arguments")
            if isinstance(raw_arguments, str):
                try:
                    tool_input: Any = json.loads(raw_arguments) if raw_arguments.strip() else {}
                except json.JSONDecodeError:
                    tool_input = {}
            else:
                tool_input = raw_arguments or {}
            blocks.append({"type": "tool_use", "id": final_id, "name": name, "input": tool_input})
        if not blocks:
            blocks.append({"type": "text", "text": "(empty)"})
        return blocks

    @classmethod
    def _render_messages(cls, messages: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = []
        # Sanitized-id collision disambiguation happens at conversion
        # time: each assistant call gets a globally unique sanitized id,
        # and the immediately following tool results translate through
        # the same mapping so pairing stays exact.
        seen_ids: set[str] = set()
        pending_map: dict[str, str] = {}
        for message in messages:
            role = message.get("role")
            if role == "assistant":
                pending_map = {}
                converted.append(
                    {
                        "role": "assistant",
                        "content": cls._assistant_blocks(
                            message, seen_ids=seen_ids, pending_map=pending_map
                        ),
                    }
                )
            elif role == "tool":
                content = message.get("content")
                if not isinstance(content, str) or not content.strip():
                    content = "(no output)"
                raw_result_id = str(message.get("tool_call_id") or "")
                result_block = {
                    "type": "tool_result",
                    "tool_use_id": pending_map.get(raw_result_id, _sanitize_tool_id(raw_result_id)),
                    "content": content,
                }
                converted.append({"role": "user", "content": [result_block]})
            else:  # user (and any residual system roles)
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    text = content
                else:
                    text = "(empty message)"
                converted.append({"role": "user", "content": [{"type": "text", "text": text}]})

        merged: list[dict[str, Any]] = []
        for item in converted:
            if merged and merged[-1]["role"] == item["role"]:
                merged[-1]["content"] = list(merged[-1]["content"]) + list(item["content"])
            else:
                merged.append(item)

        # Orphan pair stripping: every tool_use needs its result in the
        # immediately following user turn; results without a surviving
        # call are dropped as well.
        for index, item in enumerate(merged):
            if item["role"] != "assistant":
                continue
            declared_ids = [
                block["id"]
                for block in item["content"]
                if isinstance(block, dict) and block.get("type") == "tool_use"
            ]
            if not declared_ids:
                continue
            following = merged[index + 1] if index + 1 < len(merged) else None
            answered_ids: set[str] = set()
            if following is not None and following["role"] == "user":
                for block in following["content"]:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        answered_ids.add(str(block.get("tool_use_id")))
            kept_blocks: list[Any] = []
            for block in item["content"]:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and str(block.get("id")) not in answered_ids
                ):
                    kept_blocks.append({"type": "text", "text": "(tool call removed)"})
                else:
                    kept_blocks.append(block)
            item["content"] = kept_blocks
        for index, item in enumerate(merged):
            if item["role"] != "user":
                continue
            previous_ids: set[str] = set()
            if index > 0 and merged[index - 1]["role"] == "assistant":
                for block in merged[index - 1]["content"]:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        previous_ids.add(str(block.get("id")))
            kept: list[Any] = []
            for block in item["content"]:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_result"
                    and str(block.get("tool_use_id")) not in previous_ids
                ):
                    kept.append({"type": "text", "text": "(tool result removed)"})
                else:
                    kept.append(block)
            item["content"] = kept

        if not merged or merged[0]["role"] != "user":
            merged.insert(0, {"role": "user", "content": [{"type": "text", "text": "(empty)"}]})
        return merged

    def _build_body(self, request: CanonicalRequest) -> dict[str, Any]:
        max_tokens = int(request.max_tokens)
        if max_tokens <= 0:
            raise InvalidProviderRequestError("max_tokens must be positive")
        clean_messages, _stripped = validate_tool_messages(request.messages)
        # Loud multimodal rejection (Hermes parity, round 5 gap 5): the
        # Anthropic wire protocol expresses images as base64 ``source``
        # blocks, NOT OpenAI-style ``image_url`` parts. Silently dropping
        # the parts would send the model a text-only transcript while the
        # user believes the photo was seen — fail loudly instead.
        for message in clean_messages:
            for part in message.get("content_parts") or ():
                part_type = str(part.get("type") or "") if isinstance(part, Mapping) else ""
                if part_type and part_type != "text":
                    raise InvalidProviderRequestError(
                        "anthropic protocol rejects OpenAI-style "
                        f"'{part_type}' content parts; convert media to "
                        "base64 source image blocks before dispatch"
                    )
        body: dict[str, Any] = {
            "model": request.model_name,
            "max_tokens": max_tokens,
            "messages": self._render_messages(clean_messages),
        }
        system_text = (request.system_message or "").strip()
        if system_text:
            body["system"] = [
                {
                    "type": "text",
                    "text": system_text,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        if request.tools:
            body["tools"] = self._render_tools(request.tools)
            _apply_tool_choice(body, request, protocol="anthropic")
        return body

    # -- response parsing --------------------------------------------------

    @staticmethod
    def _normalize_usage(raw_usage: Any) -> TokenUsage:
        # Anthropic reports cache tokens disjointly from input_tokens;
        # never subtract.
        if not isinstance(raw_usage, Mapping):
            return TokenUsage()
        return TokenUsage(
            input_tokens=max(0, int(raw_usage.get("input_tokens", 0) or 0)),
            output_tokens=max(0, int(raw_usage.get("output_tokens", 0) or 0)),
            cache_creation_input_tokens=max(
                0, int(raw_usage.get("cache_creation_input_tokens", 0) or 0)
            ),
            cache_read_input_tokens=max(0, int(raw_usage.get("cache_read_input_tokens", 0) or 0)),
        )

    def send_request(
        self,
        request: CanonicalRequest,
        *,
        cancel_event: Event | None = None,
    ) -> CanonicalResponse:
        if cancel_event is not None and cancel_event.is_set():
            from zero.domain.providers import ProviderCancelledError

            raise ProviderCancelledError("provider request cancelled before dispatch")
        body = self._build_body(request)
        try:
            response = self._client.post(
                self._endpoint(),
                headers=self._headers(),
                json=body,
            )
        except httpx.ConnectError as exc:
            raise ProviderError("provider connection failed") from exc
        except httpx.RequestError as exc:
            raise ProviderUnknownOutcomeError("provider request outcome is unknown") from exc
        return self._parse_response(response)

    def _parse_response(self, response: httpx.Response) -> CanonicalResponse:
        if response.status_code >= 400:
            status_code = response.status_code
            if status_code == 401 or status_code == 403:
                # Shared classifier (round 6): CDN edge 403 shapes are
                # transient, JSON rejections are auth failures.
                raise _auth_status_error(response, status_code)
            if status_code == 429:
                retry_after = response.headers.get("retry-after")
                detail = f" (retry_after={retry_after})" if retry_after else ""
                raise ProviderError(f"provider rate limit hit with status {status_code}{detail}")
            if status_code in {503, 529}:
                raise ProviderError(f"provider temporarily unavailable ({status_code})")
            raise ProviderError(f"provider HTTP request failed with status {status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError("provider returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise ProviderError("provider returned an invalid response object")
        blocks = data.get("content")
        if not isinstance(blocks, list):
            raise ProviderError("provider response did not contain content blocks")

        text_parts: list[str] = []
        tool_calls: list[ToolCallResult] = []
        for block in blocks:
            if not isinstance(block, Mapping):
                continue
            block_type = block.get("type")
            if block_type == "text":
                text = str(block.get("text") or "")
                if text:
                    text_parts.append(text)
            elif block_type == "tool_use":
                tool_calls.append(
                    ToolCallResult(
                        tool_name=str(block.get("name") or ""),
                        tool_call_id=_sanitize_tool_id(str(block.get("id") or "")),
                        arguments=json.dumps(block.get("input") or {}),
                        result="",
                    )
                )
        stop_reason_raw = data.get("stop_reason")
        finish_reason = _ANTHROPIC_STOP_REASON_MAP.get(str(stop_reason_raw), "stop")
        if not text_parts and not tool_calls and stop_reason_raw not in {"end_turn", "refusal"}:
            raise ProviderError("provider returned empty content without a terminal stop reason")
        return CanonicalResponse(
            content="\n".join(text_parts),
            tool_calls=tuple(tool_calls),
            finish_reason=finish_reason,
            usage=self._normalize_usage(data.get("usage")),
            provider_message_id=(str(data["id"]) if data.get("id") is not None else None),
        )

    # -- streaming ---------------------------------------------------------

    def send_request_stream(
        self,
        request: CanonicalRequest,
        *,
        cancel_event: Event | None = None,
    ) -> Iterator[CanonicalStreamEvent]:
        """Outcome-aware errors (see OpenAI-compatible wrapper): a break
        before the first consumed SSE event is a retryable transport
        failure, not an unknown outcome."""
        saw_any_event = False
        try:
            for event in self._anthropic_stream_events(request, cancel_event=cancel_event):
                saw_any_event = True
                yield event
        except httpx.ConnectError as exc:
            raise ProviderError("provider connection failed") from exc
        except httpx.RequestError as exc:
            if not saw_any_event:
                raise ProviderError(
                    "provider stream connection failed before any stream data"
                ) from exc
            raise ProviderUnknownOutcomeError(
                "provider request outcome is unknown"
            ) from exc

    def _anthropic_stream_events(
        self,
        request: CanonicalRequest,
        *,
        cancel_event: Event | None = None,
    ) -> Iterator[CanonicalStreamEvent]:
        body = self._build_body(request)
        body["stream"] = True
        saw_message_stop = False
        saw_any_data = False
        stop_reason: str | None = None
        provider_message_id: str | None = None
        usage_map: dict[str, int] = {}
        blocks_by_index: dict[int, dict[str, Any]] = {}
        with self._client.stream(
            "POST",
            self._endpoint(),
            headers=self._headers(),
            json=body,
        ) as response:
            if response.status_code >= 400:
                raise ProviderError(
                    f"provider HTTP request failed with status {response.status_code}"
                )
            for raw_line in response.iter_lines():
                if cancel_event is not None and cancel_event.is_set():
                    from zero.domain.providers import ProviderCancelledError

                    raise ProviderCancelledError("provider stream cancelled")
                line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                payload_text = line[5:].strip()
                if not payload_text:
                    continue
                try:
                    event = json.loads(payload_text)
                except json.JSONDecodeError as exc:
                    raise ProviderError("provider returned invalid stream JSON") from exc
                if not isinstance(event, Mapping):
                    continue
                # A parsed provider event proves data was flowing; a later
                # break is mid-generation (unknown), not pre-response
                # (retryable transport failure).
                saw_any_data = True
                event_type = event.get("type")
                if event_type == "message_start":
                    message = event.get("message")
                    if isinstance(message, Mapping):
                        provider_message_id = (
                            str(message["id"]) if message.get("id") is not None else None
                        )
                        raw_usage = message.get("usage")
                        if isinstance(raw_usage, Mapping):
                            for key in (
                                "input_tokens",
                                "output_tokens",
                                "cache_creation_input_tokens",
                                "cache_read_input_tokens",
                            ):
                                value = raw_usage.get(key)
                                if isinstance(value, int):
                                    usage_map[key] = value
                elif event_type == "content_block_start":
                    index = event.get("index")
                    block = event.get("content_block")
                    if isinstance(index, int) and isinstance(block, Mapping):
                        blocks_by_index[index] = {
                            "type": block.get("type"),
                            "name": block.get("name"),
                            "id": block.get("id"),
                            "text": "",
                            "partial_json": "",
                        }
                elif event_type == "content_block_delta":
                    index = event.get("index")
                    delta = event.get("delta")
                    if not isinstance(index, int) or not isinstance(delta, Mapping):
                        continue
                    state = blocks_by_index.setdefault(
                        index,
                        {"type": delta.get("type"), "text": "", "partial_json": ""},
                    )
                    delta_type = delta.get("type")
                    if delta_type == "text_delta":
                        text = str(delta.get("text") or "")
                        state["text"] += text
                        if text:
                            yield CanonicalStreamEvent(
                                kind="text_delta",
                                text=text,
                                provider_message_id=provider_message_id,
                            )
                    elif delta_type == "input_json_delta":
                        state["partial_json"] += str(delta.get("partial_json") or "")
                elif event_type == "message_delta":
                    delta = event.get("delta")
                    if isinstance(delta, Mapping) and delta.get("stop_reason"):
                        stop_reason = str(delta["stop_reason"])
                    raw_usage = event.get("usage")
                    if isinstance(raw_usage, Mapping):
                        for key, value in raw_usage.items():
                            if isinstance(value, int):
                                usage_map[key] = value
                elif event_type == "message_stop":
                    saw_message_stop = True

        if not saw_message_stop:
            if not saw_any_data:
                # EOF (or transport break) before a single provider
                # event: retryable transport failure, not unknown outcome.
                raise ProviderError(
                    "provider stream connection failed before any stream data"
                )
            raise ProviderUnknownOutcomeError(
                "provider stream ended without a terminal message marker"
            )
        has_tool_use = any(state.get("type") == "tool_use" for state in blocks_by_index.values())
        if has_tool_use and not stop_reason:
            # SSE closed between content_block_start and message_delta:
            # partial tool input must never execute.
            raise ProviderUnknownOutcomeError(
                "provider stream ended mid-tool-call without a stop reason"
            )

        ordered_indexes = sorted(blocks_by_index)
        for index in ordered_indexes:
            state = blocks_by_index[index]
            if state.get("type") != "tool_use":
                continue
            tool_call_id = _sanitize_tool_id(str(state.get("id") or ""))
            partial = str(state.get("partial_json") or "")
            try:
                tool_input: Any = json.loads(partial) if partial.strip() else {}
            except json.JSONDecodeError:
                raise ProviderUnknownOutcomeError(
                    "provider stream delivered unparseable tool input"
                )
            yield CanonicalStreamEvent(
                kind="tool_call_delta",
                tool_call=ToolCallResult(
                    tool_name=str(state.get("name") or ""),
                    tool_call_id=tool_call_id,
                    arguments=json.dumps(tool_input),
                    result="",
                ),
                provider_message_id=provider_message_id,
            )
        if usage_map:
            yield CanonicalStreamEvent(
                kind="usage",
                usage=TokenUsage(
                    input_tokens=usage_map.get("input_tokens", 0),
                    output_tokens=usage_map.get("output_tokens", 0),
                    cache_creation_input_tokens=usage_map.get("cache_creation_input_tokens", 0),
                    cache_read_input_tokens=usage_map.get("cache_read_input_tokens", 0),
                ),
                provider_message_id=provider_message_id,
            )
        yield CanonicalStreamEvent(
            kind="message_end",
            finish_reason=_ANTHROPIC_STOP_REASON_MAP.get(str(stop_reason), "stop"),
            provider_message_id=provider_message_id,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


# ----------------------------------------------------------------------
# Deterministic fake adapter (for tests)
# ----------------------------------------------------------------------


class FakeProviderAdapter(ProviderAdapter):
    """A deterministic fake provider adapter for tests.

    Per PLAN.md M10: "one deterministic fake used only for tests."

    This adapter does NOT call any external service. It produces
    deterministic responses based on the input, so tests can assert
    exact behavior.

    The fake supports:
    - deterministic text generation (echoes the last user message);
    - tool call generation (if the user message contains "call tool");
    - usage reporting (deterministic token counts based on input size);
    - error simulation (if the user message contains "trigger error").
    """

    def __init__(self, repo: ProviderRepository) -> None:
        self._repo = repo
        self._ensure_models_registered()

    @property
    def provider_name(self) -> str:
        return "fake"

    def _ensure_models_registered(self) -> None:
        """Register fake models if they don't exist."""
        fake_models = [
            ("fake-standard", 200000, 8192, ("streaming", "native_tools", "prompt_caching")),
            ("fake-mini", 128000, 4096, ("streaming",)),
        ]
        for name, ctx, max_out, caps in fake_models:
            try:
                self._repo.get_provider_model("fake", name)
            except ProviderModelNotFoundError:
                model = ProviderModel(
                    id=ProviderModelId(generate_provider_model_id()),
                    provider="fake",
                    model_name=name,
                    context_window=ctx,
                    max_output_tokens=max_out,
                    capabilities=caps,
                    is_active=True,
                    created_at=_now_utc_iso(),
                )
                self._repo.insert_provider_model(model)

    def get_model(self, model_name: str) -> ProviderModel:
        return self._repo.get_provider_model("fake", model_name)

    def send_request(
        self,
        request: CanonicalRequest,
        *,
        cancel_event: Event | None = None,
    ) -> CanonicalResponse:
        if cancel_event is not None and cancel_event.is_set():
            from zero.domain.providers import ProviderCancelledError

            raise ProviderCancelledError("provider request cancelled before dispatch")
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
            # Orphan tool results were found. We repair by dropping them
            # rather than inventing success.
            pass

        # Find the last user message.
        last_user_msg = ""
        for m in request.messages:
            if m.role == "user":
                last_user_msg = m.content

        # Check for error trigger.
        if "trigger error" in last_user_msg.lower():
            from zero.domain.providers import InvalidProviderRequestError

            raise InvalidProviderRequestError("Fake provider triggered an error as requested")

        # Check for tool call trigger.
        tool_calls: tuple[ToolCallResult, ...] = ()
        if "call tool" in last_user_msg.lower():
            tool_calls = (
                ToolCallResult(
                    tool_name="echo",
                    tool_call_id="fake_call_1",
                    arguments='{"message": "fake tool call"}',
                    result="",
                ),
            )

        # Generate deterministic content.
        if tool_calls:
            content = "I will call the echo tool."
            finish_reason = "tool_calls"
        else:
            content = f"Fake response to: {last_user_msg[:200]}"
            finish_reason = "stop"

        # Compute deterministic usage.
        input_tokens = sum(len(m.content.encode("utf-8")) // 4 for m in request.messages)
        output_tokens = len(content.encode("utf-8")) // 4
        usage = TokenUsage(
            input_tokens=max(1, input_tokens),
            output_tokens=max(1, output_tokens),
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        )

        return CanonicalResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
            provider_message_id=(f"fake_msg_{compute_request_hash(request)[:32]}"),
        )
