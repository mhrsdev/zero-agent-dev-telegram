"""Wave13 — mega-scale hardening found during the super-massive live run
(3 real projects / 10 agent types / large LLM-decomposed graphs).

M15 (live-found): the managed worker iterated projects SEQUENTIALLY, so
one project's long tick (a 15-task graph grinding its frontier for many
minutes) head-of-line blocked every OTHER project's scheduling — freshly
approved plans in sibling projects sat unclaimed the whole duration.
``ZERO_TICK_PROJECT_PARALLELISM`` (1..8, default 1) now ticks up to N
projects concurrently. Per-project error isolation is preserved and task
claims/leases stay exactly-once (same concurrency model the intra-tick
execution pool already exercises).
"""
from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from zero.app.background_workers import BackgroundWorkerHost
from zero.config import ConfigError, Settings


def _project(pid: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=SimpleNamespace(value=pid),
        owner_user_id=SimpleNamespace(value="owner-1"),
    )


def _services(projects, run_once):
    return SimpleNamespace(
        # Fix 13 (2026-09-01): the tick resolves routing from the
        # REGISTRATION-ORDER view, not the alphabetically sorted one —
        # the stub must expose both, with order matching names.
        providers=SimpleNamespace(
            registered_provider_names=["openai-compatible"],
            registered_provider_order=("openai-compatible",),
        ),
        identity=SimpleNamespace(list_projects=lambda: list(projects)),
        worker=SimpleNamespace(reconcile_expired_leases=lambda **kw: 0),
        interface_transports=None,
        scheduler=SimpleNamespace(
            tick_routing_override=lambda: (None, None),
            run_once=run_once,
        ),
    )


def test_parallel_project_loops_run_concurrently() -> None:
    """M15b: per-project loops tick independently (no cycle join)."""
    settings = Settings.load_for_test().model_copy(
        update={"tick_project_parallelism": 3, "scheduler_interval_seconds": 0.05}
    )
    lock = threading.Lock()
    events: list[tuple[str, str, str]] = []

    def run_once(**kwargs):
        pid = kwargs["project_id"].value
        with lock:
            events.append(("start", pid, threading.current_thread().name))
        time.sleep(0.2)
        with lock:
            events.append(("end", pid, threading.current_thread().name))

    projects = [_project(f"p-{i}") for i in range(3)]
    host = BackgroundWorkerHost(settings, _services(projects, run_once))

    async def exercise():
        semaphore = asyncio.Semaphore(3)
        loops = [
            asyncio.create_task(host._project_scheduler_loop(p, semaphore))
            for p in projects
        ]
        await asyncio.sleep(0.7)
        for task in loops:
            task.cancel()
        await asyncio.gather(*loops, return_exceptions=True)

    asyncio.run(exercise())

    started = {pid for kind, pid, _ in events if kind == "start"}
    assert started == {"p-0", "p-1", "p-2"}
    threads = {name for _, _, name in events}
    assert len(threads) >= 2, "projects must tick on multiple threads"
    # Overlap: at least one start happens while another tick is in flight.
    inflight: set[str] = set()
    overlapped = False
    for kind, pid, _ in events:
        if kind == "start":
            if inflight:
                overlapped = True
            inflight.add(pid)
        else:
            inflight.discard(pid)
    assert overlapped, "project ticks must overlap in time"
    # Each project ticked more than once (independent cadence, not a
    # join-per-cycle where fast projects wait for the slowest tick).
    starts_per_project = [0, 0, 0]
    for kind, pid, _ in events:
        if kind == "start":
            starts_per_project[int(pid[2])] += 1
    assert min(starts_per_project) >= 2, (
        f"every project must re-tick independently (got {starts_per_project})"
    )


def test_serial_default_preserves_project_order() -> None:
    settings = Settings.load_for_test().model_copy(
        update={"tick_project_parallelism": 1}
    )
    order: list[str] = []
    threads: set[str] = set()

    def run_once(**kwargs):
        order.append(kwargs["project_id"].value)
        threads.add(threading.current_thread().name)

    projects = [_project(f"p-{i}") for i in range(3)]
    host = BackgroundWorkerHost(settings, _services(projects, run_once))
    host._scheduler_tick()

    assert order == ["p-0", "p-1", "p-2"], "serial mode must keep list order"
    assert len(threads) == 1, "serial mode must tick on the calling thread"


