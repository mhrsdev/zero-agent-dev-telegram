"""Real LLM provider adapters for Zero Agent.

Per ADR 0004: Zero is a pure HTTP consumer of "the Router" via OpenAI-compatible
protocol. In practice, the Router may be a separate service OR a direct LLM
provider that speaks the OpenAI protocol (Gemini, OpenAI, OpenRouter, etc.).

This module ships production-ready adapters for:
    - ``GeminiProvider`` — Google's Gemini API via the OpenAI-compatible endpoint
      (https://generativelanguage.googleapis.com/v1beta/openai/).
    - ``OpenAIProvider`` — direct OpenAI API (https://api.openai.com/v1/).
    - ``OpenRouterProvider`` — OpenRouter (https://openrouter.ai/api/v1).
    - ``GenericOpenAIProvider`` — any OpenAI-compatible endpoint.

All adapters expose the same ``complete()`` interface and emit the same headers
the RouterClient expects (``x-zero-cost-usd``, ``x-zero-request-id``,
``x-zero-cache-read-tokens``, ``x-zero-cache-write-tokens``).

The provider is selected at startup via ``router.provider`` config:
    - ``gemini``   → GeminiProvider
    - ``openai``   → OpenAIProvider
    - ``openrouter``→ OpenRouterProvider
    - ``custom``   → GenericOpenAIProvider (uses ``router.base_url``)

A small in-process HTTP server (``RouterShim``) can be started so that the
existing ``RouterClient`` (which talks to ``http://127.0.0.1:PORT/v1``) works
unchanged. This keeps the architectural boundary intact: Zero code only ever
talks to "the Router", but the Router may be a thin local shim that forwards
to a real LLM provider.
"""
from __future__ import annotations

from zero.agents.llm_provider.base import (
    LLMProvider,
    LLMProviderError,
    LLMProviderResponse,
    LLMProviderTimeoutError,
)
from zero.agents.llm_provider.factory import build_provider_from_config
from zero.agents.llm_provider.gemini import GeminiProvider
from zero.agents.llm_provider.generic import GenericOpenAIProvider
from zero.agents.llm_provider.openai_provider import OpenAIProvider
from zero.agents.llm_provider.openrouter import OpenRouterProvider
from zero.agents.llm_provider.router_shim import RouterShim, RouterShimConfig

__all__ = [
    "GeminiProvider",
    "GenericOpenAIProvider",
    "LLMProvider",
    "LLMProviderError",
    "LLMProviderResponse",
    "LLMProviderTimeoutError",
    "OpenAIProvider",
    "OpenRouterProvider",
    "RouterShim",
    "RouterShimConfig",
    "build_provider_from_config",
]
