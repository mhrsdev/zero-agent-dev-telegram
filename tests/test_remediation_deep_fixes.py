"""Regression tests for the post-audit deep remediation pass.

Each test locks in one specific defect fix:
- FTS query sanitization (malformed queries degrade, not crash).
- Knowledge-record term-overlap relevance scoring.
- Streaming collection attaches buffered names to id-bearing deltas.
- Model metadata uses a real catalog with conservative fallbacks.
- Tool declarations carry real JSON Schemas into provider payloads.
- Usage deduplication is decided by row probes, not error-text matching.
"""

from __future__ import annotations

import json

import httpx
import pytest

from zero.app.provider_adapter import OpenAICompatibleProviderAdapter
from zero.app.provider_service import ProviderService
from zero.app.retrieval_service import _knowledge_relevance
from zero.app.services import build_services
from zero.config import Settings
from zero.domain.ids import (
    generate_provider_request_id,
    generate_usage_record_id,
)
from zero.domain.providers import (
    CanonicalStreamEvent,
    ProviderRequestId,
    TokenUsage,
    ToolCallResult,
    ToolDeclaration,
    UsageRecordId,
)
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations
from zero.persistence.repositories.artifact_repository import _fts_safe_query


@pytest.fixture
def services(test_settings: Settings):
    database = Database(test_settings)
    apply_migrations(database)
    return build_services(test_settings, database)


# ----------------------------------------------------------------------
# FTS sanitization
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('hello "world"', "hello OR world"),
        ("alpha AND beta", "alpha OR beta"),
        ("NEAR(a,b)", "a OR b"),
        ("   ", ""),
        ("", ""),
        ("tag-1 tag_2", "tag OR 1 OR tag_2"),
    ],
)
def test_fts_safe_query_strips_operators_and_syntax(raw: str, expected: str) -> None:
    assert _fts_safe_query(raw) == expected


# ----------------------------------------------------------------------
# Knowledge relevance scoring
# ----------------------------------------------------------------------


def test_knowledge_relevance_full_term_coverage_dominates() -> None:
    content = "Deploy step requires the oauth secret and a retry policy."
    assert _knowledge_relevance("oauth secret retry", content) == 1.0


def test_knowledge_relevance_partial_coverage_ranks_below_verbatim() -> None:
    content = "The oauth flow stores its refresh token encrypted."
    partial = _knowledge_relevance("oauth deployment pipeline", content)
    verbatim = _knowledge_relevance("oauth refresh token", content)
    assert 0.1 <= partial < 1.0
    assert verbatim == 1.0
    assert partial < verbatim or partial == 1.0  # never above the ceiling


def test_knowledge_relevance_short_terms_do_not_drive_score() -> None:
    # Terms of length <= 2 are ignored; no terms -> floor score.
    assert _knowledge_relevance("a an the", "irrelevant content") == 0.1


# ----------------------------------------------------------------------
# Stream collection: buffered name before id
# ----------------------------------------------------------------------


def _collect(events):
    service = ProviderService.__new__(ProviderService)
    return service._collect_stream(iter(events))


def test_collect_stream_attaches_buffered_name_to_id_delta() -> None:
    events = [
        CanonicalStreamEvent(
            kind="tool_call_delta",
            tool_call=ToolCallResult(
                tool_name="echo",
                tool_call_id="",
                arguments="",
                result="",
            ),
        ),
        CanonicalStreamEvent(
            kind="tool_call_delta",
            tool_call=ToolCallResult(
                tool_name="",
                tool_call_id="call-1",
                arguments='{"m":',
                result="",
            ),
        ),
        CanonicalStreamEvent(
            kind="tool_call_delta",
            tool_call=ToolCallResult(
                tool_name="",
                tool_call_id="call-1",
                arguments='"x"}',
                result="",
            ),
        ),
        CanonicalStreamEvent(kind="message_end", finish_reason="tool_calls"),
    ]
    response = _collect(events)
    assert len(response.tool_calls) == 1
    call = response.tool_calls[0]
    assert call.tool_name == "echo"
    assert call.tool_call_id == "call-1"
    assert call.arguments == '{"m":"x"}'


