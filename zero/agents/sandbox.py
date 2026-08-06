"""Zero v2 sandbox — ADR T-8.2.

File isolation, network policy (default deny), time/memory/CPU caps, cleanup
after run. If isolated runtime unavailable, capability ``degraded``, high-risk
agents not run — **never falls back to no-sandbox**.
"""
from __future__ import annotations

import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from zero.core.errors import SandboxError
from zero.core.scope import Scope

__all__ = [
    "Sandbox",
    "SandboxError",
    "SandboxSpec",
    "SandboxUnavailableError",
]


@dataclass(frozen=True, slots=True)
class SandboxSpec:
    """Specification for a sandbox run."""

    scope: Scope
    memory_mb: int = 512
    cpu_seconds: int = 600
    timeout_seconds: int = 1800
    network_enabled: bool = False  # default deny
    work_dir: Path | None = None  # None → temp dir

    def __post_init__(self) -> None:
        if self.memory_mb <= 0:
            raise SandboxError(f"memory_mb must be positive, got {self.memory_mb}")
        if self.cpu_seconds <= 0:
            raise SandboxError(f"cpu_seconds must be positive, got {self.cpu_seconds}")
        if self.timeout_seconds <= 0:
            raise SandboxError(f"timeout_seconds must be positive, got {self.timeout_seconds}")


class SandboxUnavailableError(SandboxError):
    """Raised when sandbox runtime is unavailable and the operation cannot proceed safely."""


@dataclass(slots=True)
class Sandbox:
    """A file-isolated workspace for agent execution.

    This is the low-risk sandbox using temp directories. For full isolation
    (network, CPU, memory caps) use :class:`zero.agents.docker_sandbox.DockerSandbox`
    which runs commands inside a Docker container with ``--network none``,
    ``--cap-drop ALL``, and ``--security-opt no-new-privileges``.

    Usage:
        >>> async with Sandbox(spec) as sb:
        ...     # work in sb.work_dir
        ...     pass
        >>> # work_dir is cleaned up automatically
    """

    spec: SandboxSpec
    work_dir: Path = field(init=False)
    _entered: bool = False
    _cleaned: bool = False
    id: str = field(default_factory=lambda: f"sbx_{uuid.uuid4().hex[:12]}")

    def __post_init__(self) -> None:
        if self.spec.work_dir is not None:
            self.work_dir = self.spec.work_dir
            self.work_dir.mkdir(parents=True, exist_ok=True)
        else:
            # Create a unique temp dir.
            self.work_dir = Path(tempfile.mkdtemp(prefix=f"zero-sandbox-{self.id}-"))

    @property
    def is_degraded(self) -> bool:
        """True if sandbox cannot guarantee full isolation.

        In degraded mode, high-risk agent types (security, release) are
        refused — they only run in full-isolation sandboxes.
        """
        # Temp-dir sandbox: file isolation is provided, but
        # network/cpu/memory caps are not enforced (DockerSandbox enforces those).
        return True

    async def __aenter__(self) -> Sandbox:
        self._entered = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object | None,
    ) -> None:
        await self.cleanup()

    async def cleanup(self) -> None:
        if self._cleaned or not self._entered:
            return
        try:
            if self.spec.work_dir is None:
                # Only delete temp dirs we created.
                shutil.rmtree(self.work_dir, ignore_errors=True)
        finally:
            self._cleaned = True

    def require_full_isolation(self, *, agent_type: str) -> None:
        """Refuse to run high-risk agent types in degraded mode.

        Per ADR T-8.2: "if isolated runtime unavailable, capability ``degraded``,
        high-risk agents not run — never falls back to no-sandbox".
        """
        if self.is_degraded and agent_type in ("security", "release"):
            raise SandboxUnavailableError(
                f"agent type {agent_type!r} requires full isolation — sandbox is degraded. "
                "Refusing to run (no fallback to no-sandbox)."
            )
