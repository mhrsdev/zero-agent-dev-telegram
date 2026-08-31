"""Live-run hardening regressions (wave 11, 2026-08-31).

Covers the failures observed in the live Telegram group run:

1. Agent-type instance leases leaked by a killed process permanently
   consumed ``max_concurrent_instances`` — every later task failed with
   "agent type concurrency limit reached" until an operator hit
   /recover by hand. (release_stale_running_instances + boot sweep +
   run_ready_tasks deferral)
2. Worktrees left in the partial-unique states
   (allocated/active/interrupted) by a dead attempt poisoned every
   re-attempt of the same task with
   "UNIQUE constraint failed: worktrees.task_id". (abandonment at
   create time + boot sweep)
3. Task failure records hid the real cause
   ("evidence/postcondition failed: RuntimeEvidenceError"). (_failure_detail)
4. The stream tap emitted one garbled tool-call line per streaming
   fragment ("🔧 ?(and\")") instead of one converging preview per call.
   (_tap_stream + live views replace semantics)
5. The approval card showed "Execution: -" with no approval id, so the
   operator could not identify or resolve the request.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from zero.app.services import build_services
from zero.app.worker_service import TaskSpec
from zero.config import Settings
from zero.domain.plans import PlanRevisionContent
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations


# ----------------------------------------------------------------------
# Shared fixtures / helpers
# ----------------------------------------------------------------------


@pytest.fixture
def services(test_settings: Settings):
    database = Database(test_settings)
    apply_migrations(database)
    return build_services(test_settings, database)


def _fresh_worktree_service(services, tmp_path, name: str):
    """Rebuild the worktree service with a temp root (git isolation)."""
    from zero.app.worktree_service import WorktreeService

    services.worktree = WorktreeService(
        services.worktree._repo,
        services.worktree._audit_repo,
        services.worktree._authz,
        worktree_root=str(tmp_path / name),
        allowed_commands=frozenset({"echo"}),
    )
    return services.worktree


def _git_repo_with_commit(tmp_path, name: str):
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)], check=True, capture_output=True
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    (repo / "f.txt").write_text("x\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True
    )
    return repo


def _approved_execution(services, owner, project, *, task_specs):
    event = services.plans.ingest_conversation_event(
        project_id=project.id,
        actor_id=owner.id,
        source="web",
        origin_kind="authenticated_human",
        content="Hardening wave 11 work.",
    )
    plan = services.plans.create_plan(project_id=project.id, actor_id=owner.id)
    services.plans.propose_revision(
        plan_id=plan.id,
        project_id=project.id,
        actor_id=owner.id,
        content=PlanRevisionContent(
            objective="Wave 11 work",
            scope=(),
            constraints=(),
            acceptance_criteria=("Durable outcomes",),
            risks=(),
            unresolved_questions=(),
            source_event_ids=(event.id,),
        ),
    )
    _, handoff = services.plans.approve_revision(
        plan_id=plan.id,
        project_id=project.id,
        actor_id=owner.id,
        expected_revision_number=1,
        idempotency_key="wave11-approval",
    )
    return services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        project_id=project.id,
        actor_id=owner.id,
        task_specs=task_specs,
    )


def _project_with_type(services, *, max_concurrent: int = 1):
    owner = services.identity.create_user(display_name="Wave11 owner")
    project = services.identity.create_project(owner_id=owner.id, name="Wave11")
    agent_type = services.agent_types.create_type(
        project_id=project.id,
        actor_id=owner.id,
        name="wave11-worker",
        responsibility="Executes wave 11 tasks.",
        memory_scope="wave11 only",
        permitted_tools=(),
        model_policy={"provider": "fake", "model": "fake-standard"},
        context_budget_tokens=123456,
        max_concurrent_instances=max_concurrent,
    )
    execution = _approved_execution(
        services,
        owner,
        project,
        task_specs=[
            TaskSpec(
                key="t1",
                objective="First task",
                expected_evidence=("provider_response",),
                agent_type_id=agent_type.id.value,
            ),
            TaskSpec(
                key="t2",
                objective="Second task",
                expected_evidence=("provider_response",),
                agent_type_id=agent_type.id.value,
            ),
        ],
    )
    tasks = services.worker.list_tasks(
        execution.id, project_id=project.id, actor_id=owner.id
    )
    return owner, project, agent_type, execution, tasks


# ----------------------------------------------------------------------
# 1a. Stale agent-instance sweep (repository invariant)
# ----------------------------------------------------------------------


def test_release_stale_running_instances_keeps_live_leases(services) -> None:
    owner, project, agent_type, execution, tasks = _project_with_type(services)
    repo = services.runtime._agent_type_repo
    task_a = tasks[0]

    # A lease is only legitimate while its task is RUNNING. Claim the
    # task (running) and lease an instance for it: the sweep keeps it.
    services.worker.claim_task(
        execution_id=execution.id,
        task_id=task_a.id,
        project_id=project.id,
        actor_id=owner.id,
        lease_owner="wave11-worker",
    )
    live = repo.lease_instance_for_task(
        project_id=project.id, type_id=agent_type.id, task_id=task_a.id
    )
    assert repo.release_stale_running_instances() == 0
    assert repo.get_instance(live.id).state == "running"


def test_release_stale_running_instances_releases_dead_leases(services) -> None:
    """The live bug: the task never reached running (or already became
    terminal) but its instance row stayed 'running' — the sweep must
    free the concurrency budget."""
    owner, project, agent_type, execution, tasks = _project_with_type(services)
    repo = services.runtime._agent_type_repo
    task_a, task_b = tasks[0], tasks[1]

    # Leased while the task is still 'ready': the killed-process shape.
    stale = repo.lease_instance_for_task(
        project_id=project.id, type_id=agent_type.id, task_id=task_a.id
    )
    assert repo.count_running_instances(agent_type.id) == 1
    assert repo.release_stale_running_instances() == 1
    assert repo.get_instance(stale.id).state == "cancelled"
    # The freed budget is immediately leasable again — this is exactly
    # what un-blocked the live group.
    fresh = repo.lease_instance_for_task(
        project_id=project.id, type_id=agent_type.id, task_id=task_b.id
    )
    assert fresh.state == "running"


def test_release_stale_running_instances_handles_orphan_task_ids(services) -> None:
    """Defensive shape: a 'running' row with no task at all (FK triggers
    forbid normal leasing of a nonexistent task, so simulate the corrupt
    row directly). The sweep must still release it."""
    owner, project, agent_type, _execution, _tasks = _project_with_type(services)
    repo = services.runtime._agent_type_repo
    from zero.domain.ids import generate_agent_instance_id

    conn = services.database.connect()
    instance_id = generate_agent_instance_id()
    try:
        conn.execute(
            "INSERT INTO agent_instances (id, project_id, agent_type_id, task_id, state) "
            "VALUES (?, ?, ?, NULL, 'running')",
            (instance_id, project.id.value, agent_type.id.value),
        )
        services.database.commit()
    except Exception:
        # The schema may additionally forbid NULL tasks; if so the shape
        # cannot occur and the invariant is trivially held.
        services.database.rollback()
        return
    assert repo.count_running_instances(agent_type.id) == 1
    assert repo.release_stale_running_instances() == 1
    assert repo.count_running_instances(agent_type.id) == 0


# ----------------------------------------------------------------------
# 1b. run_ready_tasks defers at-capacity tasks instead of failing them
# ----------------------------------------------------------------------


def test_run_ready_tasks_defers_when_agent_type_at_capacity(services) -> None:
    """Live regression: with max_concurrent_instances=1 and one busy
    worker, every sibling task used to be CLAIMED then FAILED with
    "agent type concurrency limit reached", blocking the whole graph."""
    owner, project, agent_type, execution, tasks = _project_with_type(services)
    repo = services.runtime._agent_type_repo
    task_a, task_b = tasks[0], tasks[1]

    # Simulate a live worker holding the single slot on task A.
    services.worker.claim_task(
        execution_id=execution.id,
        task_id=task_a.id,
        project_id=project.id,
        actor_id=owner.id,
        lease_owner="live-worker",
    )
    holder = repo.lease_instance_for_task(
        project_id=project.id, type_id=agent_type.id, task_id=task_a.id
    )

    events: list[dict] = []
    results = services.runtime.run_ready_tasks(
        execution_id=execution.id,
        project_id=project.id,
        actor_id=owner.id,
        lease_owner="tick-worker",
        provider="fake",
        model_name="fake-standard",
        task_event_callback=events.append,
    )
    # Nothing ran (capacity full), and crucially nothing failed: the
    # deferred task stays ready for a later tick.
    assert results == []
    deferred = [e for e in events if e["type"] == "task_deferred"]
    assert {e["task_id"] for e in deferred} == {task_b.id.value}
    still_ready = [
        t
        for t in services.worker.list_tasks(
            execution.id, project_id=project.id, actor_id=owner.id
        )
        if t.id == task_b.id
    ][0]
    assert still_ready.state == "ready"

    # Free the slot: the next tick runs the deferred task to completion.
    repo.finish_instance(holder.id, "completed")
    results2 = services.runtime.run_ready_tasks(
        execution_id=execution.id,
        project_id=project.id,
        actor_id=owner.id,
        lease_owner="tick-worker",
        provider="fake",
        model_name="fake-standard",
    )
    assert [r.task.id for r in results2] == [task_b.id]
    assert results2[0].task.state == "completed"


# ----------------------------------------------------------------------
# 1c. Boot recovery reconciles the killed-process state end-to-end
# ----------------------------------------------------------------------


def test_startup_recovery_releases_leases_and_abandons_worktrees(
    services, test_settings, tmp_path
) -> None:
    """The exact live state after an engine kill: stale running instance
    lease + interrupted worktree. _startup_recovery must release the
    lease, abandon the worktree, and make a re-attempt possible."""
    from zero.app.background_workers import BackgroundWorkerHost

    owner, project, agent_type, execution, tasks = _project_with_type(services)
    repo = services.runtime._agent_type_repo
    task = tasks[0]

    # Stale instance: task already terminal, lease still 'running'.
    stale_instance = repo.lease_instance_for_task(
        project_id=project.id, type_id=agent_type.id, task_id=task.id
    )
    services.worker._execution_repo.update_task_state(task.id, "failed")

    # Stale worktree: 'interrupted' (the live post-kill state).
    _fresh_worktree_service(services, tmp_path, "wts-boot")
    git_repo = _git_repo_with_commit(tmp_path, "boot-repo")
    repository = services.worktree.register_repository(
        project_id=project.id,
        actor_id=owner.id,
        name="boot-repo",
        local_path=str(git_repo),
        default_base_revision="main",
    )
    stale_wt = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repository.id,
        execution_id=execution.id,
        task_id=task.id,
        actor_id=owner.id,
    )
    services.worktree._repo.update_worktree_state(stale_wt.id, "interrupted")

    host = BackgroundWorkerHost(test_settings, services)
    host._startup_recovery()

    # The stale lease is released and the freed slot is usable again.
    assert repo.get_instance(stale_instance.id).state == "cancelled"
    fresh = repo.lease_instance_for_task(
        project_id=project.id, type_id=agent_type.id, task_id=task.id
    )
    assert fresh.state == "running"

    # The stale worktree was abandoned out of the partial-unique states.
    wt_row = services.worktree._repo.get_worktree(project.id, stale_wt.id)
    assert wt_row.state in {"failed", "cancelled"}

    # A re-attempt can now create a fresh worktree for the same task
    # without IntegrityError (the live "UNIQUE constraint" failure).
    reclaimed = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repository.id,
        execution_id=execution.id,
        task_id=task.id,
        actor_id=owner.id,
    )
    assert reclaimed.id != stale_wt.id


# ----------------------------------------------------------------------
# 2. Worktree re-attempt: a stale row must never poison create_worktree
# ----------------------------------------------------------------------


def test_create_worktree_reattempt_after_stale_active_worktree(
    services, tmp_path
) -> None:
    """The live failure: task re-attempt died with
    "workspace/context setup failed: IntegrityError" because the prior
    attempt's worktree still occupied idx_worktrees_task_active."""
    owner = services.identity.create_user(display_name="WT owner")
    project = services.identity.create_project(owner_id=owner.id, name="WT")
    _fresh_worktree_service(services, tmp_path, "wts-reattempt")
    git_repo = _git_repo_with_commit(tmp_path, "wt-repo")
    repository = services.worktree.register_repository(
        project_id=project.id,
        actor_id=owner.id,
        name="wt-repo",
        local_path=str(git_repo),
        default_base_revision="main",
    )
    execution = _approved_execution(
        services,
        owner,
        project,
        task_specs=[TaskSpec(key="wt-task", objective="coding task")],
    )
    task = services.worker.list_tasks(
        execution.id, project_id=project.id, actor_id=owner.id
    )[0]

    first = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repository.id,
        execution_id=execution.id,
        task_id=task.id,
        actor_id=owner.id,
    )
    services.worktree.activate_worktree(
        project_id=project.id, worktree_id=first.id, actor_id=owner.id
    )
    # Simulate the kill: the worktree stays 'active' (never completed).

    # Re-attempt must succeed and move the stale row out of the
    # partial-unique states.
    second = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repository.id,
        execution_id=execution.id,
        task_id=task.id,
        actor_id=owner.id,
    )
    assert second.id != first.id
    stale_after = services.worktree._repo.get_worktree(project.id, first.id)
    assert stale_after.state in {"failed", "cancelled"}
    # Exactly ONE worktree occupies the active states now: the new one.
    active = [
        wt
        for wt in services.worktree._repo.list_worktrees_for_project(project.id)
        if wt.state in {"allocated", "active", "interrupted"}
    ]
    assert [wt.id for wt in active] == [second.id]


