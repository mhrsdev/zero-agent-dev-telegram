"""GAP 8 tests: subagent delegation with depth caps and usage tagging."""

from __future__ import annotations

import json

import pytest

from zero.app.agent_runtime import AgentRuntime
from zero.app.delegation import (
    DELEGATE_TOOL_NAME,
    MAX_DELEGATION_DEPTH,
    current_delegation_depth,
)
from zero.app.provider_adapter import ProviderAdapter
from zero.app.services import build_services
from zero.config import Settings
from zero.domain.plans import PlanRevisionContent
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations


@pytest.fixture
def services(test_settings: Settings):
    database = Database(test_settings)
    apply_migrations(database)
    return build_services(test_settings, database)


class TestDepthContextVar:
    def test_default_depth_is_zero(self):
        assert current_delegation_depth() == 0

    def test_context_manager_increases_and_restores(self):
        from zero.app.delegation import delegation_depth_increased

        with delegation_depth_increased():
            assert current_delegation_depth() == 1
            with delegation_depth_increased():
                assert current_delegation_depth() == 2
            assert current_delegation_depth() == 1
        assert current_delegation_depth() == 0


class TestDeclarationGating:
    def test_delegate_declared_when_enabled_within_budget(self, services):
        runtime = AgentRuntime(
            worker=services.worker,
            providers=services.providers,
            artifacts=services.artifacts,
            authorization=services.authorization,
            enable_delegation=True,
        )
        names = [
            d.name if isinstance(d, object) and hasattr(d, "name") else str(d)
            for d in runtime._tool_declarations(())
        ]
        assert DELEGATE_TOOL_NAME in names

    def test_delegate_not_declared_when_disabled(self, services):
        runtime = AgentRuntime(
            worker=services.worker,
            providers=services.providers,
            artifacts=services.artifacts,
            authorization=services.authorization,
        )
        names = [str(d) for d in runtime._tool_declarations(())]
        assert all(DELEGATE_TOOL_NAME not in n for n in names)

    def test_delegate_not_declared_at_max_depth(self, services, monkeypatch):
        from zero.app import delegation as delegation_module

        runtime = AgentRuntime(
            worker=services.worker,
            providers=services.providers,
            artifacts=services.artifacts,
            authorization=services.authorization,
            enable_delegation=True,
        )
        token = delegation_module._delegate_depth.set(MAX_DELEGATION_DEPTH)
        try:
            names = [str(d) for d in runtime._tool_declarations(())]
        finally:
            delegation_module._delegate_depth.reset(token)
        assert all(DELEGATE_TOOL_NAME not in n for n in names)


def _approved_task(services, *, objective="Coordinate the work"):
    owner = services.identity.create_user(display_name="del owner")
    project = services.identity.create_project(owner_id=owner.id, name="Delegation")
    event = services.plans.ingest_conversation_event(
        project_id=project.id,
        actor_id=owner.id,
        source="web",
        origin_kind="authenticated_human",
        content="delegate something",
    )
    plan = services.plans.create_plan(project_id=project.id, actor_id=owner.id)
    services.plans.propose_revision(
        plan_id=plan.id,
        project_id=project.id,
        actor_id=owner.id,
        content=PlanRevisionContent(
            objective=objective,
            scope=("backend",),
            constraints=(),
            acceptance_criteria=("ok",),
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
        idempotency_key="delegation-approval",
    )
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        project_id=project.id,
        actor_id=owner.id,
        task_specs=[
            TaskSpec(
                key="parent",
                objective=objective,
                permitted_scope=("backend",),
                expected_evidence=("provider_response",),
            )
        ],
    )
    task = services.worker.list_tasks(execution.id, project_id=project.id, actor_id=owner.id)[0]
    return owner, project, execution, task


from zero.app.worker_service import TaskSpec


class _DelegatingFakeAdapter:
    """First parent call asks to delegate; child calls answer plainly."""

    provider_name = "fake"
    _model_source = None

    # Real-run fix: the runtime now always dispatches streaming requests
    # (gateway edge-timeout resilience), so test fakes must support the
    # streaming path. The base-class default wraps send_request — the
    # exact non-streaming behavior this fake already implements.
    send_request_stream = ProviderAdapter.send_request_stream

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def get_model(self, model_name):
        global _MODEL_PROVIDERS
        if _MODEL_PROVIDERS is None:
            settings = Settings.load_for_test()
            database = Database(settings)
            apply_migrations(database)
            _MODEL_PROVIDERS = build_services(settings, database).providers
        return _MODEL_PROVIDERS.get_model("fake", model_name)

    def send_request(self, request, *, cancel_event=None, **_kwargs):
        from zero.domain.providers import CanonicalResponse, TokenUsage, ToolCallResult

        self.calls.append({"system": request.system_message})
        is_child = "focused sub-agent" in (request.system_message or "")
        has_tool_results = any(m.role == "tool" for m in request.messages)
        if not is_child and not has_tool_results:
            return CanonicalResponse(
                content="I will delegate this.",
                tool_calls=(
                    ToolCallResult(
                        tool_name=DELEGATE_TOOL_NAME,
                        tool_call_id="call_del_1",
                        arguments=json.dumps({"objective": "compute 2+2"}),
                        result="",
                    ),
                ),
                finish_reason="tool_calls",
                usage=TokenUsage(input_tokens=5, output_tokens=3),
                provider_message_id=f"fake_msg_del_{len(self.calls)}",
            )
        content = "Child answer: 4" if is_child else "Parent final answer"
        return CanonicalResponse(
            content=content,
            tool_calls=(),
            finish_reason="stop",
            usage=TokenUsage(input_tokens=7, output_tokens=2),
            provider_message_id=f"fake_msg_del_{len(self.calls)}",
        )


