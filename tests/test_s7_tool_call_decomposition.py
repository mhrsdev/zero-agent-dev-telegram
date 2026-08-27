"""S7 deepening: forced tool-call path (``tool_choice``) end-to-end.

Covers:
- canonical ``tool_choice`` normalization;
- OpenAI-compatible wire rendering (non-stream) and its absence when
  unset (byte-stable payloads);
- Anthropic ``tool_choice`` mapping and its hard 'none' refusal;
- request-hash sensitivity to ``tool_choice`` only when set;
- the decomposition ladder: forced first ask, escalated stricter re-ask,
  legacy text degradation for models without native tools, and bounded
  stop on transport failure.
"""

from __future__ import annotations

import json

import httpx
import pytest

from zero.app import provider_adapter
from zero.app.provider_adapter import compute_request_hash
from zero.app.task_decomposition import (
    DECOMPOSITION_SYSTEM_PROMPT,
    DECOMPOSITION_SYSTEM_PROMPT_ESCALATED,
    DECOMPOSITION_SYSTEM_PROMPT_STRICT,
    DECOMPOSITION_TOOL_DECLARATION,
    DECOMPOSITION_TOOL_NAME,
    TaskDecomposer,
    extract_task_graph_payload,
    normalize_task_order,
    repair_dangling_dependencies,
)
from zero.domain.providers import (
    CanonicalMessage,
    CanonicalRequest,
    CanonicalResponse,
    TokenUsage,
    ToolCallResult,
    normalize_tool_choice,
)


def _openai_adapter(handler):
    adapter_class = provider_adapter.OpenAICompatibleProviderAdapter
    return adapter_class(
        api_key="synthetic-test-key",
        base_url="https://provider.invalid/v1",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _anthropic_adapter(handler):
    adapter_class = provider_adapter.AnthropicMessagesProviderAdapter
    return adapter_class(
        api_key="synthetic-test-key",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _chat_request(**overrides) -> CanonicalRequest:
    fields = {
        "provider": "openai-compatible",
        "model_name": "test-model",
        "messages": (CanonicalMessage(role="user", content="Hello"),),
        "max_tokens": 128,
        "temperature": 0.2,
    }
    fields.update(overrides)
    return CanonicalRequest(**fields)


_OPENAI_OK = {
    "id": "chatcmpl_toolchoice_test",
    "choices": [
        {
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "any_tool", "arguments": "{}"},
                    }
                ],
            },
        }
    ],
    "usage": {"prompt_tokens": 3, "completion_tokens": 2},
}


class TestNormalizeToolChoice:
    def test_none_passes_through(self):
        assert normalize_tool_choice(None) is None

    @pytest.mark.parametrize("mode", ["auto", "none", "required", " REQUIRED "])
    def test_modes_casefold_and_strip(self, mode):
        assert normalize_tool_choice(mode) == mode.strip().lower()

    def test_shorthand_name_mapping_folds_to_canonical(self):
        assert normalize_tool_choice({"name": "emit"}) == {
            "type": "function",
            "name": "emit",
        }
        assert normalize_tool_choice({"function": {"name": "emit"}}) == {
            "type": "function",
            "name": "emit",
        }

    def test_rejects_unknown_mode_and_empty_name(self):
        with pytest.raises(ValueError):
            normalize_tool_choice("always")
        with pytest.raises(ValueError):
            normalize_tool_choice({"name": "   "})
        with pytest.raises(ValueError):
            normalize_tool_choice({"type": "builtin", "name": "x"})
        with pytest.raises(TypeError):
            normalize_tool_choice(42)


