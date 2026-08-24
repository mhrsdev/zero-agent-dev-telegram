"""GAP 6 tests: interactive chat service, rate limiter, and admin endpoints."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from zero.app.chat_service import (
    ChatRateLimitError,
    ChatService,
    TokenBucketRateLimiter,
)
from zero.app.services import build_services
from zero.app.worker_service import TaskSpec
from zero.config import Settings
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations


@pytest.fixture
def services(test_settings: Settings):
    database = Database(test_settings)
    apply_migrations(database)
    return build_services(test_settings, database)


class TestTokenBucketRateLimiter:
    def test_burst_up_to_capacity_then_rejected(self):
        limiter = TokenBucketRateLimiter(3)
        assert limiter.allow("k", now=0.0)
        assert limiter.allow("k", now=0.0)
        assert limiter.allow("k", now=0.0)
        assert not limiter.allow("k", now=0.0)

    def test_tokens_refill_over_time(self):
        limiter = TokenBucketRateLimiter(2)  # 2/min → full refill in 60 s
        assert limiter.allow("k", now=0.0)
        assert limiter.allow("k", now=0.0)
        assert not limiter.allow("k", now=1.0)  # ~0.03 tokens refilled
        assert limiter.allow("k", now=61.0)  # fully refilled by then

    def test_keys_are_independent(self):
        limiter = TokenBucketRateLimiter(1)
        assert limiter.allow("a", now=0.0)
        assert limiter.allow("b", now=0.0)
        assert not limiter.allow("a", now=0.0)

    def test_invalid_rate_rejected(self):
        with pytest.raises(ValueError):
            TokenBucketRateLimiter(0)


def limitter_allows(limiter, key, now):
    return limiter.allow(key, now=now)


class TestChatService:
    def test_complete_returns_content_and_usage_without_executions(self, services):
        owner = services.identity.create_user(display_name="chat owner")
        project = services.identity.create_project(owner_id=owner.id, name="Chat")
        chat = ChatService(
            providers=services.providers,
            authorization=services.authorization,
            tools=None,
            rate_limiter=TokenBucketRateLimiter(100),
        )
        turn = chat.complete(
            project_id=project.id,
            actor_id=owner.id,
            message="hello there",
            provider="fake",
            model_name="fake-standard",
        )
        assert turn.content.startswith("Fake response to:")
        assert turn.tool_calls_executed == ()
        assert turn.usage is not None and turn.usage["output_tokens"] > 0
        assert turn.provider_request_id
        # No plan/execution rows were created.
        conn = services.database.connect()
        executions = conn.execute("SELECT COUNT(*) c FROM executions").fetchone()["c"]
        assert executions == 0
        tasks = conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"]
        assert tasks == 0

    def test_usage_is_recorded_normally(self, services):
        owner = services.identity.create_user(display_name="usage owner")
        project = services.identity.create_project(owner_id=owner.id, name="Usage")
        chat = ChatService(
            providers=services.providers,
            authorization=services.authorization,
            rate_limiter=TokenBucketRateLimiter(100),
        )
        before = (
            services.database.connect()
            .execute("SELECT COUNT(*) c FROM usage_records")
            .fetchone()["c"]
        )
        chat.complete(
            project_id=project.id,
            actor_id=owner.id,
            message="count me",
            provider="fake",
            model_name="fake-standard",
        )
        after = (
            services.database.connect()
            .execute("SELECT COUNT(*) c FROM usage_records")
            .fetchone()["c"]
        )
        assert after == before + 1

    def test_rate_limit_blocks_flood(self, services):
        owner = services.identity.create_user(display_name="flood owner")
        project = services.identity.create_project(owner_id=owner.id, name="Flood")
        chat = ChatService(
            providers=services.providers,
            authorization=services.authorization,
            rate_limiter=TokenBucketRateLimiter(2),
        )
        kwargs = {
            "project_id": project.id,
            "actor_id": owner.id,
            "provider": "fake",
            "model_name": "fake-standard",
        }
        chat.complete(message="one", **kwargs)
        chat.complete(message="two", **kwargs)
        with pytest.raises(ChatRateLimitError):
            chat.complete(message="three", **kwargs)

    def test_empty_message_rejected(self, services):
        owner = services.identity.create_user(display_name="empty owner")
        project = services.identity.create_project(owner_id=owner.id, name="Empty")
        chat = ChatService(
            providers=services.providers,
            authorization=services.authorization,
            rate_limiter=TokenBucketRateLimiter(10),
        )
        with pytest.raises(ValueError):
            chat.complete(
                project_id=project.id,
                actor_id=owner.id,
                message="   ",
                provider="fake",
                model_name="fake-standard",
            )

    def test_tool_round_cap_bounds_provider_rounds(self, services):
        """The fake adapter answers tool calls; cap must stop the loop."""
        owner = services.identity.create_user(display_name="tool owner")
        project = services.identity.create_project(owner_id=owner.id, name="Tools")
        chat = ChatService(
            providers=services.providers,
            authorization=services.authorization,
            tools=None,
            rate_limiter=TokenBucketRateLimiter(100),
        )
        turn = chat.complete(
            project_id=project.id,
            actor_id=owner.id,
            message="call tool",
            max_tool_rounds=0,
            provider="fake",
            model_name="fake-standard",
        )
        # Round cap 0 → final round executes nothing; the fake model's
        # tool-call intent surfaces as content, never as execution.
        assert turn.tool_calls_executed == ()
        assert "echo" in turn.content


class _AdminClient:
    """Admin GUI test harness: real login flow into a temp ZERO_HOME."""

    def __init__(self, services, settings, tmp_path, monkeypatch):
        zero_home = tmp_path / "zero-home"
        zero_home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("ZERO_HOME", str(zero_home))
        from zero.manage import web

        web._sessions.clear()
        self.app = FastAPI()
        self.app.state.services = services
        self.app.state.settings = settings
        from zero.app.stream_hub import ExecutionStreamHub

        self.app.state.stream_hub = ExecutionStreamHub()
        web.register_admin(self.app, services)
        self.client = TestClient(self.app)
        # Bootstrap login.
        code_page = self.client.get("/admin/login")
        assert code_page.status_code == 200
        setup_code_file = tmp_path / "zero-home" / "setup-code.txt"
        setup_code = setup_code_file.read_text(encoding="utf-8").strip()
        resp = self.client.post(
            "/admin/login/bootstrap", data={"secret": setup_code}, follow_redirects=False
        )
        assert resp.status_code == 200
        resp = self.client.post(
            "/admin/login/setpw",
            data={"pw": "correct horse battery", "pw2": "correct horse battery"},
            follow_redirects=False,
        )
        assert resp.status_code == 303


class TestAdminEndpoints:
    def test_chat_api_requires_session(
        self, services, test_settings: Settings, tmp_path, monkeypatch
    ):
        harness = _AdminClient(services, test_settings, tmp_path, monkeypatch)
        owner = services.identity.create_user(display_name="ep owner")
        project = services.identity.create_project(owner_id=owner.id, name="EP")
        # A client that never logged in has no session cookie.
        anonymous = TestClient(harness.app)
        resp = anonymous.post(
            f"/admin/chat/{project.id.value}",
            json={"message": "hi"},
            follow_redirects=False,
        )
        assert resp.status_code == 303  # redirect to login without session

    def test_chat_api_returns_json_response(
        self, services, test_settings: Settings, tmp_path, monkeypatch
    ):
        harness = _AdminClient(services, test_settings, tmp_path, monkeypatch)
        owner = services.identity.create_user(display_name="chat api owner")
        project = services.identity.create_project(owner_id=owner.id, name="ChatAPI")
        resp = harness.client.post(
            f"/admin/chat/{project.id.value}",
            json={"message": "hello admin"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["content"].startswith("Fake response to:")
        assert body["provider_request_id"]
        assert body["usage"]["output_tokens"] > 0

    def test_stream_endpoint_unknown_execution_404(
        self, services, test_settings: Settings, tmp_path, monkeypatch
    ):
        harness = _AdminClient(services, test_settings, tmp_path, monkeypatch)
        resp = harness.client.get("/admin/executions/exec_missing/stream")
        assert resp.status_code == 404

    def test_stream_endpoint_emits_sse_frames_for_live_events(
        self, services, test_settings: Settings, tmp_path, monkeypatch
    ):
        harness = _AdminClient(services, test_settings, tmp_path, monkeypatch)
        hub = harness.app.state.stream_hub
        owner = services.identity.create_user(display_name="sse owner")
        project = services.identity.create_project(owner_id=owner.id, name="SSE")
        event = services.plans.ingest_conversation_event(
            project_id=project.id,
            actor_id=owner.id,
            source="web",
            origin_kind="authenticated_human",
            content="go",
        )
        plan = services.plans.create_plan(project_id=project.id, actor_id=owner.id)
        from zero.domain.plans import PlanRevisionContent

        services.plans.propose_revision(
            plan_id=plan.id,
            project_id=project.id,
            actor_id=owner.id,
            content=PlanRevisionContent(
                objective="stream",
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
            idempotency_key="sse-approval",
        )
        execution = services.worker.create_execution_from_handoff(
            handoff_id=handoff.id,
            project_id=project.id,
            actor_id=owner.id,
            task_specs=[
                TaskSpec(
                    key="t",
                    objective="stream",
                    permitted_scope=("backend",),
                    expected_evidence=("provider_response",),
                )
            ],
        )

        # Publish events from "the runtime" while the SSE request reads.
        import threading

        def publish():
            for text in ("chunk-one ", "chunk-two"):
                hub.publish(execution.id.value, {"type": "text_delta", "text": text})
            hub.publish(execution.id.value, {"type": "done", "finish_reason": "stop"})

        timer = threading.Timer(0.05, publish)
        timer.start()
        with harness.client.stream(
            "GET", f"/admin/executions/{execution.id.value}/stream"
        ) as response:
            assert response.headers["content-type"].startswith("text/event-stream")
            body = b"".join(response.iter_raw()).decode("utf-8")
        timer.join()
        assert ": connected" in body
        assert '{"type": "text_delta", "text": "chunk-one "}' in body
        assert '"finish_reason": "stop"' in body
