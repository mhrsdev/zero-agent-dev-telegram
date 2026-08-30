"""Hermes-parity audit regressions (deep-read audit, 2026-08-28).

Each test pins one defect found by cross-referencing the audited Hermes
agent reference (nousresearch/hermes-agent) against this codebase:

1. Model-level fallback routing — the wizard's ``routing.fallback_models``
   promise was never wired into the runtime; a primary-model outage
   failed tasks even though alternative models were configured.
2. Auth failures escalate to the fallback chain — the OpenAI-compatible
   adapter raised a generic "HTTP 401" error that classified as
   ``invalid_request`` and skipped fallback; Hermes escalates auth
   failures to the chain (fail fast, then failover).
3. Empty-response ladder — a no-content/no-tool-call response completed
   the task silently with an empty deliverable; Hermes nudges the model
   with a bounded budget before accepting the empty terminal.
4. Identical-failure steering rides ON the tool result — a bare ``user``
   message between one batch's tool results breaks tool-call/result
   pairing on strict wire formats (covered further in
   test_agent_runtime.py; here we pin the exact suffix shape).
5. Handler-failure detail preservation — ``Tool X handler failed`` hid
   the underlying reason (which path was unreadable, which binary the
   policy refused); Hermes surfaces a bounded, sanitized reason.
6. Chat tool loop — unparseable arguments were silently coerced to
   ``{}`` and EXECUTED; Hermes never executes guessed arguments.
7. Delegation tool errors carried only the exception class name — the
   sub-agent could not self-correct without the reason.
"""

from __future__ import annotations

import contextlib
import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import ClassVar

import pytest

from zero.config import Settings
from tests.conftest import loopback_http_works
from zero.domain.providers import CanonicalMessage, CanonicalRequest, CanonicalResponse

# ----------------------------------------------------------------------
# Programmable loopback upstream (same shape as test_audit_real_http)
# ----------------------------------------------------------------------


