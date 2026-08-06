"""Docker-based sandbox — replaces the temp-dir Sandbox.

Per ADR T-8.2:
    - File isolation via Docker container
    - Network policy (default deny via --network=none)
    - Time/memory/CPU caps via Docker resource limits
    - Cleanup after run (container removed)
    - If Docker unavailable, capability ``degraded``; high-risk agents not run
    - **never falls back to no-sandbox** for security/release agent types

Uses ``docker`` CLI via subprocess (no Docker SDK dependency).
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from zero.agents.sandbox import Sandbox, SandboxError, SandboxSpec, SandboxUnavailableError
from zero.core.logging import get_logger

__all__ = ["DockerSandbox", "DockerSandboxSpec", "is_docker_available"]

_log = get_logger("zero.agents.sandbox.docker")


def is_docker_available() -> bool:
    """Check if Docker is installed and the daemon is running."""
    docker_path = shutil.which("docker")
    if docker_path is None:
        return False
    # Check daemon.
    import subprocess  # noqa: PLC0415

    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=5,
            check=False,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


@dataclass(frozen=True, slots=True)
class DockerSandboxSpec(SandboxSpec):
    """Extended SandboxSpec for Docker-based execution."""

    image: str = "python:3.12-slim"
    work_dir_mount: str = "/workspace"
    network_mode: str = "none"  # default deny
    cpu_quota: int = 100_000  # 1 CPU (100ms per 100ms period)
    memory_limit: str = "512m"
    pids_limit: int = 100
    tmpfs_size: str = "100m"
    read_only_root: bool = True
    # Capabilities to drop (security hardening).
    cap_drop: tuple[str, ...] = ("ALL",)
    security_opt: tuple[str, ...] = ("no-new-privileges",)


class DockerSandbox(Sandbox):
    """Docker-based sandbox for full file/network/process isolation.

    Lifecycle:
        1. Create temp workdir on host
        2. ``docker run -d`` a container with the workdir mounted
        3. Execute commands via ``docker exec``
        4. On cleanup: ``docker rm -f`` the container + delete temp workdir

    Usage:
        >>> async with DockerSandbox(spec) as sb:
        ...     exit_code, output = await sb.exec_command("python script.py")
    """

    def __init__(self, spec: DockerSandboxSpec) -> None:
        # Don't call super().__init__ — we manage work_dir differently.
        self.spec: DockerSandboxSpec = spec
        self._container_id: str | None = None
        self._entered = False
        self._cleaned = False
        self._work_dir_host = Path(tempfile.mkdtemp(prefix=f"zero-docker-{uuid.uuid4().hex[:8]}-"))
        self.id = f"dksbx_{uuid.uuid4().hex[:12]}"

    @property
    def work_dir(self) -> Path:  # type: ignore[override]
        return self._work_dir_host

    @property
    def is_degraded(self) -> bool:
        """DockerSandbox is never degraded (full isolation when available)."""
        return False

    @property
    def container_id(self) -> str | None:
        return self._container_id

    async def __aenter__(self) -> DockerSandbox:
        await self._start_container()
        self._entered = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object | None,
    ) -> None:
        await self.cleanup()

    async def _start_container(self) -> None:
        """Start the Docker container."""
        if not is_docker_available():
            raise SandboxUnavailableError(
                "Docker is not available — cannot start DockerSandbox. "
                "Install Docker or use the temp-dir Sandbox for low-risk agents."
            )

        spec: DockerSandboxSpec = self.spec
        cmd = [
            "docker", "run", "-d",
            "--name", f"zero-sandbox-{self.id}",
            "--network", spec.network_mode,
            "--memory", spec.memory_limit,
            "--cpu-quota", str(spec.cpu_quota),
            "--pids-limit", str(spec.pids_limit),
            "--tmpfs", f"/tmp:{spec.tmpfs_size}",
            "--read-only" if spec.read_only_root else "",
            "-v", f"{self._work_dir_host}:{spec.work_dir_mount}",
            "-w", spec.work_dir_mount,
        ]
        # Add cap-drop.
        for cap in spec.cap_drop:
            cmd.extend(["--cap-drop", cap])
        # Add security-opt.
        for opt in spec.security_opt:
            cmd.extend(["--security-opt", opt])
        # Filter empty strings.
        cmd = [c for c in cmd if c]
        # Image + sleep infinity (keep container alive).
        cmd.extend([spec.image, "sleep", "infinity"])

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise SandboxError(
                f"docker run failed (exit {proc.returncode}): "
                f"{stderr.decode('utf-8', errors='replace')[:500]}"
            )
        self._container_id = stdout.decode("utf-8", errors="replace").strip()
        _log.info(f"Docker sandbox {self.id} started: container={self._container_id}")

    async def exec_command(
        self,
        command: str,
        *,
        timeout_seconds: int | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str, str]:
        """Execute a command inside the container.

        Returns (exit_code, stdout, stderr).
        """
        if self._container_id is None:
            raise SandboxError("container not started — use async with")

        spec: DockerSandboxSpec = self.spec
        cmd = [
            "docker", "exec",
            "-w", cwd or spec.work_dir_mount,
        ]
        for k, v in (env or {}).items():
            cmd.extend(["-e", f"{k}={v}"])
        cmd.append(self._container_id)
        # Use sh -c so shell features (pipes, redirection) work.
        cmd.extend(["sh", "-c", command])

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout_seconds or self.spec.timeout_seconds,
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise SandboxError(
                f"command timed out after {timeout_seconds or self.spec.timeout_seconds}s"
            ) from None

        return (
            proc.returncode or 0,
            stdout_b.decode("utf-8", errors="replace"),
            stderr_b.decode("utf-8", errors="replace"),
        )

    async def write_file(self, path: str, content: str) -> None:
        """Write a file inside the container's workdir."""
        # Write to host mount (simpler than docker cp).
        host_path = self._work_dir_host / path.lstrip("/")
        host_path.parent.mkdir(parents=True, exist_ok=True)
        host_path.write_text(content, encoding="utf-8")

    async def read_file(self, path: str) -> str:
        """Read a file from the container's workdir."""
        host_path = self._work_dir_host / path.lstrip("/")
        return host_path.read_text(encoding="utf-8", errors="replace")

    async def cleanup(self) -> None:
        """Remove the container and the host workdir."""
        if self._cleaned:
            return
        self._cleaned = True

        # Stop + remove container.
        if self._container_id is not None:
            for cmd in (
                ["docker", "rm", "-f", self._container_id],
            ):
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.communicate()
            _log.info(f"Docker sandbox {self.id} cleaned up")
            self._container_id = None

        # Delete host workdir.
        if self._work_dir_host.exists():
            shutil.rmtree(self._work_dir_host, ignore_errors=True)

    def require_full_isolation(self, *, agent_type: str) -> None:
        """Refuse to run high-risk agent types in degraded mode.

        DockerSandbox is never degraded, so this always passes.
        """
        if self.is_degraded and agent_type in ("security", "release"):
            raise SandboxUnavailableError(
                f"agent type {agent_type!r} requires full isolation — sandbox is degraded. "
                "Refusing to run (no fallback to no-sandbox)."
            )
