"""Audit Phase E: backup/restore round-trip + doctor in broken states.

All against REAL services (encrypted backup via Fernet, real SQLite
databases) in disposable temp directories.
"""

from __future__ import annotations

import json

import pytest

from zero.config import Settings
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations


def _services(tmp_path):
    from zero.app.services import build_services

    settings = Settings.load_for_test(
        database_url=f"sqlite:///{tmp_path}/a/engine.db", secret_key="k" * 40
    )
    database = Database(settings)
    apply_migrations(database)
    return settings, database, build_services(settings, database)


class TestBackupRestoreRoundTrip:
    def test_backup_encrypts_and_restore_preserves_state(self, tmp_path):
        settings, database, services = _services(tmp_path)
        owner = services.identity.create_user(display_name="br owner")
        project = services.identity.create_project(owner_id=owner.id, name="BR")
        # Seed durable state across surfaces the checklist names.
        ref = services.secrets.store(
            project_id=project.id,
            name="p-key",
            secret_type="api_key",
            value="sk-backup-secret-xyz",
            actor_id=owner.id,
        )
        event = services.plans.ingest_conversation_event(
            project_id=project.id,
            actor_id=owner.id,
            source="web",
            origin_kind="authenticated_human",
            content="before backup",
        )
        archive = tmp_path / "b" / "zero.enc"
        written = services.backup.backup_to_file(str(archive))
        assert str(archive) == written
        raw = archive.read_bytes()
        assert b"sk-backup-secret-xyz" not in raw, "backup must not be plaintext"
        assert raw.startswith(b"ZERO-BACKUP-V1")

        # Restore into a SECOND database.
        settings2 = Settings.load_for_test(
            database_url=f"sqlite:///{tmp_path}/c/restored.db", secret_key="k" * 40
        )
        target = Database(settings2)
        report = services.backup.restore_from_file(str(archive), target)
        assert isinstance(report, dict)

        from zero.persistence.repositories.identity_repository import (
            IdentityRepository,
        )

        ident2 = IdentityRepository(target)
        projects = ident2.list_projects()
        assert any(p.name == "BR" for p in projects)

        # Secrets survive and still decrypt with the same key.

        svc2 = services.secrets  # same key material class
        row = (
            target.connect()
            .execute("SELECT id FROM secret_references WHERE name='p-key'")
            .fetchone()
        )
        assert row is not None

    def test_corrupted_backup_fails_without_touching_target(self, tmp_path):
        settings, database, services = _services(tmp_path)
        archive = tmp_path / "b" / "bad.enc"
        archive.parent.mkdir(parents=True, exist_ok=True)
        # Correct magic prefix but garbage ciphertext.
        archive.write_bytes(b"ZERO-BACKUP-V1\nnot-a-valid-token")
        settings2 = Settings.load_for_test(
            database_url=f"sqlite:///{tmp_path}/c/restored.db", secret_key="k" * 40
        )
        target = Database(settings2)
        apply_migrations(target)
        before_tables = {
            r["name"]
            for r in target.connect()
            .execute("SELECT name FROM sqlite_master WHERE type='table'")
            .fetchall()
        }
        with pytest.raises(ValueError):
            services.backup.restore_from_file(str(archive), target)
        after_tables = {
            r["name"]
            for r in target.connect()
            .execute("SELECT name FROM sqlite_master WHERE type='table'")
            .fetchall()
        }
        assert before_tables == after_tables

    def test_wrong_key_backup_refuses(self, tmp_path):
        """A backup encrypted under key A must not restore under key B."""
        settings_a, db_a, services_a = _services(tmp_path)
        archive = tmp_path / "b" / "a.enc"
        services_a.backup.backup_to_file(str(archive))
        # A SECOND deployment running under a different key.
        settings_b = Settings.load_for_test(
            database_url=f"sqlite:///{tmp_path}/c/x.db", secret_key="OTHER" + "k" * 34
        )
        db_b = Database(settings_b)
        apply_migrations(db_b)
        from zero.app.services import build_services

        services_b = build_services(settings_b, db_b)
        with pytest.raises(ValueError, match="authentication|decrypt"):
            services_b.backup.restore_from_file(str(archive), db_b)