class _Upstream(BaseHTTPRequestHandler):
    plan: ClassVar[dict] = {}

    def log_message(self, *args):
        pass

    def _dispatch(self, method: str):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        handler = None
        for key, fn in list(_Upstream.plan.items()):
            if key in self.path:
                handler = fn
                break
        if handler is None:
            self._send(404, {"error": "no route"})
            return
        result = handler(method, raw, dict(self.headers))
        if len(result) == 4:
            status, payload, ctype, extra = result
        else:
            status, payload, ctype = result
            extra = {}
        if isinstance(payload, (dict, list)):
            body = json.dumps(payload).encode()
            ctype = ctype or "application/json"
        else:
            body = payload if isinstance(payload, bytes) else str(payload).encode()
        self._send(status, body, ctype or "application/json", extra or {})

    def _send(self, status, payload, ctype="application/json", extra=None):
        if isinstance(payload, (dict, list)):
            body = json.dumps(payload).encode()
        else:
            body = payload if isinstance(payload, bytes) else str(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")


@pytest.fixture(scope="module")
def upstream():
    if not loopback_http_works():
        pytest.skip("loopback HTTP round-trips do not complete in this environment")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def _named_openai_adapter(provider_name: str, base_url: str):
    from zero.app.provider_adapter import OpenAICompatibleProviderAdapter

    class _Named(OpenAICompatibleProviderAdapter):
        @property
        def provider_name(self):
            return provider_name

    return _Named(api_key="sk-test", base_url=f"{base_url}/{provider_name}")


def _provider_service(tmp_path, *, upstream_url: str, provider: str, max_attempts: int = 1):
    from zero.app.artifact_service import ArtifactService
    from zero.app.authorization_service import AuthorizationService
    from zero.app.identity_service import IdentityService
    from zero.app.provider_service import ProviderService
    from zero.persistence.connection import Database
    from zero.persistence.migrations import apply_migrations
    from zero.persistence.repositories.artifact_repository import ArtifactRepository
    from zero.persistence.repositories.audit_repository import AuditRepository
    from zero.persistence.repositories.identity_repository import IdentityRepository
    from zero.persistence.repositories.provider_repository import ProviderRepository

    settings = Settings.load_for_test(
        database_url=f"sqlite:///{tmp_path}/engine.db",
        provider_max_attempts=max_attempts,
    )
    database = Database(settings)
    apply_migrations(database)
    identity_repo = IdentityRepository(database)
    audit_repo = AuditRepository(database)
    authz = AuthorizationService(identity_repo, audit_repo)
    identity = IdentityService(identity_repo, audit_repo, authz)
    artifacts = ArtifactService(
        ArtifactRepository(database), None, audit_repo, authz
    )
    svc = ProviderService(
        ProviderRepository(database),
        artifacts,
        audit_repo,
        authz,
        include_fake=False,
        provider_max_attempts=max_attempts,
    )
    svc.register_adapter(_named_openai_adapter(provider, upstream_url))
    owner = identity.create_user(display_name="parity owner")
    project = identity.create_project(owner_id=owner.id, name="parity")
    return svc, owner, project


_CHAT_OK = {
    "id": "c1",
    "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "final"}}],
    "usage": {"prompt_tokens": 11, "completion_tokens": 3},
}


def _chat_request(provider: str, model: str) -> CanonicalRequest:
    return CanonicalRequest(
        provider=provider,
        model_name=model,
        messages=(CanonicalMessage(role="user", content="hi"),),
        max_tokens=16,
    )


def _close_adapters(svc) -> None:
    """Close adapter httpx clients so no keep-alive socket outlives a test."""
    for adapter in getattr(svc, "_adapters", {}).values():
        with contextlib.suppress(Exception):
            adapter.close()


# ----------------------------------------------------------------------
# FIX 1+2 — fallback routing: auth escalation + model-level chain
# ----------------------------------------------------------------------


class TestFallbackRouting:
    def test_primary_model_outage_routes_to_fallback_model(self, upstream, tmp_path):
        """Primary model 500s; the same provider's fallback model serves."""
        _Upstream.plan.clear()
        hits: dict[str, int] = {"m-primary": 0, "m-fallback": 0}

        def chat(method, body, headers):
            payload = json.loads(body or b"{}")
            model = payload.get("model")
            hits[model] = hits.get(model, 0) + 1
            if model == "m-primary":
                return 500, {"error": "primary is down"}, None
            return 200, _CHAT_OK, None

        _Upstream.plan["/p1/chat/completions"] = chat
        _Upstream.plan["/p1/models"] = lambda m, b, h: (
            200,
            {"data": [{"id": "m-primary"}, {"id": "m-fallback"}]},
            None,
        )
        svc, owner, project = _provider_service(
            tmp_path, upstream_url=upstream, provider="p1", max_attempts=1
        )
        svc.set_fallback_models(("m-fallback",))

        _preq, resp = svc.send_request_with_fallback(
            project_id=project.id,
            actor_id=owner.id,
            request=_chat_request("p1", "m-primary"),
            source="system",
        )
        assert resp.content == "final"
        assert hits == {"m-primary": 1, "m-fallback": 1}
        conn = svc._repo.database.connect()
        rows = conn.execute(
            "SELECT model_name, state FROM provider_requests"
        ).fetchall()
        by_model = {(r["model_name"], r["state"]) for r in rows}
        assert ("m-primary", "failed") in by_model
        assert ("m-fallback", "completed") in by_model
        _close_adapters(svc)

    def test_fallback_models_skipped_when_primary_succeeds(self, upstream, tmp_path):
        _Upstream.plan.clear()
        hits = {"n": 0}

        def chat(method, body, headers):
            hits["n"] += 1
            return 200, _CHAT_OK, None

        _Upstream.plan["/p1/chat/completions"] = chat
        _Upstream.plan["/p1/models"] = lambda m, b, h: (200, {"data": [{"id": "m-primary"}]}, None)
        svc, owner, project = _provider_service(
            tmp_path, upstream_url=upstream, provider="p1", max_attempts=1
        )
        svc.set_fallback_models(("m-fallback",))

        _preq, resp = svc.send_request_with_fallback(
            project_id=project.id,
            actor_id=owner.id,
            request=_chat_request("p1", "m-primary"),
            source="system",
        )
        assert resp.content == "final"
        assert hits["n"] == 1
        _close_adapters(svc)

    def test_auth_failure_escalates_to_fallback_chain(self, upstream, tmp_path):
        """Hermes parity: 401 on the primary must reach a healthy fallback."""
        _Upstream.plan.clear()
        calls = {"primary": 0, "secondary": 0}
        _Upstream.plan["/primary/chat/completions"] = lambda m, b, h: (
            calls.__setitem__("primary", calls["primary"] + 1) or (401, {"error": "bad key"}, None)
        )
        _Upstream.plan["/secondary/chat/completions"] = lambda m, b, h: (
            calls.__setitem__("secondary", calls["secondary"] + 1),
            200,
            _CHAT_OK,
            None,
        )[-3:]
        _Upstream.plan["/primary/models"] = lambda m, b, h: (
            200,
            {"data": [{"id": "m"}]},
            None,
        )
        _Upstream.plan["/secondary/models"] = lambda m, b, h: (
            200,
            {"data": [{"id": "m"}]},
            None,
        )
        svc, owner, project = _provider_service(
            tmp_path, upstream_url=upstream, provider="primary", max_attempts=1
        )
        svc.register_adapter(_named_openai_adapter("secondary", upstream))
        svc.set_fallback_chain(("primary", "secondary"))

        _preq, resp = svc.send_request_with_fallback(
            project_id=project.id,
            actor_id=owner.id,
            request=_chat_request("primary", "m"),
            source="system",
        )
        assert resp.content == "final"
        assert calls == {"primary": 1, "secondary": 1}
        _close_adapters(svc)

    def test_openai_adapter_401_classifies_as_auth_failure(self, upstream, tmp_path):
        """401/403 carry an auth-flavored message (classification input)."""
        from zero.app.provider_adapter import ProviderError

        _Upstream.plan.clear()
        _Upstream.plan["/p1/chat/completions"] = lambda m, b, h: (401, {"error": "x"}, None)
        _Upstream.plan["/p1/models"] = lambda m, b, h: (200, {"data": [{"id": "m"}]}, None)
        svc, owner, project = _provider_service(
            tmp_path, upstream_url=upstream, provider="p1", max_attempts=1
        )
        with pytest.raises(ProviderError) as excinfo:
            svc.send_request(
                project_id=project.id,
                actor_id=owner.id,
                request=_chat_request("p1", "m"),
                source="system",
            )
        assert "auth" in str(excinfo.value)
        assert svc._classify_error(excinfo.value) == "auth_failure"
        _close_adapters(svc)

    def test_fallback_eligible_classes_include_auth(self):
        from zero.app.provider_service import ProviderService

        assert "auth_failure" in ProviderService.FALLBACK_ELIGIBLE_CLASSES


def test_settings_parse_fallback_models(monkeypatch: pytest.MonkeyPatch):
    """ZERO_OPENAI_FALLBACK_MODELS parses, dedups, and drops the primary."""
    monkeypatch.setenv("ZERO_ENV", "development")
    monkeypatch.delenv("ZERO_DATABASE_URL", raising=False)
    monkeypatch.setenv("ZERO_OPENAI_API_KEY", "synthetic-provider-key")
    monkeypatch.setenv("ZERO_OPENAI_MODEL", "m-primary")
    monkeypatch.setenv(
        "ZERO_OPENAI_FALLBACK_MODELS", " m-fallback , m-primary , m-fallback ,"
    )
    settings = Settings.load()
    assert settings.openai_fallback_models == ("m-fallback",)


def test_settings_fallback_models_default_empty(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ZERO_ENV", "development")
    monkeypatch.delenv("ZERO_DATABASE_URL", raising=False)
    monkeypatch.delenv("ZERO_OPENAI_FALLBACK_MODELS", raising=False)
    settings = Settings.load()
    assert settings.openai_fallback_models == ()


# ----------------------------------------------------------------------
# FIX 3 — empty-response ladder in the task tool loop
# ----------------------------------------------------------------------


@dataclass
class _FakeRequest:
    messages: tuple = ()
    provider: str = "p"
    model_name: str = "m"
    max_tokens: int = 1024
    tools: tuple = ()
    stream: bool = False


def _empty_harness(responses: list[CanonicalResponse]):
    """Drive _run_tool_rounds with a scripted provider."""
    from zero.app.agent_runtime import AgentRuntime

    state = {"calls": []}
    provider_request = SimpleNamespace(id=SimpleNamespace(value="preq_1"))

    def send(**kwargs):
        state["calls"].append(kwargs["request"])
        return provider_request, responses.pop(0)

    def renew(**kwargs):
        return None

    runtime = AgentRuntime.__new__(AgentRuntime)
    object.__setattr__(runtime, "_worker", SimpleNamespace(renew_task_lease=renew))
    object.__setattr__(runtime, "_providers", SimpleNamespace(send_request_with_fallback=send))
    object.__setattr__(runtime, "_tools", SimpleNamespace())
    object.__setattr__(runtime, "_enable_delegation", False)
    object.__setattr__(runtime, "_approval_gate", None)
    object.__setattr__(runtime, "_metrics", None)

    def run(first_response, *, max_tool_rounds=4):
        task = SimpleNamespace(id=SimpleNamespace(value="task_1"), project_id=SimpleNamespace(value="p_1"))
        attempt = SimpleNamespace(id=SimpleNamespace(value="att_1"))
        return runtime._run_tool_rounds(
            task=task,
            attempt=attempt,
            actor_id=SimpleNamespace(value="zu_1"),
            execution_id=SimpleNamespace(value="exec_1"),
            project_id=SimpleNamespace(value="p_1"),
            request=_FakeRequest(),
            response=first_response,
            provider_request_id=provider_request.id,
            agent_scope="main_worker",
            tool_names=("echo",),
            max_tool_rounds=max_tool_rounds,
            cancel_event=None,
            lease_owner="t",
            lease_duration_seconds=300,
            source="system",
        )

    return run, state


class TestEmptyResponseLadder:
    def test_empty_response_is_nudged_and_recovered(self):
        """Empty response → bounded nudge → model produces the answer."""
        run, state = _empty_harness(
            [
                CanonicalResponse(content="recovered answer", finish_reason="stop"),
            ]
        )
        first = CanonicalResponse(content="", finish_reason="stop")
        response, _request_id, messages = run(first)
        assert response.content == "recovered answer"
        # assistant "(empty)" marker + user nudge were injected
        roles = [(m.role, m.content) for m in messages]
        assert ("assistant", "(empty)") in roles
        assert any(
            m.role == "user" and "empty" in (m.content or "").lower() for m in messages
        )
        assert len(state["calls"]) == 1, "exactly one nudge re-request"

    def test_persistent_empty_response_terminates_after_budget(self):
        run, state = _empty_harness(
            [
                CanonicalResponse(content="", finish_reason="stop"),
                CanonicalResponse(content="", finish_reason="stop"),
            ]
        )
        first = CanonicalResponse(content="   ", finish_reason="stop")
        response, _request_id, _messages = run(first)
        # Bounded ladder: initial + 2 nudges, then the empty terminal stands.
        assert (response.content or "").strip() == ""
        assert len(state["calls"]) == 2

    def test_nonempty_response_returns_without_nudge(self):
        run, state = _empty_harness([])
        first = CanonicalResponse(content="the answer", finish_reason="stop")
        response, _request_id, messages = run(first)
        assert response.content == "the answer"
        assert state["calls"] == []
        assert [(m.role, m.content) for m in messages] == [("user", None)] or all(
            m.role != "assistant" for m in messages
        )


# ----------------------------------------------------------------------
# FIX 4 — identical-failure warn suffix shape (pairing-safe)
# ----------------------------------------------------------------------


class TestWarnSuffixShape:
    def test_warn_is_a_tool_result_suffix_not_a_user_message(self):
        """The steering text lives inside a role='tool' message."""
        from zero.app.agent_runtime import AgentRuntime

        state = {"calls": []}
        provider_request = SimpleNamespace(id=SimpleNamespace(value="preq_1"))
        failing = SimpleNamespace(
            content="", tool_calls=(SimpleNamespace(tool_name="echo", tool_call_id="c1", arguments="{}"),),
            finish_reason="tool_calls",
        )
        responses = [failing] * 4 + [
            CanonicalResponse(content="steered summary", finish_reason="stop")
        ]

        def send(**kwargs):
            state["calls"].append(kwargs["request"])
            return provider_request, responses.pop(0)

        def renew(**kwargs):
            return None

        tool_result = SimpleNamespace(
            model_facing='{"status":"error"}', status="failure", error="boom"
        )

        runtime = AgentRuntime.__new__(AgentRuntime)
        object.__setattr__(runtime, "_worker", SimpleNamespace(renew_task_lease=renew))
        object.__setattr__(runtime, "_providers", SimpleNamespace(send_request_with_fallback=send))
        object.__setattr__(
            runtime,
            "_tools",
            SimpleNamespace(invoke=lambda **kwargs: tool_result),
        )
        object.__setattr__(runtime, "_enable_delegation", False)
        object.__setattr__(runtime, "_approval_gate", None)
        object.__setattr__(runtime, "_metrics", None)

        task = SimpleNamespace(id=SimpleNamespace(value="task_1"), project_id=SimpleNamespace(value="p_1"))
        attempt = SimpleNamespace(id=SimpleNamespace(value="att_1"))
        _response, _req_id, messages = runtime._run_tool_rounds(
            task=task,
            attempt=attempt,
            actor_id=SimpleNamespace(value="zu_1"),
            execution_id=SimpleNamespace(value="exec_1"),
            project_id=SimpleNamespace(value="p_1"),
            request=_FakeRequest(),
            response=failing,
            provider_request_id=provider_request.id,
            agent_scope="main_worker",
            tool_names=("echo",),
            max_tool_rounds=6,
            cancel_event=None,
            lease_owner="t",
            lease_duration_seconds=300,
            source="system",
        )
        warn_rows = [m for m in messages if "identical failure" in (m.content or "")]
        assert warn_rows, "the warn suffix must be present at count=3"
        for row in warn_rows:
            assert row.role == "tool"
            assert row.tool_call_id == "c1"
        # The loop ended through the breaker + toolless summary path; the
        # only user-role row is the final nudge request, never steering
        # injected between tool results of one batch.
        user_rows = [m for m in messages if m.role == "user"]
        assert all(
            "identical failure" not in (m.content or "") for m in user_rows
        )
        assert _response.content == "steered summary"


# ----------------------------------------------------------------------
# FIX 5 — handler-failure detail preservation in ToolService
# ----------------------------------------------------------------------


class TestHandlerFailureDetail:
    def _service(self, tmp_path, *, handler):
        from zero.app.authorization_service import AuthorizationService
        from zero.app.identity_service import IdentityService
        from zero.app.tool_service import ToolService
        from zero.persistence.connection import Database
        from zero.persistence.migrations import apply_migrations
        from zero.persistence.repositories.audit_repository import AuditRepository
        from zero.persistence.repositories.identity_repository import IdentityRepository
        from zero.persistence.repositories.tool_repository import ToolRepository

        settings = Settings.load_for_test(database_url=f"sqlite:///{tmp_path}/engine.db")
        database = Database(settings)
        apply_migrations(database)
        identity_repo = IdentityRepository(database)
        audit_repo = AuditRepository(database)
        authz = AuthorizationService(identity_repo, audit_repo)
        identity = IdentityService(identity_repo, audit_repo, authz)
        owner = identity.create_user(display_name="tool owner")
        project = identity.create_project(owner_id=owner.id, name="tools")
        service = ToolService(ToolRepository(database), audit_repo, authz)
        tool = service.register_tool(
            name="exploder",
            description="always fails",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            output_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler_key="exploder",
            handler=handler,
            inline=True,
        )
        service.grant_tool(
            project_id=project.id,
            actor_id=owner.id,
            tool_id=tool.id,
            agent_scope="main_worker",
        )
        return service, owner, project

    def test_generic_handler_failure_carries_reason(self, tmp_path):
        from zero.app.tool_service import ToolError

        def handler(_input, _context):
            raise RuntimeError("task file does not exist: src/app.py")

        service, owner, project = self._service(tmp_path, handler=handler)
        with pytest.raises(ToolError) as excinfo:
            service.invoke(
                project_id=project.id,
                actor_id=owner.id,
                agent_scope="main_worker",
                tool_name="exploder",
                input_data={},
                source="system",
            )
        message = str(excinfo.value)
        assert "Error executing tool 'exploder'" in message
        assert "task file does not exist: src/app.py" in message

    def test_handler_failure_detail_is_redacted_and_bounded(self, tmp_path):
        from zero.app.tool_service import ToolError

        secret = "api_key=sk-supersecret-value-1234567890"

        def handler(_input, _context):
            raise RuntimeError(f"failed connecting with {secret} to host")

        service, owner, project = self._service(tmp_path, handler=handler)
        with pytest.raises(ToolError) as excinfo:
            service.invoke(
                project_id=project.id,
                actor_id=owner.id,
                agent_scope="main_worker",
                tool_name="exploder",
                input_data={},
                source="system",
            )
        message = str(excinfo.value)
        assert "sk-supersecret-value-1234567890" not in message


# ----------------------------------------------------------------------
# FIX 6 — chat tool loop never executes guessed arguments
# ----------------------------------------------------------------------


class TestChatArgumentSafety:
    def _chat_service(self, tmp_path):
        from zero.app.authorization_service import AuthorizationService
        from zero.app.chat_service import ChatService, TokenBucketRateLimiter
        from zero.app.identity_service import IdentityService
        from zero.app.tool_service import ToolService
        from zero.persistence.connection import Database
        from zero.persistence.migrations import apply_migrations
        from zero.persistence.repositories.audit_repository import AuditRepository
        from zero.persistence.repositories.identity_repository import IdentityRepository
        from zero.persistence.repositories.tool_repository import ToolRepository

        settings = Settings.load_for_test(database_url=f"sqlite:///{tmp_path}/engine.db")
        database = Database(settings)
        apply_migrations(database)
        identity_repo = IdentityRepository(database)
        audit_repo = AuditRepository(database)
        authz = AuthorizationService(identity_repo, audit_repo)
        identity = IdentityService(identity_repo, audit_repo, authz)
        owner = identity.create_user(display_name="chat owner")
        project = identity.create_project(owner_id=owner.id, name="chat")
        tools = ToolService(ToolRepository(database), audit_repo, authz)
        chat = ChatService(
            providers=None,
            authorization=authz,
            tools=tools,
            rate_limiter=TokenBucketRateLimiter(100),
        )
        return chat, tools, owner, project

    def test_invalid_json_arguments_are_not_executed(self, tmp_path):
        """Malformed args produce a structured error, never a tool run."""
        chat, _tools, owner, project = self._chat_service(tmp_path)
        invoked = []
        object.__setattr__(
            chat,
            "_tools",
            SimpleNamespace(invoke=lambda **kw: invoked.append(kw)),
        )
        payload = chat._invoke_tool(
            project_id=project.id,
            actor_id=owner.id,
            agent_scope="main_worker",
            tool_name="echo",
            arguments_text='{"message": "unterminated',
            source="test",
        )
        assert invoked == [], "guessed arguments must never reach the handler"
        result = json.loads(payload["result"])
        assert result["error"] == "invalid_tool_arguments"
        assert "hint" in result

    def test_non_dict_arguments_are_not_executed(self, tmp_path):
        chat, _tools, owner, project = self._chat_service(tmp_path)
        invoked = []
        object.__setattr__(
            chat,
            "_tools",
            SimpleNamespace(invoke=lambda **kw: invoked.append(kw)),
        )
        payload = chat._invoke_tool(
            project_id=project.id,
            actor_id=owner.id,
            agent_scope="main_worker",
            tool_name="echo",
            arguments_text='"just a string"',
            source="test",
        )
        assert invoked == []
        assert json.loads(payload["result"])["error"] == "invalid_tool_arguments"


# ----------------------------------------------------------------------
# FIX 7 — delegation tool errors carry the reason
# ----------------------------------------------------------------------


class TestDelegationErrorDetail:
    def test_sub_agent_tool_error_includes_reason(self):
        from zero.app.agent_runtime import AgentRuntime
        from zero.domain.tools import ToolError

        def send(**_kwargs):
            return SimpleNamespace(id=SimpleNamespace(value="preq_d")), CanonicalResponse(
                content="done", finish_reason="stop"
            )

        def invoke(**_kwargs):
            raise ToolError("command 'bash' is not allowlisted; allowed: python3, git")

        runtime = AgentRuntime.__new__(AgentRuntime)
        object.__setattr__(runtime, "_providers", SimpleNamespace(send_request_with_fallback=send))
        object.__setattr__(runtime, "_tools", SimpleNamespace(invoke=invoke))
        object.__setattr__(runtime, "_enable_delegation", True)

        payload = runtime._execute_delegation(
            call_arguments=json.dumps(
                {"objective": "inspect", "tools": ["read_file"]}
            ),
            parent_allowed_tools=("read_file",),
            execution_id=SimpleNamespace(value="exec_1"),
            project_id=SimpleNamespace(value="p_1"),
            actor_id=SimpleNamespace(value="zu_1"),
            provider="p",
            model_name="m",
        )
        assert payload["status"] == "completed"
        # The child's tool loop fed the real reason back to the model; the
        # final response is the scripted one, so assert via the captured
        # messages path instead: re-run with a capturing provider.
        captured: list[CanonicalRequest] = []
        scripted = [
            CanonicalResponse(
                content="",
                tool_calls=(
                    SimpleNamespace(
                        tool_name="run_command", tool_call_id="sub_1", arguments="{}"
                    ),
                ),
                finish_reason="tool_calls",
            ),
            CanonicalResponse(content="done", finish_reason="stop"),
        ]

        def capturing_send(**kwargs):
            captured.append(kwargs["request"])
            return SimpleNamespace(id=SimpleNamespace(value="preq_d")), scripted.pop(0)

        object.__setattr__(runtime, "_providers", SimpleNamespace(send_request_with_fallback=capturing_send))

        def invoke_then_raise(**_kwargs):
            raise ToolError("command 'bash' is not allowlisted; allowed: python3, git")

        object.__setattr__(runtime, "_tools", SimpleNamespace(invoke=invoke_then_raise))
        runtime._execute_delegation(
            call_arguments=json.dumps({"objective": "run things", "tools": ["run_command"]}),
            parent_allowed_tools=("run_command",),
            execution_id=SimpleNamespace(value="exec_1"),
            project_id=SimpleNamespace(value="p_1"),
            actor_id=SimpleNamespace(value="zu_1"),
            provider="p",
            model_name="m",
        )
        tool_rows = [
            m for m in captured[-1].messages if m.role == "tool" and m.content
        ]
        assert tool_rows, "sub-agent must receive tool results"
        assert any("allowlisted" in (m.content or "") for m in tool_rows), (
            "the sub-agent's tool error must carry the policy reason, not a bare class name"
        )