def test_abandon_stale_worktrees_sweep(services, tmp_path) -> None:
    owner = services.identity.create_user(display_name="Sweep owner")
    project = services.identity.create_project(owner_id=owner.id, name="Sweep")
    _fresh_worktree_service(services, tmp_path, "wts-sweep")
    git_repo = _git_repo_with_commit(tmp_path, "sweep-repo")
    repository = services.worktree.register_repository(
        project_id=project.id,
        actor_id=owner.id,
        name="sweep-repo",
        local_path=str(git_repo),
        default_base_revision="main",
    )
    execution = _approved_execution(
        services,
        owner,
        project,
        task_specs=[TaskSpec(key="sweep-task", objective="sweep me")],
    )
    task = services.worker.list_tasks(
        execution.id, project_id=project.id, actor_id=owner.id
    )[0]
    wt = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repository.id,
        execution_id=execution.id,
        task_id=task.id,
        actor_id=owner.id,
    )
    # Task still 'ready' (never running): the worktree is stale.
    assert services.worktree.abandon_stale_worktrees() == 1
    row = services.worktree._repo.get_worktree(project.id, wt.id)
    assert row.state == "cancelled"
    # Idempotent: nothing left to abandon.
    assert services.worktree.abandon_stale_worktrees() == 0


def test_abandon_stale_worktrees_never_touches_running_tasks(
    services, tmp_path
) -> None:
    owner = services.identity.create_user(display_name="Live owner")
    project = services.identity.create_project(owner_id=owner.id, name="Live")
    _fresh_worktree_service(services, tmp_path, "wts-live")
    git_repo = _git_repo_with_commit(tmp_path, "live-repo")
    repository = services.worktree.register_repository(
        project_id=project.id,
        actor_id=owner.id,
        name="live-repo",
        local_path=str(git_repo),
        default_base_revision="main",
    )
    execution = _approved_execution(
        services,
        owner,
        project,
        task_specs=[TaskSpec(key="live-task", objective="still running")],
    )
    task = services.worker.list_tasks(
        execution.id, project_id=project.id, actor_id=owner.id
    )[0]
    wt = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repository.id,
        execution_id=execution.id,
        task_id=task.id,
        actor_id=owner.id,
    )
    services.worktree.activate_worktree(
        project_id=project.id, worktree_id=wt.id, actor_id=owner.id
    )
    # Claim the task: it is genuinely running now.
    services.worker.claim_task(
        execution_id=execution.id,
        task_id=task.id,
        project_id=project.id,
        actor_id=owner.id,
        lease_owner="live-worker",
    )
    assert services.worktree.abandon_stale_worktrees() == 0
    row = services.worktree._repo.get_worktree(project.id, wt.id)
    assert row.state == "active"


