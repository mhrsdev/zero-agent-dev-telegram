"""Audit e2e: `zero setup --non-interactive` against REAL local HTTP.

Proves audit finding D1 is fixed end-to-end: the CLI wizard persists
raw secrets to the engine's encrypted store (real Fernet), writes
config.yaml with durable refs only, and never leaks plaintext into the
draft or config. The wizard's network probes hit a real local HTTP
server (integration fixture, not a mock of the code under test).
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from zero.manage.cli import main

FAKE_TOKEN = "123456:AUDIT-FAKE-TOKEN"
FAKE_KEY = "sk-audit-fake-key"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence
        pass

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if "/getMe" in self.path:
            # Real Bot API rejects malformed tokens; mirror that.
            if FAKE_TOKEN not in self.path:
                self._json({"ok": False, "error_code": 401}, 401)
                return
            self._json({"ok": True, "result": {"id": 777, "username": "audit_bot", "is_bot": True}})
            return
        if self.path.endswith("/models"):
            self._json({"data": [{"id": "fake-mini"}, {"id": "fake-standard"}]})
            return
        self._json({"ok": False}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if self.path.endswith("/chat/completions"):
            self._json(
                {
                    "id": "chatcmpl-audit",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "pong"},
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }
            )
            return
        self._json({"ok": False}, 404)


@pytest.fixture(scope="module")
def api_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def cli_home(tmp_path, monkeypatch):
    home = tmp_path / "zero-home"
    monkeypatch.setenv("ZERO_HOME", str(home))
    monkeypatch.setenv("ZERO_ENV", "development")
    monkeypatch.setenv("ZERO_DATABASE_URL", f"sqlite:///{tmp_path / 'engine.db'}")
    return home


def test_non_interactive_setup_stores_secrets_and_writes_config(
    cli_home, api_server, monkeypatch, capsys
):
    from zero.manage.core import probes

    monkeypatch.setattr(probes, "TELEGRAM_API", api_server)

    rc = main(
        [
            "setup",
            "--non-interactive",
            "--step",
            "provider_add.id=fake-primary",
            "--step",
            f"provider_add.base_url={api_server}/v1",
            "--step",
            "provider_add.protocol=openai_compatible",
            "--step",
            f"provider_add.api_key={FAKE_KEY}",
            "--step",
            "telegram_credentials.token=" + FAKE_TOKEN,
            "--step",
            "model_assign.primary_model=fake-mini",
        ]
    )
    assert rc == 0, capsys.readouterr().out

    # 1. config.yaml written and contains ONLY references.
    cfg_text = (cli_home / "config.yaml").read_text(encoding="utf-8")
    assert "sec_" in cfg_text
    assert FAKE_TOKEN not in cfg_text
    assert FAKE_KEY not in cfg_text

    # 2. Draft masks raw secrets too.
    draft_files = list(cli_home.glob("*.draft*")) + list((cli_home).glob("draft*"))
    for path in cli_home.rglob("*draft*"):
        content = path.read_text(encoding="utf-8", errors="ignore")
        assert FAKE_TOKEN not in content, f"raw token leaked into {path.name}"
        assert FAKE_KEY not in content, f"raw key leaked into {path.name}"

    # 3. Secrets resolve back correctly through the encrypted store.
    import yaml

    from zero.app.services import build_services
    from zero.config import Settings
    from zero.persistence.connection import open_database
    from zero.persistence.migrations import apply_migrations

    # A real engine start loads $ZERO_HOME/.env; mirror that here.
    env_file = cli_home / ".env"
    assert env_file.exists(), "wizard must persist the encryption key env"
    env_map = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            env_map[k] = v
    assert "ZERO_SECRET_KEY" in env_map
    key_file = cli_home / "secret.key"
    assert key_file.exists()
    assert key_file.read_text(encoding="utf-8").strip() == env_map["ZERO_SECRET_KEY"]

    settings = Settings.load_for_test(
        database_url=f"sqlite:///{cli_home.parent / 'engine.db'}",
        secret_key=env_map["ZERO_SECRET_KEY"],
    )
    database = open_database(settings)
    apply_migrations(database)
    services = build_services(settings, database)
    project = next(p for p in services.identity.list_projects() if p.name == "Zero Management")

    cfg = yaml.safe_load(cfg_text)
    token_ref = cfg["telegram"]["bot_token_ref"]
    provider_ref = cfg["providers"][0]["api_key_ref"]
    from zero.domain.secrets import SecretReferenceId

    resolved_token = services.secrets.resolve_value(
        project_id=project.id,
        secret_id=SecretReferenceId(token_ref),
        actor_id=project.owner_user_id,
    )
    resolved_key = services.secrets.resolve_value(
        project_id=project.id,
        secret_id=SecretReferenceId(provider_ref),
        actor_id=project.owner_user_id,
    )
    assert resolved_token == FAKE_TOKEN
    assert resolved_key == FAKE_KEY


def test_non_interactive_setup_fails_cleanly_on_bad_token(
    cli_home, api_server, monkeypatch, capsys
):
    from zero.manage.core import probes

    monkeypatch.setattr(probes, "TELEGRAM_API", api_server)
    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "setup",
                "--non-interactive",
                "--step",
                "telegram_credentials.token=not-a-token",
                "--step",
                f"provider_add.base_url={api_server}/v1",
                "--step",
                "provider_add.id=x",
                "--step",
                "provider_add.protocol=openai_compatible",
                "--step",
                f"provider_add.api_key={FAKE_KEY}",
            ]
        )
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "telegram_credentials failed" in err