class TestOpenAIWireRendering:
    def test_forced_function_uses_nested_wire_shape(self):
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["payload"] = json.loads(request.read())
            return httpx.Response(200, json=_OPENAI_OK)

        response = _openai_adapter(handler).send_request(
            _chat_request(
                tools=(DECOMPOSITION_TOOL_DECLARATION,),
                tool_choice={"type": "function", "name": DECOMPOSITION_TOOL_NAME},
            )
        )
        payload = seen["payload"]
        assert payload["tool_choice"] == {
            "type": "function",
            "function": {"name": DECOMPOSITION_TOOL_NAME},
        }
        assert response.finish_reason == "tool_calls"
        assert response.tool_calls[0].tool_name == "any_tool"

    @pytest.mark.parametrize("mode", ["auto", "none", "required"])
    def test_mode_strings_travel_verbatim(self, mode):
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["payload"] = json.loads(request.read())
            return httpx.Response(200, json=_OPENAI_OK)

        _openai_adapter(handler).send_request(_chat_request(tools=("any_tool",), tool_choice=mode))
        assert seen["payload"]["tool_choice"] == mode

    def test_unset_choice_keeps_payload_free_of_the_key(self):
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["payload"] = json.loads(request.read())
            return httpx.Response(200, json=_OPENAI_OK)

        _openai_adapter(handler).send_request(_chat_request(tools=("any_tool",)))
        assert "tool_choice" not in seen["payload"]

    def test_stream_path_sends_the_same_forcing_shape(self):
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["payload"] = json.loads(request.read())
            body = (
                'data: {"id":"chatcmpl-tc","choices":[{"delta":{},'
                '"finish_reason":"tool_calls"}]}\ndata: [DONE]\n'
            )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=body.encode(),
            )

        events = list(
            _openai_adapter(handler).send_request_stream(
                _chat_request(
                    tools=(DECOMPOSITION_TOOL_DECLARATION,),
                    tool_choice={"type": "function", "name": DECOMPOSITION_TOOL_NAME},
                )
            )
        )
        assert seen["payload"]["tool_choice"] == {
            "type": "function",
            "function": {"name": DECOMPOSITION_TOOL_NAME},
        }
        assert any(event.finish_reason == "tool_calls" for event in events)

    def test_choice_without_tools_is_not_sent(self):
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["payload"] = json.loads(request.read())
            return httpx.Response(200, json=_OPENAI_OK)

        _openai_adapter(handler).send_request(_chat_request(tool_choice="required"))
        assert "tools" not in seen["payload"]
        assert "tool_choice" not in seen["payload"]


class TestAnthropicMapping:
    def _body(self, tool_choice) -> dict:
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.read())
            return httpx.Response(
                200,
                json={
                    "id": "msg_test",
                    "role": "assistant",
                    "model": "claude-test",
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "ok"}],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            )

        _anthropic_adapter(handler).send_request(
            _chat_request(
                provider="anthropic",
                tools=("any_tool",),
                tool_choice=tool_choice,
            )
        )
        return seen["body"]

    def test_auto_maps_to_auto(self):
        assert self._body("auto")["tool_choice"] == {"type": "auto"}

    def test_required_maps_to_any(self):
        assert self._body("required")["tool_choice"] == {"type": "any"}

    def test_forced_function_maps_to_named_tool(self):
        assert self._body({"name": "emit_task_graph"})["tool_choice"] == {
            "type": "tool",
            "name": "emit_task_graph",
        }

    def test_none_mode_is_refused_loudly(self):
        with pytest.raises(Exception, match="'none'"):
            self._body("none")


class TestRequestHashSensitivity:
    def test_unset_vs_default_hashes_are_identical(self):
        plain = _chat_request()
        explicit_none = _chat_request(tool_choice=None)
        assert compute_request_hash(plain) == compute_request_hash(explicit_none)

    def test_set_choice_changes_hash(self):
        base = compute_request_hash(_chat_request(tools=("t",)))
        forced = compute_request_hash(_chat_request(tools=("t",), tool_choice={"name": "t"}))
        required = compute_request_hash(_chat_request(tools=("t",), tool_choice="required"))
        assert len({base, forced, required}) == 3


@pytest.fixture
def services(test_settings):
    from zero.app.services import build_services
    from zero.config import Settings as _Settings  # noqa: F401
    from zero.persistence.connection import Database
    from zero.persistence.migrations import apply_migrations

    database = Database(test_settings)
    apply_migrations(database)
    return build_services(test_settings, database)


