"""Regression tests for the "Telegram bot completely dead" session.

Root cause (2026-08-29): the engine resolved its development database as
``sqlite:///./zero_develop.db`` — RELATIVE TO CWD. ``zero setup`` stored
the operator's secrets into the database it saw from ITS directory, and
``zero start`` later booted the engine from another directory, silently
creating a fresh, secret-less database. Every ``sec_...`` reference in
config.yaml then failed with ``SecretNotFoundError``, no Telegram binding
was ever created, and the bot never responded to /start.

These tests pin down each layer of the fix:
- Settings.load() defaults to $ZERO_HOME/.env (the pinned anchor);
- the database pin is absolute and stable across directories;
- `zero doctor` finds the drifted database and repairs (--fix);
- config sync self-heals from ZERO_TELEGRAM_BOT_TOKEN / ZERO_OPENAI_API_KEY;
- missing credentials fail LOUDLY with actionable guidance;
- the cross-process poll lock prevents dual-instance 409 fights;
- Telegram 409 responses surface as a typed conflict error.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from zero.config import Settings


def _clean_zero_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "ZERO_ENV",
        "ZERO_DATABASE_URL",
        "ZERO_SECRET_KEY",
        "ZERO_TELEGRAM_BOT_TOKEN",
        "ZERO_OPENAI_API_KEY",
        "ZERO_ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def zero_home(env_snapshot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fresh $ZERO_HOME with development env + stable encryption key."""
    _clean_zero_env(monkeypatch)
    home = tmp_path / "zero-home"
    home.mkdir()
    monkeypatch.setenv("ZERO_HOME", str(home))
    monkeypatch.setenv("ZERO_SECRET_KEY", "k" * 64)
    (home / ".env").write_text("ZERO_ENV=development\n", encoding="utf-8")
    return home


def _engine_for(home: Path):
    """Build engine services exactly like the CLI bridges do."""
    from zero.persistence.connection import open_database
    from zero.persistence.migrations import apply_migrations

    settings = Settings.load(env_file=str(home / ".env"), zero_env_fallback="development")
    database = open_database(settings)
    apply_migrations(database)
    from zero.app.services import build_services

    return settings, build_services(settings, database)


def _wizard_store_secrets(services, n: int = 1) -> tuple[str, list[str]]:
    """Store a bot token (+ api keys) under a fresh management project."""
    from zero.manage.cli import _ensure_management_scope

    project = _ensure_management_scope(services)
    ref = services.secrets.store(
        project_id=project.id,
        name="telegram-bot-token",
        secret_type="token",
        value="123:TEST_TOKEN",
        actor_id=project.owner_user_id,
    )
    refs = [ref.id.value]
    for i in range(n):
        key_ref = services.secrets.store(
            project_id=project.id,
            name=f"openai-{i}-api-key",
            secret_type="api_key",
            value="sk-TEST",
            actor_id=project.owner_user_id,
        )
        refs.append(key_ref.id.value)
    return project.id.value, refs


def _write_config(home: Path, bot_ref: str, api_refs: list[str]) -> None:
    from zero.manage.core.config import ConfigService, ProviderCfg, RoutingCfg

    cfgsvc = ConfigService(home)
    cfg = cfgsvc.load()
    cfg.telegram.bot_token_ref = bot_ref
    cfg.providers = [
        ProviderCfg(
            id=f"openai-{i}",
            protocol="openai_compatible",
            base_url="https://api.example.com/v1",
            api_key_ref=ref,
            models=["test-model"],
        )
        for i, ref in enumerate(api_refs)
    ]
    cfg.routing = RoutingCfg(primary_model="test-model")
    cfgsvc.save(cfg)


