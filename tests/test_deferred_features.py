"""Tests for the deferred-features production round.

Covers: capability probes + cache, backup daemon, GUI wizard end-to-end
(real secret store), Retry-After toasts payload, TUI data layer, and CI
workflow file validity.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import httpx
import pytest

from zero.app.services import build_services
from zero.config import Settings
from zero.manage.core.capabilities import (
    CapabilityCache,
    _anthropic_tool_probe,
    _openai_stream_probe,
    _openai_tool_probe,
    probe_capabilities,
)
from zero.manage.core.config import ConfigService, ZeroConfig
from zero.manage.services.backup_daemon import BackupDaemon
from zero.manage.services.setup import SetupService
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations


@pytest.fixture
def services(test_settings: Settings):
    settings = Settings.load_for_test(secret_key="a" * 64)
    database = Database(settings)
    apply_migrations(database)
    return build_services(settings, database)


# ----------------------------------------------------------------------
# Capability probes
# ----------------------------------------------------------------------


def _ok_tools_openai() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "c1",
                                "type": "function",
                                "function": {"name": "zero_probe_tool", "arguments": "{}"},
                            }
                        ]
                    }
                }
            ]
        },
    )


def test_openai_tool_probe_supported() -> None:
    transport = httpx.MockTransport(lambda req: _ok_tools_openai())
    state, _detail = _openai_tool_probe("https://x/v1", "k", "m", transport=transport)
    assert state == "supported"


def test_openai_tool_probe_unsupported_when_provider_rejects() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "tool_choice is not supported"}})

    state, _ = _openai_tool_probe("https://x/v1", "k", "m", transport=httpx.MockTransport(handler))
    assert state == "unsupported"


def test_openai_stream_probe_supported_and_unknown() -> None:
    body = b'data: {"choices":[{"delta":{"content":"h"}}]}\n\ndata: [DONE]\n\n'

    ok = httpx.MockTransport(lambda req: httpx.Response(200, content=body))
    state, _ = _openai_stream_probe("https://x/v1", "k", "m", transport=ok)
    assert state == "supported"

    reject = httpx.MockTransport(
        lambda req: httpx.Response(400, content=b'{"error":{"message":"stream unsupported here"}}')
    )
    state2, detail2 = _openai_stream_probe("https://x/v1", "k", "m", transport=reject)
    assert state2 == "unsupported" and "unsupported" in detail2


def test_anthropic_tool_probe_supported() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "zero_probe_tool", "input": {}}
                ],
                "stop_reason": "tool_use",
            },
        )

    state, _ = _anthropic_tool_probe(
        "https://x", "k", "claude-m", transport=httpx.MockTransport(handler)
    )
    assert state == "supported"


def test_transport_error_is_unavailable_not_unknown() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    state, detail = _openai_tool_probe(
        "https://x/v1", "k", "m", transport=httpx.MockTransport(handler)
    )
    assert state == "unavailable" and "network" in detail


def test_capability_cache_roundtrip_and_ttl(tmp_path: Path) -> None:
    cache = CapabilityCache(tmp_path, ttl_seconds=60)
    report = probe_capabilities(
        protocol="openai_compatible",
        base_url="https://x/v1",
        api_key="k",
        model="m",
        provider_id="p1",
    )
    cache.put(report)
    got = cache.get("p1", "m", "")
    assert got is not None and got.tool_calls == report.tool_calls

    expired = CapabilityCache(tmp_path, ttl_seconds=-1)
    assert expired.get("p1", "m", "") is None


# ----------------------------------------------------------------------
# Backup daemon
# ----------------------------------------------------------------------


def _daemon(home: Path, schedule="daily", retention=3, runner=None) -> BackupDaemon:
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    if runner is None:

        def runner() -> str:
            f = home / "backups" / f"zero-backup-{time.time_ns()}.enc"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"x")
            return str(f)

    return BackupDaemon(home=home, schedule=schedule, retention=retention, backup_runner=runner)


def test_daemon_due_matrix(tmp_path: Path) -> None:
    d_off = _daemon(tmp_path / "a", schedule="off")
    assert d_off.due() is False
    d_hourly = _daemon(tmp_path / "b", schedule="hourly")
    assert d_hourly.due(now=time.time()) is True  # no last state → catch-up


def test_daemon_run_once_creates_state_and_respects_retention(tmp_path: Path) -> None:
    d = _daemon(tmp_path / "h", retention=2)
    res = d.run_once()
    assert res["ran"] and res["ok"]
    # seed two older archives then run again → retention prunes to 2
    bdir = tmp_path / "h" / "backups"
    for i in range(2):
        (bdir / f"zero-backup-old{i}.enc").write_bytes(b"o")
    res2 = d.run_once(force=True)
    assert res2["ok"] and res2["pruned"] >= 1
    remaining = list(bdir.glob("zero-backup-*.enc"))
    assert len(remaining) <= 2


def test_daemon_busy_lock_blocks_run(tmp_path: Path) -> None:
    d = _daemon(tmp_path / "h")
    lock_dir = tmp_path / "h" / "backups"
    lock_dir.mkdir(parents=True)
    lock = lock_dir / ".backup.lock"

    # Fresh foreign lock -> busy.
    lock.write_text("999999")
    res_busy = d.run_once(force=True)
    assert res_busy["ran"] is False and res_busy["reason"] == "already running"


def test_daemon_stale_lock_is_stolen(tmp_path: Path) -> None:
    import os as _os

    d = _daemon(tmp_path / "h2")
    lock_dir = tmp_path / "h2" / "backups"
    lock_dir.mkdir(parents=True)
    lock = lock_dir / ".backup.lock"
    lock.write_text("999999")
    old_ts = time.time() - 700
    _os.utime(lock, (old_ts, old_ts))
    res = d.run_once(force=True)
    assert res["ran"] and res["ok"], res


def test_daemon_same_pid_leftover_is_stealable(tmp_path: Path) -> None:
    _daemon(tmp_path / "h3")
    lock_dir = tmp_path / "h3" / "backups"
    lock_dir.mkdir(parents=True)
    lock = lock_dir / ".backup.lock"
    lock.write_text(str(os.getpid()))
    lk = BackupDaemon._Lock(lock, stale_after_seconds=600)
    assert lk.__enter__() is True
    lk.__exit__(None, None, None)


def test_daemon_runner_failure_recorded(tmp_path: Path) -> None:
    def bad_runner() -> str:
        raise RuntimeError("disk on fire")

    d = _daemon(tmp_path / "h", runner=bad_runner)
    res = d.run_once(force=True)
    assert res["ok"] is False and "disk on fire" in res["error"]
    assert d.last_error and "RuntimeError" in d.last_error


# ----------------------------------------------------------------------
# GUI wizard end-to-end (real secret store via engine services fixture)
# ----------------------------------------------------------------------


@pytest.fixture
def gui(services, monkeypatch, tmp_path):
    from fastapi import FastAPI

    app = FastAPI()
    from zero.manage.web import register_admin

    register_admin(app, services)
    monkeypatch.setenv("ZERO_HOME", str(tmp_path))
    from fastapi.testclient import TestClient

    client = TestClient(app)
    # bootstrap admin session (GET first creates the one-time code file)
    client.get("/admin/login")
    setup_code = (tmp_path / "setup-code.txt").read_text(encoding="utf-8").strip()
    code_page = client.post("/admin/login/bootstrap", data={"secret": setup_code})
    assert code_page.status_code in (200, 303)
    pw_page = client.post(
        "/admin/login/setpw",
        data={"pw": "supersecret123", "pw2": "supersecret123"},
        follow_redirects=False,
    )
    if pw_page.status_code != 303:
        (tmp_path / "setpw-debug.html").write_text(pw_page.text, encoding="utf-8")
    assert pw_page.status_code == 303
    return client, services, tmp_path


def test_gui_wizard_full_flow(gui, monkeypatch) -> None:
    client, services, tmp_path = gui
    monkeypatch.setattr(
        "zero.manage.core.probes.telegram_get_me",
        lambda token, timeout=10.0: {"ok": True, "id": 5, "username": "wizbot"},
    )
    monkeypatch.setattr(
        "zero.manage.core.probes.openai_list_models",
        lambda base, key, timeout=15.0: {"ok": True, "models": ["gpt-4o-mini"]},
    )
    monkeypatch.setattr(
        "zero.manage.core.probes.openai_completion_probe",
        lambda base_url, api_key, model, timeout=30.0: {"ok": True},
    )

    def post_step(step, **fields):
        data = {"csrf": _last_csrf(client), "step": step, "action": "answer"}
        data.update(fields)
        r = client.post("/admin/wizard/answer", data=data)
        assert r.status_code in (200, 303), r.text[:300]

    post_step("environment", environment="development")
    post_step("version", channel="stable")
    post_step("telegram_mode", mode="bot_api")
    post_step("telegram_credentials", token="123456:ABC-real")
    post_step(
        "provider_add",
        id="openai-primary",
        protocol="openai_compatible",
        base_url="https://api.openai.com/v1",
        api_key="sk-test-key",
    )
    post_step("provider_test", model="gpt-4o-mini")
    post_step("model_assign", primary_model="gpt-4o-mini", fallback_models_csv="")
    post_step("access_mode", mode="groups")
    post_step("groups", chat_id="-100777", title="Wizard Group")
    post_step("agents", default_agent="main_worker")
    post_step("memory_storage", compaction_threshold_percent="85")
    post_step("privacy", telemetry_enabled="false")
    post_step("updates", channel="stable", auto_apply="false")
    post_step("backup_policy", schedule="daily", retention="7")

    r = client.post(
        "/admin/wizard/answer",
        data={"csrf": _last_csrf(client), "step": "final_validation", "action": "commit"},
    )
    assert r.status_code == 200  # TestClient follows the 303 to /admin

    cfg_file = tmp_path / "config.yaml"
    hist = [str(h.headers.get("location")) for h in r.history]
    assert cfg_file.exists(), f"commit failed: url={r.url} hist={hist}"
    text = cfg_file.read_text(encoding="utf-8")
    assert "sec_" in text  # secrets stored as references
    assert "sk-test-key" not in text and "ABC-real" not in text
    # and the secrets really live in the engine store (resolvable)
    proj = next(p for p in services.identity.list_projects() if p.name == "Zero Management")
    import re as _re

    refs = _re.findall(r"sec_[a-z0-9_]+", text)
    assert refs, "expected at least one secret reference in config"
    ref_cls = __import__("zero.domain.secrets", fromlist=["SecretReferenceId"]).SecretReferenceId
    val = services.secrets.resolve_value(
        project_id=proj.id, secret_id=ref_cls(refs[0]), actor_id=proj.owner_user_id
    )
    assert val == "123456:ABC-real"


def _last_csrf(client):
    page = client.get("/admin/wizard").text
    marker = 'name="csrf" value="'
    idx = page.index(marker) + len(marker)
    return page[idx : page.index('"', idx)]


def test_wizard_commit_refuses_dangling_secrets(tmp_path, monkeypatch) -> None:
    """Dry mode (no backend wired) must refuse to write config pointing
    at un-stored secrets."""
    monkeypatch.setattr(
        "zero.manage.core.probes.telegram_get_me",
        lambda token, timeout=10.0: {"ok": True, "id": 1, "username": "t"},
    )
    from zero.manage.core.config import ConfigError, ConfigService

    cfgsvc = ConfigService(tmp_path / "h")
    svc = SetupService(cfgsvc, lambda: None, secret_store=None)
    svc.answer("telegram_credentials", {"token": "raw-token"})
    with pytest.raises(ConfigError, match="secrets not stored"):
        svc.commit()


def test_retry_after_toast_payload(services, gui, monkeypatch) -> None:
    client, _services, _tmp = gui
    op = services.identity.create_user(display_name="Toast Operator")
    project = services.identity.create_project(owner_id=op.id, name="Toast Management")
    ref = services.secrets.store(
        project_id=project.id,
        name="p1-key",
        secret_type="api_key",
        value="sk-x",
        actor_id=project.owner_user_id,
    )
    from zero.manage.core.config import ProviderCfg

    cfgsvc = ConfigService(_tmp)
    cfg = cfgsvc.load() if cfgsvc.exists() else ZeroConfig()
    cfg.providers.append(
        ProviderCfg(
            id="p1", base_url="https://api.openai.com/v1", api_key_ref=ref.id.value, models=["m1"]
        )
    )
    cfgsvc.save(cfg)

    def forced_429(base_url, api_key, model):
        return ("unavailable", "rate limited (retry_after=45)")

    monkeypatch.setattr(
        "zero.manage.core.capabilities._openai_tool_probe",
        lambda *a, **kw: forced_429(*a[:3]),
    )
    monkeypatch.setattr(
        "zero.manage.core.capabilities._openai_stream_probe", lambda *a, **kw: ("supported", "")
    )

    resp = client.post("/admin/providers/p1/test")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["retry_after"] == 45
    assert payload["capabilities"]["tool_calls"] == "unavailable"


def test_tui_data_overview_smoke(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ZERO_HOME", str(tmp_path))
    from zero.manage.tui import data

    o = data.overview()
    assert o["initialized"] is False
    assert "providers" in o


def test_ci_workflow_files_exist_and_parse() -> None:
    repo = Path(__file__).resolve().parents[1]
    wf = repo / ".github" / "workflows" / "ci.yml"
    assert wf.exists(), "ci.yml missing"
    try:
        import yaml

        doc = yaml.safe_load(wf.read_text(encoding="utf-8"))
        assert "jobs" in doc
    except ImportError:  # pragma: no cover
        pass
