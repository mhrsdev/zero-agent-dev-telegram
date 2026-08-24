"""Reference-parity fixes (Hermes Agent + Claude Code round).

- Same-provider bounded retry with jittered exponential backoff and
  Retry-After honoring (Hermes conversation_loop/retry_utils parity).
- Retry-After surfaced by the Anthropic adapter on 429 responses.
- Duplicate tool names deduplicated in OpenAI payloads (Hermes guard).
- Compaction threshold reserves output tokens; fit ladder protects the
  head message.
- Hardline dangerous-command floor in the worktree policy
  (Hermes approval.py hardline parity, argv-shaped for Zero's runner).
"""

from __future__ import annotations

import httpx
import pytest

from zero.app.provider_adapter import (
    AnthropicMessagesProviderAdapter,
    OpenAICompatibleProviderAdapter,
    _render_tools,
)
from zero.app.services import build_services
from zero.config import Settings
from zero.domain.providers import (
    CanonicalMessage,
    CanonicalRequest,
    ProviderError,
    ToolDeclaration,
)
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations


@pytest.fixture
def services(test_settings: Settings):
    database = Database(test_settings)
    apply_migrations(database)
    return build_services(test_settings, database)


def _request(provider="fake", model="fake-standard", content="hello"):
    return CanonicalRequest(
        provider=provider,
        model_name=model,
        messages=(CanonicalMessage(role="user", content=content),),
    )


# ----------------------------------------------------------------------
# Same-provider retry (Hermes conversation_loop parity)
# ----------------------------------------------------------------------


def test_rate_limit_failure_retried_with_retry_after_cap(services, monkeypatch):
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(owner_id=owner.id, name="RetryCap")
    request = _request()
    adapter = services.providers._adapters["fake"]
    calls = {"n": 0}
    sleeps: list[float] = []

    def rate_limited(req):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ProviderError("provider HTTP request failed with status 429 (retry_after=900)")
        return type(adapter).send_request(adapter, req)

    monkeypatch.setattr(adapter, "send_request", rate_limited)
    monkeypatch.setattr("zero.app.provider_service.time.sleep", lambda s: sleeps.append(s))

    _req, response = services.providers.send_request(
        project_id=project.id, actor_id=owner.id, request=request
    )
    assert calls["n"] == 2
    assert "Fake response" in response.content
    # Retry-After honored but capped under the request lease window.
    assert len(sleeps) == 1
    assert sleeps[0] <= 60.0 * 1.5  # cap + jitter headroom


def test_backoff_helper_parses_retry_after_and_caps() -> None:
    from zero.app.provider_service import _same_provider_backoff_seconds

    capped = _same_provider_backoff_seconds(Exception("(retry_after=600)"), 1)
    assert 60.0 <= capped <= 90.0  # provider cap 60s + jitter headroom

    exponential = _same_provider_backoff_seconds(Exception("connection reset"), 3)
    assert 4.0 <= exponential <= 6.0  # 1 * 2**2 = 4s base + jitter


def test_retry_disabled_when_attempts_is_one(services, monkeypatch):
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(owner_id=owner.id, name="RetryOne")

    original_attempts = services.providers._provider_max_attempts
    services.providers._provider_max_attempts = 1

    request = _request(content="trigger error")  # invalid_request class
    adapter = services.providers._adapters["fake"]
    calls = {"n": 0}

    def counting_send(req):
        calls["n"] += 1
        return type(adapter).send_request(adapter, req)

    monkeypatch.setattr(adapter, "send_request", counting_send)

    from zero.domain.providers import InvalidProviderRequestError

    with pytest.raises(InvalidProviderRequestError):
        services.providers.send_request(project_id=project.id, actor_id=owner.id, request=request)
    services.providers._provider_max_attempts = original_attempts
    assert calls["n"] == 1  # non-retryable class: exactly one dispatch


# ----------------------------------------------------------------------
# Adapter payload guards
# ----------------------------------------------------------------------


def test_openai_payload_dedupes_duplicate_tool_names() -> None:
    rendered = _render_tools(
        (
            ToolDeclaration(name="echo", description="", parameters={"type": "object"}),
            ToolDeclaration(name="echo", description="dup", parameters={"type": "object"}),
            ToolDeclaration(name="write_file", description="", parameters=None),
        )
    )
    names = [r["function"]["name"] for r in rendered]
    assert names == ["echo", "write_file"]