class TestFallbackChainPropagatesChoice:
    def test_reconstructed_attempt_keeps_tool_choice(self, services):
        """When the primary provider fails transiently, the request that
        reaches the fallback must still carry tools AND tool_choice —
        forcing is a semantic of the task, not of one provider."""
        from zero.app.provider_adapter import ProviderAdapter

        captured: dict = {}

        class TransientFailureAdapter(ProviderAdapter):
            provider_name = "fake-s7-fail"

            def __init__(self, inner):
                self._inner = inner

            def get_model(self, model_name: str):
                return self._inner.get_model(model_name)

            def send_request(self, request, cancel_event=None):
                raise RuntimeError("connection reset; transient outage")

        class CaptureMirror(ProviderAdapter):
            provider_name = "fake-mirror-s7"

            def __init__(self, inner):
                self._inner = inner

            def get_model(self, model_name: str):
                return self._inner.get_model(model_name)

            def send_request(self, request, cancel_event=None):
                captured["provider"] = request.provider
                captured["tools"] = request.tools
                captured["tool_choice"] = request.tool_choice
                return self._inner.send_request(request, cancel_event=cancel_event)

        owner = services.identity.create_user(display_name="s7 fallback owner")
        project = services.identity.create_project(owner_id=owner.id, name="S7Fallback")
        # Pre-seed provider_models rows for both synthetic providers so the
        # service-level get_model path resolves from the repository.
        conn = services.database.connect()
        for index, provider_name in enumerate(("fake-s7-fail", "fake-mirror-s7")):
            conn.execute(
                "INSERT INTO provider_models "
                "(id, provider, model_name, context_window, max_output_tokens, "
                "capabilities, is_active) VALUES (?, ?, ?, ?, ?, ?, 1)",
                (
                    f"pm_s7_seed_{index}",
                    provider_name,
                    "fake-standard",
                    200000,
                    8192,
                    json.dumps(["streaming", "native_tools"]),
                ),
            )
        conn.commit()

        fake_inner = services.providers._adapters["fake"]
        services.providers.register_adapter(TransientFailureAdapter(fake_inner))
        services.providers.register_adapter(CaptureMirror(fake_inner))
        services.providers.set_fallback_chain(("fake-s7-fail", "fake-mirror-s7"))

        services.providers.send_request_with_fallback(
            project_id=project.id,
            actor_id=owner.id,
            request=_chat_request(
                provider="fake-s7-fail",
                model_name="fake-standard",
                tools=(DECOMPOSITION_TOOL_DECLARATION,),
                tool_choice={"type": "function", "name": DECOMPOSITION_TOOL_NAME},
            ),
        )

        assert captured["provider"] == "fake-mirror-s7"
        assert [t.name for t in captured["tools"]] == [DECOMPOSITION_TOOL_NAME]
        assert captured["tool_choice"] == {
            "type": "function",
            "name": DECOMPOSITION_TOOL_NAME,
        }


# ----------------------------------------------------------------------
# Decomposition ladder
# ----------------------------------------------------------------------

_VALID_TASKS = [
    {"key": "auth", "objective": "Implement authentication"},
    {"key": "api", "objective": "Build API", "depends_on": ["auth"]},
]

_TOOL_CALL_RESPONSE_ARGS = {
    "tasks": [
        {"key": "auth", "objective": "Implement authentication", "scope": [], "depends_on": []},
        {"key": "api", "objective": "Build API", "scope": [], "depends_on": ["auth"]},
    ]
}


class _RecordingProviders:
    """Scripted send_request double capturing every call."""

    def __init__(self, scripted: list[CanonicalResponse | Exception]) -> None:
        self._scripted = list(scripted)
        self.calls: list[dict] = []

    def send_request(self, *, request, idempotency_key=None, source="system", **_kwargs):
        self.calls.append({"request": request, "idempotency_key": idempotency_key})
        outcome = self._scripted.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return (None, outcome)

    @staticmethod
    def tool_response(arguments: dict | str) -> CanonicalResponse:
        args = arguments if isinstance(arguments, str) else json.dumps(arguments)
        return CanonicalResponse(
            content="",
            tool_calls=(
                ToolCallResult(
                    tool_name=DECOMPOSITION_TOOL_NAME,
                    tool_call_id=f"tc_{len(args) % 97}",
                    arguments=args,
                    result="",
                ),
            ),
            finish_reason="tool_calls",
            usage=TokenUsage(input_tokens=10, output_tokens=5),
        )

    @staticmethod
    def text_response(content: str) -> CanonicalResponse:
        return CanonicalResponse(
            content=content,
            finish_reason="stop",
            usage=TokenUsage(input_tokens=9, output_tokens=4),
        )


