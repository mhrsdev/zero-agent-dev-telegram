"""GAP 5 tests: execution stream hub, provider stream tap, runtime callback."""

from __future__ import annotations

import queue

import pytest

from zero.app.agent_runtime import AgentRuntime
from zero.app.services import build_services
from zero.app.stream_hub import ExecutionStreamHub
from zero.app.worker_service import TaskSpec
from zero.config import Settings
from zero.domain.plans import PlanRevisionContent
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations


class TestExecutionStreamHub:
    def test_publish_reaches_subscribers(self):
        hub = ExecutionStreamHub()
        q1 = hub.subscribe("exec_1")
        q2 = hub.subscribe("exec_1")
        delivered = hub.publish("exec_1", {"type": "text_delta", "text": "hi"})
        assert delivered == 2
        assert q1.get_nowait() == {"type": "text_delta", "text": "hi"}
        assert q2.get_nowait() == {"type": "text_delta", "text": "hi"}

    def test_publish_without_subscribers_is_a_drop(self):
        hub = ExecutionStreamHub()
        assert hub.publish("exec_none", {"type": "done"}) == 0

    def test_unsubscribe_stops_delivery(self):
        hub = ExecutionStreamHub()
        q = hub.subscribe("exec_1")
        hub.unsubscribe("exec_1", q)
        assert hub.subscriber_count("exec_1") == 0
        assert hub.publish("exec_1", {"type": "done"}) == 0

    def test_unsubscribe_unknown_queue_is_tolerated(self):
        hub = ExecutionStreamHub()
        q: queue.SimpleQueue = queue.SimpleQueue()
        hub.unsubscribe("missing", q)  # must not raise
        hub.subscribe("exec_1")
        hub.unsubscribe("exec_1", q)

    def test_subscriber_cap_enforced(self):
        hub = ExecutionStreamHub(max_subscribers_per_execution=2)
        hub.subscribe("exec_1")
        hub.subscribe("exec_1")
        with pytest.raises(LookupError):
            hub.subscribe("exec_1")

    def test_cap_is_per_execution(self):
        hub = ExecutionStreamHub(max_subscribers_per_execution=1)
        hub.subscribe("exec_a")
        hub.subscribe("exec_b")
        assert hub.subscriber_count("exec_a") == 1
        assert hub.subscriber_count("exec_b") == 1

    def test_invalid_cap_rejected(self):
        with pytest.raises(ValueError):
            ExecutionStreamHub(max_subscribers_per_execution=0)


class TestProviderStreamTap:
    @staticmethod
    def _events():
        from zero.domain.providers import CanonicalStreamEvent, TokenUsage, ToolCallResult

        return [
            CanonicalStreamEvent(kind="text_delta", text="Hello "),
            CanonicalStreamEvent(kind="text_delta", text="world"),
            CanonicalStreamEvent(
                kind="tool_call_delta",
                tool_call=ToolCallResult(
                    tool_name="echo",
                    tool_call_id="call_1",
                    arguments='{"x": 1}',
                    result="",
                ),
            ),
            CanonicalStreamEvent(kind="usage", usage=TokenUsage(input_tokens=3, output_tokens=4)),
            CanonicalStreamEvent(kind="message_end", finish_reason="stop"),
        ]

    def test_observer_sees_client_safe_events_in_order(self):
        from zero.app.provider_service import ProviderService

        seen: list[dict] = []
        drained = list(
            ProviderService._tap_stream(self._events(), lambda payload: seen.append(payload))
        )
        assert [p["type"] for p in seen] == [
            "text_delta",
            "text_delta",
            "tool_call",
            "done",
        ]
        assert seen[0]["text"] == "Hello "
        assert seen[1] == {"type": "text_delta", "text": "world"}
        # Wave-11 contract: tool_call events carry a `replace` flag so
        # live views update the pending line per call instead of stacking
        # one garbled line per streaming fragment (live: "🔧 ?(and\")").
        assert seen[2] == {
            "type": "tool_call",
            "name": "echo",
            "arguments": {"x": 1},
            "replace": False,
        }
        assert seen[3]["finish_reason"] == "stop"
        # Usage stayed internal; all events still flow to the collector.
        assert len(drained) == 5
        assert drained[-1].kind == "message_end"

    def test_observer_failure_does_not_break_the_stream(self):
        from zero.app.provider_service import ProviderService

        def boom(_payload):
            raise RuntimeError("observer exploded")

        drained = list(ProviderService._tap_stream(self._events(), boom))
        assert len(drained) == 5

    def test_tool_arguments_fall_back_to_raw_text_when_not_json(self):
        from zero.app.provider_service import ProviderService
        from zero.domain.providers import CanonicalStreamEvent, ToolCallResult

        seen: list[dict] = []
        events = [
            CanonicalStreamEvent(
                kind="tool_call_delta",
                tool_call=ToolCallResult(
                    tool_name="t",
                    tool_call_id="c",
                    arguments="not-json{",
                    result="",
                ),
            ),
        ]
        list(ProviderService._tap_stream(iter(events), seen.append))
        assert seen[0]["arguments"] == "not-json{"