# ----------------------------------------------------------------------
# Settings.load: $ZERO_HOME/.env is the default anchor
# ----------------------------------------------------------------------
def test_settings_load_defaults_to_zero_home_env(zero_home, tmp_path, monkeypatch):
    target = tmp_path / "pinned.db"
    (zero_home / ".env").write_text(
        "ZERO_ENV=development\n"
        f"ZERO_DATABASE_URL=sqlite:///{target}\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)  # relative fallback would hit ./zero_develop.db
    settings = Settings.load(zero_env_fallback="development")
    assert str(target) in settings.database_url


def test_process_env_still_wins_over_default_env_file(zero_home, tmp_path):
    (zero_home / ".env").write_text(
        "ZERO_ENV=development\nZERO_DATABASE_URL=sqlite:///./from-file.db\n",
        encoding="utf-8",
    )
    monkey_target = tmp_path / "from-env.db"
    monkeypatch_env = f"sqlite:///{monkey_target}"
    os.environ["ZERO_DATABASE_URL"] = monkeypatch_env
    try:
        settings = Settings.load(zero_env_fallback="development")
        assert str(monkey_target) in settings.database_url
    finally:
        os.environ.pop("ZERO_DATABASE_URL", None)


# ----------------------------------------------------------------------
# Database pinning (env_file helpers)
# ----------------------------------------------------------------------
def test_pin_database_url_is_absolute_and_cwd_stable(zero_home, tmp_path, monkeypatch):
    from zero.manage.core.env_file import pin_database_url

    dir_a = tmp_path / "repo"
    dir_a.mkdir()
    monkeypatch.chdir(dir_a)

    report = pin_database_url()
    assert report["database_url"] == f"sqlite:///{dir_a / 'zero_develop.db'}"
    env_text = (zero_home / ".env").read_text()
    assert f"ZERO_DATABASE_URL=sqlite:///{dir_a / 'zero_develop.db'}" in env_text

    # Idempotent: a second pin reports no changes.
    report2 = pin_database_url()
    assert report2["pinned"] == []

    # The pinned URL now resolves identically from another directory.
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)
    settings = Settings.load(env_file=str(zero_home / ".env"), zero_env_fallback="development")
    assert settings.database_url == f"sqlite:///{dir_a / 'zero_develop.db'}"


def test_pin_respects_operator_env_override(zero_home, monkeypatch):
    from zero.manage.core.env_file import pin_database_url

    monkeypatch.setenv("ZERO_DATABASE_URL", "sqlite:///./operator-choice.db")
    report = pin_database_url()
    assert report["skipped"] and report["pinned"] == []
    assert "ZERO_DATABASE_URL" not in (zero_home / ".env").read_text()


def test_upsert_preserves_unrelated_lines(zero_home):
    from zero.manage.core.env_file import upsert_dotenv

    env_path = zero_home / ".env"
    env_path.write_text(
        "ZERO_ENV=development\nZERO_SECRET_KEY=keep-me\n# comment\n",
        encoding="utf-8",
    )
    changed = upsert_dotenv(env_path, {"ZERO_DATABASE_URL": "sqlite:///./x.db"})
    assert changed == ["ZERO_DATABASE_URL"]
    text = env_path.read_text()
    assert "ZERO_SECRET_KEY=keep-me" in text
    assert "# comment" in text


# ----------------------------------------------------------------------
# config sync: self-heal + loud failures
# ----------------------------------------------------------------------
def _sync(home: Path) -> tuple[object, list, list]:
    from zero.app.config_sync import sync_management_config

    settings, services = _engine_for(home)
    sync_management_config(settings, services)
    bindings = [
        (b.platform, b.chat_id, b.bot_token_ref)
        for p in services.identity.list_projects()
        for b in services.interfaces.list_bindings(p.id)
    ]
    providers = list(services.providers.registered_provider_names)
    return services, bindings, providers


def test_config_sync_recovers_bot_token_from_env(
    zero_home, tmp_path, monkeypatch
):
    from zero.manage.core.config import ConfigService

    dir_engine = tmp_path / "engine-cwd"
    dir_engine.mkdir()
    monkeypatch.chdir(dir_engine)

    settings, services = _engine_for(zero_home)
    _project_id, refs = _wizard_store_secrets(services)
    _write_config(zero_home, bot_ref="sec_" + "z" * 24, api_refs=refs[1:])

    monkeypatch.setenv("ZERO_TELEGRAM_BOT_TOKEN", "987:RECOVERED")
    _svc, bindings, _providers = _sync(zero_home)

    cfg = ConfigService(zero_home).load()
    assert cfg.telegram.bot_token_ref != "sec_" + "z" * 24
    assert any(b[2] == cfg.telegram.bot_token_ref for b in bindings)