class TestExtractTaskGraphPayload:
    def test_prefers_emit_task_graph_tool_call(self):
        response = _RecordingProviders.tool_response(_TOOL_CALL_RESPONSE_ARGS)
        tasks = extract_task_graph_payload(response)
        assert tasks is not None and [t["key"] for t in tasks] == ["auth", "api"]

    def test_accepts_tasks_wrapped_object_arguments(self):
        wrapped = CanonicalResponse(
            content="",
            tool_calls=(
                ToolCallResult(
                    tool_name=DECOMPOSITION_TOOL_NAME,
                    tool_call_id="tc_w",
                    arguments=json.dumps({"tasks": _VALID_TASKS}),
                    result="",
                ),
            ),
        )
        tasks = extract_task_graph_payload(wrapped)
        assert tasks is not None and len(tasks) == 2

    def test_content_inline_array_is_second_chance(self):
        response = _RecordingProviders.text_response(json.dumps(_VALID_TASKS))
        tasks = extract_task_graph_payload(response)
        assert tasks is not None and len(tasks) == 2

    def test_unrelated_tool_calls_are_ignored(self):
        response = CanonicalResponse(
            content="",
            tool_calls=(
                ToolCallResult(
                    tool_name="other_tool",
                    tool_call_id="tc_x",
                    arguments=json.dumps({"tasks": _VALID_TASKS}),
                    result="",
                ),
            ),
        )
        assert extract_task_graph_payload(response) is None


class TestNormalizeTaskOrder:
    def test_already_ordered_passes_through_unchanged(self):
        tasks = [
            {"key": "a", "objective": "A", "depends_on": []},
            {"key": "b", "objective": "B", "depends_on": ["a"]},
        ]
        assert normalize_task_order(tasks) == tasks

    def test_forward_reference_is_repaired(self):
        forward = [
            {"key": "deploy", "objective": "Ship it", "depends_on": ["test"]},
            {"key": "build", "objective": "Compile", "depends_on": []},
            {"key": "test", "objective": "Verify", "depends_on": ["build"]},
        ]
        ordered = normalize_task_order(forward)
        assert ordered is not None
        keys = [entry["key"] for entry in ordered]
        assert keys.index("test") < keys.index("deploy")
        assert set(keys) == {"deploy", "build", "test"}

    def test_dangling_reference_stays_rejected(self):
        dangling = [{"key": "a", "objective": "A", "depends_on": ["ghost"]}]
        assert normalize_task_order(dangling) is None

    def test_self_dependency_stays_rejected(self):
        self_dep = [{"key": "a", "objective": "A", "depends_on": ["a"]}]
        assert normalize_task_order(self_dep) is None

    def test_cycle_stays_rejected(self):
        cycle = [
            {"key": "a", "objective": "A", "depends_on": ["b"]},
            {"key": "b", "objective": "B", "depends_on": ["a"]},
        ]
        assert normalize_task_order(cycle) is None

    def test_decomposer_accepts_forward_referenced_tool_output(self):
        forward_response = CanonicalResponse(
            content="",
            tool_calls=(
                ToolCallResult(
                    tool_name=DECOMPOSITION_TOOL_NAME,
                    tool_call_id="tc_fwd",
                    arguments=json.dumps(
                        {
                            "tasks": [
                                {
                                    "key": "wire_ui",
                                    "objective": "Wire UI to API",
                                    "scope": [],
                                    "depends_on": ["api"],
                                },
                                {"key": "auth", "objective": "Auth", "scope": [], "depends_on": []},
                                {
                                    "key": "api",
                                    "objective": "Build API",
                                    "scope": [],
                                    "depends_on": ["auth"],
                                },
                            ]
                        }
                    ),
                    result="",
                ),
            ),
            finish_reason="tool_calls",
        )
        mock = _RecordingProviders([forward_response])
        harness = TestDecomposerLadder()
        graph, calls, _keys = harness._decompose(mock)
        assert graph is not None and len(graph.specs) == 3
        assert sorted(spec.key for spec in graph.specs) == ["api", "auth", "wire_ui"]
        dependency_pairs = {
            (
                edge.task_key.key if hasattr(edge.task_key, "key") else edge.task_key,
                edge.depends_on_key,
            )
            for edge in graph.dependencies
        }
        assert ("wire_ui", "api") in dependency_pairs
        assert ("api", "auth") in dependency_pairs
        assert len(calls) == 1  # accepted on the first ask, no escalation