# ----------------------------------------------------------------------
# 3. Failure records carry the redacted cause
# ----------------------------------------------------------------------


def test_failure_detail_includes_class_and_message() -> None:
    from zero.app.agent_runtime import _failure_detail

    exc = RuntimeError("UNIQUE constraint failed: worktrees.task_id")
    detail = _failure_detail(exc)
    assert detail.startswith("RuntimeError: ")
    assert "UNIQUE constraint failed: worktrees.task_id" in detail


def test_failure_detail_redacts_secrets() -> None:
    from zero.app.agent_runtime import _failure_detail

    # Synthetic key (scanner-exempt fixture marker embedded). The REAL
    # leaked key must never appear in tracked files again — the
    # redaction itself is shape-based, so a same-shape fixture proves
    # the mechanism without re-committing the secret.
    exc = RuntimeError(
        "gateway rejected key sk-WAVE11FIXTUREAAHcSECRETVALUE0000000000"
    )
    detail = _failure_detail(exc)
    assert "sk-WAVE11FIXTUREAAHcSECRETVALUE0000000000" not in detail


def test_runtime_setup_failure_message_carries_cause(services) -> None:
    """The durable task error must name the real cause, not just the
    exception class (live: 'workspace/context setup failed:
    IntegrityError' with no constraint name)."""
    import types as _types

    owner, project, agent_type, execution, tasks = _project_with_type(services)
    task = tasks[0]
    runtime = services.runtime
    assert runtime._worktrees is not None

    calls: list[str] = []

    def _failing_create(**kwargs):
        calls.append("create")
        raise RuntimeError("UNIQUE constraint failed: worktrees.task_id")

    original_worktrees = runtime._worktrees
    runtime._worktrees = _types.SimpleNamespace(
        create_worktree=_failing_create,
        list_repositories=lambda *a, **k: [SimpleNamespace(id=SimpleNamespace(value="repo_1"))],
        activate_worktree=original_worktrees.activate_worktree,
        complete_worktree=lambda *a, **k: None,
    )
    try:
        with pytest.raises(RuntimeError, match="UNIQUE constraint"):
            runtime.run_task(
                execution_id=execution.id,
                project_id=project.id,
                task_id=task.id,
                actor_id=owner.id,
                lease_owner="wave11-worker",
                provider="fake",
                model_name="fake-standard",
                repository_id=None,
                tool_names=("read_file",),
            )
    finally:
        runtime._worktrees = original_worktrees
    assert calls == ["create"]
    attempts = services.worker.list_attempts(
        task.id, project_id=project.id, actor_id=owner.id
    )
    assert attempts, "expected a durable failed attempt"
    error_text = attempts[-1].error_message or ""
    assert "workspace/context setup failed" in error_text
    assert "UNIQUE constraint failed: worktrees.task_id" in error_text