def test_config_sync_recovers_provider_key_from_env(
    zero_home, tmp_path, monkeypatch
):
    dir_engine = tmp_path / "engine-cwd"
    dir_engine.mkdir()
    monkeypatch.chdir(dir_engine)

    settings, services = _engine_for(zero_home)
    _project_id, refs = _wizard_store_secrets(services)
    _write_config(zero_home, bot_ref=refs[0], api_refs=["sec_" + "y" * 24])

    monkeypatch.setenv("ZERO_OPENAI_API_KEY", "sk-RECOVERED")
    _svc, _bindings, providers = _sync(zero_home)
    assert providers, "recovered provider key must register an adapter"


def test_config_sync_pins_scheduler_tick_to_routing_primary_model(
    zero_home, tmp_path, monkeypatch
):
    """Round-7 live fix: the scheduler tick (task execution + LLM
    decomposition) resolved its model from ``settings.openai_model``
    (gpt-4o-mini default) — ``routing.primary_model`` never reached the
    tasks. On the operator's gateway the gpt-4o-mini default stopped
    being served outright (every decomposition/task call edge-403'd)
    while the aligned planner and chat kept working. config_sync must
    pin the tick to the SAME routing truth."""
    dir_engine = tmp_path / "engine-cwd"
    dir_engine.mkdir()
    monkeypatch.chdir(dir_engine)

    settings, services = _engine_for(zero_home)
    _project_id, refs = _wizard_store_secrets(services)
    _write_config(zero_home, bot_ref=refs[0], api_refs=["sec_" + "y" * 24])

    # Before the sync, no routing override is pinned.
    assert services.scheduler.tick_routing_override() == (None, None)

    monkeypatch.setenv("ZERO_OPENAI_API_KEY", "sk-RECOVERED")
    _svc, _bindings, providers = _sync(zero_home)
    assert providers, "provider must register for the alignment to fire"
    provider, model = _svc.scheduler.tick_routing_override()
    assert provider == "openai-compatible"
    assert model == "test-model"  # routing.primary_model written by _write_config


def test_config_sync_fails_loud_without_recovery_path(
    zero_home, tmp_path, monkeypatch, caplog
):
    dir_engine = tmp_path / "engine-cwd"
    dir_engine.mkdir()
    monkeypatch.chdir(dir_engine)

    settings, services = _engine_for(zero_home)
    _project_id, refs = _wizard_store_secrets(services)
    _write_config(zero_home, bot_ref="sec_" + "z" * 24, api_refs=refs[1:])

    import logging

    with caplog.at_level(logging.ERROR, logger="zero.app.config_sync"):
        _svc, bindings, _providers = _sync(zero_home)
    assert not bindings
    assert "TELEGRAM POLLING IS DISABLED" in caplog.text
    assert "zero doctor" in caplog.text


# ----------------------------------------------------------------------
# doctor: drift scan + repair
# ----------------------------------------------------------------------
def test_doctor_fix_finds_and_pins_drifted_database(
    zero_home, tmp_path, monkeypatch, capsys
):
    from zero.manage.cli import _engine_services
    from zero.manage.core.config import ConfigService
    from zero.manage.services.doctor import DoctorService

    root = tmp_path / "home-dir"
    repo = root / "repo"
    repo.mkdir(parents=True)
    monkeypatch.chdir(repo)

    # Wizard phase: secrets land in repo/zero_develop.db, config.yaml written.
    settings, services = _engine_services(env_file=str(zero_home / ".env"))
    _project_id, refs = _wizard_store_secrets(services)
    _write_config(zero_home, bot_ref=refs[0], api_refs=refs[1:])

    # Operator phase: engine runs from the parent dir (drift).
    monkeypatch.chdir(root)
    doctor = DoctorService(
        ConfigService(zero_home), lambda: _engine_services(env_file=None)
    )
    fix_report = doctor.fix()

    assert fix_report["recheck"] and fix_report["recheck"]["ok"], fix_report
    env_text = (zero_home / ".env").read_text()
    assert f"ZERO_DATABASE_URL=sqlite:///{repo / 'zero_develop.db'}" in env_text

    # A post-fix doctor run reports the references as healthy.
    run_report = doctor.run()
    ref_check = next(c for c in run_report["checks"] if c["name"] == "secret-references")
    assert ref_check["status"] == "ok", ref_check