@pytest.fixture
def services(test_settings: Settings):
    database = Database(test_settings)
    apply_migrations(database)
    return build_services(test_settings, database)


def _approved_task(services, *, objective="Produce a provider response"):
    owner = services.identity.create_user(display_name="stream owner")
    project = services.identity.create_project(owner_id=owner.id, name="Streaming")
    event = services.plans.ingest_conversation_event(
        project_id=project.id,
        actor_id=owner.id,
        source="web",
        origin_kind="authenticated_human",
        content="Run it.",
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
            acceptance_criteria=("A provider response is durably recorded",),
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
        idempotency_key="stream-approval",
    )
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        project_id=project.id,
        actor_id=owner.id,
        task_specs=[
            TaskSpec(
                key="stream-task",
                objective=objective,
                permitted_scope=("backend",),
                expected_evidence=("provider_response",),
            )
        ],
    )
    task = services.worker.list_tasks(execution.id, project_id=project.id, actor_id=owner.id)[0]
    return owner, project, execution, task


class TestRuntimeStreamCallback:
    def test_stream_callback_receives_text_and_done(self, services):
        owner, project, execution, task = _approved_task(services)
        runtime = AgentRuntime(
            worker=services.worker,
            providers=services.providers,
            artifacts=services.artifacts,
            authorization=services.authorization,
        )
        received: list[tuple[str, dict]] = []
        result = runtime.run_task(
            execution_id=execution.id,
            project_id=project.id,
            task_id=task.id,
            actor_id=owner.id,
            lease_owner="stream-worker",
            provider="fake",
            model_name="fake-standard",
            source="system",
            stream_callback=lambda exec_id, payload: received.append((exec_id, payload)),
        )
        assert result.task.state == "completed"
        types = [payload["type"] for _exec, payload in received]
        assert "text_delta" in types
        assert types[-1] == "done"
        exec_ids = {exec_id for exec_id, _payload in received}
        assert exec_ids == {execution.id.value}
        text = "".join(p["text"] for _e, p in received if p["type"] == "text_delta")
        assert "Fake response to:" in text

    def test_no_callback_keeps_default_behavior(self, services):
        owner, project, execution, task = _approved_task(services)
        runtime = AgentRuntime(
            worker=services.worker,
            providers=services.providers,
            artifacts=services.artifacts,
            authorization=services.authorization,
        )
        result = runtime.run_task(
            execution_id=execution.id,
            project_id=project.id,
            task_id=task.id,
            actor_id=owner.id,
            lease_owner="plain-worker",
            provider="fake",
            model_name="fake-standard",
            source="system",
        )
        assert result.task.state == "completed"
