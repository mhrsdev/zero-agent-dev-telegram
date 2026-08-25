"""GAP 3 tests: pluggable sandbox command executors."""

from __future__ import annotations

import os
import sys

import pytest

from zero.app.capabilities import worktree_execution_capability
from zero.app.executors.sandbox import (
    DockerExecutor,
    FirejailExecutor,
    HostBoundedExecutor,
    SandboxUnavailableError,
)
from zero.config import ConfigError, Settings


class TestHostBoundedExecutor:
    def test_runs_command_and_captures_output(self, tmp_path):
        executor = HostBoundedExecutor()
        result = executor.execute(
            [sys.executable, "-c", "print('sandbox-ok')"],
            cwd=str(tmp_path),
            timeout_seconds=30,
            output_limit=64 * 1024,
        )
        assert result.exit_code == 0
        assert "sandbox-ok" in result.stdout
        assert result.timed_out is False

    def test_missing_binary_reports_127(self, tmp_path):
        executor = HostBoundedExecutor()
        result = executor.execute(
            ["definitely-not-a-real-binary-xyz"],
            cwd=str(tmp_path),
            timeout_seconds=10,
            output_limit=4096,
        )
        assert result.exit_code == 127
        assert "Command not found" in result.stderr

    def test_scrubbed_env_exposes_no_host_secrets(self, tmp_path):
        probe = "import json,os;print(json.dumps(dict(os.environ)))"
        executor = HostBoundedExecutor()
        result = executor.execute(
            [sys.executable, "-c", probe],
            cwd=str(tmp_path),
            timeout_seconds=30,
            output_limit=64 * 1024,
        )
        host_secret = os.environ.get("ZERO_OPENAI_API_KEY", "")
        assert host_secret == "" or host_secret not in result.stdout
        # The fixed allow-list is present regardless of the host env.
        assert "/usr/local/bin:/usr/bin:/bin" in result.stdout


class TestDockerArgumentAssembly:
    def test_hardening_flags_present(self, tmp_path):
        executor = DockerExecutor(image="python:3.12-slim")
        args = executor.build_run_args(str(tmp_path))
        assert args[0:2] == ["docker", "run"]
        for flag in (
            "--network",
            "none",
            "--pids-limit",
            "--memory",
            "--cpus",
            "--security-opt",
            "no-new-privileges:true",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "--user",
            "1000:1000",
        ):
            assert flag in args

    def test_single_worktree_bind_mount(self, tmp_path):
        executor = DockerExecutor()
        args = executor.build_run_args(str(tmp_path))
        mounts = [item for item in args if isinstance(item, str) and ":/workspace" in item]
        assert mounts == [f"{tmp_path!s}:/workspace"]
        assert args[args.index("-w") + 1] == "/workspace"

    def test_image_and_command_appended(self, tmp_path):
        executor = DockerExecutor()
        full = executor.build_run_args(str(tmp_path)) + ["echo", "hi"]
        assert full[-2:] == ["echo", "hi"]

    def test_container_name_generated_per_call(self, tmp_path):
        executor = DockerExecutor()

        def name_from(args):
            return args[args.index("--name") + 1]

        name1 = name_from(executor.build_run_args(str(tmp_path)))
        name2 = name_from(executor.build_run_args(str(tmp_path)))
        assert name1.startswith("zero-sbx-")
        assert name2.startswith("zero-sbx-")
        assert name1 != name2

    def test_empty_image_rejected(self):
        with pytest.raises(ValueError):
            DockerExecutor(image="   ")

    def test_docker_probe_honest_when_daemon_missing(self):
        executor = DockerExecutor(docker_bin="docker-definitely-missing-xyz")
        assert executor.available() is False


class TestFirejailBackend:
    def test_argument_assembly(self, tmp_path):
        executor = FirejailExecutor()
        args = executor.build_run_args(str(tmp_path))
        assert args[0] == "firejail"
        assert "--net=none" in args
        assert "--private-tmp" in args
        assert any(a.startswith("--whitelist=") for a in args)

    def test_missing_binary_reports_unavailable(self):
        executor = FirejailExecutor(firejail_bin="firejail-definitely-missing")
        assert executor.available() is False


class TestExecutorSelection:
    def test_none_selects_nothing(self):
        from zero.app.executors.sandbox import build_command_executor

        assert build_command_executor("none", sandbox_image="x") is None

    def test_unknown_backend_rejected(self):
        from zero.app.executors.sandbox import build_command_executor

        with pytest.raises(ValueError):
            build_command_executor("bubblewrap", sandbox_image="x")

    def test_unavailable_backend_fails_closed(self):
        from zero.app.executors.sandbox import build_command_executor

        with pytest.raises(SandboxUnavailableError):
            build_command_executor("docker", sandbox_image="x")

    def test_firejail_on_non_posix_refused(self, monkeypatch):
        import zero.app.executors.sandbox as sb

        monkeypatch.setattr(sb.os, "name", "nt")
        with pytest.raises(SandboxUnavailableError):
            sb.build_command_executor("firejail", sandbox_image="x")