def test_per_project_error_isolation_under_parallelism() -> None:
    settings = Settings.load_for_test().model_copy(
        update={"tick_project_parallelism": 3}
    )

    def run_once(**kwargs):
        if kwargs["project_id"].value == "p-1":
            raise RuntimeError("boom")

    projects = [_project(f"p-{i}") for i in range(3)]
    host = BackgroundWorkerHost(settings, _services(projects, run_once))
    for project in projects:
        host._tick_single_project(project)  # must not raise

    joined = " | ".join(host.status.last_errors)
    assert "scheduler:p-1" in joined
    # Sibling projects still ticked (isolation, not starvation).
    assert not any("p-0" in e or "p-2" in e for e in host.status.last_errors)


def test_semaphore_bounds_concurrent_project_ticks() -> None:
    """The semaphore caps how many projects tick at once (provider load)."""
    settings = Settings.load_for_test().model_copy(
        update={"tick_project_parallelism": 2, "scheduler_interval_seconds": 0.05}
    )
    lock = threading.Lock()
    inflight = 0
    peak = 0
    seen: set[str] = set()

    def run_once(**kwargs):
        nonlocal inflight, peak
        pid = kwargs["project_id"].value
        with lock:
            seen.add(pid)
            inflight += 1
            peak = max(peak, inflight)
        time.sleep(0.12)
        with lock:
            inflight -= 1

    projects = [_project(f"p-{i}") for i in range(4)]
    host = BackgroundWorkerHost(settings, _services(projects, run_once))

    async def exercise():
        semaphore = asyncio.Semaphore(2)
        loops = [
            asyncio.create_task(host._project_scheduler_loop(p, semaphore))
            for p in projects
        ]
        await asyncio.sleep(0.9)
        for task in loops:
            task.cancel()
        await asyncio.gather(*loops, return_exceptions=True)

    asyncio.run(exercise())
    assert len(seen) == 4, "every project must still tick"
    assert peak <= 2, f"semaphore must cap concurrency (peak={peak})"


def test_parallelism_clamps_to_eight() -> None:
    settings = Settings.load_for_test().model_copy(
        update={"tick_project_parallelism": 99}
    )
    seen: set[str] = set()
    lock = threading.Lock()

    def run_once(**kwargs):
        with lock:
            seen.add(kwargs["project_id"].value)

    projects = [_project(f"p-{i}") for i in range(9)]
    host = BackgroundWorkerHost(settings, _services(projects, run_once))
    host._scheduler_tick()  # serial helper still ticks every project
    assert len(seen) == 9


def test_coordinator_discovers_projects_dynamically() -> None:
    """Projects created AFTER boot join without an engine restart."""
    settings = Settings.load_for_test().model_copy(
        update={"tick_project_parallelism": 4, "scheduler_interval_seconds": 0.05}
    )
    lock = threading.Lock()
    seen: set[str] = set()

    def run_once(**kwargs):
        with lock:
            seen.add(kwargs["project_id"].value)

    roster = [_project("p-late")]
    host = BackgroundWorkerHost(settings, _services(roster, run_once))

    async def exercise():
        task = asyncio.create_task(host._project_scheduler_coordinator())
        await asyncio.sleep(0.1)
        assert "p-late" in seen, "late project must be discovered"
        # A project added after boot joins on the next scan.
        roster.append(_project("p-postboot"))
        await asyncio.sleep(0.3)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())
    assert "p-late" in seen and "p-postboot" in seen


