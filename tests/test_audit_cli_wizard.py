"""Audit regression tests: D4 (dead CLI commands), D5 (wizard field drops)."""

from __future__ import annotations

import pytest

from zero.manage.cli import main
from zero.manage.services.setup import SetupService
from zero.manage.services.wizard_forms import WIZARD_STEPS


@pytest.fixture
def cli_home(tmp_path, monkeypatch):
    home = tmp_path / "zh"
    monkeypatch.setenv("ZERO_HOME", str(home))
    return home


class TestD4DeadCommands:
    def test_capabilities_show_runs(self, cli_home, capsys):
        assert main(["capabilities", "show"]) == 0
        out = capsys.readouterr().out.strip()
        assert out.startswith("{") or out.startswith("["), "JSON cache dump expected"

    def test_backup_daemon_respects_off_schedule(self, cli_home, capsys):
        from zero.manage.core.config import ConfigService, ZeroConfig

        cfgsvc = ConfigService(cli_home)
        cfg = ZeroConfig()
        cfg.backups.schedule = "off"
        cfgsvc.save(cfg)
        assert main(["backup-daemon"]) == 0
        assert "off" in capsys.readouterr().out

    def test_backup_status_runs(self, cli_home, capsys):
        assert main(["backup-status"]) == 0


class TestD5WizardFieldPersistence:
    """Collected wizard values must survive into the committed config."""

    @staticmethod
    def _setup(tmp_path, monkeypatch):
        monkeypatch.setenv("ZERO_HOME", str(tmp_path / "zh"))
        from zero.manage.core.config import ConfigService

        return SetupService(ConfigService(tmp_path / "zh"), lambda: None)

    def test_fallback_models_csv_persists(self, tmp_path, monkeypatch):
        setup = self._setup(tmp_path, monkeypatch)
        data = setup.resume().setdefault("data", {})
        data["model_assign"] = {
            "primary_model": "m1",
            "fallback_models_csv": " m2 , m3 ",
        }
        setup.cfg.save_draft({"data": data})
        cfg = setup.commit()
        assert cfg.routing.primary_model == "m1"
        assert cfg.routing.fallback_models == ["m2", "m3"]

    def test_updates_auto_apply_persists(self, tmp_path, monkeypatch):
        setup = self._setup(tmp_path, monkeypatch)
        data = setup.resume().setdefault("data", {})
        data["updates"] = {"channel": "beta", "auto_apply": True}
        setup.cfg.save_draft({"data": data})
        cfg = setup.commit()
        assert cfg.updates.auto_apply is True

    def test_agents_default_agent_applies_to_groups(self, tmp_path, monkeypatch):
        setup = self._setup(tmp_path, monkeypatch)
        data = setup.resume().setdefault("data", {})
        data["access_mode"] = {"mode": "groups"}
        data["groups"] = {"confirmed": [{"chat_id": "-100123", "title": "g"}]}
        data["agents"] = {"default_agent": "worker_b"}
        setup.cfg.save_draft({"data": data})
        cfg = setup.commit()
        assert [g.default_agent for g in cfg.access.groups] == ["worker_b"]

    def test_memory_step_no_longer_collects_unwired_field(self):
        fields = {f.name for f in WIZARD_STEPS["memory_storage"].fields}
        assert "compaction_threshold_percent" not in fields

    def test_groups_discovery_uses_token_field_name(self):
        names = {f.name for f in WIZARD_STEPS["groups"].fields}
        assert "token" in names
        assert "discover_token" not in names