# ----------------------------------------------------------------------
# Model catalog
# ----------------------------------------------------------------------


def test_model_catalog_known_and_unknown_models() -> None:
    adapter = OpenAICompatibleProviderAdapter(api_key="test-key")
    known = adapter.get_model("gpt-4o-mini")
    assert (known.context_window, known.max_output_tokens) == (128_000, 16_384)

    dated = adapter.get_model("gpt-4o-2024-08-06")
    assert dated.context_window == 128_000

    unknown = adapter.get_model("totally-unknown-model")
    assert (
        unknown.context_window,
        unknown.max_output_tokens,
    ) == (
        OpenAICompatibleProviderAdapter._DEFAULT_CONTEXT_WINDOW,
        OpenAICompatibleProviderAdapter._DEFAULT_MAX_OUTPUT_TOKENS,
    )
    assert unknown.context_window < 128_000  # conservative, not fabricated flagship


# ----------------------------------------------------------------------
# Tool declarations reach the wire payload with real schemas
# ----------------------------------------------------------------------


def test_openai_payload_includes_declaration_schemas() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = OpenAICompatibleProviderAdapter(api_key="k", client=client)
    from zero.domain.providers import CanonicalMessage, CanonicalRequest

    request = CanonicalRequest(
        provider="openai-compatible",
        model_name="gpt-4o-mini",
        messages=(CanonicalMessage(role="user", content="hi"),),
        tools=(
            ToolDeclaration(
                name="write_file",
                description="Write a file.",
                parameters={
                    "type": "object",
                    "properties": {"relative_path": {"type": "string"}},
                    "required": ["relative_path"],
                },
            ),
            "bare_tool",
        ),
    )
    adapter.send_request(request)
    payload_tools = captured["payload"]["tools"]
    assert payload_tools[0]["function"]["parameters"]["required"] == ["relative_path"]
    assert payload_tools[0]["function"]["description"] == "Write a file."
    assert payload_tools[1]["function"]["parameters"] == {"type": "object"}


# ----------------------------------------------------------------------
# Usage dedup via row probe
# ----------------------------------------------------------------------


def test_usage_duplicate_detection_is_probe_based(services) -> None:
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(owner_id=owner.id, name="Usage Dedup")

    from zero.domain.providers import ProviderRequest

    repo = services.providers._repo
    req = ProviderRequest(
        id=ProviderRequestId(generate_provider_request_id()),
        project_id=project.id,
        execution_id=None,
        provider="fake",
        model_name="fake-standard",
        request_hash="hash-dedup-probe",
        state="completed",
        started_at="2026-01-01T00:00:00.000000Z",
    )
    assert repo.insert_provider_request(req) is True

    def make_record(message_id, record_id=None):
        from zero.domain.providers import UsageRecord

        return UsageRecord(
            id=record_id or UsageRecordId(generate_usage_record_id()),
            project_id=project.id,
            provider_request_id=req.id,
            execution_id=None,
            provider_message_id=message_id,
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            estimated_cost_usd="0",
            pricing_catalog_version=1,
            is_whole_tree=True,
            created_at="2026-01-01T00:00:00.000000Z",
        )

    first = make_record("msg-1")
    assert repo.insert_usage_record(first) is True
    duplicate = make_record("msg-1")
    assert repo.insert_usage_record(duplicate) is False

    different_id = make_record("msg-2")
    assert repo.insert_usage_record(different_id) is True

    # A primary-key collision is NOT treated as a logical duplicate.
    colliding = make_record("msg-3", record_id=first.id)
    with pytest.raises(Exception):  # noqa: B017 - IntegrityError surfaces loudly
        repo.insert_usage_record(colliding)
