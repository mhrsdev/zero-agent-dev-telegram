"""Regressions for the reported Windows console session (2026-08-29).

Covers every defect visible in the pasted ``zero setup`` / ``zero start`` /
``zero-develop serve`` transcript:

1. ``zero-develop`` bare invocation printed argparse's terse "the following
   arguments are required: command" instead of the full help (``zero``
   already printed help).
2. ``zero-develop serve`` died on an ugly WinError 10048 bind traceback
   whenever the managed service (or any process) already held port 8000,
   and still exited 0. It must refuse with guidance instead.
3. The dev-key banner said "generated a development encryption key" on
   EVERY serve run even when the existing key was merely reloaded.
4. The dev banner kept advertising "run 'zero setup'" to operators who had
   already configured the installation (config.yaml present).
5. ``zero start`` blind-spawned: no already-running guard (a second start
   overwrote zero.pid with a process doomed to die on the bind error) and
   no post-spawn verification; ``zero stop`` printed "stopped" even when
   nothing was running.
6. Wizard dead-loop: on a DETERMINISTIC validation failure (websearch with
   required provider_id/api_key left empty) "Enter=retry same answers"
   failed identically forever. One identical failure must auto re-ask the
   step's fields (prefilled) while transient probe errors keep their
   one-keypress retry.
7. The final "Send test message" step never sent anything and printed the
   self-referencing transition "ok -> test_message". It must send for real
   and report completion.
8. ``model_assign`` silently accepted a fallback identical to the primary
   (no resilience) — now a non-blocking warning.
"""

from __future__ import annotations

import argparse
import socket

from zero.manage.core import probes
from zero.manage.core.config import ConfigService
from zero.manage.services.setup import SetupService


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def _svc(tmp_path) -> SetupService:
    return SetupService(ConfigService(tmp_path), lambda: None, secret_store=lambda n, t, v: "sec_x")


def _ns() -> argparse.Namespace:
    return argparse.Namespace(json=False)


def _no_systemctl(monkeypatch) -> None:
    """The CI host ships a systemd shim; force the plain-process path."""
    monkeypatch.setattr("zero.manage.cli.shutil.which", lambda name: None)


class _FakeProc:
    """Stands in for Popen: pid + poll() (None = alive, int = exited)."""

    def __init__(self, pid: int = 999, poll_result: int | None = None):
        self.pid = pid
        self._poll_result = poll_result

    def poll(self):
        return self._poll_result


# ----------------------------------------------------------------------
# zero-develop CLI: bare help + serve pre-checks + dev banner
# ----------------------------------------------------------------------
def test_zero_develop_bare_invocation_prints_help(capsys):
    from zero.cli import main as dev_main

    rc = dev_main([])
    out = capsys.readouterr().out
    assert rc == 2
    assert "usage: zero-develop" in out
    assert "serve" in out and "migrate" in out and "reconcile" in out
    assert "the following arguments are required" not in out