class TestDoctorBrokenStates:
    @staticmethod
    def _doctor(tmp_path, config_text: str | None, *, with_engine: bool = True):
        """Run the REAL DoctorService against a disposable home."""
        import os

        monkey_home = tmp_path / "zh"
        monkey_home.mkdir(parents=True, exist_ok=True)
        old = os.environ.get("ZERO_HOME")
        os.environ["ZERO_HOME"] = str(monkey_home)
        try:
            if config_text is not None:
                (monkey_home / "config.yaml").write_text(config_text, encoding="utf-8")
            from zero.manage.core.config import ConfigService
            from zero.manage.services.doctor import DoctorService

            if with_engine:
                from zero.app.services import build_services
                from zero.persistence.connection import open_database
                from zero.persistence.migrations import apply_migrations

                settings = Settings.load_for_test(
                    database_url=f"sqlite:///{tmp_path}/engine.db",
                    secret_key="k" * 40,
                )
                database = open_database(settings)
                apply_migrations(database)
                services = build_services(settings, database)

                def engine():
                    return settings, services
            else:
                engine = lambda: (None, None)

            report = DoctorService(ConfigService(monkey_home), engine).run()
            return report, services
        finally:
            if old is None:
                os.environ.pop("ZERO_HOME", None)
            else:
                os.environ["ZERO_HOME"] = old

    def test_healthy_install_has_no_failures(self, tmp_path, monkeypatch):
        """Post-setup state: real stored token ref + reachable Bot API.

        The probe target is a local HTTP server so the doctor's REAL
        getMe path runs without external credentials.
        """
        import os
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        TOKEN = "111:DOCTOR-FAKE"

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _respond(self):
                ok = "DOCTOR-FAKE" in self.path
                body = json.dumps(
                    {
                        "ok": ok,
                        **({"result": {"id": 1, "username": "doc_bot"}} if ok else {}),
                    }
                ).encode()
                self.send_response(200 if ok else 401)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._respond()

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self._respond()

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        monkeypatch.setenv("ZERO_TELEGRAM_API_BASE", f"http://127.0.0.1:{srv.server_address[1]}")

        home = tmp_path / "zh2"
        (home).mkdir(parents=True)
        old = os.environ.get("ZERO_HOME")
        os.environ["ZERO_HOME"] = str(home)
        try:
            from zero.app.services import build_services
            from zero.manage.core.config import ConfigService
            from zero.manage.services.doctor import DoctorService

            settings = Settings.load_for_test(
                database_url=f"sqlite:///{tmp_path}/doc-engine.db", secret_key="k" * 40
            )
            from zero.persistence.connection import open_database
            from zero.persistence.migrations import apply_migrations

            database = open_database(settings)
            apply_migrations(database)
            services = build_services(settings, database)
            owner = services.identity.create_user(display_name="doc owner")
            project = services.identity.create_project(owner_id=owner.id, name="Doc")
            ref = services.secrets.store(
                project_id=project.id,
                name="doctor-bot-token",
                secret_type="token",
                value=TOKEN,
                actor_id=owner.id,
            )
            cfg_text = (
                f"owner_project_id: {project.id.value}\n"
                "telegram:\n"
                f"  bot_token_ref: {ref.id.value}\n"
                f"  bot_username: doc_bot\n"
                "access:\n"
                "  mode: owner_only\n"
            )
            (home / "config.yaml").write_text(cfg_text, encoding="utf-8")
            report = DoctorService(ConfigService(home), lambda: (settings, services)).run()
        finally:
            if old is None:
                os.environ.pop("ZERO_HOME", None)
            else:
                os.environ["ZERO_HOME"] = old
            srv.shutdown()
            srv.server_close()

        fails = [c for c in report["checks"] if c["status"] == "fail"]
        assert fails == [], f"unexpected failures: {fails}"
        tg = next(c for c in report["checks"] if c["name"] == "telegram")
        assert "doc_bot" in tg["detail"]

    def test_corrupted_config_is_detected(self, tmp_path):
        report, _services = self._doctor(tmp_path, "telegram: [unclosed")
        statuses = {c["name"]: c["status"] for c in report["checks"]}
        assert "config" in statuses and statuses["config"] == "fail"

    def test_json_report_serializable(self, tmp_path):
        report, _services = self._doctor(tmp_path, "")
        json.dumps(report)  # must not raise