class TestRepairDanglingDependencies:
    def test_near_miss_typo_is_repaired(self):
        tasks = [
            {"key": "develop_vendor_dashboard", "objective": "Dashboard", "depends_on": ["auth"]},
            {"key": "auth", "objective": "Auth", "depends_on": []},
            {
                "key": "admin_dashboard",
                "objective": "Admin",
                "depends_on": ["build_vendor_dashboard"],
            },
        ]
        repaired, note = repair_dangling_dependencies(tasks)
        assert repaired is not None
        assert repaired[2]["depends_on"] == ["develop_vendor_dashboard"]
        assert "build_vendor_dashboard" in note

    def test_case_only_mismatch_is_normalized(self):
        tasks = [
            {"key": "Auth_Module", "objective": "A", "depends_on": []},
            {"key": "ui", "objective": "U", "depends_on": ["AUTH_MODULE"]},
        ]
        repaired, _note = repair_dangling_dependencies(tasks)
        assert repaired is not None
        assert repaired[1]["depends_on"] == ["Auth_Module"]

    def test_ambiguous_candidate_aborts_repair(self):
        tasks = [
            {"key": "vendor_dashboard_v1", "objective": "A", "depends_on": []},
            {"key": "vendor_dashboard_v2", "objective": "B", "depends_on": []},
            {"key": "x", "objective": "C", "depends_on": ["vendor_dashboard_v3"]},
        ]
        repaired, _note = repair_dangling_dependencies(tasks)
        assert repaired is None or repaired == tasks

    def test_far_off_reference_is_not_repaired(self):
        tasks = [
            {"key": "alpha", "objective": "A", "depends_on": []},
            {"key": "beta", "objective": "B", "depends_on": ["completely_unrelated_name"]},
        ]
        repaired, _note = repair_dangling_dependencies(tasks)
        assert repaired is None or repaired == tasks

    def test_repair_result_passes_validator(self):
        tasks = [
            {
                "key": "develop_vendor_dashboard",
                "objective": "Dashboard",
                "scope": [],
                "depends_on": [],
            },
            {
                "key": "admin",
                "objective": "Admin",
                "scope": [],
                "depends_on": ["build_vendor_dashboard"],
            },
        ]
        repaired, _note = repair_dangling_dependencies(tasks)
        from zero.app.task_decomposition import validate_decomposition as _validate

        assert _validate(json.dumps(repaired)) is not None