def test_serve_refuses_when_managed_service_running(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ZERO_HOME", str(tmp_path))
    (tmp_path / "zero.pid").write_text("424242", encoding="utf-8")
    monkeypatch.setattr("zero.manage.cli._pid_alive", lambda pid: True)

    from zero.cli import main as dev_main

    rc = dev_main(["serve"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "already running (pid 424242" in err
    assert "zero stop" in err
    assert "--port 8001" in err


def test_serve_refuses_when_port_busy(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ZERO_HOME", str(tmp_path))
    monkeypatch.delenv("ZERO_ENV", raising=False)
    monkeypatch.setattr("zero.cli._port_available", lambda host, port: False)

    from zero.cli import main as dev_main

    rc = dev_main(["serve"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "already in use" in err
    assert "--port 8001" in err


def test_port_available_detects_real_listener():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        from zero.cli import _port_available

        assert _port_available("127.0.0.1", port) is False

        # A freshly closed port is bindable again.
        free = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        free.bind(("127.0.0.1", 0))
        free_port = free.getsockname()[1]
        free.close()
        assert _port_available("127.0.0.1", free_port) is True
    finally:
        listener.close()


def test_dev_key_banner_reuses_existing_key(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ZERO_HOME", str(tmp_path))
    monkeypatch.setenv("ZERO_DATABASE_URL", f"sqlite:///{tmp_path / 'e.db'}")
    monkeypatch.delenv("ZERO_ENV", raising=False)
    monkeypatch.delenv("ZERO_SECRET_KEY", raising=False)
    existing = "k" * 64
    (tmp_path / "secret.key").write_text(existing, encoding="utf-8")

    from zero.cli import _ensure_development_secret_key
    from zero.config import Settings

    settings = Settings.load(env_file=None, zero_env_fallback="development")
    _ensure_development_secret_key(settings, None)
    _ensure_development_secret_key(settings, None)
    err = capsys.readouterr().err
    assert "generated" not in err
    assert "reusing the existing development encryption key" in err
    # The key file was never rotated.
    assert (tmp_path / "secret.key").read_text(encoding="utf-8") == existing
    # The .env persistence is idempotent: exactly one key line, same value.
    env_lines = (tmp_path / ".env").read_text(encoding="utf-8").splitlines()
    key_lines = [ln for ln in env_lines if ln.startswith("ZERO_SECRET_KEY=")]
    assert key_lines == [f"ZERO_SECRET_KEY={existing}"]


def test_dev_key_banner_reports_generation_on_fresh_host(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ZERO_HOME", str(tmp_path))
    monkeypatch.setenv("ZERO_DATABASE_URL", f"sqlite:///{tmp_path / 'e.db'}")
    monkeypatch.delenv("ZERO_ENV", raising=False)
    monkeypatch.delenv("ZERO_SECRET_KEY", raising=False)

    from zero.cli import _ensure_development_secret_key
    from zero.config import Settings

    settings = Settings.load(env_file=None, zero_env_fallback="development")
    _ensure_development_secret_key(settings, None)
    err = capsys.readouterr().err
    assert "generated a development encryption key" in err
    assert (tmp_path / "secret.key").exists()


def test_dev_banner_points_at_zero_start_when_config_exists(tmp_path):
    from zero.cli import _dev_serve_banner

    default_lines = _dev_serve_banner(tmp_path)
    assert len(default_lines) == 2
    assert "zero setup" in default_lines[1]

    (tmp_path / "config.yaml").write_text("telegram: {}\n", encoding="utf-8")
    configured_lines = _dev_serve_banner(tmp_path)
    assert "zero start" in configured_lines[1]
    assert str(tmp_path / "config.yaml") in configured_lines[1]


# ----------------------------------------------------------------------
# zero start/stop: already-running guard + spawn verification + honesty
# ----------------------------------------------------------------------
def test_start_refuses_when_already_running(tmp_path, monkeypatch, capsys):
    _no_systemctl(monkeypatch)
    monkeypatch.setenv("ZERO_HOME", str(tmp_path))
    (tmp_path / "zero.pid").write_text("424242", encoding="utf-8")
    monkeypatch.setattr("zero.manage.cli._pid_alive", lambda pid: True)

    from zero.manage.cli import cmd_start

    assert cmd_start(_ns()) == 1
    out = capsys.readouterr().out
    assert "service already running (pid 424242)" in out
    assert "zero restart" in out and "zero stop" in out


def test_start_verifies_spawned_process_and_reports_healthy(tmp_path, monkeypatch, capsys):
    _no_systemctl(monkeypatch)
    monkeypatch.setenv("ZERO_HOME", str(tmp_path))
    monkeypatch.setattr("zero.manage.cli._port_busy", lambda host, port: False)
    monkeypatch.setattr(
        "zero.manage.cli.subprocess.Popen", lambda *a, **k: _FakeProc(999, poll_result=None)
    )
    monkeypatch.setattr("zero.manage.cli._healthz_ok", lambda url, timeout=1.0: True)

    from zero.manage.cli import cmd_start

    assert cmd_start(_ns()) == 0
    out = capsys.readouterr().out
    assert "service healthy at http://127.0.0.1:8000 (pid=999)" in out
    assert (tmp_path / "zero.pid").read_text(encoding="utf-8") == "999"


def test_start_reports_dead_process_with_log_tail(tmp_path, monkeypatch, capsys):
    _no_systemctl(monkeypatch)
    monkeypatch.setenv("ZERO_HOME", str(tmp_path))
    (tmp_path / "zero.log").write_text(
        "INFO:     Started server process\nERROR:    [Errno 10048] bind\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("zero.manage.cli._port_busy", lambda host, port: False)
    monkeypatch.setattr(
        "zero.manage.cli.subprocess.Popen", lambda *a, **k: _FakeProc(999, poll_result=1)
    )

    from zero.manage.cli import cmd_start

    assert cmd_start(_ns()) == 1
    err = capsys.readouterr().err
    assert "exited during startup" in err
    assert "[Errno 10048] bind" in err


def test_start_refuses_when_port_occupied_by_foreign_service(tmp_path, monkeypatch, capsys):
    """The reported WinError 10048 scenario: port 8000 held by another
    (healthy) Zero service while zero.pid is absent. A doomed child must
    never be spawned, and the foreign /healthz must never be credited to
    the child that is about to die."""
    _no_systemctl(monkeypatch)
    monkeypatch.setenv("ZERO_HOME", str(tmp_path))
    monkeypatch.setattr("zero.manage.cli._port_busy", lambda host, port: True)
    monkeypatch.setattr("zero.manage.cli._healthz_ok", lambda url, timeout=1.0: True)
    spawned = []
    monkeypatch.setattr(
        "zero.manage.cli.subprocess.Popen", lambda *a, **k: spawned.append(a) or _FakeProc(1)
    )

    from zero.manage.cli import cmd_start

    assert cmd_start(_ns()) == 1
    out = capsys.readouterr().out
    assert "already serves a healthy Zero service" in out
    assert not spawned, "a doomed child must never be spawned on a busy port"


def test_stop_without_pid_file_is_honest(tmp_path, monkeypatch, capsys):
    _no_systemctl(monkeypatch)
    monkeypatch.setenv("ZERO_HOME", str(tmp_path))

    from zero.manage.cli import cmd_stop

    assert cmd_stop(_ns()) == 0
    assert "service not running (no pid file)" in capsys.readouterr().out


def test_stop_kills_and_clears_pid_file(tmp_path, monkeypatch, capsys):
    _no_systemctl(monkeypatch)
    monkeypatch.setenv("ZERO_HOME", str(tmp_path))
    (tmp_path / "zero.pid").write_text("424242", encoding="utf-8")
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr("zero.manage.cli.os.kill", lambda pid, sig: killed.append((pid, sig)))

    from zero.manage.cli import cmd_stop

    assert cmd_stop(_ns()) == 0
    assert killed == [(424242, 15)]
    assert not (tmp_path / "zero.pid").exists()
    assert "stopped" in capsys.readouterr().out


# ----------------------------------------------------------------------
# setup service: test_message really sends; duplicate-fallback warning
# ----------------------------------------------------------------------
def test_test_message_step_actually_sends(tmp_path, monkeypatch):
    svc = _svc(tmp_path)
    draft = svc.resume()
    draft["current_step"] = "test_message"
    draft["data"] = {
        "telegram_credentials": {"token_ref": "sec_x", "bot_username": "t", "_raw": {"token": "123:abc"}}
    }
    svc.cfg.save_draft(draft)

    captured: dict = {}

    def fake_send(token, chat_id, text, **k):
        captured.update(token=token, chat_id=chat_id, text=text)
        return {"ok": True, "message_id": 7}

    monkeypatch.setattr(probes, "telegram_send_message", fake_send)
    value: dict = {"chat_id": "-100123"}
    result = svc.validate("test_message", value)
    assert result.ok, result.errors
    assert captured == {
        "token": "123:abc",
        "chat_id": "-100123",
        "text": "Zero setup complete — this is a test message.",
    }
    assert value["sent_message_id"] == 7


def test_test_message_empty_chat_id_keeps_skip_semantics(tmp_path, monkeypatch):
    monkeypatch.setattr(
        probes,
        "telegram_send_message",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not send without a chat id")),
    )
    result = _svc(tmp_path).validate("test_message", {})
    assert result.ok
    assert not result.warnings


def test_test_message_surfaces_send_failure(tmp_path, monkeypatch):
    svc = _svc(tmp_path)
    draft = svc.resume()
    draft["current_step"] = "test_message"
    draft["data"] = {"telegram_credentials": {"_raw": {"token": "123:abc"}}}
    svc.cfg.save_draft(draft)
    monkeypatch.setattr(
        probes,
        "telegram_send_message",
        lambda *a, **k: {"ok": False, "error": "http 400 — chat not found"},
    )
    result = svc.validate("test_message", {"chat_id": "-100123"})
    assert not result.ok
    assert any("chat not found" in e for e in result.errors)


def test_test_message_without_token_soft_passes_with_warning(tmp_path, monkeypatch):
    monkeypatch.setattr(
        probes,
        "telegram_send_message",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not send without a token")),
    )
    result = _svc(tmp_path).validate("test_message", {"chat_id": "-100123"})
    assert result.ok, "resumed draft without a resolvable token must not fail the optional step"
    assert any("test message not sent" in w for w in result.warnings or [])


def test_model_assign_warns_on_duplicate_fallback(tmp_path):
    svc = _svc(tmp_path)
    dup = svc.validate("model_assign", {"primary_model": "m1", "fallback_models_csv": "m1, m2"})
    assert dup.ok
    assert any("m1" in w and "no resilience" in w for w in dup.warnings or [])

    distinct = svc.validate("model_assign", {"primary_model": "m1", "fallback_models_csv": "m2"})
    assert distinct.ok
    assert not distinct.warnings


# ----------------------------------------------------------------------
# interactive wizard: dead-loop recovery + last-step completion
# ----------------------------------------------------------------------
def _wizard_env(tmp_path, monkeypatch, step_order, *, input_iter, getpass_iter):
    monkeypatch.setenv("ZERO_HOME", str(tmp_path))
    monkeypatch.setenv("ZERO_DATABASE_URL", f"sqlite:///{tmp_path / 'e.db'}")
    monkeypatch.delenv("ZERO_ENV", raising=False)
    monkeypatch.setattr("zero.manage.services.setup.STEP_ORDER", step_order)
    monkeypatch.setattr("zero.manage.cli.getpass.getpass", lambda _p="": next(getpass_iter))
    monkeypatch.setattr("builtins.input", lambda _p="": next(input_iter))
    from zero.manage.cli import _interactive_setup

    return _interactive_setup


def test_wizard_recovers_from_deterministic_validation_failure(tmp_path, monkeypatch, capsys):
    """The exact reported trap: websearch enabled with the required fields
    left empty. Enter-retry fails identically ONCE, then the wizard must
    re-ask the fields (prefilled) instead of looping forever."""
    monkeypatch.setattr(probes, "telegram_get_me", lambda token, timeout=10.0: {"ok": True})
    monkeypatch.setattr(
        probes, "openai_list_models", lambda base, key, timeout=15.0: {"ok": True, "models": ["m-1"]}
    )
    # provider_add: id/protocol/base_url all default; websearch: enabled,
    # empty provider_id + empty key (the trap), Enter-retry, then re-ask
    # fills provider_id (validated against the configured provider now)
    # and the key; test_message chat id left empty (skip semantics).
    inputs = iter(["", "", "", "y", "", "", "", "openai-primary", ""])
    secrets_in = iter(["123:abc", "sk-p1", "", "sk-ws"])

    run = _wizard_env(
        tmp_path,
        monkeypatch,
        ["telegram_credentials", "provider_add", "websearch", "test_message"],
        input_iter=inputs,
        getpass_iter=secrets_in,
    )
    svc = SetupService(
        ConfigService(tmp_path), lambda: None, secret_store=lambda n, t, v: "sec_x"
    )
    rc = run(svc)
    out = capsys.readouterr().out
    assert rc == 0
    assert "same answers failed twice — re-asking this step's fields" in out
    assert "ok — setup complete" in out
    cfg = ConfigService(tmp_path).load()
    assert cfg.websearch.enabled is True
    assert cfg.websearch.provider_id == "openai-primary"
    assert cfg.websearch.api_key_ref == "sec_x"


def test_websearch_provider_id_validated_against_configured_providers(tmp_path):
    """Commit-trap regression: ZeroConfig rejects a websearch provider_id
    that references no provider — the wizard must reject it at the step,
    with the available ids, instead of failing commit after all 18 steps."""
    svc = _svc(tmp_path)
    draft = svc.resume()
    draft["data"] = {"provider_add": {"id": "openai-primary"}}
    svc.cfg.save_draft(draft)

    unknown = svc.validate("websearch", {"enabled": True, "provider_id": "tavily", "api_key": "sk-x"})
    assert not unknown.ok
    assert any(
        "does not match a configured provider" in e and "openai-primary" in e for e in unknown.errors
    )

    match = svc.validate(
        "websearch", {"enabled": True, "provider_id": "openai-primary", "api_key": "sk-x"}
    )
    assert match.ok, match.errors

    none_configured = _svc(tmp_path / "fresh").validate(
        "websearch", {"enabled": True, "provider_id": "tavily", "api_key": "sk-x"}
    )
    assert not none_configured.ok
    assert any("no provider is configured yet" in e for e in none_configured.errors)


def test_wizard_transient_error_retry_stays_one_keypress(tmp_path, monkeypatch, capsys):
    """Transient probe errors must keep the single-Enter retry (no field
    re-asking) — the earlier UX fix must not regress."""
    calls = {"n": 0}

    def flaky_probe(base, key, model, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"ok": False, "error": "unreachable: ConnectError"}
        return {"ok": True}

    monkeypatch.setattr(probes, "openai_completion_probe", flaky_probe)
    draft = SetupService(ConfigService(tmp_path), lambda: None).resume()
    draft["current_step"] = "provider_test"
    draft["data"] = {
        "provider_add": {
            "id": "p1",
            "protocol": "openai_compatible",
            "base_url": "https://x/v1",
            "api_key": "sk-a…xyz",
            "models": ["m1"],
            "api_key_ref": "sec_test",
            "_raw": {"api_key": "sk-live"},
        }
    }
    ConfigService(tmp_path).save_draft(draft)

    run = _wizard_env(
        tmp_path,
        monkeypatch,
        ["provider_test", "test_message"],
        input_iter=iter(["", "", ""]),
        getpass_iter=iter([]),
    )
    svc = SetupService(
        ConfigService(tmp_path), lambda: None, secret_store=lambda n, t, v: "sec_x"
    )
    rc = run(svc)
    out = capsys.readouterr().out
    assert rc == 0
    assert calls["n"] == 2, "second attempt must retry the SAME answers"
    assert "re-asking this step's fields" not in out
    assert "ok -> test_message" in out


def test_wizard_last_step_reports_completion_and_sends(tmp_path, monkeypatch, capsys):
    """The final step must send the message and print completion — never
    the self-referencing "ok -> test_message" transition."""
    sends: list[tuple[str, str]] = []

    def fake_send(token, chat_id, text, **k):
        sends.append((token, chat_id))
        return {"ok": True, "message_id": 77}

    monkeypatch.setattr(probes, "telegram_send_message", fake_send)

    run = _wizard_env(
        tmp_path,
        monkeypatch,
        ["test_message"],
        input_iter=iter(["-100123"]),
        getpass_iter=iter([]),
    )
    svc = SetupService(
        ConfigService(tmp_path), lambda: None, secret_store=lambda n, t, v: "sec_x"
    )
    draft = svc.resume()
    draft["current_step"] = "test_message"
    draft["data"] = {
        "telegram_credentials": {"token_ref": "sec_x", "bot_username": "t", "_raw": {"token": "123:abc"}}
    }
    svc.cfg.save_draft(draft)

    rc = run(svc)
    out = capsys.readouterr().out
    assert rc == 0
    assert sends == [("123:abc", "-100123")]
    assert "ok — setup complete" in out
    assert "ok -> test_message" not in out
    # commit() clears the draft on success — the config must exist.
    assert (tmp_path / "config.yaml").exists()


def test_wizard_skipping_last_step_reports_completion(tmp_path, monkeypatch, capsys):
    run = _wizard_env(
        tmp_path,
        monkeypatch,
        ["test_message"],
        input_iter=iter(["s"]),
        getpass_iter=iter([]),
    )
    svc = SetupService(
        ConfigService(tmp_path), lambda: None, secret_store=lambda n, t, v: "sec_x"
    )
    rc = run(svc)
    out = capsys.readouterr().out
    assert rc == 0
    assert "skipped — setup complete" in out


# ----------------------------------------------------------------------
# probe layer: sendMessage contract
# ----------------------------------------------------------------------
def test_telegram_send_message_rejects_dirty_token_without_network(monkeypatch):
    monkeypatch.setattr(probes.httpx, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    res = probes.telegram_send_message("123:abc\u2026", "-100", "hi")
    assert res["ok"] is False
    assert "invalid characters" in str(res["error"])


def test_telegram_send_message_surfaces_telegram_error_description(monkeypatch):
    class _Resp:
        status_code = 400

        @staticmethod
        def json():
            return {"ok": False, "description": "chat not found"}

    monkeypatch.setattr(probes.httpx, "post", lambda *a, **k: _Resp())
    res = probes.telegram_send_message("123:abc", "-100", "hi")
    assert res["ok"] is False
    assert "chat not found" in str(res["error"])


def test_telegram_send_message_returns_message_id(monkeypatch):
    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": True, "result": {"message_id": 42}}

    monkeypatch.setattr(probes.httpx, "post", lambda *a, **k: _Resp())
    res = probes.telegram_send_message("123:abc", "-100", "hi")
    assert res == {"ok": True, "message_id": 42}