_MODEL_PROVIDERS = None


class TestDelegationEndToEnd:
    def test_parent_receives_child_result_inline(self, services):
        owner, project, execution, task = _approved_task(services)
        adapter = _DelegatingFakeAdapter()
        services.providers.register_adapter(adapter)
        runtime = AgentRuntime(
            worker=services.worker,
            providers=services.providers,
            artifacts=services.artifacts,
            authorization=services.authorization,
            tools=services.tools,
            enable_delegation=True,
        )
        result = runtime.run_task(
            execution_id=execution.id,
            project_id=project.id,
            task_id=task.id,
            actor_id=owner.id,
            lease_owner="del-worker",
            provider="fake",
            model_name="fake-standard",
            source="system",
        )
        assert result.task.state == "completed"
        # The child context actually ran between the two parent calls.
        assert len(adapter.calls) >= 2
        assert any("focused sub-agent" in (c["system"] or "") for c in adapter.calls)
        assert result.response.content == "Parent final answer"

    def test_subagent_usage_tagged_not_whole_tree(self, services):
        owner, project, execution, task = _approved_task(services)
        adapter = _DelegatingFakeAdapter()
        services.providers.register_adapter(adapter)
        runtime = AgentRuntime(
            worker=services.worker,
            providers=services.providers,
            artifacts=services.artifacts,
            authorization=services.authorization,
            tools=services.tools,
            enable_delegation=True,
        )
        runtime.run_task(
            execution_id=execution.id,
            project_id=project.id,
            task_id=task.id,
            actor_id=owner.id,
            lease_owner="del-worker",
            provider="fake",
            model_name="fake-standard",
            source="system",
        )
        conn = services.database.connect()
        scopes = {
            row["is_whole_tree"]
            for row in conn.execute("SELECT DISTINCT is_whole_tree FROM usage_records")
        }
        assert scopes == {0, 1}  # child tagged separately; parent whole-tree


class TestDelegationLimits:
    def test_depth_limit_returns_error_payload(self, services):
        owner, project, execution, _task = _approved_task(services)
        runtime = AgentRuntime(
            worker=services.worker,
            providers=services.providers,
            artifacts=services.artifacts,
            authorization=services.authorization,
            enable_delegation=True,
        )
        with _at_max_depth():
            payload = runtime._execute_delegation(
                call_arguments=json.dumps({"objective": "go deeper"}),
                parent_allowed_tools=(),
                execution_id=execution.id,
                project_id=project.id,
                actor_id=owner.id,
                provider="fake",
                model_name="fake-standard",
            )
        assert payload["status"] == "error"
        assert "depth limit" in payload["error"]

    def test_invalid_arguments_return_error_payload(self, services):
        owner, project, execution, _task = _approved_task(services)
        runtime = AgentRuntime(
            worker=services.worker,
            providers=services.providers,
            artifacts=services.artifacts,
            authorization=services.authorization,
            enable_delegation=True,
        )
        payload = runtime._execute_delegation(
            call_arguments="{not json",
            parent_allowed_tools=(),
            execution_id=execution.id,
            project_id=project.id,
            actor_id=owner.id,
            provider="fake",
            model_name="fake-standard",
        )
        assert payload["status"] == "error"

    def test_tool_narrowing_is_intersection_only(self, services):
        owner, project, execution, _task = _approved_task(services)
        runtime = AgentRuntime(
            worker=services.worker,
            providers=services.providers,
            artifacts=services.artifacts,
            authorization=services.authorization,
            enable_delegation=True,
        )
        payload = runtime._execute_delegation(
            call_arguments=json.dumps(
                {"objective": "x", "tools": ["read_file", "secret_admin_tool"]}
            ),
            parent_allowed_tools=("read_file",),
            execution_id=execution.id,
            project_id=project.id,
            actor_id=owner.id,
            provider="fake",
            model_name="fake-standard",
        )
        # Only the intersection survives; the unknown tool is dropped.
        assert payload["tools_used"] == ["read_file"]

    def test_workspace_tools_excluded_by_default(self, services):
        owner, project, execution, _task = _approved_task(services)
        runtime = AgentRuntime(
            worker=services.worker,
            providers=services.providers,
            artifacts=services.artifacts,
            authorization=services.authorization,
            enable_delegation=True,
        )
        payload = runtime._execute_delegation(
            call_arguments=json.dumps({"objective": "x"}),
            parent_allowed_tools=("read_file", "write_file", "echo"),
            execution_id=execution.id,
            project_id=project.id,
            actor_id=owner.id,
            provider="fake",
            model_name="fake-standard",
        )
        assert payload["tools_used"] == ["echo"]


class _AtMaxDepth:
    """Context manager parking the depth var at the cap."""

    def __enter__(self):
        from zero.app.delegation import _delegate_depth

        self._token = _delegate_depth.set(MAX_DELEGATION_DEPTH)
        return self

    def __exit__(self, *exc_info):
        from zero.app.delegation import _delegate_depth

        _delegate_depth.reset(self._token)
        return False


def _at_max_depth():
    return _AtMaxDepth()
