"""Hermes-parity wave 14 regressions (deep re-read, 2026-08-31).

Pins the defects found by a SECOND full pass over the Hermes agent
reference (nousresearch/hermes-agent), focused on the operator's three
follow-up asks — approval auto-mode tuning, a real second-provider
failover, and a bigger multi-team decomposition drill:

Fix 13 — provider instance naming + real multi-provider failover:
  the adapter registry keyed every OpenAI-compatible gateway under ONE
  protocol name, so the second ``providers:`` entry was silently dropped
  ("already registered — skipped") and no second provider could ever be
  wired. ``fallback_priority``, ``routing.max_attempts_per_provider`` and
  ``routing.breaker`` were parsed-and-ignored config fiction. Hermes
  parity: chain order is operator policy; rate-limited/auth-dead
  gateways get time-boxed cooldowns (``_rate_limited_until`` /
  ``_unavailable_fallback_keys``); success clears the state.

Fix 14 — approval posture is runtime-tunable:
  Hermes re-reads ``approvals.mode`` on every check; Zero froze the mode
  at boot from ``ZERO_TOOL_APPROVAL_MODE``. The gate now exists in every
  mode and config.yaml ``approvals.{mode,pending_ttl_seconds}`` retunes
  it on every config sync.

Fix 15 — Telegram approval card session grain:
  the card shipped once/always/deny only; the gate's ``session`` grain
  was unreachable from chat. Hermes ships once/session/always/deny.

Fix 16 — scheduler knobs become config surfaces:
  decomposition, task retry budget and tick parallelism were env-only.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

from zero.config import Settings
from zero.domain.providers import CanonicalMessage, CanonicalRequest

PROJECT = "p_wave14"


# ----------------------------------------------------------------------
# Loopback upstream (same harness shape as test_hermes_parity_audit)
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
        status, payload, ctype = handler(method, raw, dict(self.headers))
        self._send(status, payload, ctype or "application/json")

    def _send(self, status, payload, ctype="application/json"):
        body = (
            json.dumps(payload).encode()
            if isinstance(payload, (dict, list))
            else (payload if isinstance(payload, bytes) else str(payload).encode())
        )
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")


@pytest.fixture(scope="module")
def upstream():
    import tests.conftest as _c

    if _c.loopback_http_works():
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{server.server_address[1]}"
        server.shutdown()
        server.server_close()
    else:
        pytest.skip("loopback HTTP round-trips do not complete in this environment")


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


def _provider_service(tmp_path, *, max_attempts: int = 1):
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
    artifacts = ArtifactService(ArtifactRepository(database), None, audit_repo, authz)
    svc = ProviderService(
        ProviderRepository(database),
        artifacts,
        audit_repo,
        authz,
        include_fake=False,
        provider_max_attempts=max_attempts,
    )
    owner = identity.create_user(display_name="wave14 owner")
    project = identity.create_project(owner_id=owner.id, name="wave14")
    return svc, owner, project


def _named_adapter(name: str, base_url: str):
    from zero.app.provider_adapter import OpenAICompatibleProviderAdapter

    return OpenAICompatibleProviderAdapter(
        api_key="sk-test", base_url=base_url, name=name
    )


def _close_adapters(svc) -> None:
    for adapter in getattr(svc, "_adapters", {}).values():
        with contextlib.suppress(Exception):
            adapter.close()


# ----------------------------------------------------------------------
# Fix 13 — multi-instance providers, operator-ordered chain, breaker
# ----------------------------------------------------------------------


class TestProviderInstances:
    def test_two_same_protocol_adapters_register_under_own_names(self):
        """The old registry collapsed same-protocol adapters under one
        name — the second config entry was silently skipped forever."""
        from zero.app.provider_adapter import OpenAICompatibleProviderAdapter

        a = OpenAICompatibleProviderAdapter(
            api_key="sk-a", base_url="https://a.example/v1", name="justwoker"
        )
        b = OpenAICompatibleProviderAdapter(
            api_key="sk-b", base_url="https://b.example/v1", name="backup-gw"
        )
        assert a.provider_name == "justwoker"
        assert b.provider_name == "backup-gw"
        # Default name keeps the historical env-path behavior.
        c = OpenAICompatibleProviderAdapter(api_key="sk-c", base_url="https://c.example/v1")
        assert c.provider_name == "openai-compatible"
        for adapter in (a, b, c):
            adapter.close()

    def test_registration_order_preserved_not_alphabetical(self):
        svc, _owner, _project = _provider_service(Path("/tmp/w14-order"))
        try:
            svc.register_adapter(_named_adapter("zeta", "https://z.example/v1"))
            svc.register_adapter(_named_adapter("alpha", "https://a.example/v1"))
            # sorted view unchanged (compat) but ORDER view = registration.
            assert svc.registered_provider_names == ("alpha", "zeta")
            assert svc.registered_provider_order == ("zeta", "alpha")
        finally:
            _close_adapters(svc)

    def test_second_provider_failover_over_real_http(self, upstream, tmp_path):
        """Primary instance 401s; a SECOND same-protocol instance serves.
        Before fix 13 this drill was impossible: the second adapter never
        registered, so the chain had exactly one entry."""
        _Upstream.plan.clear()
        calls = {"justwoker": 0, "backup-gw": 0}
        _Upstream.plan["/justwoker/chat/completions"] = lambda m, b, h: (
            calls.__setitem__("justwoker", calls["justwoker"] + 1)
            or (401, {"error": {"message": "bad key"}}, "application/json")
        )
        _Upstream.plan["/backup-gw/chat/completions"] = lambda m, b, h: (
            calls.__setitem__("backup-gw", calls["backup-gw"] + 1),
            (200, _CHAT_OK, "application/json"),
        )[-1]
        _Upstream.plan["/justwoker/models"] = lambda m, b, h: (
            200,
            {"data": [{"id": "m"}]},
            "application/json",
        )
        _Upstream.plan["/backup-gw/models"] = lambda m, b, h: (
            200,
            {"data": [{"id": "m"}]},
            "application/json",
        )
        svc, owner, project = _provider_service(tmp_path)
        try:
            svc.register_adapter(_named_adapter("justwoker", f"{upstream}/justwoker"))
            svc.register_adapter(_named_adapter("backup-gw", f"{upstream}/backup-gw"))
            svc.set_fallback_chain(("justwoker", "backup-gw"))

            _preq, resp = svc.send_request_with_fallback(
                project_id=project.id,
                actor_id=owner.id,
                request=_chat_request("justwoker", "m"),
                source="system",
            )
            assert resp.content == "final"
            assert calls == {"justwoker": 1, "backup-gw": 1}
        finally:
            _close_adapters(svc)

    def test_max_attempts_configured_and_clamped(self):
        svc, _owner, _project = _provider_service(Path("/tmp/w14-clamp"))
        try:
            assert svc.provider_max_attempts == 1
            svc.set_provider_max_attempts(99)
            assert svc.provider_max_attempts == 8
            svc.set_provider_max_attempts(0)
            assert svc.provider_max_attempts == 1
            svc.set_provider_max_attempts(3)
            assert svc.provider_max_attempts == 3
        finally:
            _close_adapters(svc)


class TestProviderBreaker:
    def test_auth_failure_arms_long_cooldown(self):
        svc, _owner, _project = _provider_service(Path("/tmp/w14-auth"))
        try:
            svc._arm_provider_cooldown("justwoker", "auth_failure")
            remaining = svc.provider_cooldown_remaining("justwoker")
            assert remaining >= 299.0
            # A healthy success clears the state.
            svc._clear_provider_cooldown("justwoker")
            assert svc.provider_cooldown_remaining("justwoker") == 0.0
        finally:
            _close_adapters(svc)

    def test_rate_limit_cooldown_escalates(self):
        svc, _owner, _project = _provider_service(Path("/tmp/w14-rl"))
        try:
            svc.set_breaker_policy(failure_threshold=5, cooldown_seconds=60)
            svc._arm_provider_cooldown("p", "rate_limit")
            first = svc.provider_cooldown_remaining("p")
            svc._clear_provider_cooldown("p")
            svc._arm_provider_cooldown("p", "rate_limit")
            svc._arm_provider_cooldown("p", "rate_limit")
            second = svc.provider_cooldown_remaining("p")
            assert 55.0 <= first <= 60.0
            assert second > first  # exponential escalation
        finally:
            _close_adapters(svc)

    def test_transient_breaker_opens_only_at_threshold(self):
        svc, _owner, _project = _provider_service(Path("/tmp/w14-thr"))
        try:
            svc.set_breaker_policy(failure_threshold=3, cooldown_seconds=10)
            svc._arm_provider_cooldown("p", "transient")
            svc._arm_provider_cooldown("p", "transient")
            assert svc.provider_cooldown_remaining("p") == 0.0  # below threshold
            svc._arm_provider_cooldown("p", "transient")
            assert svc.provider_cooldown_remaining("p") > 0.0  # breaker opened
        finally:
            _close_adapters(svc)

    def test_chain_skips_cooled_provider_but_fails_open(self):
        svc, _owner, _project = _provider_service(Path("/tmp/w14-chain"))
        try:
            chain = [("primary", "m"), ("cool-a", "m"), ("healthy", "m"), ("cool-b", "m")]
            # One cooled fallback among healthy ones → skipped.
            svc._arm_provider_cooldown("cool-a", "auth_failure")
            svc._arm_provider_cooldown("cool-b", "auth_failure")
            filtered = svc._available_chain_candidates(chain, primary="primary")
            assert filtered == [("primary", "m"), ("healthy", "m")]
            # ALL fallback candidates cooling → fail open (keep them).
            assert (
                svc._available_chain_candidates(
                    [("primary", "m"), ("cool-a", "m"), ("cool-b", "m")],
                    primary="primary",
                )
                == [("primary", "m"), ("cool-a", "m"), ("cool-b", "m")]
            )
            # Same-provider (model-level) entries are never filtered.
            assert svc._available_chain_candidates(
                [("primary", "m"), ("primary", "m2"), ("cool-a", "m")],
                primary="primary",
            ) == [("primary", "m"), ("primary", "m2")]
        finally:
            _close_adapters(svc)

    def test_success_clears_streak(self):
        svc, _owner, _project = _provider_service(Path("/tmp/w14-clear"))
        try:
            svc.set_breaker_policy(failure_threshold=1, cooldown_seconds=10)
            svc._arm_provider_cooldown("p", "transient")
            assert svc.provider_cooldown_remaining("p") > 0.0
            svc._clear_provider_cooldown("p")
            assert svc._provider_fail_streak.get("p") is None
        finally:
            _close_adapters(svc)


# ----------------------------------------------------------------------
# Fix 13 — config_sync registers instances + applies routing policy
# ----------------------------------------------------------------------


class TestConfigSyncProviders:
    def _services(self, tmp_path):
        from zero.app.artifact_service import ArtifactService
        from zero.app.authorization_service import AuthorizationService
        from zero.app.provider_service import ProviderService
        from zero.persistence.connection import Database
        from zero.persistence.migrations import apply_migrations
        from zero.persistence.repositories.artifact_repository import ArtifactRepository
        from zero.persistence.repositories.audit_repository import AuditRepository
        from zero.persistence.repositories.identity_repository import IdentityRepository
        from zero.persistence.repositories.provider_repository import ProviderRepository

        settings = Settings.load_for_test(
            database_url=f"sqlite:///{tmp_path}/sync.db"
        )
        database = Database(settings)
        apply_migrations(database)
        identity_repo = IdentityRepository(database)
        audit_repo = AuditRepository(database)
        authz = AuthorizationService(identity_repo, audit_repo)
        artifacts = ArtifactService(ArtifactRepository(database), None, audit_repo, authz)
        providers = ProviderService(
            ProviderRepository(database), artifacts, audit_repo, authz
        )
        secrets = SimpleNamespace(
            resolve_value=lambda **kwargs: "sk-sync-test-key"
        )
        return SimpleNamespace(secrets=secrets, providers=providers), providers

    def test_sync_registers_instances_and_orders_chain(self, tmp_path):
        from zero.app.config_sync import _sync_providers
        from zero.manage.core.config import ZeroConfig

        services, providers = self._services(tmp_path)
        settings = SimpleNamespace(openai_timeout_seconds=5, anthropic_timeout_seconds=5)
        cfg = ZeroConfig.model_validate(
            {
                "providers": [
                    {
                        "id": "justwoker",
                        "base_url": "https://justwoker.example/v1",
                        "api_key_ref": "sec_a",
                        "fallback_priority": 10,
                        "models": ["claude-opus-5"],
                    },
                    {
                        "id": "backup-gw",
                        "base_url": "https://backup.example/v1",
                        "api_key_ref": "sec_b",
                        "fallback_priority": 5,
                        "models": ["claude-opus-5"],
                    },
                ],
                "routing": {
                    "primary_model": "claude-opus-5",
                    "max_attempts_per_provider": 3,
                },
            }
        )
        project = SimpleNamespace(id="p_sync")
        _sync_providers(settings, services, project, "u_owner", cfg, None)

        registered = set(providers.registered_provider_names)
        assert {"justwoker", "backup-gw"} <= registered
        # The entry serving primary_model leads the chain regardless of
        # registration order (backup-gw had the higher priority).
        assert providers._fallback_chain == ("justwoker", "backup-gw")
        assert providers.provider_max_attempts == 3
        assert providers._breaker_threshold == 5
        for adapter in providers._adapters.values():
            with contextlib.suppress(Exception):
                adapter.close()

    def test_sync_applies_breaker_policy(self, tmp_path):
        from zero.app.config_sync import _sync_providers
        from zero.manage.core.config import ZeroConfig

        services, providers = self._services(tmp_path)
        settings = SimpleNamespace(openai_timeout_seconds=5, anthropic_timeout_seconds=5)
        cfg = ZeroConfig.model_validate(
            {
                "providers": [
                    {
                        "id": "solo",
                        "base_url": "https://solo.example/v1",
                        "api_key_ref": "sec_a",
                        "models": ["m"],
                    }
                ],
                "routing": {
                    "primary_model": "m",
                    "breaker": {"failure_threshold": 2, "cooldown_seconds": 30},
                },
            }
        )
        _sync_providers(
            settings, services, SimpleNamespace(id="p_sync"), "u_owner", cfg, None
        )
        assert providers._breaker_threshold == 2
        assert providers._breaker_base_seconds == 30.0
        for adapter in providers._adapters.values():
            with contextlib.suppress(Exception):
                adapter.close()


# ----------------------------------------------------------------------
# Fix 14 — approval posture retunable at runtime
# ----------------------------------------------------------------------


def _gate(mode: str = "off", **kwargs):
    from zero.app.approval_gate import ToolApprovalGate
    from zero.persistence.connection import Database
    from zero.persistence.migrations import apply_migrations

    settings = Settings.load_for_test()
    database = Database(settings)
    apply_migrations(database)
    # A real user + project row keeps FK integrity honest (same seed as
    # tests/test_tool_approval_gate.py).
    conn = database.connect()
    conn.execute(
        "INSERT OR IGNORE INTO users (id, display_name, status, created_at) "
        "VALUES (?, ?, 'active', ?)",
        ("zu_wave14owner000000000000", "Wave14 Owner", "2026-08-31T00:00:00"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO projects (id, name, created_at) VALUES (?, ?, ?)",
        (PROJECT, "wave14-approvals", "2026-08-31T00:00:00"),
    )
    conn.execute(
        "UPDATE projects SET owner_user_id = ? WHERE id = ? AND owner_user_id IS NULL",
        ("zu_wave14owner000000000000", PROJECT),
    )
    database.commit()
    return ToolApprovalGate(database, mode=mode, **kwargs)


class TestApprovalRuntimeTuning:
    def test_set_mode_flips_posture_without_rebuild(self):
        gate = _gate("off")
        assert gate.mode == "off"
        assert gate.evaluate(
            project_id=PROJECT,
            execution_id="e1",
            tool_name="write_file",
            input_data={"path": "/tmp/x"},
        ).cause == "mode_off"
        gate.set_mode("manual")
        assert gate.mode == "manual"
        verdict = gate.evaluate(
            project_id=PROJECT,
            execution_id="e1",
            tool_name="write_file",
            input_data={"path": "/tmp/x"},
        )
        assert verdict.state == "pending"  # now gated
        gate.set_mode("auto")
        verdict2 = gate.evaluate(
            project_id=PROJECT,
            execution_id="e1",
            tool_name="write_file",
            input_data={"path": "/tmp/x"},
        )
        assert verdict2.state == "allowed" and verdict2.cause == "mode_auto"

    def test_set_mode_rejects_unknown_values(self):
        gate = _gate("manual")
        with pytest.raises(ValueError):
            gate.set_mode("yolo")
        with pytest.raises(ValueError):
            gate.set_mode("Auto")
        assert gate.mode == "manual"

    def test_hardline_floor_survives_every_mode(self):
        gate = _gate("auto")
        verdict = gate.evaluate(
            project_id=PROJECT,
            execution_id="e1",
            tool_name="run_command",
            input_data={"command": "rm", "args": ["-rf", "/"]},
        )
        assert verdict.state == "denied"
        assert verdict.cause.startswith("hardline:")

    def test_set_pending_ttl(self):
        gate = _gate("manual", pending_ttl_seconds=600.0)
        gate.set_pending_ttl(30.0)
        assert gate._ttl == 30.0
        with pytest.raises(ValueError):
            gate.set_pending_ttl(0)

    def test_sync_applies_config_yaml_approvals(self):
        from zero.app.config_sync import _sync_approvals
        from zero.manage.core.config import ZeroConfig

        gate = _gate("manual")
        services = SimpleNamespace(approval_gate=gate)
        cfg = ZeroConfig.model_validate(
            {"approvals": {"mode": "auto", "pending_ttl_seconds": 45}}
        )
        _sync_approvals(services, cfg)
        assert gate.mode == "auto"
        assert gate._ttl == 45.0
        # Unset keys keep the current values.
        cfg2 = ZeroConfig()
        _sync_approvals(services, cfg2)
        assert gate.mode == "auto"
        assert gate._ttl == 45.0

    def test_config_schema_accepts_approvals_and_features(self):
        from zero.manage.core.config import ZeroConfig

        cfg = ZeroConfig.model_validate(
            {
                "approvals": {"mode": "manual"},
                "features": {"decomposition_enabled": True},
            }
        )
        assert cfg.approvals.mode == "manual"
        assert cfg.features.decomposition_enabled is True
        with pytest.raises(Exception):
            ZeroConfig.model_validate({"approvals": {"mode": "yolo"}})


# ----------------------------------------------------------------------
# Fix 15 — session grain on the approval card (DB-level pin)
# ----------------------------------------------------------------------


class TestSessionActionSurface:
    def test_migration_accepts_allow_session_and_preserves_legacy_rows(self, tmp_path):
        """Run the 0033 DDL, store a legacy row, then apply the 0034
        rebuild: the row survives and the widened CHECK accepts (and
        rejects) the right actions."""
        repo_root = Path(__file__).resolve().parents[1]
        old_sql = (
            repo_root / "src/zero/persistence/migrations/0033_tool_approval_tokens.sql"
        ).read_text()
        new_sql = (
            repo_root / "src/zero/persistence/migrations/0034_tool_approval_session_action.sql"
        ).read_text()

        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(old_sql)
            conn.execute(
                "INSERT INTO tool_approval_tokens "
                "(id, project_id, approval_id, action, expires_at, used_at, created_at) "
                "VALUES ('t1', 'p1', 'ta1', 'allow_once', '2027-01-01T00:00:00Z', NULL, datetime('now'))"
            )
            conn.executescript(new_sql)
            # Legacy row preserved.
            row = conn.execute(
                "SELECT id, action FROM tool_approval_tokens WHERE id = 't1'"
            ).fetchone()
            assert row == ("t1", "allow_once")
            # Widened vocabulary accepts allow_session.
            conn.execute(
                "INSERT INTO tool_approval_tokens "
                "(id, project_id, approval_id, action, expires_at, used_at, created_at) "
                "VALUES ('t2', 'p1', 'ta1', 'allow_session', '2027-01-01T00:00:00Z', NULL, datetime('now'))"
            )
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO tool_approval_tokens "
                    "(id, project_id, approval_id, action, expires_at, used_at, created_at) "
                    "VALUES ('t3', 'p1', 'ta1', 'allow_forever', '2027-01-01T00:00:00Z', NULL, datetime('now'))"
                )
        finally:
            conn.close()

    def test_migrated_engine_accepts_session_token_roundtrip(self, tmp_path, monkeypatch):
        from tests.test_dead_bot_regressions import zero_home  # noqa: F401

        monkeypatch.setenv("ZERO_HOME", str(tmp_path / "zh-session"))
        (tmp_path / "zh-session").mkdir(parents=True, exist_ok=True)
        from zero.domain.ids import generate_tool_approval_token_id
        from zero.domain.interfaces import (
            ProjectId,
            ToolApprovalToken,
            ToolApprovalTokenId,
        )
        from zero.persistence.connection import Database
        from zero.persistence.migrations import apply_migrations
        from zero.persistence.repositories.interface_repository import InterfaceRepository

        settings = Settings.load_for_test()
        database = Database(settings)
        apply_migrations(database)
        repo = InterfaceRepository(database)
        # The token FK references projects(id) — seed the parent row first.
        with database.connect() as conn:
            conn.execute(
                "INSERT INTO projects (id, name, created_at) "
                "VALUES ('p_any', 'session-roundtrip', '2026-08-31T00:00:00Z')"
            )
        token = ToolApprovalToken(
            id=ToolApprovalTokenId(generate_tool_approval_token_id()),
            project_id=ProjectId("p_any"),
            approval_id="ta_1",
            action="allow_session",
            expires_at="2027-01-01T00:00:00Z",
            used_at=None,
            created_by=None,
            created_at="2026-08-31T00:00:00Z",
        )
        repo.insert_tool_approval_token(token)
        fetched = repo.get_tool_approval_token(token.id)
        assert fetched.action == "allow_session"

    def test_session_action_resolves_to_execution_scoped_grant(self):
        """The callback mapping contract: allow_session → allow/session,
        which the gate turns into an in-process execution grant."""
        gate = _gate("manual")
        req = gate.evaluate(
            project_id=PROJECT,
            execution_id="exec_card",
            tool_name="write_file",
            input_data={"path": "/tmp/card.txt"},
        )
        assert req.state == "pending" and req.request is not None
        gate.resolve(
            req.request.id, decision="allow", decided_by_user_id="u1", grain="session"
        )
        again = gate.evaluate(
            project_id=PROJECT,
            execution_id="exec_card",
            tool_name="write_file",
            input_data={"path": "/tmp/card.txt"},
        )
        assert again.state == "allowed" and again.cause == "session_grant"


# ----------------------------------------------------------------------
# Fix 16 — scheduler knobs as config surfaces
# ----------------------------------------------------------------------


class TestSchedulerConfigSurfaces:
    def _scheduler(self):
        from zero.app.scheduler_service import SchedulerService

        return SchedulerService(
            plans=object(),
            worker=object(),
            runtime=object(),
            authorization=object(),
            task_max_attempts=0,
        )

    def test_setters_roundtrip_and_clamp(self):
        sched = self._scheduler()
        sched.set_decomposition_enabled(True)
        assert sched.decomposition_enabled is True
        sched.set_task_max_attempts(4)
        assert sched.task_max_attempts == 4
        sched.set_task_max_attempts(-3)
        assert sched.task_max_attempts == 0
        sched.set_parallel_executions(99)
        assert sched._parallel_executions == 8
        sched.set_parallel_executions(0)
        assert sched._parallel_executions == 1

    def test_sync_applies_features_block(self):
        from zero.app.config_sync import _sync_features
        from zero.manage.core.config import ZeroConfig

        sched = self._scheduler()
        services = SimpleNamespace(scheduler=sched)
        cfg = ZeroConfig.model_validate(
            {
                "features": {
                    "decomposition_enabled": True,
                    "task_max_attempts": 3,
                    "tick_parallel_executions": 4,
                }
            }
        )
        _sync_features(services, cfg)
        assert sched.decomposition_enabled is True
        assert sched.task_max_attempts == 3
        assert sched._parallel_executions == 4
        # Unset keys leave the live values alone.
        _sync_features(services, ZeroConfig())
        assert sched.decomposition_enabled is True
        assert sched.task_max_attempts == 3
        assert sched._parallel_executions == 4
