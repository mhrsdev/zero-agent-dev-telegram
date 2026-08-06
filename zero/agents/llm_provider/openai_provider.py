"""OpenAI provider — direct OpenAI API.

Endpoint: https://api.openai.com/v1
Auth: Bearer <OPENAI_API_KEY>

Pricing source (as of 2025-08, per OpenAI's public pricing page):
    gpt-4o:        $2.50/1M input, $10.00/1M output
    gpt-4o-mini:   $0.15/1M input, $0.60/1M output
    gpt-4-turbo:   $10.00/1M input, $30.00/1M output
    gpt-3.5-turbo: $0.50/1M input, $1.50/1M output
    o1:            $15.00/1M input, $60.00/1M output
    o1-mini:       $3.00/1M input, $12.00/1M output
    o3-mini:       $3.00/1M input, $12.00/1M output
"""
from __future__ import annotations

from zero.agents.llm_provider.generic import GenericOpenAIProvider, PricingTable

__all__ = ["OPENAI_PRICING", "OpenAIProvider"]


OPENAI_BASE_URL = "https://api.openai.com/v1"

OPENAI_PRICING: PricingTable = {
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4-turbo": (10.00, 30.00),
    "gpt-4": (30.00, 60.00),
    "gpt-3.5-turbo": (0.50, 1.50),
    "o1": (15.00, 60.00),
    "o1-mini": (3.00, 12.00),
    "o1-pro": (150.00, 600.00),
    "o3-mini": (3.00, 12.00),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
}


class OpenAIProvider(GenericOpenAIProvider):
    """Direct OpenAI API provider."""

    provider_name = "openai"
    base_url = OPENAI_BASE_URL
    default_model = "gpt-4o-mini"
    pricing = OPENAI_PRICING