class TestDecomposerLadder:
    def _revision_fields(self):
        from uuid import uuid4

        revision_id = f"rev_{uuid4().hex[:12]}"
        from zero.domain.plans import PlanRevisionContent

        content = PlanRevisionContent(
            objective="build the thing",
            scope=("backend",),
            constraints=(),
            acceptance_criteria=("works",),
            risks=(),
            unresolved_questions=(),
            source_event_ids=(),
        )
        return revision_id, content

    def _decompose(self, providers_mock, *, provider="openai-compatible", model_name="glm-4.6"):
        revision_id, content = self._revision_fields()
        decomposer = TaskDecomposer(providers=providers_mock)

        class _Project:
            value = "proj_test"

        class _Actor:
            value = "usr_test"

        graph = decomposer.decompose(
            project_id=_Project(),
            actor_id=_Actor(),
            revision_id=revision_id,
            revision_content=content,
            provider=provider,
            model_name=model_name,
        )
        keys = [c["idempotency_key"] for c in providers_mock.calls]
        return graph, providers_mock.calls, keys

    def test_native_tool_call_success_on_first_ask(self):
        mock = _RecordingProviders([_RecordingProviders.tool_response(_TOOL_CALL_RESPONSE_ARGS)])
        graph, calls, keys = self._decompose(mock)
        assert graph is not None and len(graph.specs) == 2
        assert len(calls) == 1
        assert keys[-1].endswith(":t1")
        sent = calls[0]["request"]
        assert sent.tool_choice == {"type": "function", "name": DECOMPOSITION_TOOL_NAME}
        assert sent.system_message == DECOMPOSITION_SYSTEM_PROMPT_STRICT
        assert sent.tools[0].name == DECOMPOSITION_TOOL_NAME

    def test_invalid_then_escalated_stricter_ask_succeeds(self):
        mock = _RecordingProviders(
            [
                _RecordingProviders.text_response("I'm sorry, I cannot..."),
                _RecordingProviders.tool_response(_TOOL_CALL_RESPONSE_ARGS),
            ]
        )
        graph, calls, keys = self._decompose(mock)
        assert graph is not None and len(graph.specs) == 2
        assert len(calls) == 2
        assert keys == [keys[0], keys[1]]
        assert keys[0].endswith(":t1") and keys[1].endswith(":t2")
        assert calls[1]["request"].system_message == DECOMPOSITION_SYSTEM_PROMPT_ESCALATED

    def test_both_attempts_invalid_returns_none_with_two_calls(self):
        mock = _RecordingProviders(
            [
                _RecordingProviders.text_response("nope"),
                _RecordingProviders.text_response("still nope"),
            ]
        )
        graph, calls, _keys = self._decompose(mock)
        assert graph is None
        assert len(calls) == 2

    def test_transport_failure_stops_ladder_immediately(self):
        mock = _RecordingProviders([RuntimeError("connection refused")])
        graph, calls, _keys = self._decompose(mock)
        assert graph is None
        assert len(calls) == 1

    def test_missing_native_tools_degrades_to_legacy_text_path(self):
        capability_error = ValueError(
            "provider model fake:text-only-model does not support native tools"
        )
        mock = _RecordingProviders(
            [
                capability_error,
                _RecordingProviders.text_response(json.dumps(_VALID_TASKS)),
            ]
        )
        graph, calls, keys = self._decompose(mock, provider="fake", model_name="text-only-model")
        assert graph is not None and len(graph.specs) == 2
        assert len(calls) == 2
        native_request = calls[0]["request"]
        assert native_request.tools != ()  # first ask tried to force
        legacy_request = calls[1]["request"]
        assert legacy_request.tools == ()
        assert legacy_request.tool_choice is None
        assert legacy_request.system_message == DECOMPOSITION_SYSTEM_PROMPT
        assert keys[0].endswith(":t1")
        assert ":t" not in keys[1].rsplit("decompose:", 1)[1]

    def test_native_rejection_translates_to_legacy_path_without_native_noise(self):
        capability_error = ValueError("provider model fake:text-only does not support native tools")
        mock = _RecordingProviders([capability_error])
        graph, calls, keys = self._decompose(mock, provider="fake", model_name="text-only")
        assert graph is None  # degraded legacy ask also had nothing usable left
        assert len(calls) == 2
        legacy_call = calls[1]
        assert legacy_call["request"].system_message == DECOMPOSITION_SYSTEM_PROMPT
        assert legacy_call["request"].tools == ()
        assert keys[0].endswith(":t1")
        assert ":t" not in keys[1].rsplit("decompose:", 1)[1]

    def test_gateway_rejecting_forced_shape_degrades_to_legacy(self):
        from zero.domain.providers import ProviderError

        gateway_400 = ProviderError("provider HTTP request failed with status 400")
        mock = _RecordingProviders(
            [
                gateway_400,
                _RecordingProviders.text_response(json.dumps(_VALID_TASKS)),
            ]
        )
        graph, calls, keys = self._decompose(mock)
        assert graph is not None and len(graph.specs) == 2
        assert len(calls) == 2
        assert calls[1]["request"].tools == ()
        assert calls[1]["request"].system_message == DECOMPOSITION_SYSTEM_PROMPT
        assert keys[0].endswith(":t1")

    def test_gateway_rate_limit_stops_ladder_without_legacy_spend(self):
        from zero.domain.providers import ProviderError

        rate_limited = ProviderError("provider HTTP request failed with status 429")
        mock = _RecordingProviders([rate_limited])
        graph, calls, _keys = self._decompose(mock)
        assert graph is None
        # Transient 429 is an outage signal, not a shape rejection: the
        # ladder must stop instead of burning a legacy attempt.
        assert len(calls) == 1

    def test_validator_still_guards_tool_call_output(self):
        cyclic = {
            "tasks": [
                {"key": "a", "objective": "A", "depends_on": ["b"]},
                {"key": "b", "objective": "B", "depends_on": ["a"]},
            ]
        }
        mock = _RecordingProviders(
            [
                _RecordingProviders.tool_response(cyclic),
                _RecordingProviders.tool_response(cyclic),
            ]
        )
        graph, calls, _keys = self._decompose(mock)
        assert graph is None
        assert len(calls) == 2
