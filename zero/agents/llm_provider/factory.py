"""Factory for building LLM providers from config.

Reads ``router.provider`` from config and instantiates the right provider
class with the right ``secret://`` API key ref.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from zero.agents.llm_provider.base import LLMProvider
from zero.agents.llm_provider.gemini import GeminiProvider
from zero.agents.llm_provider.generic import GenericOpenAIProvider
from zero.agents.llm_provider.openai_provider import OpenAIProvider
from zero.agents.llm_provider.openrouter import OpenRouterProvider
from zero.core.errors import ConfigError

if TYPE_CHECKING:
    from zero.core.config import RouterConfig
    from zero.core.secret import SecretResolver

__all__ = ["build_provider_from_config", "SUPPORTED_PROVIDERS"]


SUPPORTED_PROVIDERS = frozenset({
    "gemini",
    "openai",
    "openrouter",
    "custom",
})


def build_provider_from_config(
    *,
    router_cfg: RouterConfig,
    resolver: SecretResolver,
) -> LLMProvider:
    """Build an LLM provider from the RouterConfig.

    Config fields used:
        - ``router.provider`` — one of SUPPORTED_PROVIDERS (default: "custom")
        - ``router.base_url`` — used for "custom" provider
        - ``router.api_key`` — secret:// ref (always required)
        - ``router.timeout_seconds`` — call timeout
        - ``router.max_retries`` — retry count for 5xx/timeout
        - ``router.default_model`` — fallback model name
    """
    provider_name = router_cfg.provider
    if provider_name not in SUPPORTED_PROVIDERS:
        raise ConfigError(
            f"unsupported router.provider {provider_name!r}; "
            f"must be one of {sorted(SUPPORTED_PROVIDERS)}"
        )

    common_kwargs = {
        "api_key_ref": router_cfg.api_key,
        "resolver": resolver,
        "timeout_seconds": router_cfg.timeout_seconds,
        "max_retries": router_cfg.max_retries,
        "default_model": router_cfg.default_model,
    }

    if provider_name == "gemini":
        return GeminiProvider(**common_kwargs)  # type: ignore[arg-type]
    if provider_name == "openai":
        return OpenAIProvider(**common_kwargs)  # type: ignore[arg-type]
    if provider_name == "openrouter":
        return OpenRouterProvider(**common_kwargs)  # type: ignore[arg-type]
    # custom / shim: use the configured base_url.
    provider = GenericOpenAIProvider(**common_kwargs)  # type: ignore[arg-type]
    provider.base_url = router_cfg.base_url.rstrip("/")
    return provider
