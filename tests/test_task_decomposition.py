"""GAP 10 tests: LLM-driven task decomposition with safe fallback."""

from __future__ import annotations

import json

import pytest

from zero.app.scheduler_service import SchedulerService
from zero.app.services import build_services
from zero.app.task_decomposition import (
    TaskDecomposer,
    validate_decomposition,
)
from zero.app.worker_service import TaskSpec
from zero.config import Settings
from zero.domain.plans import PlanRevisionContent
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations

VALID_GRAPH = json.dumps(
    [
        {
            "key": "auth",
            "objective": "Implement authentication",
            "scope": ["backend"],
            "depends_on": [],
        },
        {
            "key": "api",
            "objective": "Build the API layer",
            "scope": ["backend"],
            "depends_on": ["auth"],
        },
        {
            "key": "ui",
            "objective": "Wire the UI to the API",
            "scope": ["frontend"],
            "depends_on": ["auth", "api"],
        },
    ]
)


class TestValidateDecomposition:
    def test_valid_chain_passes(self):
        graph = validate_decomposition(VALID_GRAPH)
        assert graph is not None
        assert [s.key for s in graph.specs] == ["auth", "api", "ui"]
        assert len(graph.dependencies) == 3
        assert all(isinstance(s, TaskSpec) for s in graph.specs)

    def test_code_fenced_json_passes(self):
        fenced = "```json\n" + VALID_GRAPH + "\n```"
        graph = validate_decomposition(fenced)
        assert graph is not None and len(graph.specs) == 3

    def test_rejects_malformed_json(self):
        assert validate_decomposition("not json at all") is None
        assert validate_decomposition("") is None
        assert validate_decomposition("[]") is None  # empty graph → fallback
        assert validate_decomposition('{"key": "x"}') is None  # not a list

    def test_rejects_dangling_dependency(self):
        payload = json.dumps(
            [
                {"key": "a", "objective": "A", "depends_on": ["ghost"]},
            ]
        )
        assert validate_decomposition(payload) is None

    def test_rejects_cycles(self):
        payload = json.dumps(
            [
                {"key": "a", "objective": "A", "depends_on": ["b"]},
                {"key": "b", "objective": "B", "depends_on": ["a"]},
            ]
        )
        assert validate_decomposition(payload) is None

    def test_rejects_duplicate_keys(self):
        payload = json.dumps(
            [
                {"key": "a", "objective": "A"},
                {"key": "a", "objective": "A2"},
            ]
        )
        assert validate_decomposition(payload) is None

    def test_rejects_empty_objective_and_bad_key(self):
        bad_objective = json.dumps([{"key": "a", "objective": "   "}])
        bad_key = json.dumps([{"key": "has space!", "objective": "A"}])
        missing_key = json.dumps([{"objective": "A"}])
        for payload in (bad_objective, bad_key, missing_key):
            assert validate_decomposition(payload) is None

    def test_rejects_oversized_graph(self):
        big = [{"key": f"t{i}", "objective": f"task {i}", "depends_on": []} for i in range(300)]
        assert validate_decomposition(json.dumps(big)) is None

    def test_rejects_oversized_payload(self):
        huge = json.dumps([{"key": "a", "objective": "x" * (70 * 1024), "depends_on": []}])
        assert validate_decomposition(huge) is None

    def test_self_dependency_rejected(self):
        payload = json.dumps([{"key": "a", "objective": "A", "depends_on": ["a"]}])
        assert validate_decomposition(payload) is None


@pytest.fixture
def services(test_settings: Settings):
    database = Database(test_settings)
    apply_migrations(database)
    return build_services(test_settings, database)


class _ScriptedAdapter:
    """Minimal provider adapter returning a canned response."""

    def __init__(self, content: str) -> None:
        self._content = content
        self.calls = 0

    @property
    def provider_name(self):
        return "fake"

    def get_model(self, model_name):
        from zero.domain.ids import generate_provider_model_id
        from zero.domain.providers import ProviderModel, ProviderModelId

        return ProviderModel(
            id=ProviderModelId(generate_provider_model_id()),
            provider="fake",
            model_name=model_name,
            context_window=200000,
            max_output_tokens=8192,
            capabilities=("streaming",),
            is_active=True,
        )

    def send_request(self, request, *, cancel_event=None, **_kwargs):
        """Mirror ProviderService.send_request's (request, response) shape."""
        from zero.domain.providers import CanonicalResponse, TokenUsage

        self.calls += 1
        return (
            None,
            CanonicalResponse(
                content=self._content,
                tool_calls=(),
                finish_reason="stop",
                usage=TokenUsage(input_tokens=10, output_tokens=5),
                provider_message_id="fake_msg_decomp",
            ),
        )


def _approved_revision(services, *, idem: str):
    owner = services.identity.create_user(display_name="dec owner")
    project = services.identity.create_project(owner_id=owner.id, name="Dec")
    event = services.plans.ingest_conversation_event(
        project_id=project.id,
        actor_id=owner.id,
        source="web",
        origin_kind="authenticated_human",
        content="decompose me",
    )
    plan = services.plans.create_plan(project_id=project.id, actor_id=owner.id)
    services.plans.propose_revision(
        plan_id=plan.id,
        project_id=project.id,
        actor_id=owner.id,
        content=PlanRevisionContent(
            objective="build the thing",
            scope=("backend",),
            constraints=(),
            acceptance_criteria=("works",),
            risks=(),
            unresolved_questions=(),
            source_event_ids=(event.id,),
        ),
    )
    _, handoff = services.plans.approve_revision(
        plan_id=plan.id,
        project_id=project.id,
        actor_id=owner.id,
        expected_revision_number=1,
        idempotency_key=idem,
    )
    revision = services.plans.get_revision(
        handoff.revision_id, project_id=project.id, actor_id=owner.id
    )
    return owner, project, handoff, revision


