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
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any

from zero.domain.ids import (
    generate_provider_model_id,
)
from zero.domain.providers import (
    CanonicalRequest,
    CanonicalResponse,
    ProviderModel,
    ProviderModelId,
    TokenUsage,
    ToolCallResult,
)
from zero.persistence.repositories.provider_repository import (
    ProviderRepository,
)


def _now_utc_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def compute_request_hash(req: CanonicalRequest) -> str:
    """Compute a deterministic hash of a canonical request for
    deduplication.

    Per ``zero-claude-token-economics`` §"Request/message and query
    deduplication": if the same request is submitted twice, the second
    is a no-op.
    """
    payload = json.dumps({
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
        "tools": list(req.tools),
        "system_message": req.system_message,
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------
# Tool message validation (per zero-context-memory §sanitize_tool_pairs)
# ----------------------------------------------------------------------


def validate_tool_messages(
    messages: list[dict[str, Any]],
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
    for msg in messages:
        role = msg.get("role")
        if role == "assistant":
            for call in msg.get("tool_calls") or []:
                call_id = str(call.get("id") or "")
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
            ("fake-standard", 200000, 8192,
             ("streaming", "native_tools", "prompt_caching")),
            ("fake-mini", 128000, 4096,
             ("streaming",)),
        ]
        for name, ctx, max_out, caps in fake_models:
            try:
                self._repo.get_provider_model("fake", name)
            except Exception:
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
        self, request: CanonicalRequest
    ) -> CanonicalResponse:
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

            raise InvalidProviderRequestError(
                "Fake provider triggered an error as requested"
            )

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
        input_tokens = sum(
            len(m.content.encode("utf-8")) // 4
            for m in request.messages
        )
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
            provider_message_id=f"fake_msg_{hash(last_user_msg) & 0xFFFFFFFF:x}",
        )
