"""Gemini provider — uses Google's OpenAI-compatible endpoint.

Endpoint: https://generativelanguage.googleapis.com/v1beta/openai
Auth: Bearer <API_KEY> (the Gemini API key from Google AI Studio)

The Gemini API key is resolved from a ``secret://`` ref at call time, per
ADR 0007 §2 ("value resolved in memory at moment of use").

Pricing source (as of 2025-08):
    gemini-2.0-flash:         $0.10/1M input, $0.40/1M output (≤200k tokens)
    gemini-2.0-flash-lite:   $0.075/1M input, $0.30/1M output
    gemini-1.5-flash:        $0.075/1M input, $0.30/1M output
    gemini-1.5-pro:          $1.25/1M input, $5.00/1M output
    gemini-2.5-flash:        $0.075/1M input, $0.30/1M output (preview)

These are updated periodically from https://ai.google.dev/pricing.
"""
from __future__ import annotations

from zero.agents.llm_provider.generic import GenericOpenAIProvider, PricingTable

__all__ = ["GEMINI_PRICING", "GeminiProvider"]


GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"

GEMINI_PRICING: PricingTable = {
    # Format: (input_per_1m_tokens, output_per_1m_tokens)
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-2.0-flash-lite": (0.075, 0.30),
    "gemini-2.5-flash": (0.075, 0.30),
    "gemini-2.5-pro": (1.25, 5.00),
    "gemini-1.5-flash": (0.075, 0.30),
    "gemini-1.5-pro": (1.25, 5.00),
    "gemini-1.5-flash-8b": (0.0375, 0.15),
}


class GeminiProvider(GenericOpenAIProvider):
    """Google Gemini via the OpenAI-compatible endpoint.

    Construction:
        >>> provider = GeminiProvider(
        ...     api_key_ref="secret://env/GEMINI_API_KEY",
        ...     resolver=CompositeSecretResolver(),
        ... )

    Usage:
        >>> resp = await provider.complete(
        ...     messages=[ProviderMessage(role="user", content="hello")],
        ...     scope=project_scope,
        ...     model="gemini-2.0-flash",  # optional; defaults to gemini-2.0-flash
        ... )
    """

    provider_name = "gemini"
    base_url = GEMINI_BASE_URL
    default_model = "gemini-2.0-flash"
    pricing = GEMINI_PRICING