def test_anthropic_429_surfaces_retry_after_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "17"}, json={})

    adapter = AnthropicMessagesProviderAdapter(
        api_key="k", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    request = CanonicalRequest(
        provider="anthropic",
        model_name="claude-sonnet-4",
        messages=(CanonicalMessage(role="user", content="hi"),),
        max_tokens=64,
    )
    with pytest.raises(ProviderError) as exc_info:
        adapter.send_request(request)
    assert "(retry_after=17)" in str(exc_info.value)


def test_openai_tools_never_carry_cache_control() -> None:
    """The OpenAI wire format must not receive the Anthropic-only cache
    marker (parity guard against shared-renderer drift)."""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        payload = _json.loads(request.content.decode("utf-8"))
        captured["tools"] = payload.get("tools") or []
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {},
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = OpenAICompatibleProviderAdapter(api_key="k", client=client)
    request = CanonicalRequest(
        provider="openai-compatible",
        model_name="gpt-4o-mini",
        messages=(CanonicalMessage(role="user", content="hi"),),
        tools=(ToolDeclaration(name="read_file", description="", parameters=None),),
    )
    adapter.send_request(request)
    tools = captured["tools"]
    assert len(tools) == 1
    assert "cache_control" not in tools[0]


# ----------------------------------------------------------------------
# Compaction: output reserve + head protection
# ----------------------------------------------------------------------


def _seed_active_context(services, project_id, execution_id, version, token_count):
    from datetime import UTC, datetime

    from zero.domain.context import ContextVersion, ContextVersionId
    from zero.domain.ids import generate_context_version_id

    cv = ContextVersion(
        id=ContextVersionId(generate_context_version_id()),
        project_id=project_id,
        execution_id=execution_id,
        version=version,
        active=True,
        system_message="",
        user_prefix="",
        plan_contract="",
        execution_snapshot="",
        retrieved_context="[]",
        conversation_tail="[]",
        compaction_summary="",
        token_count=token_count,
        created_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    )
    services.compaction._context_repo.insert_context_version(cv)
    services.compaction._context_repo.activate_context_version(execution_id, version)


def test_should_compact_reserves_output_tokens(services) -> None:
    # Compaction records carry project-lineage triggers, so seed through
    # a real approved-plan execution.
    from tests.test_anthropic_and_hermes_parity import _approved_execution

    _owner, project, execution = _approved_execution(services, project_name="Reserve Owner")

    execution_id = execution.id
    window = 1000

    # 700 tokens: below the raw-window threshold (85%) but ABOVE the
    # reserved threshold ((1000-200)*85% = 680) when output is reserved.
    _seed_active_context(services, project.id, execution_id, 1, token_count=700)

    assert services.compaction.should_compact(execution_id, window) is False
    assert services.compaction.should_compact(execution_id, window, max_output_tokens=200) is True


def test_fit_ladder_protects_head_message() -> None:
    from zero.app.compaction_service import CompactionService

    messages = [
        {"role": "user", "content": "OBJECTIVE-KEEP-ME"},
        *[{"role": "assistant", "content": "filler" * 400} for _ in range(4)],
        {"role": "user", "content": "latest"},
    ]
    rung, kept = CompactionService._fit_messages(None, messages, budget=1200)
    assert rung in {"history_turn_selected", "tool_truncated"}
    assert kept[0]["content"] == "OBJECTIVE-KEEP-ME"
    assert kept[-1]["content"] == "latest"


# ----------------------------------------------------------------------
# Worktree hardline floor (Hermes approval.py parity)
# ----------------------------------------------------------------------


def _policy_service():
    settings = Settings.load_for_test()
    database = Database(settings)
    apply_migrations(database)
    svc = build_services(settings, database)
    from zero.app.worktree_service import WorktreeService
    from zero.persistence.repositories.worktree_repository import WorktreeRepository

    return WorktreeService(
        WorktreeRepository(svc.database),
        svc.worker._audit_repo,
        svc.authorization,
        allowed_commands={"pytest", "rm", "dd"},
        isolation_mode="host_bounded",
    )


def test_hardline_floor_blocks_destructive_commands() -> None:
    svc = _policy_service()
    with pytest.raises(Exception, match="unconditionally refused"):
        svc._validate_command("mkfs.ext4", (), 10)
    with pytest.raises(Exception, match="host-destructive"):
        svc._validate_command("dd", ("of=/dev/sda", "if=/dev/zero"), 10)
    with pytest.raises(Exception, match="filesystem root"):
        svc._validate_command("rm", ("-rf", "/"), 10)


def test_hardline_floor_allows_normal_commands() -> None:
    svc = _policy_service()
    svc._validate_command("pytest", ("-q",), 60)
    svc._validate_command("dd", ("if=input.bin", "of=./output.bin"), 30)
