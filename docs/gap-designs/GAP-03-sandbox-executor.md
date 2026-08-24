# GAP 3 Design — Production Sandbox Executor

Status: design accepted · Phase 4

## Problem

`host_bounded` worktree execution runs commands directly on the host
with a scrubbed environment (`WorktreeService._run_bounded_process`).
Production refuses it entirely
(`config._enforce_production_rules`, `capabilities.worktree_execution_capability`)
because there is no container/namespace isolation backend.

## Architecture

A pluggable executor protocol behind which all command execution goes.
Callers (worktree service) never know the backend.

```python
@dataclass(frozen=True)
class ExecResult:
    exit_code: int | None
    timed_out: bool
    stdout: str
    stderr: str

class CommandExecutor(Protocol):
    name: str                      # "host_bounded" | "docker" | "firejail"
    def execute(self, argv, cwd, timeout, output_limit) -> ExecResult: ...
    def available(self) -> bool:   # startup probe
```

Implementations in `src/zero/app/executors/`:

1. **HostBoundedExecutor** — wraps today's `_run_bounded_process`
   logic (clean env, reader threads, SIGKILL on timeout). Dev/test only;
   production refuses when selected.
2. **DockerExecutor** — `docker run --rm` with:
   - image from `ZERO_SANDBOX_IMAGE` (default `python:3.12-slim`),
     pinned by digest when provided as `repo@sha256:…`;
   - `--network none`, `--pids-limit 128`, `--memory 512m`,
     `--cpus 1.0`;
   - `--security-opt no-new-privileges:true`,
     `--cap-drop ALL --cap-add CHOWN --cap-add SETUID`;
   - worktree dir bind-mounted read-write at `/workspace`
     (`-v {cwd}:/workspace`), `--user` non-root (1000:1000),
     `--workdir /workspace`; nothing else mounted; env scrubbed to the
     same fixed allow-list as host_bounded;
   - CLI-level `--stop-timeout` semantics emulated: docker run under
     our own watchdog thread → on timeout `docker kill` the container,
     mark timed_out. Output capped via bounded readers (same as now).
3. **FirejailExecutor** (Linux-only) — `firejail --net=none
   --private-tmp --private-dev --read-only=/ /usr/bin …` with worktree
   path whitelisted read-write (`--whitelist`), caps preserved from
   host policy.

### Selection and wiring

- Config: `ZERO_SANDBOX_EXECUTOR = none | docker | firejail`
  (default `none`). New `Settings.sandbox_executor` field.
- Production rules change:
  `host_bounded` remains refused in production **unless**
  `sandbox_executor in {"docker","firejail"}` AND that backend's
  `available()` probe passes at composition time; then commands run
  through the sandboxed backend while the *policy* layer stays
  identical (`_validate_command` unchanged).
  When `sandbox_executor == "none"` production continues to refuse.
- Startup validation: `build_command_executor(settings)` probes Docker
  socket (`docker version --format {{.Server.Version}}`, 5 s timeout)
  or firejail binary presence (`shutil.which`); failure raises
  `ConfigError` (fail closed) rather than degrading silently.
- Capability report (`manage/core/capabilities.py`) honestly reports
  which executor is active: `"worktree_execution": available` plus new
  `"executor": <name>` detail.

## Data model changes

None.

## API surface

- `/capabilities` gains `"executor"` field inside the worktree
  execution capability details.
- No other route changes; `WorktreeService.run_command` contract is
  unchanged.

## Security considerations

- Container escape surface minimized: no network, no extra caps,
  non-root uid, single bind mount of the worktree only.
- The docker socket itself is NOT mounted into sandboxes.
- Image pinning encouraged but not forced; digest form validated.
- Windows hosts: docker backend works through Docker Desktop;
  firejail backend rejects selection on non-POSIX at load time.
- Timeouts enforced outside the container so a hung container cannot
  outlive the lease.

## Test strategy

- Unit tests with mocked subprocess/docker CLI: argument assembly for
  both backends (assert exact flag sets), timeout→kill mapping, output
  capping, env scrubbing, non-zero exit propagation.
- Executor-selection tests: production + `none` refuses; production +
  docker (probe mocked OK) accepts and reports executor "docker";
  unavailable backend fails closed with ConfigError.
- Integration tests (skipif no Docker socket): run `python -c
  print(...)` in a temp worktree; assert isolation flags prevent
  network (`socket.create_connection` fails inside) and host fs access.

## Migration path

New package + settings field; WorktreeService gains an optional
executor constructor parameter defaulting to the built-in host path so
existing tests are untouched.

## Rollback strategy

Set `ZERO_SANDBOX_EXECUTOR=none` (or remove config); behavior returns
to current refusal-in-production exactly.

## Acceptance criteria

- Production can enable host_bounded execution when a sandbox executor
  is configured and probed healthy.
- Sandboxed commands cannot reach the network or host filesystem
  beyond the mounted worktree.
- Capability report reflects the active executor truthfully.