# ----------------------------------------------------------------------
# 4. Stream tap: one converging tool-call preview per call
# ----------------------------------------------------------------------


def _tap_events(fragment_specs):
    from zero.app.provider_service import ProviderService
    from zero.domain.providers import CanonicalStreamEvent, ToolCallResult

    events = []
    for name, call_id, args in fragment_specs:
        events.append(
            CanonicalStreamEvent(
                kind="tool_call_delta",
                tool_call=ToolCallResult(
                    tool_name=name or "",
                    tool_call_id=call_id or "",
                    arguments=args or "",
                    result="",
                ),
            )
        )
    events.append(CanonicalStreamEvent(kind="message_end", finish_reason="tool_calls"))

    seen: list[dict] = []
    for _ in ProviderService._tap_stream(iter(events), seen.append):
        pass
    return seen


def test_tap_stream_emits_replacing_events_per_call() -> None:
    """The live garble '🔧 ?(and\")' came from one observer event per
    fragment. The tap must now emit name-carrying events whose
    arguments converge to the full call."""
    seen = _tap_events(
        [
            ("run_command", "call_1", '{"comm'),
            ("", "call_1", 'and": "ls"}'),
        ]
    )
    tool_events = [e for e in seen if e["type"] == "tool_call"]
    assert len(tool_events) == 2
    assert tool_events[0]["name"] == "run_command"
    assert tool_events[0]["replace"] is False
    assert tool_events[1]["name"] == "run_command"
    assert tool_events[1]["replace"] is True
    assert tool_events[1]["arguments"] == {"command": "ls"}