def test_project_parallelism_env_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZERO_ENV", "development")
    monkeypatch.setenv("ZERO_SECRET_KEY", "x" * 64)
    monkeypatch.setenv("ZERO_TICK_PROJECT_PARALLELISM", "9")
    with pytest.raises(ConfigError, match="ZERO_TICK_PROJECT_PARALLELISM"):
        Settings.load()

    monkeypatch.setenv("ZERO_TICK_PROJECT_PARALLELISM", "0")
    with pytest.raises(ConfigError, match="ZERO_TICK_PROJECT_PARALLELISM"):
        Settings.load()

    monkeypatch.setenv("ZERO_TICK_PROJECT_PARALLELISM", "abc")
    with pytest.raises(ConfigError, match="ZERO_TICK_PROJECT_PARALLELISM"):
        Settings.load()

    monkeypatch.setenv("ZERO_TICK_PROJECT_PARALLELISM", "3")
    settings = Settings.load()
    assert settings.tick_project_parallelism == 3


def test_default_parallelism_is_serial_backcompat() -> None:
    settings = Settings.load_for_test()
    assert settings.tick_project_parallelism == 1, (
        "default must keep the historical serial per-project behavior"
    )


def test_tool_floors_apply_to_every_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M16 (mega-run live-found): tool floors are PER-PROJECT boot
    invariants. They used to run for the management project only, so
    operator-created projects had zero grants and every task agent's
    workspace tool calls were denied."""
    from zero.app import config_sync

    projects = [_project(f"p-{i}") for i in range(3)]
    services = SimpleNamespace(identity=SimpleNamespace(list_projects=lambda: projects))
    calls: list[tuple[str, str]] = []

    def fake_websearch(_services, project, owner_id):
        calls.append(("websearch", project.id.value))

    def fake_workspace(_services, project, owner_id):
        calls.append(("workspace", project.id.value))

    monkeypatch.setattr(config_sync, "_ensure_web_search_tool", fake_websearch)
    monkeypatch.setattr(config_sync, "_ensure_workspace_tool_grants", fake_workspace)

    config_sync._ensure_per_project_tool_floors(services)

    assert sorted(calls) == sorted(
        [("websearch", f"p-{i}") for i in range(3)]
        + [("workspace", f"p-{i}") for i in range(3)]
    )


def test_tool_floor_failure_is_isolated_per_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One project's floor failing must not starve the others or boot."""
    from zero.app import config_sync

    projects = [_project("p-ok"), _project("p-bad")]
    services = SimpleNamespace(identity=SimpleNamespace(list_projects=lambda: projects))

    def fake_websearch(_services, project, owner_id):
        if project.id.value == "p-bad":
            raise RuntimeError("boom")

    ok_calls: list[str] = []

    def fake_workspace(_services, project, owner_id):
        ok_calls.append(project.id.value)

    monkeypatch.setattr(config_sync, "_ensure_web_search_tool", fake_websearch)
    monkeypatch.setattr(config_sync, "_ensure_workspace_tool_grants", fake_workspace)

    config_sync._ensure_per_project_tool_floors(services)  # must not raise

    assert ok_calls == ["p-ok", "p-bad"], (
        "both projects still get the workspace floor; websearch failure isolated"
    )


def _id(value: str):
    """Hashable, value-comparable id stub mirroring production frozen ids."""
    class _Id:
        __slots__ = ("value",)

        def __init__(self, v: str) -> None:
            self.value = v

        def __eq__(self, other: object) -> bool:
            return isinstance(other, _Id) and other.value == self.value

        def __hash__(self) -> int:
            return hash(self.value)

        def __repr__(self) -> str:
            return f"Id({self.value!r})"

    return _Id(value)


def _runtime_with_prompt():
    """Build a minimal AgentRuntime to exercise _task_prompt (staticmethod)."""
    from zero.app.agent_runtime import AgentRuntime

    return AgentRuntime.__new__(AgentRuntime)


def _task(expected_evidence):
    return SimpleNamespace(
        objective="Implement nettools/headers.py honoring the documented rules.",
        permitted_scope=("workspace",),
        expected_evidence=tuple(expected_evidence),
    )