# ----------------------------------------------------------------------
# Poll lock: one poller per bot token
# ----------------------------------------------------------------------
def test_poll_lock_same_process_conflict(zero_home):
    from zero.app.poll_lock import TokenPollLock

    lock_a = TokenPollLock(zero_home)
    lock_b = TokenPollLock(zero_home)
    ok_a, _ = lock_a.try_acquire("123:TOKEN")
    ok_b, holder = lock_b.try_acquire("123:TOKEN")
    assert ok_a
    assert not ok_b and holder == os.getpid()
    lock_a.release("123:TOKEN")
    ok_c, _ = lock_b.try_acquire("123:TOKEN")
    assert ok_c
    lock_b.release("123:TOKEN")


def test_poll_lock_steals_stale_lock(zero_home):
    from zero.app.poll_lock import TokenPollLock, token_fingerprint

    stale = TokenPollLock(zero_home)
    lock_dir = stale.lock_dir
    lock_dir.mkdir(parents=True, exist_ok=True)
    dead_pid = _find_dead_pid()
    (lock_dir / f"{token_fingerprint('123:TOKEN')}.lock").write_text(
        f'{{"pid": {dead_pid}, "ts": 1}}', encoding="utf-8"
    )
    ok, _ = stale.try_acquire("123:TOKEN")
    assert ok, "a lock held by a dead pid must be stealable"
    stale.release("123:TOKEN")


def _find_dead_pid() -> int:
    """A pid that is certainly not alive (best effort)."""
    from zero.app.poll_lock import _pid_alive

    for candidate in range(400000, 300, -1):
        if not _pid_alive(candidate):
            return candidate
    return 399999


# ----------------------------------------------------------------------
# Polling loop: 409 typed error + backoff (no hot loop)
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_polling_loop_backs_off_on_conflict(zero_home, monkeypatch):
    from zero.adapters.telegram import TelegramConflictError
    from zero.app.background_workers import BackgroundWorkerHost

    settings = Settings.load(env_file=str(zero_home / ".env"), zero_env_fallback="development")
    # Floor is 0.1s (max(0.1, ...)); anything small keeps the test fast.
    settings = settings.model_copy(update={"polling_interval_seconds": 0.01})
    _database, services = None, SimpleNamespace()
    host = BackgroundWorkerHost(settings, services)

    binding = SimpleNamespace(id=SimpleNamespace(value="b-1"))
    monkeypatch.setattr(
        host, "_telegram_poll_targets", lambda: [(None, binding, "123:TOKEN")]
    )

    calls = {"n": 0}

    def _fake_adapter(**kwargs):
        calls["n"] += 1
        raise TelegramConflictError("another getUpdates consumer")

    monkeypatch.setattr(
        "zero.app.background_workers._build_binding_adapter", _fake_adapter
    )

    import asyncio

    task = asyncio.create_task(host._polling_loop())
    await asyncio.sleep(0.25)
    host._stop.set()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert host.status.polling_conflicts >= 1
    # Backoff (>=5s) must keep the binding skipped during the window —
    # a hot loop would have hammered the adapter dozens of times.
    assert calls["n"] <= 2, f"conflict backoff violated: {calls['n']} polls"


# ----------------------------------------------------------------------
# Telegram adapter: 409 -> typed conflict
# ----------------------------------------------------------------------
def test_telegram_409_becomes_conflict_error(zero_home):
    from zero.adapters.messaging import PermanentTransportError
    from zero.adapters.telegram import TelegramAdapter, TelegramConflictError

    class Transport409:
        status_code = 409

        def request(self, *a, **k):
            raise PermanentTransportError("provider returned HTTP status 409")

        def close(self):
            pass

    adapter = TelegramAdapter(
        event_handler=lambda e: None,
        transport=Transport409(),
        bot_token="1:t",
        poll_timeout_seconds=0,
    )
    with pytest.raises(TelegramConflictError):
        adapter.poll_once(scope_key="x")


