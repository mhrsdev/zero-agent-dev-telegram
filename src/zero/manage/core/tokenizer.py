"""Real token counting with graceful degradation.

Per GAP 11 design (``docs/gap-designs/GAP-11-real-token-counting.md``):

- ``count_tokens(text, model)`` uses tiktoken when it is installed and
  the model maps to a known encoding; everything else falls back to the
  historical bytes÷4 heuristic.
- Encoding objects are cached at module level because
  ``tiktoken.get_encoding`` is expensive to initialize.
- This module imports nothing from ``zero`` so the domain layer can
  call it lazily without creating a dependency cycle.
"""

from __future__ import annotations

from typing import Any, Final

BYTES_PER_TOKEN: Final[int] = 4

#: Model-name prefix/family → tiktoken encoding. Claude models have no
#: public tiktoken encoding and deliberately resolve to ``None``.
_MODEL_ENCODINGS: Final[tuple[tuple[str, str], ...]] = (
    # o200k_base families first evaluated by longest-prefix match below;
    # order here does not matter because matching sorts by length.
    ("gpt-4o", "o200k_base"),
    ("o1", "o200k_base"),
    ("o3", "o200k_base"),
    ("o4", "o200k_base"),
    ("chatgpt-4o", "o200k_base"),
    ("gpt-4", "cl100k_base"),
    ("gpt-3.5", "cl100k_base"),
)

_ENCODING_CACHE: dict[str, Any] = {}
_TIKTOKEN_MODULE: Any = None
_TIKTOKEN_LOADED = False
_TIKTOKEN_DISABLED = False


def _load_tiktoken() -> Any:
    """Import tiktoken once; cache the module handle (or None)."""
    global _TIKTOKEN_MODULE, _TIKTOKEN_LOADED
    if not _TIKTOKEN_LOADED:
        _TIKTOKEN_LOADED = True
        try:
            import tiktoken as _tiktoken

            _TIKTOKEN_MODULE = _tiktoken
        except Exception:  # noqa: BLE001 - optional dependency
            _TIKTOKEN_MODULE = None
    return _TIKTOKEN_MODULE


def disable_tiktoken_for_testing() -> None:
    """Force the heuristic path (deterministic tests)."""
    global _TIKTOKEN_DISABLED
    _TIKTOKEN_DISABLED = True


def enable_tiktoken_for_testing() -> None:
    """Restore normal tiktoken detection after a test disable."""
    global _TIKTOKEN_DISABLED
    _TIKTOKEN_DISABLED = False


def tiktoken_available() -> bool:
    """Return True when exact counting is usable right now."""
    return not _TIKTOKEN_DISABLED and _load_tiktoken() is not None


def encoding_for_model(model: str | None) -> str | None:
    """Map a model name to its tiktoken encoding name (or None).

    Unknown models, empty names, and providers without public tiktoken
    encodings (e.g. Anthropic) return ``None`` so callers keep the
    documented approximate heuristic.
    """
    if not model or not model.strip():
        return None
    name = model.strip().lower()
    best: tuple[str, str] | None = None
    for prefix, encoding in _MODEL_ENCODINGS:
        if (name == prefix or name.startswith(prefix)) and (
            best is None or len(prefix) > len(best[0])
        ):
            best = (prefix, encoding)
    return best[1] if best else None


def _get_cached_encoding(encoding_name: str) -> Any:
    cached = _ENCODING_CACHE.get(encoding_name)
    if cached is not None:
        return cached
    tiktoken = _load_tiktoken()
    if tiktoken is None:
        return None
    try:
        encoding = tiktoken.get_encoding(encoding_name)
    except Exception:  # noqa: BLE001 - offline/unknown encoding → fallback
        return None
    _ENCODING_CACHE[encoding_name] = encoding
    return encoding


def _heuristic_tokens(text: str) -> int:
    """The historical bytes÷4 approximation (never zero for content)."""
    if not text:
        return 0
    return max(1, len(text.encode("utf-8")) // BYTES_PER_TOKEN)


def count_tokens(text: str, model: str | None = None) -> int:
    """Count tokens exactly when possible, approximately otherwise.

    With ``model`` set and a known encoding available the count is
    exact. Every other path — no tiktoken installed, unknown model,
    offline BPE load failure, or explicit test disable — returns the
    documented bytes÷4 approximation used across the control plane.
    """
    if not text:
        return 0
    if _TIKTOKEN_DISABLED:
        return _heuristic_tokens(text)
    encoding_name = encoding_for_model(model)
    if encoding_name is None:
        return _heuristic_tokens(text)
    encoding = _get_cached_encoding(encoding_name)
    if encoding is None:
        return _heuristic_tokens(text)
    try:
        return max(1, len(encoding.encode(text)))
    except Exception:  # noqa: BLE001 - never let counting break callers
        return _heuristic_tokens(text)


def estimate_tokens_for_model(text: str, model: str) -> int:
    """Model-aware alias kept for call-site readability."""
    return count_tokens(text, model)


__all__ = [
    "BYTES_PER_TOKEN",
    "count_tokens",
    "disable_tiktoken_for_testing",
    "enable_tiktoken_for_testing",
    "encoding_for_model",
    "estimate_tokens_for_model",
    "tiktoken_available",
]