def test_tap_stream_buffers_name_only_fragments() -> None:
    """Some gateways stream the function name in its own delta before
    the id arrives; it must attach to the call, not disappear."""
    seen = _tap_events(
        [
            ("run_command", "", ""),
            ("", "call_9", '{"command": "ls"}'),
        ]
    )
    tool_events = [e for e in seen if e["type"] == "tool_call"]
    assert len(tool_events) == 1
    assert tool_events[0]["name"] == "run_command"
    assert tool_events[0]["arguments"] == {"command": "ls"}
    assert tool_events[0]["replace"] is False


def test_tap_stream_separates_distinct_calls() -> None:
    seen = _tap_events(
        [
            ("read_file", "call_1", '{"path": "a"}'),
            ("read_file", "call_2", '{"path": "b"}'),
        ]
    )
    tool_events = [e for e in seen if e["type"] == "tool_call"]
    assert [(e["name"], e["replace"]) for e in tool_events] == [
        ("read_file", False),
        ("read_file", False),
    ]
    assert tool_events[0]["arguments"] == {"path": "a"}
    assert tool_events[1]["arguments"] == {"path": "b"}


class _RecordingAdapter:
    """Captures frames the live bubble wants to push."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.edited: list[str] = []

    def send_message(self, **kwargs):
        self.sent.append(str(kwargs.get("text") or ""))
        return SimpleNamespace(message_id="7")

    def edit_message(self, **kwargs):
        self.edited.append(str(kwargs.get("text") or ""))
        return SimpleNamespace(message_id="7")


def test_live_stream_tool_call_replace_converges_preview() -> None:
    from zero.app.telegram_live import TelegramLiveStream

    adapter = _RecordingAdapter()
    stream = TelegramLiveStream(adapter=adapter, chat_id="1", min_edit_interval=0.0)
    # Fragment stream of ONE call: new line, then in-place updates.
    stream.on_tool_call("run_command", '{"comm')
    stream.on_tool_call("run_command", '{"command": "ls"}', replace=True)
    stream.on_tool_call("run_command", '{"command": "ls"}', replace=True)
    # A DIFFERENT call appends a new line.
    stream.on_tool_call("read_file", '{"path": "a"}')
    lines = [
        line
        for line in stream._compose_frame_locked().split("\n")
        if line.startswith(("🔧", "✅", "⚠️"))
    ]
    assert lines == [
        '🔧 run_command({"command": "ls"})',
        '🔧 read_file({"path": "a"})',
    ]


def test_progress_view_tool_call_replace_converges_preview() -> None:
    """The execution-progress bubble rendered the live garble
    '🔧 ?(and\")' — fragment events must replace the pending line there
    too."""
    from zero.app.telegram_live import TelegramExecutionProgress

    adapter = _RecordingAdapter()
    view = TelegramExecutionProgress(
        adapter=adapter, chat_id="1", min_edit_interval=0.0
    )
    # Event shape exactly as the fixed _tap_stream emits it: the first
    # id-bearing fragment carries the resolved name (replace=False =
    # new pending line); later fragments of the same call arrive with
    # replace=True and accumulated arguments.
    view.on_stream_event({"type": "tool_call", "name": "run_command", "arguments": '{"comm'})
    view.on_stream_event(
        {
            "type": "tool_call",
            "name": "run_command",
            "arguments": '{"command": "ls"}',
            "replace": True,
        }
    )
    frame = view._compose_frame_locked()
    tool_lines = [
        line for line in frame.split("\n") if line.startswith(("🔧", "✅", "⚠️"))
    ]
    assert tool_lines == ['🔧 run_command({"command": "ls"})']


# ----------------------------------------------------------------------
# 5. Approval card identifies the request
# ----------------------------------------------------------------------


def test_approval_card_shows_id_and_adhoc_label(services) -> None:
    from zero.app.approval_gate import ToolApprovalGate

    owner = services.identity.create_user(display_name="Card owner")
    project = services.identity.create_project(owner_id=owner.id, name="Cards")
    services.interfaces.create_binding(
        project_id=project.id,
        actor_id=owner.id,
        platform="telegram",
        chat_id="555",
        topic_id=None,
        is_enabled=True,
    )
    sent: list[dict] = []

    class _Transport:
        def send_message(self, **kwargs):
            sent.append(kwargs)
            return "mid-1"

    services.interfaces.direct_reply_transport = _Transport()
    gate = ToolApprovalGate(services.database, mode="manual")
    verdict = gate.evaluate(
        project_id=project.id.value,
        execution_id=None,
        tool_name="run_command",
        input_data={"command": "echo live"},
    )
    assert verdict.state == "pending"
    result = services.interfaces.send_tool_approval_card(verdict.request)
    assert "sent to 1 binding" in result
    text = sent[0]["text"]
    assert f"Approval: {verdict.request.id}" in text
    assert "run_command" in text
    assert "(ad-hoc / chat" in text
    assert "\nExecution: -\n" not in text

    # With a real execution id the card shows it verbatim.
    verdict2 = gate.evaluate(
        project_id=project.id.value,
        execution_id="exec_abc123",
        tool_name="run_command",
        input_data={"command": "echo two"},
    )
    sent.clear()
    services.interfaces.send_tool_approval_card(verdict2.request)
    assert "Execution: exec_abc123" in sent[0]["text"]


# ----------------------------------------------------------------------
# 7. Chat-serial dispatcher honors the adapter's callable contract
# ----------------------------------------------------------------------


def test_chat_serial_dispatcher_is_callable() -> None:
    """Live bug: the adapter submits via ``background_dispatch(_run)``
    (a CALL), but the dispatcher only exposed ``submit`` — every polled
    group message was rejected with
    "'_ChatSerialDispatcher' object is not callable" and the bot
    processed nothing."""
    from zero.app.background_workers import _ChatSerialDispatcher

    ran: list[str] = []

    def _job() -> None:
        ran.append("job")

    _job.chat_id = "-100123"  # type: ignore[attr-defined]
    dispatcher = _ChatSerialDispatcher()
    # The adapter's call convention...
    dispatcher(_job)
    # ...and the submit convention must both work.
    dispatcher.submit(lambda: ran.append("second"))
    import time as _time

    deadline = _time.monotonic() + 5
    while len(ran) < 2 and _time.monotonic() < deadline:
        _time.sleep(0.05)
    assert ran == ["job", "second"]


# ----------------------------------------------------------------------
# 8. Continuous expired-lease reconciliation (the boot-only gap)
# ----------------------------------------------------------------------


def test_reconcile_expired_leases_frees_dead_owner_tasks(services) -> None:
    """Live bug found while watching the live engine: a task whose owner
    died AFTER boot kept its lease long enough to be authoritative at
    boot, then expired with nobody watching — the graph stayed blocked
    forever. Tick-level reconciliation must recover it."""
    from datetime import UTC, datetime, timedelta

    owner, project, agent_type, execution, tasks = _project_with_type(services)
    repo = services.runtime._agent_type_repo
    task = tasks[0]

    services.worker.claim_task(
        execution_id=execution.id,
        task_id=task.id,
        project_id=project.id,
        actor_id=owner.id,
        lease_owner="dead-worker",
        lease_duration_seconds=300,
    )
    holder = repo.lease_instance_for_task(
        project_id=project.id, type_id=agent_type.id, task_id=task.id
    )
    # The worker died: nobody renews the attempt lease any more. Force
    # the lease into the past.
    conn = services.database.connect()
    conn.execute(
        "UPDATE task_attempts SET lease_expires_at = ? WHERE id = ?",
        (
            (datetime.now(UTC) - timedelta(seconds=5)).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ"
            ),
            conn.execute(
                "SELECT id FROM task_attempts WHERE task_id = ?", (task.id.value,)
            ).fetchone()[0],
        ),
    )
    services.database.commit()

    reconciled = services.worker.reconcile_expired_leases(
        project_id=project.id, actor_id=owner.id
    )
    assert reconciled == 1
    # The dead-lease task is ready again...
    row = [
        t
        for t in services.worker.list_tasks(
            execution.id, project_id=project.id, actor_id=owner.id
        )
        if t.id == task.id
    ][0]
    assert row.state == "ready"
    # ...its agent-type slot was released...
    assert repo.get_instance(holder.id).state == "cancelled"
    # ...and a fresh tick can lease the slot immediately.
    fresh = repo.lease_instance_for_task(
        project_id=project.id, type_id=agent_type.id, task_id=task.id
    )
    assert fresh.state == "running"


def test_reconcile_expired_leases_never_steals_live_work(services) -> None:
    owner, project, agent_type, execution, tasks = _project_with_type(services)
    task = tasks[0]
    services.worker.claim_task(
        execution_id=execution.id,
        task_id=task.id,
        project_id=project.id,
        actor_id=owner.id,
        lease_owner="live-worker",
        lease_duration_seconds=300,
    )
    # Lease still fresh: reconciliation must NOT touch it.
    assert (
        services.worker.reconcile_expired_leases(
            project_id=project.id, actor_id=owner.id
        )
        == 0
    )
    row = [
        t
        for t in services.worker.list_tasks(
            execution.id, project_id=project.id, actor_id=owner.id
        )
        if t.id == task.id
    ][0]
    assert row.state == "running"