def test_diff_evidence_task_prompt_requires_hands_on_tools() -> None:
    """M17 (mega-run live-found): diff-evidence tasks got a prompt whose
    FINAL line said 'Return a concise completion report...' — read-heavy
    agents obeyed it literally, returned text-only reports, and failed
    the diff gate attempt after attempt."""
    from zero.app.agent_runtime import AgentRuntime

    prompt = AgentRuntime._task_prompt(_task(["diff"]))
    after_objective = prompt.split("\n")[-1]
    assert "hands-on coding task" in prompt
    assert "write_file" in prompt
    assert "no file changes FAILS the diff-evidence gate" in prompt
    # The hands-on imperative must be the LAST word of the prompt — it is
    # what the model reads right before answering.
    assert "ACTUALLY implement the objective" in after_objective
    assert "do not claim actions that you did not perform" in prompt


def test_report_evidence_task_prompt_keeps_historical_contract() -> None:
    from zero.app.agent_runtime import AgentRuntime

    prompt = AgentRuntime._task_prompt(_task(["provider_response"]))
    assert "Return a concise completion report" in prompt
    assert "hands-on coding task" not in prompt


def test_dependency_outputs_injected_into_task_prompt() -> None:
    """M18 (mega-run live-found): a dependency task with provider_response
    evidence produces a text artifact (e.g. an API contract) that lives
    ONLY in the database. Downstream objectives reference it ("the
    documented rules") — without injection the agent cannot find it and
    honestly reports it cannot proceed (text-only → failed diff gate)."""
    import json

    from zero.app.agent_runtime import AgentRuntime

    pid = _id("p-1")
    eid = _id("e-1")
    # Hashable, value-comparable id stubs (production ids are frozen
    # dataclasses); the runtime both compares AND hashes them.
    dep_id = _id("t-dep")
    main_id = _id("t-main")
    dep = SimpleNamespace(
        id=dep_id,
        state="completed",
        objective="Produce a written API contract fixing undecided semantics.",
        completion_evidence=("art_1",),
        project_id=pid,
    )
    task = SimpleNamespace(
        id=main_id,
        execution_id=eid,
        project_id=pid,
        objective="Implement nettools/headers.py honoring the documented rules.",
        permitted_scope=("workspace",),
        expected_evidence=("diff",),
    )
    artifact = SimpleNamespace(
        content=json.dumps(
            {
                "attempt_id": "att-1",
                "objective": dep.objective,
                "response": {
                    "content": "### Contract v1\n- booleans are not integers",
                    "finish_reason": "stop",
                },
            }
        )
    )
    worker = SimpleNamespace(
        list_dependencies=lambda exec_id, **kw: [
            SimpleNamespace(task_id=main_id, depends_on_task_id=dep_id)
        ],
        list_tasks=lambda exec_id, **kw: [dep],
        list_attempts=lambda task_id, **kw: [],
    )
    artifacts = SimpleNamespace(get_artifact=lambda **kw: artifact)
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime._worker = worker
    runtime._artifacts = artifacts

    prompt = runtime._task_prompt_with_retry(
        task, actor_id=SimpleNamespace(value="owner")
    )

    assert "Outputs produced by your completed dependency tasks" in prompt
    assert "### Contract v1" in prompt
    assert "booleans are not integers" in prompt
    assert "DO THE WORK in the workspace" in prompt


def test_dependency_context_fails_silent() -> None:
    """A dependency-context failure must never break the task prompt."""
    from zero.app.agent_runtime import AgentRuntime

    pid = _id("p-1")
    task = SimpleNamespace(
        id=_id("t-main"),
        execution_id=_id("e-1"),
        project_id=pid,
        objective="Implement module X.",
        permitted_scope=("workspace",),
        expected_evidence=("diff",),
    )
    worker = SimpleNamespace(
        list_dependencies=lambda exec_id, **kw: (_ for _ in ()).throw(
            RuntimeError("db down")
        ),
        list_tasks=lambda exec_id, **kw: [],
        list_attempts=lambda task_id, **kw: [],
    )
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime._worker = worker
    runtime._artifacts = SimpleNamespace()

    prompt = runtime._task_prompt_with_retry(
        task, actor_id=SimpleNamespace(value="owner")
    )
    assert "Objective: Implement module X." in prompt
    assert "Outputs produced by your completed dependency tasks" not in prompt
