"""GAP 11 tests: real token counting with graceful degradation."""

from __future__ import annotations

import pytest

from zero.domain.context import estimate_tokens
from zero.manage.core import tokenizer


@pytest.fixture(autouse=True)
def _restore_tiktoken_state():
    """Every test starts and ends with normal tiktoken detection."""
    tokenizer.enable_tiktoken_for_testing()
    yield
    tokenizer.enable_tiktoken_for_testing()


class TestModelEncodingMapping:
    def test_gpt4o_family_maps_to_o200k(self):
        assert tokenizer.encoding_for_model("gpt-4o") == "o200k_base"
        assert tokenizer.encoding_for_model("gpt-4o-mini") == "o200k_base"
        assert tokenizer.encoding_for_model("gpt-4o-2024-08-06") == "o200k_base"

    def test_reasoning_family_maps_to_o200k(self):
        assert tokenizer.encoding_for_model("o1") == "o200k_base"
        assert tokenizer.encoding_for_model("o3-mini") == "o200k_base"

    def test_legacy_family_maps_to_cl100k(self):
        assert tokenizer.encoding_for_model("gpt-4") == "cl100k_base"
        assert tokenizer.encoding_for_model("gpt-4-turbo-preview") == "cl100k_base"
        assert tokenizer.encoding_for_model("gpt-3.5-turbo") == "cl100k_base"

    def test_longest_prefix_wins(self):
        # gpt-4o-* must not be captured by the gpt-4 rule.
        assert tokenizer.encoding_for_model("gpt-4o-mini-2024-07-18") == "o200k_base"

    def test_unknown_and_empty_names_return_none(self):
        assert tokenizer.encoding_for_model("fake-standard") is None
        assert tokenizer.encoding_for_model("claude-sonnet-4") is None
        assert tokenizer.encoding_for_model("") is None
        assert tokenizer.encoding_for_model(None) is None


class TestExactCounting:
    @pytest.mark.skipif(not tokenizer.tiktoken_available(), reason="tiktoken extra not installed")
    def test_exact_count_matches_tiktoken_for_known_model(self):
        import tiktoken

        text = "def hello(name):\n    return f'hi {name}'\n" * 5
        expected = len(tiktoken.get_encoding("o200k_base").encode(text))
        assert tokenizer.count_tokens(text, "gpt-4o-mini") == expected

    @pytest.mark.skipif(not tokenizer.tiktoken_available(), reason="tiktoken extra not installed")
    def test_encoding_object_is_cached(self):
        first = tokenizer.count_tokens("hello world", "gpt-4")
        second = tokenizer.count_tokens("hello world again", "gpt-4")
        assert first > 0
        assert second > 0
        assert tokenizer._ENCODING_CACHE["cl100k_base"] is tokenizer._ENCODING_CACHE["cl100k_base"]

    def test_empty_text_counts_zero_regardless_of_model(self):
        assert tokenizer.count_tokens("", "gpt-4o") == 0
        assert tokenizer.count_tokens("", None) == 0


class TestFallback:
    def test_disable_switch_forces_heuristic(self):
        text = "The quick brown fox jumps over the lazy dog."
        tokenizer.disable_tiktoken_for_testing()
        assert tokenizer.count_tokens(text, "gpt-4o") == estimate_tokens(text)

    def test_unknown_model_uses_heuristic_even_with_tiktoken(self):
        text = "The quick brown fox jumps over the lazy dog."
        expected = max(1, len(text.encode("utf-8")) // 4)
        assert tokenizer.count_tokens(text, "totally-unknown-model") == expected

    def test_estimate_tokens_domain_seam_matches_heuristic(self):
        text = "x" * 500
        assert estimate_tokens(text) == len(text.encode("utf-8")) // 4

    def test_estimate_tokens_non_ascii_never_zero(self):
        assert estimate_tokens("日本語テキスト") >= 1
        assert tokenizer.count_tokens("日本語テキスト", None) >= 1


class TestEstimateTokensForModelAlias:
    def test_alias_delegates(self):
        text = "sample text for counting"
        assert tokenizer.estimate_tokens_for_model(text, "unknown-model") == tokenizer.count_tokens(
            text, "unknown-model"
        )
