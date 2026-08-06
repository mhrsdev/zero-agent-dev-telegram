"""Tests for Docker sandbox (skipped if Docker not available)."""
from __future__ import annotations

import shutil

import pytest

from zero.agents.docker_sandbox import DockerSandbox, DockerSandboxSpec, is_docker_available

pytestmark = pytest.mark.skipif(
    not is_docker_available(),
    reason="Docker not available — install docker to run these tests",
)


@pytest.fixture
def spec() -> DockerSandboxSpec:
    return DockerSandboxSpec(
        scope=__import__("zero.core.scope", fromlist=["Scope"]).Scope.development(
            org_id="org_test", workspace_id="ws_test",
            project_id="prj_test", group_id="grp_test", topic_id=1,
        ).with_default_memory_scope(),
        memory_mb=256,
        cpu_seconds=30,
        timeout_seconds=30,
    )


class TestDockerSandbox:
    @pytest.mark.asyncio
    async def test_sandbox_starts_and_stops(self, spec: DockerSandboxSpec) -> None:
        async with DockerSandbox(spec) as sb:
            assert sb.container_id is not None
            assert sb.is_degraded is False

    @pytest.mark.asyncio
    async def test_exec_command(self, spec: DockerSandboxSpec) -> None:
        async with DockerSandbox(spec) as sb:
            exit_code, stdout, stderr = await sb.exec_command("echo hello docker")
            assert exit_code == 0
            assert "hello docker" in stdout

    @pytest.mark.asyncio
    async def test_write_and_read_file(self, spec: DockerSandboxSpec) -> None:
        async with DockerSandbox(spec) as sb:
            await sb.write_file("test.txt", "hello from test")
            content = await sb.read_file("test.txt")
            assert content == "hello from test"

    @pytest.mark.asyncio
    async def test_network_disabled(self, spec: DockerSandboxSpec) -> None:
        """Sandbox has no network access (--network=none)."""
        async with DockerSandbox(spec) as sb:
            # Try to reach external network — should fail.
            exit_code, stdout, stderr = await sb.exec_command(
                "ping -c 1 -W 2 8.8.8.8 2>&1 || echo 'network blocked'"
            )
            # ping should fail (no network).
            assert exit_code != 0 or "network blocked" in stdout


class TestDockerSandboxSpec:
    def test_default_spec_is_secure(self) -> None:
        """Default spec has security hardening."""
        from zero.core.scope import Scope

        spec = DockerSandboxSpec(
            scope=Scope.development(
                org_id="org_x", workspace_id="ws_x",
                project_id="prj_x", group_id="grp_x", topic_id=1,
            ).with_default_memory_scope(),
        )
        assert spec.network_mode == "none"
        assert "ALL" in spec.cap_drop
        assert "no-new-privileges" in spec.security_opt
        assert spec.read_only_root is True
        assert spec.memory_limit == "512m"
