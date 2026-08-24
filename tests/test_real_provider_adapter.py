"""Deterministic HTTP contract tests for the real provider adapter."""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from zero.app import provider_adapter
from zero.app.services import build_services
from zero.config import Settings
from zero.domain.providers import (
    CanonicalMessage,
    CanonicalRequest,
    ProviderModelNotFoundError,
    ProviderUnknownOutcomeError,
)
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations


def test_openai_compatible_adapter_maps_chat_completion_over_http() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers["authorization"]
        seen["payload"] = request.read()
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl_test_123",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "Hello from HTTP"},
                    }
                ],
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 4,
                    "prompt_tokens_details": {"cached_tokens": 3},
                },
            },
        )

    adapter_class = getattr(provider_adapter, "OpenAICompatibleProviderAdapter", None)
    assert adapter_class is not None
    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = adapter_class(
        api_key="synthetic-test-key",
        base_url="https://provider.invalid/v1",
        client=client,
    )
    request = CanonicalRequest(
        provider="openai-compatible",
        model_name="test-model",
        messages=(CanonicalMessage(role="user", content="Hello"),),
        max_tokens=128,
        temperature=0.2,
    )

    response = adapter.send_request(request)

    assert seen["method"] == "POST"
    assert seen["url"] == "https://provider.invalid/v1/chat/completions"
    assert seen["authorization"] == "Bearer " + "synthetic-" + "test-key"
    assert b'"model":"test-model"' in seen["payload"]  # type: ignore[operator]
    assert response.content == "Hello from HTTP"
    assert response.provider_message_id == "chatcmpl_test_123"
    assert response.usage.input_tokens == 8
    assert response.usage.total_input_tokens == 11
    assert response.usage.output_tokens == 4
    assert response.usage.cache_read_input_tokens == 3


def test_openai_compatible_adapter_redacts_credentials_from_http_errors() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": {"message": "invalid key synthetic-test-key"}},
        )

    adapter_class = getattr(provider_adapter, "OpenAICompatibleProviderAdapter", None)
    assert adapter_class is not None
    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = adapter_class(
        api_key="synthetic-test-key",
        base_url="https://provider.invalid/v1",
        client=client,
    )
    request = CanonicalRequest(
        provider="openai-compatible",
        model_name="test-model",
        messages=(CanonicalMessage(role="user", content="Hello"),),
    )

    with pytest.raises(Exception) as raised:
        adapter.send_request(request)

    assert "synthetic-test-key" not in str(raised.value)
    assert "401" in str(raised.value)


def test_openai_compatible_read_timeout_is_unknown_outcome() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("response timed out after dispatch")

    adapter_class = getattr(provider_adapter, "OpenAICompatibleProviderAdapter", None)
    assert adapter_class is not None
    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = adapter_class(
        api_key="synthetic-test-key",
        base_url="https://provider.invalid/v1",
        client=client,
    )
    request = CanonicalRequest(
        provider="openai-compatible",
        model_name="test-model",
        messages=(CanonicalMessage(role="user", content="Hello"),),
    )

    with pytest.raises(ProviderUnknownOutcomeError):
        adapter.send_request(request)


def test_configured_provider_is_reachable_from_service_composition() -> None:
    settings = Settings.load_for_test(
        openai_api_key="synthetic-test-key",
        openai_base_url="https://provider.invalid/v1",
        openai_model="test-model",
    )
    database = Database(settings)
    apply_migrations(database)

    services = build_services(settings, database)

    model = services.providers.get_model("openai-compatible", "test-model")

    assert model.provider == "openai-compatible"
    assert model.model_name == "test-model"


def test_development_composition_does_not_register_fake_provider() -> None:
    settings = Settings(
        zero_env="development",
        database_url="sqlite::memory:",
        secret_key=SecretStr("development-test-backup-key"),
    )
    database = Database(settings)
    apply_migrations(database)

    services = build_services(settings, database)

    with pytest.raises(ProviderModelNotFoundError):
        services.providers.get_model("fake", "fake-standard")
