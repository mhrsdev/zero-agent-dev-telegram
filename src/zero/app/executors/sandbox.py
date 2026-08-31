"""Pluggable sandboxed command execution backends (GAP 3).

Per ``docs/gap-designs/GAP-03-sandbox-executor.md``: worktree commands
run through a :class:`CommandExecutor` so callers never know the
backend. ``host_bounded`` is the historical dev/test path; ``docker``
and ``firejail`` are genuine isolation backends that production can
enable.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class ExecResult:
    """One bounded command execution outcome."""

    exit_code: int | None
    timed_out: bool
    stdout: str
    stderr: str


class CommandExecutor(Protocol):
    """The contract every execution backend implements."""

    name: str

    def execute(
        self,
        argv: list[str],
        *,
        cwd: str,
        timeout_seconds: int,
        output_limit: int,
    ) -> ExecResult: ...

    def available(self) -> bool:
        """Cheap probe used at startup (fail closed when False)."""


#: The fixed, scrubbed environment shared by every backend. No host
#: environment variables pass through — this list IS the environment.
def scrubbed_env(cwd: str) -> dict[str, str]:
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": cwd,
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    # Live-run fix (2026-08-31, B11): the engine runs inside the project
    # virtualenv (``.venv``), but this scrubbed PATH resolved bare
    # ``python3``/``pytest`` to the SYSTEM interpreter — which has no
    # pytest and no project stack. The evidence test command therefore
    # failed with "/usr/bin/python3: No module named pytest" on EVERY
    # attempt regardless of what the agent produced. When the engine
    # itself is venv-mounted, expose that venv to child commands: the
    # same interpreter the operator validated is used, isolation is
    # unchanged (still a fixed env — no host variables leak through),
    # and the allowlist semantics are identical.
    if sys.prefix != sys.base_prefix:  # engine is venv-mounted
        venv_bin = os.path.join(sys.prefix, "bin")
        env["VIRTUAL_ENV"] = sys.prefix
        env["PATH"] = venv_bin + os.pathsep + env["PATH"]
    return env


#: Bare interpreter names that resolve through PATH inside the scrubbed
#: environment. Only BARE names are rewritten — an explicit absolute or
#: relative interpreter path is honored as written.
_INTERPRETER_BASENAMES = frozenset(
    {
        "python",
        "python3",
        f"python{sys.version_info.major}",
        f"python{sys.version_info.major}.{sys.version_info.minor}",
        "pytest",
    }
)


def host_interpreter_argv(argv: list[str]) -> list[str]:
    """Route bare CPython/pytest invocations to the engine's interpreter.

    Live-run fix (2026-08-31, B11): with ``host_bounded`` isolation the
    scrubbed env resolves ``python3 -m pytest`` to the system python,
    which typically lacks the project's test stack, so the configured
    evidence test command could never pass. When the engine runs inside
    a virtualenv, a bare ``python``/``python3``/``python3.N`` argv[0]
    is rewritten to :data:`sys.executable` and a bare ``pytest`` to
    ``[sys.executable, "-m", "pytest"]`` so the verified environment
    executes the command. Absolute interpreter paths are honored as
    written, non-venv deployments are untouched, and this helper must
    NEVER be applied on container backends (the container interpreter
    differs from the host venv).
    """
    if not argv or sys.prefix == sys.base_prefix:
        return argv
    base = os.path.basename(argv[0])
    if base not in _INTERPRETER_BASENAMES:
        return argv
    if os.path.sep in argv[0] or (os.altsep or "") and os.path.altsep in argv[0]:
        # Explicit path: honor it exactly as configured.
        return argv
    if base == "pytest":
        return [sys.executable, "-m", "pytest", *argv[1:]]
    return [sys.executable, *argv[1:]]


#: Host variables the ``docker`` CLI itself needs to reach the daemon.
#: The wrapper process gets these and nothing else, so host credentials
#: and tokens never reach the CLI's environment; the container's own
#: environment is built separately from explicit ``-e`` flags.
_DOCKER_CLI_PASSTHROUGH = (
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_CONFIG",
    "DOCKER_CERT_PATH",
    "DOCKER_TLS",
    "DOCKER_TLS_VERIFY",
    "SYSTEMROOT",
    "WINDIR",
    "USERPROFILE",
    "TEMP",
    "TMP",
)


def docker_cli_env() -> dict[str, str]:
    """Minimal environment for the ``docker`` client process itself."""
    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LANG": "C",
        "LC_ALL": "C",
    }
    for name in _DOCKER_CLI_PASSTHROUGH:
        value = os.environ.get(name)
        if value:
            env[name] = value
    return env


def run_bounded_process(
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    timeout_seconds: int,
    output_limit: int,
    kill_callbacks: tuple[Callable[[], None], ...] = (),
) -> ExecResult:
    """Run one command with bounded output and process-group cleanup.

    This is the single bounded-execution primitive; backends wrap their
    CLI around it. ``kill_callbacks`` run after the group SIGKILL so a
    backend can tear down its own container/jail.
    """
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError:
        return ExecResult(127, False, "", f"Command not found: {argv[0]}")

    output: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}

    def drain(name: str, stream) -> None:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                return
            remaining = output_limit - len(output[name])
            if remaining > 0:
                output[name].extend(chunk[:remaining])

    readers = [
        threading.Thread(target=drain, args=(name, stream), daemon=True)
        for name, stream in (
            ("stdout", proc.stdout),
            ("stderr", proc.stderr),
        )
        if stream is not None
    ]
    for reader in readers:
        reader.start()
    timed_out = False
    try:
        exit_code = proc.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        exit_code = None
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        for callback in kill_callbacks:
            try:
                callback()
            except OSError:
                pass
    for reader in readers:
        reader.join(timeout=2)
    if proc.stdout is not None:
        proc.stdout.close()
    if proc.stderr is not None:
        proc.stderr.close()

    marker = f"\n[output truncated after {output_limit} bytes]"

    def decode(name: str) -> str:
        raw = bytes(output[name])
        truncated = len(raw) >= output_limit
        if truncated:
            raw = raw[: max(0, output_limit - len(marker))]
            return raw.decode("utf-8", errors="replace") + marker
        return raw.decode("utf-8", errors="replace")

    stderr = decode("stderr")
    if timed_out:
        timeout_marker = f"\n[Command timed out after {timeout_seconds}s]"
        if len(stderr.encode("utf-8")) + len(timeout_marker.encode("utf-8")) <= output_limit:
            stderr += timeout_marker
    return ExecResult(exit_code, timed_out, decode("stdout"), stderr)


class HostBoundedExecutor:
    """The historical direct-host path with a scrubbed environment.

    Dev/test only: production refuses this backend (config layer).
    """

    name = "host_bounded"

    def available(self) -> bool:
        return True

    def execute(
        self,
        argv: list[str],
        *,
        cwd: str,
        timeout_seconds: int,
        output_limit: int,
    ) -> ExecResult:
        return run_bounded_process(
            argv,
            cwd=cwd,
            env=scrubbed_env(cwd),
            timeout_seconds=timeout_seconds,
            output_limit=output_limit,
        )


def _resolve_worktree_mount(cwd: str) -> tuple[str, str]:
    """Return (host_path_for_cli, container_mount)."""
    resolved = str(Path(cwd).resolve())
    return resolved, "/workspace"


class DockerExecutor:
    """Run commands inside a pinned, resource-limited container.

    Hardening flags (see GAP 03 design): no network, pid/memory/cpu
    caps, no-new-privileges, all capabilities dropped except
    CHOWN/SETUID, non-root uid, and exactly one bind mount — the
    worktree, read-write. Nothing else from the host is visible.
    """

    name = "docker"

    def __init__(
        self,
        *,
        image: str = "python:3.12-slim",
        docker_bin: str = "docker",
        cpus: float = 1.0,
        memory: str = "512m",
        pids_limit: int = 128,
        uid: str = "1000:1000",
        timeout_grace_seconds: int = 5,
    ) -> None:
        if not image or not image.strip():
            raise ValueError("sandbox image must not be empty")
        self._image = image.strip()
        self._docker_bin = docker_bin
        self._cpus = cpus
        self._memory = memory
        self._pids_limit = pids_limit
        self._uid = uid
        self._grace = timeout_grace_seconds

    def build_run_args(self, cwd: str, image: str | None = None) -> list[str]:
        """The hardened `docker run` argument prefix (pure, testable)."""
        host_path, mount = _resolve_worktree_mount(cwd)
        return [
            self._docker_bin,
            "run",
            "--rm",
            "--name",
            self._container_name(),
            "--network",
            "none",
            "--pids-limit",
            str(self._pids_limit),
            "--memory",
            self._memory,
            "--cpus",
            str(self._cpus),
            "--security-opt",
            "no-new-privileges:true",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "CHOWN",
            "--cap-add",
            "SETUID",
            "--user",
            self._uid,
            "-v",
            f"{host_path}:{mount}",
            "-w",
            mount,
            "-e",
            f"PATH={scrubbed_env(cwd)['PATH']}",
            "-e",
            "LANG=C",
            "-e",
            "LC_ALL=C",
            image or self._image,
        ]

    def _container_name(self) -> str:
        import uuid

        return f"zero-sbx-{uuid.uuid4().hex[:16]}"

    def available(self) -> bool:
        """Probe the Docker daemon cheaply (5 s budget)."""
        import subprocess as sp

        try:
            proc = sp.run(
                [self._docker_bin, "version", "--format", "{{.Server.Version}}"],
                capture_output=True,
                check=False,
                timeout=5.0,
            )
        except (OSError, sp.TimeoutExpired):
            return False
        return proc.returncode == 0

    def execute(
        self,
        argv: list[str],
        *,
        cwd: str,
        timeout_seconds: int,
        output_limit: int,
    ) -> ExecResult:
        full_argv = self.build_run_args(cwd) + list(argv)
        container_name = full_argv[full_argv.index("--name") + 1]

        def kill_container() -> None:
            import subprocess as sp

            try:
                sp.run(
                    [self._docker_bin, "kill", container_name],
                    capture_output=True,
                    check=False,
                    timeout=10.0,
                )
            except (OSError, sp.TimeoutExpired):
                pass

        result = run_bounded_process(
            full_argv,
            cwd=cwd,
            env=docker_cli_env(),
            timeout_seconds=timeout_seconds + self._grace,
            output_limit=output_limit,
            kill_callbacks=(kill_container,),
        )
        # Report the caller-requested timeout semantics regardless of
        # the internal grace window added for teardown.
        return result


class FirejailExecutor:
    """Linux-only firejail wrapper: no network, private tmp/dev."""

    name = "firejail"

    def __init__(self, *, firejail_bin: str = "firejail") -> None:
        self._bin = firejail_bin

    def build_run_args(self, cwd: str) -> list[str]:
        return [
            self._bin,
            "--quiet",
            "--net=none",
            "--private-tmp",
            "--private-dev",
            "--nodbus",
            "--nou2f",
            f"--whitelist={cwd}",
            f"--cd={cwd}",
        ]

    def available(self) -> bool:
        import shutil

        return shutil.which(self._bin) is not None

    def execute(
        self,
        argv: list[str],
        *,
        cwd: str,
        timeout_seconds: int,
        output_limit: int,
    ) -> ExecResult:
        full_argv = self.build_run_args(cwd) + ["--"] + list(argv)
        return run_bounded_process(
            full_argv,
            cwd=cwd,
            env=scrubbed_env(cwd),
            timeout_seconds=timeout_seconds,
            output_limit=output_limit,
        )


class SandboxUnavailableError(RuntimeError):
    """A configured sandbox backend failed its startup probe."""


def build_command_executor(sandbox_executor: str, *, sandbox_image: str):
    """Resolve the configured backend and probe it (fail closed).

    Returns None for ``none`` (execution refused upstream), otherwise a
    probed executor instance. Raises SandboxUnavailableError when the
    configured backend cannot actually run here.
    """
    normalized = (sandbox_executor or "none").strip().lower()
    if normalized == "none":
        return None
    executor: CommandExecutor
    if normalized == "docker":
        executor = DockerExecutor(image=sandbox_image)
    elif normalized == "firejail":
        if os.name != "posix":
            raise SandboxUnavailableError(
                "firejail sandbox requires a POSIX host; configure ZERO_SANDBOX_EXECUTOR=docker"
            )
        executor = FirejailExecutor()
    else:
        raise ValueError("ZERO_SANDBOX_EXECUTOR must be one of none, docker, firejail")
    if not executor.available():
        raise SandboxUnavailableError(
            f"sandbox executor {normalized!r} is configured but unavailable on this host"
        )
    return executor


__all__ = [
    "CommandExecutor",
    "DockerExecutor",
    "ExecResult",
    "FirejailExecutor",
    "HostBoundedExecutor",
    "SandboxUnavailableError",
    "build_command_executor",
    "docker_cli_env",
    "run_bounded_process",
    "scrubbed_env",
]