def test_telegram_other_4xx_stays_permanent(zero_home):
    from zero.adapters.messaging import PermanentTransportError
    from zero.adapters.telegram import TelegramAdapter

    class Transport403:
        status_code = 403

        def request(self, *a, **k):
            raise PermanentTransportError("provider returned HTTP status 403")

        def close(self):
            pass

    adapter = TelegramAdapter(
        event_handler=lambda e: None,
        transport=Transport403(),
        bot_token="1:t",
        poll_timeout_seconds=0,
    )
    with pytest.raises(PermanentTransportError) as exc_info:
        adapter.poll_once(scope_key="x")
    # Sibling classes: a 403 must NOT surface as the 409 conflict type.
    assert type(exc_info.value) is PermanentTransportError
    assert "403" in str(exc_info.value)


# ----------------------------------------------------------------------
# sqlite reference location scan used by the doctor
# ----------------------------------------------------------------------
def test_refs_in_database_scans_readonly(zero_home, tmp_path):
    from zero.manage.services.doctor import DoctorService

    db = tmp_path / "some.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE secret_references (id TEXT, project_id TEXT, name TEXT,"
        " secret_type TEXT, encrypted_value TEXT, key_id TEXT, created_at TEXT,"
        " revoked_at TEXT)"
    )
    conn.execute("INSERT INTO secret_references (id) VALUES ('sec_abc')")
    conn.commit()
    conn.close()

    found = DoctorService._refs_in_database(db, ["sec_abc", "sec_missing"])
    assert found == {"sec_abc"}


# ----------------------------------------------------------------------
# /start & /help command replies (the "is this bot alive" probe)
# ----------------------------------------------------------------------
def _intake_service(secret_service):
    from zero.app.interface_service import InterfaceAdapterService

    return InterfaceAdapterService(
        interface_repo=None,
        audit_repo=None,
        plan_service=None,
        authorization_service=None,
        identity_repo=None,
        secret_service=secret_service,
    )


class _Event:
    def __init__(self, content: str, kind: str = "message"):
        self.content = content
        self.event_kind = kind
        self.chat_id = "-100123"
        self.topic_id = None


class _Binding:
    def __init__(self):
        from zero.domain.identity import ProjectId

        self.project_id = ProjectId("p_test")
        self.id = SimpleNamespace(value="ib_test")
        self.platform = "telegram"


def test_start_command_replies_via_direct_transport(monkeypatch):
    import asyncio

    svc = _intake_service(secret_service=None)
    sent: list[str] = []

    class _Transport:
        def send_message(self, *, project_id, binding_id, actor_id, text, **_extra):
            sent.append(text)
            return "mid-1"

    svc.direct_reply_transport = _Transport()
    called: dict = {}

    def _fake_process(binding, event, user_id):
        called["ok"] = True
        entry = SimpleNamespace()
        return entry

    monkeypatch.setattr(svc, "_record_event", lambda entry, succeeded=True: entry)
    entry = SimpleNamespace()
    # Drive _maybe_send_command_reply directly: a /start message must send.
    svc._maybe_send_command_reply(
        binding=_Binding(),
        event=_Event("/start"),
        user_id="u_test",
    )
    assert len(sent) == 1 and "Zero is online" in sent[0]

    # /help replies too; plain text does not.
    svc._maybe_send_command_reply(
        binding=_Binding(), event=_Event("/help"), user_id="u_test"
    )
    assert len(sent) == 2 and "Zero commands" in sent[1]
    svc._maybe_send_command_reply(
        binding=_Binding(), event=_Event("do the thing"), user_id="u_test"
    )
    assert len(sent) == 2


def test_command_reply_failure_never_raises():
    svc = _intake_service(secret_service=None)

    class _Broken:
        def send_message(self, **k):
            raise RuntimeError("telegram down")

    svc.direct_reply_transport = _Broken()
    # Must not raise.
    svc._maybe_send_command_reply(
        binding=_Binding(), event=_Event("/start"), user_id="u_test"
    )


def test_command_reply_without_transport_is_silent():
    svc = _intake_service(secret_service=None)
    svc.direct_reply_transport = None
    # Must not raise even with no transport wired (test envs).
    svc._maybe_send_command_reply(
        binding=_Binding(), event=_Event("/start"), user_id="u_test"
    )