class TestTaskDecomposer:
    def test_valid_response_produces_cached_graph(self, services):
        owner, project, _handoff, revision = _approved_revision(services, idem="dec-1")
        adapter = _ScriptedAdapter(VALID_GRAPH)
        decomposer = TaskDecomposer(providers=adapter)
        graph = decomposer.decompose(
            project_id=project.id,
            actor_id=owner.id,
            revision_id=revision.id.value,
            revision_content=revision.content,
            provider="fake",
            model_name="fake-standard",
        )
        assert graph is not None and len(graph.specs) == 3
        assert adapter.calls == 1
        # Idempotent: second call uses the cache.
        again = decomposer.decompose(
            project_id=project.id,
            actor_id=owner.id,
            revision_id=revision.id.value,
            revision_content=revision.content,
            provider="fake",
            model_name="fake-standard",
        )
        assert again is graph
        assert adapter.calls == 1

    def test_invalid_output_returns_none(self, services):
        owner, project, _handoff, revision = _approved_revision(services, idem="dec-2")
        adapter = _ScriptedAdapter("I cannot produce JSON, sorry!")
        decomposer = TaskDecomposer(providers=adapter)
        graph = decomposer.decompose(
            project_id=project.id,
            actor_id=owner.id,
            revision_id=revision.id.value,
            revision_content=revision.content,
            provider="fake",
            model_name="fake-standard",
        )
        assert graph is None

    def test_provider_error_returns_none(self, services):
        owner, project, _handoff, revision = _approved_revision(services, idem="dec-3")

        class Exploding(_ScriptedAdapter):
            def send_request(self, request, *, cancel_event=None):
                raise RuntimeError("provider exploded")

        decomposer = TaskDecomposer(providers=Exploding(""))
        graph = decomposer.decompose(
            project_id=project.id,
            actor_id=owner.id,
            revision_id=revision.id.value,
            revision_content=revision.content,
            provider="fake",
            model_name="fake-standard",
        )
        assert graph is None


class TestSchedulerIntegration:
    def _scheduler(self, services, decomposer=None, enabled=False):
        return SchedulerService(
            plans=services.plans,
            worker=services.worker,
            runtime=services.runtime,
            authorization=services.authorization,
            decomposer=decomposer,
            decomposition_enabled=enabled,
            task_max_attempts=0,
        )

    def _execution_for(self, services, revision):
        return services.worker._execution_repo.get_execution_for_revision(revision.id)

    def test_default_config_creates_single_task(self, services):
        owner, project, _handoff, revision = _approved_revision(services, idem="sched-1")
        scheduler = self._scheduler(services)  # disabled by default
        scheduler.run_once(
            project_id=project.id,
            actor_id=owner.id,
            lease_owner="tick",
            provider="fake",
            model_name="fake-standard",
        )
        execution = self._execution_for(services, revision)
        tasks = services.worker.list_tasks(execution.id, project_id=project.id, actor_id=owner.id)
        assert len(tasks) == 1
        assert tasks[0].objective == "build the thing"

    def test_enabled_decomposition_builds_multi_task_graph(self, services):
        owner, project, _handoff, revision = _approved_revision(services, idem="sched-2")
        adapter = _ScriptedAdapter(VALID_GRAPH)
        scheduler = self._scheduler(
            services, decomposer=TaskDecomposer(providers=adapter), enabled=True
        )
        scheduler.run_once(
            project_id=project.id,
            actor_id=owner.id,
            lease_owner="tick",
            provider="fake",
            model_name="fake-standard",
        )
        execution = self._execution_for(services, revision)
        tasks = services.worker.list_tasks(execution.id, project_id=project.id, actor_id=owner.id)
        assert len(tasks) == 3
        # Dependencies exist: api→auth and ui→auth+api (matched by
        # objective since keys are creation-time only).
        objectives = {t.id.value: t.objective for t in tasks}
        conn = services.database.connect()
        edges = conn.execute("SELECT task_id, depends_on_task_id FROM task_dependencies").fetchall()
        pairs = {
            (objectives[row["task_id"]], objectives[row["depends_on_task_id"]]) for row in edges
        }
        expected = {
            ("Build the API layer", "Implement authentication"),
            ("Wire the UI to the API", "Implement authentication"),
            ("Wire the UI to the API", "Build the API layer"),
        }
        assert pairs == expected

    def test_decomposition_failure_falls_back_to_single_task(self, services):
        owner, project, _handoff, revision = _approved_revision(services, idem="sched-3")
        adapter = _ScriptedAdapter("garbage")
        scheduler = self._scheduler(
            services, decomposer=TaskDecomposer(providers=adapter), enabled=True
        )
        scheduler.run_once(
            project_id=project.id,
            actor_id=owner.id,
            lease_owner="tick",
            provider="fake",
            model_name="fake-standard",
        )
        execution = self._execution_for(services, revision)
        tasks = services.worker.list_tasks(execution.id, project_id=project.id, actor_id=owner.id)
        assert len(tasks) == 1
