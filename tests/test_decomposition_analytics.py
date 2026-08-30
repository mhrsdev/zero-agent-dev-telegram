"""S7 recovery analytics: per-model typo-rate tracking.

Covers the structured repair planner (planning vs applying), outcome
recording aggregates (typo rates per model, first-ask discipline,
fallbacks), the JSONL evidence sink, and end-to-end decomposer wiring
(near-miss typos rescued AND recorded against the offending model).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from zero.app.decomposition_analytics import (
    OUTCOME_NATIVE_FIRST_ASK,
    OUTCOME_RECOVERED_ORDER,
    OUTCOME_RECOVERED_REPAIR,
    OUTCOME_SINGLE_TASK_FALLBACK,
    OUTCOME_TRANSPORT_ERROR,
    PATH_NATIVE,
    DecompositionAnalytics,
    DecompositionOutcome,
    DependencyRepair,
    resolve_sink_path,
)
from zero.app.task_decomposition import (
    DECOMPOSITION_TOOL_NAME,
    TaskDecomposer,
    apply_dependency_repairs,
    plan_dependency_repairs,
)
from zero.domain.plans import PlanRevisionContent
from zero.domain.providers import (
    CanonicalResponse,
    TokenUsage,
    ToolCallResult,
)

# ---------------------------------------------------------------- repair planning


class TestPlanDependencyRepairs:
    def test_fuzzy_unique_match_records_similarity(self):
        tasks = [
            {"key": "develop_vendor_dashboard", "objective": "A", "depends_on": []},
            {"key": "admin", "objective": "B", "depends_on": ["build_vendor_dashboard"]},
        ]
        repairs = plan_dependency_repairs(tasks)
        fix = repairs[("admin", "build_vendor_dashboard")]
        assert fix.repaired_to == "develop_vendor_dashboard"
        assert fix.similarity is not None and 0.5 <= fix.similarity <= 1.0
        assert fix.task_key == "admin" and fix.raw_dependency == "build_vendor_dashboard"

    def test_case_only_fix_has_no_similarity_score(self):
        # Upper-cased reference cannot hit the exact-key skip and must
        # normalize through the lowercase index without a fuzzy score.
        tasks = [
            {"key": "Auth_Module", "objective": "A", "depends_on": []},
            {"key": "api", "objective": "B", "depends_on": ["AUTH_MODULE"]},
        ]
        repairs = plan_dependency_repairs(tasks)
        fix = repairs[("api", "AUTH_MODULE")]
        assert fix.similarity is None
        assert fix.repaired_to == "Auth_Module"

    def test_tie_yields_no_repair_entry(self):
        tasks = [
            {"key": "vendor_dashboard_v1", "objective": "A", "depends_on": []},
            {"key": "vendor_dashboard_v2", "objective": "B", "depends_on": []},
            {"key": "x", "objective": "C", "depends_on": ["vendor_dashboard_v3"]},
        ]
        assert plan_dependency_repairs(tasks) == {}

    def test_far_off_reference_not_planned(self):
        tasks = [
            {"key": "alpha", "objective": "A", "depends_on": []},
            {"key": "beta", "objective": "B", "depends_on": ["completely_unrelated_name"]},
        ]
        assert plan_dependency_repairs(tasks) == {}

    def test_apply_round_trip_and_validator(self):
        tasks = [
            {"key": "develop_vendor_dashboard", "objective": "A", "scope": [], "depends_on": []},
            {
                "key": "admin",
                "objective": "B",
                "scope": [],
                "depends_on": ["build_vendor_dashboard"],
            },
        ]
        from zero.app.task_decomposition import validate_decomposition

        repaired, note = apply_dependency_repairs(tasks, plan_dependency_repairs(tasks))
        assert "build_vendor_dashboard->develop_vendor_dashboard" in note
        assert validate_decomposition(json.dumps(repaired)) is not None


# ---------------------------------------------------------------- ledger + sinks


def _outcome(**overrides) -> DecompositionOutcome:
    fields = {
        "ts_utc": "2026-08-27T00:00:00.000+00:00",
        "revision_id": "rev_x",
        "provider": "openai-compatible",
        "model_name": "glm-4.6",
        "outcome": OUTCOME_NATIVE_FIRST_ASK,
        "path": PATH_NATIVE,
        "attempts_used": 1,
        "task_count": 5,
        "edge_count": 4,
        "repairs": (),
        "elapsed_ms": 9000,
    }
    fields.update(overrides)
    return DecompositionOutcome(**fields)


class TestAnalyticsAggregates:
    def test_typo_rate_per_model(self):
        analytics = DecompositionAnalytics()
        for index in range(4):
            analytics.record(_outcome(outcome=OUTCOME_NATIVE_FIRST_ASK, revision_id=f"r{index}"))
        for index in range(2):
            analytics.record(
                _outcome(
                    outcome=OUTCOME_RECOVERED_REPAIR,
                    revision_id=f"t{index}",
                    repairs=(
                        DependencyRepair("a", "buld_step_one", "build_step_one", 0.6),
                        DependencyRepair("b", "auth_modul", "auth_module", None),
                    ),
                )
            )
        snap = analytics.snapshot()
        model = snap["models"]["openai-compatible:glm-4.6"]
        assert model["decomposition_attempts"] == 6
        assert model["graphs_validated"] == 6
        assert model["first_ask_ok"] == 4
        assert model["recovered_key_repair"] == 2
        assert model["typo_references_rescued"] == 4
        assert model["typo_rate_per_graph"] == round(4 / 6, 4)
        assert model["avg_tasks_per_graph"] == 5.0

    def test_fallbacks_transport_errors_and_exceptions_separate(self):
        analytics = DecompositionAnalytics()
        analytics.record(_outcome(outcome=OUTCOME_SINGLE_TASK_FALLBACK, task_count=0))
        analytics.record(_outcome(outcome=OUTCOME_TRANSPORT_ERROR, task_count=0))
        analytics.record(_outcome(outcome="decomposer_exception", task_count=0))
        model = analytics.snapshot()["models"]["openai-compatible:glm-4.6"]
        assert model["single_task_fallbacks"] == 1
        assert model["transport_errors"] == 1
        assert model["decomposer_exceptions"] == 1
        assert model["success_rate"] == 0.0

    def test_models_are_grouped_independently(self):
        analytics = DecompositionAnalytics()
        analytics.record(
            _outcome(
                model_name="glm-4.6",
                outcome=OUTCOME_RECOVERED_REPAIR,
                repairs=(DependencyRepair("a", "x_y", "x_yy", 0.7),),
            )
        )
        analytics.record(_outcome(provider="anthropic", model_name="claude-sonnet-4"))
        snap = analytics.snapshot()
        assert snap["models"]["openai-compatible:glm-4.6"]["typo_references_rescued"] == 1
        assert snap["models"]["anthropic:claude-sonnet-4"]["first_ask_ok"] == 1

    def test_markdown_renders_table_rows(self):
        analytics = DecompositionAnalytics()
        analytics.record(_outcome())
        rendered = analytics.render_markdown()
        assert "openai-compatible:glm-4.6" in rendered
        assert "| 1 |" in rendered

    def test_jsonl_sink_persists_each_outcome(self, tmp_path):
        sink = tmp_path / "decomp_analytics.jsonl"
        analytics = DecompositionAnalytics(sink_path=sink)
        analytics.record(_outcome(outcome=OUTCOME_RECOVERED_ORDER, revision_id="r_ord"))
        analytics.record(
            _outcome(
                outcome=OUTCOME_RECOVERED_REPAIR,
                revision_id="r_rep",
                repairs=(DependencyRepair("t", "mispeled_key", "misspelled_key", 0.55),),
            )
        )
        lines = [json.loads(line) for line in sink.read_text(encoding="utf-8").splitlines()]
        assert len(lines) == 2
        assert lines[0]["revision_id"] == "r_ord"
        assert lines[1]["repairs"][0]["raw_dependency"] == "mispeled_key"
        assert lines[1]["repairs"][0]["similarity"] == 0.55

    def test_resolve_sink_path_from_env(self, monkeypatch):
        # Compare Path objects, not their string form: ``str(Path(...))``
        # renders the platform separator, so asserting the POSIX spelling
        # failed on Windows for a resolver that was behaving correctly.
        monkeypatch.setenv("ZERO_DECOMPOSITION_ANALYTICS_PATH", "/tmp/x/ledger.jsonl")
        assert resolve_sink_path() == Path("/tmp/x/ledger.jsonl")
        monkeypatch.setenv("ZERO_DECOMPOSITION_ANALYTICS_PATH", "   ")
        assert resolve_sink_path() is None


class _ScriptedProviders:
    """Minimal send_request double; each entry response or exception."""

    def __init__(self, scripted):
        self._scripted = list(scripted)

    def send_request(self, *, request, idempotency_key=None, source="system", **_kwargs):
        outcome = self._scripted.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return (None, outcome)

    @staticmethod
    def tool_response(arguments: dict) -> CanonicalResponse:
        return CanonicalResponse(
            content="",
            tool_calls=(
                ToolCallResult(
                    tool_name=DECOMPOSITION_TOOL_NAME,
                    tool_call_id="tc_1",
                    arguments=json.dumps(arguments),
                    result="",
                ),
            ),
            finish_reason="tool_calls",
            usage=TokenUsage(input_tokens=10, output_tokens=5),
        )


class TestDecomposerAnalyticsWiring:
    def _revision_content(self):
        return PlanRevisionContent(
            objective="ship the dashboard",
            scope=("web",),
            constraints=(),
            acceptance_criteria=("renders",),
            risks=(),
            unresolved_questions=(),
            source_event_ids=(),
        )

    def _decompose(self, providers_mock, analytics):
        decomposer = TaskDecomposer(providers=providers_mock, analytics=analytics)

        class _Id:
            value = "proj_test"

        return decomposer.decompose(
            project_id=_Id(),
            actor_id=_Id(),
            revision_id=f"rev_{id(providers_mock):x}",
            revision_content=self._revision_content(),
            provider="openai-compatible",
            model_name="glm-4.6",
        )

    def test_near_miss_typos_rescued_and_recorded_per_model(self, tmp_path):
        sink = tmp_path / "sink.jsonl"
        analytics = DecompositionAnalytics(sink_path=sink)
        scripted_payload = {
            "tasks": [
                {"key": "build_step_one", "objective": "A", "depends_on": []},
                {"key": "step_two", "objective": "B", "depends_on": ["buld_step_one"]},
            ]
        }
        graph = self._decompose(
            _ScriptedProviders([_ScriptedProviders.tool_response(scripted_payload)]), analytics
        )
        assert graph is not None and len(graph.specs) == 2
        record = json.loads(sink.read_text(encoding="utf-8").splitlines()[0])
        assert record["outcome"] == OUTCOME_RECOVERED_REPAIR
        assert record["provider"] == "openai-compatible" and record["model_name"] == "glm-4.6"
        assert record["repairs"][0]["raw_dependency"] == "buld_step_one"
        assert record["task_count"] == 2 and record["edge_count"] == 1
        model = analytics.snapshot()["models"]["openai-compatible:glm-4.6"]
        assert model["typo_rate_per_graph"] == 1.0

    def test_clean_first_ask_recorded_as_native_success(self):
        analytics = DecompositionAnalytics()
        clean_payload = {
            "tasks": [
                {"key": "one", "objective": "A", "depends_on": []},
                {"key": "two", "objective": "B", "depends_on": ["one"]},
            ]
        }
        graph = self._decompose(
            _ScriptedProviders([_ScriptedProviders.tool_response(clean_payload)]), analytics
        )
        assert graph is not None
        model = analytics.snapshot()["models"]["openai-compatible:glm-4.6"]
        assert model["first_ask_ok"] == 1
        assert model["typo_references_rescued"] == 0

    def test_recording_never_breaks_decomposition(self):
        class _BoomSink:
            def record(self, _outcome):
                raise RuntimeError("sink exploded")

        analytics = _BoomSink()
        clean_payload = {"tasks": [{"key": "only", "objective": "A", "depends_on": []}]}
        graph = self._decompose(
            _ScriptedProviders([_ScriptedProviders.tool_response(clean_payload)]), analytics
        )
        assert graph is not None and len(graph.specs) == 1


class TestConcurrency:
    def test_parallel_records_keep_counts_exact(self, tmp_path):
        sink = tmp_path / "conc.jsonl"
        analytics = DecompositionAnalytics(sink_path=sink)
        threads = []

        def worker(tag: str) -> None:
            for index in range(25):
                analytics.record(
                    _outcome(
                        revision_id=f"{tag}_{index}",
                        outcome=OUTCOME_NATIVE_FIRST_ASK if index % 2 else OUTCOME_RECOVERED_REPAIR,
                        task_count=3,
                        edge_count=2,
                        repairs=(DependencyRepair("k", "aa_bb", "aa_bc", 0.66),),
                    )
                )

        for i in range(8):
            thread = threading.Thread(target=worker, args=(f"w{i}",))
            threads.append(thread)
            thread.start()
        for thread in threads:
            thread.join()
        model = analytics.snapshot()["models"]["openai-compatible:glm-4.6"]
        assert model["decomposition_attempts"] == 200
        # range(25) yields 13 even indexes per worker -> 13*8 recovered.
        assert model["typo_references_rescued"] == 104
        lines = sink.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 200


class TestTransportErrorNotPinned:
    """Transport blips must not permanently sentence a revision to
    single-task fallback: the cache stays open so a later tick (the
    background worker loop, another API process) can decompose."""

    def _revision_content(self):
        return PlanRevisionContent(
            objective="ship it",
            scope=("web",),
            constraints=(),
            acceptance_criteria=("works",),
            risks=(),
            unresolved_questions=(),
            source_event_ids=(),
        )

    def test_transport_failure_then_success_reruns_and_wins(self):
        analytics = DecompositionAnalytics()
        providers = _ScriptedProviders(
            [
                RuntimeError("provider HTTP request failed: connection reset"),
                _ScriptedProviders.tool_response(
                    {
                        "tasks": [
                            {"key": "one", "objective": "A", "depends_on": []},
                            {"key": "two", "objective": "B", "depends_on": ["one"]},
                        ]
                    }
                ),
            ]
        )
        decomposer = TaskDecomposer(providers=providers, analytics=analytics)

        class _Id:
            value = "proj_x"

        first = decomposer.decompose(
            project_id=_Id(),
            actor_id=_Id(),
            revision_id="rev_transport",
            revision_content=self._revision_content(),
            provider="openai-compatible",
            model_name="glm-4.6",
        )
        assert first is None  # degraded this round...
        second = decomposer.decompose(
            project_id=_Id(),
            actor_id=_Id(),
            revision_id="rev_transport",
            revision_content=self._revision_content(),
            provider="openai-compatible",
            model_name="glm-4.6",
        )
        assert second is not None and len(second.specs) == 2  # ...recovers next tick
        model = analytics.snapshot()["models"]["openai-compatible:glm-4.6"]
        assert model["transport_errors"] == 1
        assert model["first_ask_ok"] == 1

    def test_model_verdict_none_still_cached(self):
        # A definitive 'cannot produce valid output' answer IS pinned:
        # the strict ask AND the escalated re-ask already ran inside the
        # ladder before the None verdict.
        cyclic = {
            "tasks": [
                {"key": "a", "objective": "A", "depends_on": ["b"]},
                {"key": "b", "objective": "B", "depends_on": ["a"]},
            ]
        }
        analytics = DecompositionAnalytics()
        providers = _ScriptedProviders(
            [
                _ScriptedProviders.tool_response(cyclic),
                _ScriptedProviders.tool_response(cyclic),
                RuntimeError("should never be reached - outcome cached"),
            ]
        )
        decomposer = TaskDecomposer(providers=providers, analytics=analytics)

        class _Id:
            value = "proj_y"

        kwargs = {
            "project_id": _Id(),
            "actor_id": _Id(),
            "revision_id": "rev_pin",
            "revision_content": self._revision_content(),
            "provider": "openai-compatible",
            "model_name": "glm-4.6",
        }
        assert decomposer.decompose(**kwargs) is None
        assert decomposer.decompose(**kwargs) is None  # served from cache, no new call
        model = analytics.snapshot()["models"]["openai-compatible:glm-4.6"]
        assert model["decomposition_attempts"] == 1  # second call was cached