_PROD_ENV = {
    "ZERO_DATABASE_URL": "sqlite:////tmp/zero-prod.db",
    "ZERO_SECRET_KEY": "s" * 40,
    "ZERO_BOOTSTRAP_TOKEN": "b" * 40,
}


class TestConfigGating:
    def _load(self, monkeypatch, **env):
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return Settings.load()

    def test_default_is_none_with_default_image(self, monkeypatch):
        monkeypatch.delenv("ZERO_SANDBOX_EXECUTOR", raising=False)
        monkeypatch.delenv("ZERO_SANDBOX_IMAGE", raising=False)
        settings = self._load(monkeypatch, ZERO_ENV="development")
        assert settings.sandbox_executor == "none"
        assert settings.sandbox_image == "python:3.12-slim"

    def test_invalid_executor_rejected_at_load(self, monkeypatch):
        with pytest.raises(ConfigError):
            self._load(monkeypatch, ZERO_ENV="development", ZERO_SANDBOX_EXECUTOR="nsjail")

    def test_blank_image_rejected(self, monkeypatch):
        with pytest.raises(ConfigError):
            self._load(monkeypatch, ZERO_ENV="development", ZERO_SANDBOX_IMAGE="  ")

    def test_production_still_refuses_without_backend(self, monkeypatch):
        with pytest.raises(ConfigError, match="isolation backend"):
            self._load(
                monkeypatch,
                ZERO_ENV="production",
                ZERO_WORKTREE_ISOLATION_MODE="host_bounded",
                ZERO_SANDBOX_EXECUTOR="none",
                **_PROD_ENV,
            )

    def test_production_accepts_docker_backend(self, monkeypatch):
        settings = self._load(
            monkeypatch,
            ZERO_ENV="production",
            ZERO_WORKTREE_ISOLATION_MODE="host_bounded",
            ZERO_SANDBOX_EXECUTOR="docker",
            **_PROD_ENV,
        )
        assert settings.worktree_isolation_mode == "host_bounded"
        assert settings.sandbox_executor == "docker"


class TestCapabilityReporting:
    def _load(self, monkeypatch, **env):
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return Settings.load()

    def test_dev_host_bounded_reports_host_bounded(self, test_settings):
        cap = worktree_execution_capability(test_settings)
        assert cap.status == "available"
        assert "host_bounded" in cap.detail

    def test_disabled_mode_reports_disabled(self, monkeypatch):
        settings = self._load(
            monkeypatch,
            ZERO_ENV="development",
            ZERO_WORKTREE_ISOLATION_MODE="disabled",
        )
        cap = worktree_execution_capability(settings)
        assert cap.status == "unavailable"

    def test_production_with_docker_reports_sandbox(self, monkeypatch):
        settings = self._load(
            monkeypatch,
            ZERO_ENV="production",
            ZERO_DATABASE_URL="sqlite:////tmp/zero-prod.db",
            ZERO_SECRET_KEY="s" * 40,
            ZERO_BOOTSTRAP_TOKEN="b" * 40,
            ZERO_WORKTREE_ISOLATION_MODE="host_bounded",
            ZERO_SANDBOX_EXECUTOR="docker",
        )
        cap = worktree_execution_capability(settings)
        assert cap.status == "available"
        assert "docker" in cap.detail

    def test_dev_with_docker_selected_reports_sandbox(self, monkeypatch):
        settings = self._load(
            monkeypatch,
            ZERO_ENV="development",
            ZERO_WORKTREE_ISOLATION_MODE="host_bounded",
            ZERO_SANDBOX_EXECUTOR="docker",
        )
        cap = worktree_execution_capability(settings)
        assert cap.status == "available"
        assert "sandbox=docker" in cap.detail


class TestWorktreeServiceDelegation:
    @staticmethod
    def _services():
        from zero.app.services import build_services
        from zero.persistence.connection import Database
        from zero.persistence.migrations import apply_migrations

        settings = Settings.load_for_test(worktree_isolation_mode="host_bounded")
        database = Database(settings)
        apply_migrations(database)
        return build_services(settings, database), settings

    def test_service_routes_through_injected_executor(self, tmp_path):
        from zero.app.executors.sandbox import ExecResult

        services, _settings = self._services()
        executed: list[tuple[list[str], str]] = []

        class StubExecutor:
            name = "stub"

            def available(self):
                return True

            def execute(self, argv, *, cwd, timeout_seconds, output_limit):
                executed.append((argv, cwd))
                return ExecResult(0, False, "stub-out", "")

        services.worktree._command_executor = StubExecutor()
        code, _timed_out, out, _err = services.worktree._run_bounded_process(
            ["echo", "hi"],
            cwd=str(tmp_path),
            timeout_seconds=10,
        )
        assert (code, _timed_out, out, _err) == (0, False, "stub-out", "")
        assert executed == [(["echo", "hi"], str(tmp_path))]

    def test_service_without_executor_keeps_host_behavior(self, tmp_path):
        services, _settings = self._services()
        assert services.worktree._command_executor is None
        code, _timed_out, out, _err = services.worktree._run_bounded_process(
            [sys.executable, "-c", "print('direct')"],
            cwd=str(tmp_path),
            timeout_seconds=30,
        )
        assert code == 0
        assert "direct" in out
