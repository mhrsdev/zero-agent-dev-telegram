"""Wave 12 — live-run hardening (B10/B11/B12a/B12b).

Bugs found in the real resumed deployment (2026-08-31, engine on
127.0.0.1:8011 with the live gateway + Telegram group):

B10  capture_diff's cumulative fallback always contained the
     server-managed hygiene ``.gitignore`` (committed at worktree
     creation), so "required diff evidence" was satisfied by
     infrastructure even when the agent produced nothing — the live
     test-module agent finalized "I could not perform the task" and the
     task still COMPLETED. Fix: hygiene paths are excluded from
     incremental/cumulative diffs and the status section; a genuinely
     change-less attempt now yields EMPTY diff content, which the
     runtime's evidence gate already rejects.

B11  The scrubbed child environment resolved bare ``python3``/``pytest``
     to the SYSTEM interpreter (no pytest), so the configured evidence
     test command failed with
     "/usr/bin/python3: No module named pytest" on every attempt.
     Fix: venv-aware scrubbed env (VIRTUAL_ENV + venv PATH) and bare
     interpreter/pytest argv rewritten to the engine's own interpreter
     on the host path only.

B12a ZERO_TOOL_APPROVAL_MODE now accepts ``auto``: hardline floor +
     operator deny rules stay enforced, everything else flows without a
     human click (unattended pipelines).

B12b manual mode auto-allows PROVABLY read-only calls (read_file,
     capture_diff, ls/grep, read-only git subcommands) — the live agent
     burned its whole attempt on pending approvals for pure reads.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from zero.app.approval_gate import ToolApprovalGate, _is_safe_readonly
from zero.app.services import build_services
from zero.app.worker_service import DependencySpec, TaskSpec
from zero.app.worktree_service import WorktreeService
from zero.config import Settings
from zero.domain.plans import PlanRevisionContent
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations

GATE_PROJECT = "p_testwave12approval0000000"


# ----------------------------------------------------------------------
# B11 — scrubbed env + interpreter override
# ----------------------------------------------------------------------


def test_scrubbed_env_exposes_engine_venv_when_mounted(monkeypatch, tmp_path):
    from zero.app.executors.sandbox import scrubbed_env

    monkeypatch.setattr(sys, "prefix", str(tmp_path / "venv"))
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    env = scrubbed_env(str(tmp_path / "wt"))
    assert env["VIRTUAL_ENV"] == str(tmp_path / "venv")
    venv_bin = str(tmp_path / "venv" / "bin")
    assert env["PATH"].startswith(venv_bin + ":")
    # The fixed base entries must still be present after the venv prefix.
    assert env["PATH"].endswith("/usr/local/bin:/usr/bin:/bin")
    # Isolation contract unchanged: no host env leak.
    assert set(env) <= {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "GIT_TERMINAL_PROMPT",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_GLOBAL",
        "VIRTUAL_ENV",
    }


def test_scrubbed_env_stays_bare_without_venv(monkeypatch, tmp_path):
    from zero.app.executors.sandbox import scrubbed_env

    monkeypatch.setattr(sys, "prefix", "/usr")
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    env = scrubbed_env(str(tmp_path))
    assert "VIRTUAL_ENV" not in env
    assert env["PATH"] == "/usr/local/bin:/usr/bin:/bin"


def test_host_interpreter_argv_rewrites_bare_names(monkeypatch):
    from zero.app.executors.sandbox import host_interpreter_argv

    monkeypatch.setattr(sys, "prefix", "/opt/venv")
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    fake = "/opt/venv/bin/python"

    monkeypatch.setattr(sys, "executable", fake, raising=False)
    assert host_interpreter_argv(["python3", "-m", "pytest", "-q"]) == [
        fake,
        "-m",
        "pytest",
        "-q",
    ]
    assert host_interpreter_argv(["python", "script.py"]) == [fake, "script.py"]
    assert host_interpreter_argv(["pytest", "-q"]) == [fake, "-m", "pytest", "-q"]
    assert host_interpreter_argv([f"python{sys.version_info.major}", "-V"]) == [fake, "-V"]
    # Explicit paths are honored as written.
    assert host_interpreter_argv(["/usr/bin/python3", "-V"]) == ["/usr/bin/python3", "-V"]
    # Non-interpreter commands untouched.
    assert host_interpreter_argv(["git", "status"]) == ["git", "status"]
    assert host_interpreter_argv([]) == []


def test_host_interpreter_argv_noop_without_venv(monkeypatch):
    from zero.app.executors.sandbox import host_interpreter_argv

    monkeypatch.setattr(sys, "prefix", "/usr")
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    assert host_interpreter_argv(["python3", "-m", "pytest"]) == ["python3", "-m", "pytest"]


# ----------------------------------------------------------------------
# services/worktree fixture (B10 + B11 integration)
# ----------------------------------------------------------------------


@pytest.fixture
def services(test_settings: Settings, tmp_path):
    database = Database(test_settings)
    apply_migrations(database)
    s = build_services(test_settings, database)
    s.worktree = WorktreeService(
        s.worktree._repo,
        s.worktree._audit_repo,
        s.worktree._authz,
        worktree_root=str(tmp_path / "worktrees"),
        allowed_commands=frozenset({"python3", "python", "pytest", "git", "ls", "grep"}),
    )
    return s


def _make_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "core.autocrlf", "false"],
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@test.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "README.md").write_text("# Test Repo\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "initial"], check=True, capture_output=True)
    return repo


def _make_approved_plan(services) -> tuple:
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(owner_id=owner.id, name="P")
    event = services.plans.ingest_conversation_event(
        project_id=project.id,
        actor_id=owner.id,
        source="web",
        origin_kind="authenticated_human",
        content="Add a feature.",
    )
    plan = services.plans.create_plan(project_id=project.id, actor_id=owner.id)
    content = PlanRevisionContent(
        objective="Add a feature",
        scope=(),
        constraints=(),
        acceptance_criteria=("Works",),
        risks=(),
        unresolved_questions=(),
        source_event_ids=(event.id,),
    )
    services.plans.propose_revision(
        plan_id=plan.id, project_id=project.id, actor_id=owner.id, content=content
    )
    _, handoff = services.plans.approve_revision(
        plan_id=plan.id,
        project_id=project.id,
        actor_id=owner.id,
        expected_revision_number=1,
        idempotency_key="w12",
    )
    return owner, project, handoff


@pytest.fixture
def two_task_execution(services, tmp_path):
    owner, project, handoff = _make_approved_plan(services)
    repo_path = _make_repo(tmp_path)
    repo = services.worktree.register_repository(
        project_id=project.id,
        actor_id=owner.id,
        name="test-repo",
        local_path=str(repo_path),
        default_base_revision="main",
    )
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        project_id=project.id,
        actor_id=owner.id,
        task_specs=[
            TaskSpec(key="A", objective="Create the module"),
            TaskSpec(key="B", objective="Run the tests over A's work"),
        ],
        dependency_specs=[
            DependencySpec(task_key="B", depends_on_key="A"),
        ],
    )
    tasks = services.worker.list_tasks(execution.id, project_id=project.id, actor_id=owner.id)
    by_key = {t.objective: t for t in tasks}
    return services, owner, project, repo, execution, by_key


# ----------------------------------------------------------------------
# B10 — hygiene .gitignore can no longer satisfy diff evidence
# ----------------------------------------------------------------------


def test_no_change_worktree_yields_empty_diff(two_task_execution):
    """The live bug: a do-nothing task's diff artifact contained the
    hygiene .gitignore payload, satisfying 'required diff evidence'.
    After B10 the content is EMPTY (the runtime gate then fails it)."""
    services, owner, project, repo, execution, by_key = two_task_execution
    task_a = by_key["Create the module"]
    wt = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repo.id,
        execution_id=execution.id,
        task_id=task_a.id,
        actor_id=owner.id,
    )
    services.worktree.activate_worktree(
        project_id=project.id, worktree_id=wt.id, actor_id=owner.id
    )
    artifact = services.worktree.capture_diff(
        project_id=project.id,
        worktree_id=wt.id,
        task_id=task_a.id,
        actor_id=owner.id,
        source="system",
    )
    assert artifact.content.strip() == "", (
        "no-change worktree must produce EMPTY diff evidence; "
        f"got: {artifact.content[:200]!r}"
    )
    assert ".gitignore" not in artifact.content


def test_real_work_still_shows_in_diff(two_task_execution):
    services, owner, project, repo, execution, by_key = two_task_execution
    task_a = by_key["Create the module"]
    wt = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repo.id,
        execution_id=execution.id,
        task_id=task_a.id,
        actor_id=owner.id,
    )
    services.worktree.activate_worktree(
        project_id=project.id, worktree_id=wt.id, actor_id=owner.id
    )
    (Path(wt.worktree_path) / "module.py").write_text("VALUE = 1\n")
    artifact = services.worktree.capture_diff(
        project_id=project.id,
        worktree_id=wt.id,
        task_id=task_a.id,
        actor_id=owner.id,
        source="system",
    )
    assert "module.py" in artifact.content
    assert ".gitignore" not in artifact.content


def test_chained_cumulative_diff_shows_dependency_work_only(two_task_execution):
    """B's no-change diff falls back to the cumulative execution diff:
    A's REAL module.py must appear, the hygiene .gitignore must not."""
    services, owner, project, repo, execution, by_key = two_task_execution
    task_a = by_key["Create the module"]
    task_b = by_key["Run the tests over A's work"]

    wt_a = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repo.id,
        execution_id=execution.id,
        task_id=task_a.id,
        actor_id=owner.id,
    )
    services.worktree.activate_worktree(
        project_id=project.id, worktree_id=wt_a.id, actor_id=owner.id
    )
    (Path(wt_a.worktree_path) / "module.py").write_text("VALUE = 1\n")
    services.worktree.complete_worktree(
        project_id=project.id, worktree_id=wt_a.id, actor_id=owner.id, succeeded=True
    )
    base, _extra = services.runtime._dependency_worktree_bases(
        project_id=project.id,
        execution_id=execution.id,
        task_id=task_b.id,
        actor_id=owner.id,
        source="system",
    )
    wt_b = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repo.id,
        execution_id=execution.id,
        task_id=task_b.id,
        actor_id=owner.id,
        base_revision=base,
    )
    services.worktree.activate_worktree(
        project_id=project.id, worktree_id=wt_b.id, actor_id=owner.id
    )
    artifact = services.worktree.capture_diff(
        project_id=project.id,
        worktree_id=wt_b.id,
        task_id=task_b.id,
        actor_id=owner.id,
        source="system",
    )
    assert "module.py" in artifact.content
    assert ".gitignore" not in artifact.content


# ----------------------------------------------------------------------
# B11 — evidence test command runs the engine's environment
# ----------------------------------------------------------------------


@pytest.mark.skipif(
    sys.prefix == sys.base_prefix,
    reason="engine not running inside a virtualenv; nothing to override",
)
def test_evidence_style_command_uses_engine_interpreter(two_task_execution):
    """The live failure: `python3 -m pytest` -> /usr/bin/python3 has no
    pytest. After B11 the command runs the engine's own interpreter."""
    services, owner, project, repo, execution, by_key = two_task_execution
    task_a = by_key["Create the module"]
    wt = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repo.id,
        execution_id=execution.id,
        task_id=task_a.id,
        actor_id=owner.id,
    )
    services.worktree.activate_worktree(
        project_id=project.id, worktree_id=wt.id, actor_id=owner.id
    )
    command_run, artifacts = services.worktree.run_command(
        project_id=project.id,
        worktree_id=wt.id,
        task_id=task_a.id,
        actor_id=owner.id,
        command="python3",
        args=("-m", "pytest", "--version"),
        source="system",
    )
    assert command_run.state == "completed"
    assert command_run.exit_code == 0, (
        f"pytest must run under the engine interpreter; stderr="
        f"{[a.content for a in artifacts if a.kind == 'stderr']}"
    )
    stdout = next(a for a in artifacts if a.kind == "stdout")
    assert "pytest" in stdout.content


# ----------------------------------------------------------------------
# B12a — auto approval mode (hardline + deny rules, no human loop)
# ----------------------------------------------------------------------


def _gate_database():
    settings = Settings.load_for_test()
    database = Database(settings)
    apply_migrations(database)
    conn = database.connect()
    conn.execute(
        "INSERT OR IGNORE INTO users (id, display_name, status, created_at) "
        "VALUES (?, ?, 'active', ?)",
        ("zu_ownerplaceholder0000000000", "Gate Owner", "2026-08-27T00:00:00"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO projects (id, name, created_at) VALUES (?, ?, ?)",
        (GATE_PROJECT, "wave12", "2026-08-27T00:00:00"),
    )
    conn.execute(
        "UPDATE projects SET owner_user_id = ? WHERE id = ? AND owner_user_id IS NULL",
        ("zu_ownerplaceholder0000000000", GATE_PROJECT),
    )
    database.commit()
    return database


def _eval(gate, tool="echo", **input_data):
    return gate.evaluate(
        project_id=GATE_PROJECT,
        execution_id=None,
        tool_name=tool,
        input_data=input_data or {"message": "hi"},
    )


def test_gate_accepts_auto_mode():
    gate = ToolApprovalGate(_gate_database(), mode="auto")
    assert gate.mode == "auto"


def test_auto_mode_allows_normal_calls_without_human():
    gate = ToolApprovalGate(_gate_database(), mode="auto")
    verdict = _eval(gate, "write_file", path="x.txt", content="hi")
    assert verdict.state == "allowed"
    assert verdict.cause == "mode_auto"


def test_auto_mode_still_enforces_hardline():
    gate = ToolApprovalGate(_gate_database(), mode="auto")
    # Shell-string shape... and argv shape (normalized matching) both deny.
    for payload in (
        {"command": "bash", "args": ["-c", "rm -rf / "]},
        {"command": "rm", "args": ["-rf", "/"]},
    ):
        verdict = _eval(gate, "run_command", **payload)
        assert verdict.state == "denied", payload
        assert (verdict.cause or "").startswith("hardline:"), payload


def test_auto_mode_still_enforces_deny_rules():
    database = _gate_database()
    # Mint the deny rule through a manual gate (auto never creates
    # pendings by design), then assert the auto gate honors it.
    manual = ToolApprovalGate(database, mode="manual")
    gate = ToolApprovalGate(database, mode="auto")
    gate.resolve(
        manual.evaluate(
            project_id=GATE_PROJECT,
            execution_id=None,
            tool_name="write_file",
            input_data={"path": "x"},
        ).request.id,
        decision="deny",
        decided_by_user_id="zu_ownerplaceholder0000000000",
        grain="always",
    )
    verdict = _eval(gate, "write_file", path="anything-else.txt", content="x")
    assert verdict.state == "denied"
    assert verdict.cause == "rule"


def test_settings_accept_auto_approval_mode(monkeypatch):
    monkeypatch.setenv("ZERO_TOOL_APPROVAL_MODE", "auto")
    settings = Settings.load(zero_env_fallback="development")
    assert settings.tool_approval_mode == "auto"
    monkeypatch.setenv("ZERO_TOOL_APPROVAL_MODE", "nonsense")
    with pytest.raises(Exception):
        Settings.load(zero_env_fallback="development")


# ----------------------------------------------------------------------
# B12b — manual mode triage of provably read-only calls
# ----------------------------------------------------------------------


def test_manual_mode_auto_allows_readonly_calls():
    gate = ToolApprovalGate(_gate_database(), mode="manual")
    for tool, payload in (
        ("read_file", {"path": "greeting.py"}),
        ("capture_diff", {}),
        ("run_command", {"command": "ls", "args": ["-la"]}),
        ("run_command", {"command": "grep", "args": ["-n", "def", "greeting.py"]}),
        ("run_command", {"command": "git", "args": ["status", "--short"]}),
        ("run_command", {"command": "git", "args": ["worktree", "list"]}),
    ):
        verdict = _eval(gate, tool, **payload)
        assert verdict.state == "allowed", f"{tool} {payload} -> {verdict}"
        assert verdict.cause == "safe_readonly"


def test_manual_mode_still_gates_mutable_calls():
    gate = ToolApprovalGate(_gate_database(), mode="manual")
    for tool, payload in (
        ("write_file", {"path": "x.txt", "content": "hi"}),
        ("run_command", {"command": "python3", "args": ["-c", "print(1)"]}),
        ("run_command", {"command": "pytest", "args": ["-q"]}),
        ("run_command", {"command": "git", "args": ["push", "origin", "main"]}),
        ("run_command", {"command": "git", "args": ["worktree", "add", "../x"]}),
        ("run_command", {"command": "rm", "args": ["-rf", "x"]}),
    ):
        verdict = _eval(gate, tool, **payload)
        assert verdict.state == "pending", f"{tool} {payload} -> {verdict}"


def test_deny_rule_outranks_safe_readonly():
    database = _gate_database()
    gate = ToolApprovalGate(database, mode="manual")
    # read_file is safe-readonly (never mints a pending row), so insert
    # the pending request directly and resolve it to a tool-wide deny —
    # the same rows the REST surface creates.
    conn = database.connect()
    conn.execute(
        "INSERT INTO tool_approval_decisions "
        "(id, project_id, execution_id, tool_name, args_hash, grain, decision, created_at) "
        "VALUES (?, ?, NULL, 'read_file', '', 'once', NULL, ?)",
        ("ta_wave12denyreadfile0000", GATE_PROJECT, "2026-08-27T00:00:01"),
    )
    database.commit()
    gate.resolve(
        "ta_wave12denyreadfile0000",
        decision="deny",
        decided_by_user_id="zu_ownerplaceholder0000000000",
        grain="always",
    )
    verdict = _eval(gate, "read_file", path="anything")
    assert verdict.state == "denied"
    assert verdict.cause == "rule"


def test_is_safe_readonly_rejects_malformed_shapes():
    assert not _is_safe_readonly("run_command", {"command": ""})
    assert not _is_safe_readonly("run_command", {"command": "git"})
    assert not _is_safe_readonly("run_command", {"command": "ls", "args": ["ok", 1]})
    assert not _is_safe_readonly("run_command", {})
    assert not _is_safe_readonly("mcp_server", {"x": 1})


# ----------------------------------------------------------------------
# B13 — retries carry the previous failure into the agent prompt
# ----------------------------------------------------------------------


def test_retry_prompt_includes_previous_failure(two_task_execution):
    """B13: the retrying agent must SEE why the previous attempt failed
    (live: the no-change agent retried blind up to the attempt cap)."""
    from zero.domain.execution import TaskAttempt, TaskAttemptId

    services, owner, project, repo, execution, by_key = two_task_execution
    task_a = by_key["Create the module"]

    services.worktree  # touch for readability
    attempt = TaskAttempt(
        id=TaskAttemptId("att_wave12failedattempt0001"),
        task_id=task_a.id,
        project_id=project.id,
        attempt_number=1,
        state="failed",
        error_message=(
            "evidence/postcondition failed: RuntimeEvidenceError: "
            "required diff evidence contains no file change"
        ),
        started_at="2026-08-31T00:00:00Z",
        completed_at="2026-08-31T00:01:00Z",
    )
    services.runtime._worker._execution_repo.insert_attempt(attempt)

    prompt = services.runtime._task_prompt_with_retry(task_a, actor_id=owner.id)
    assert "Previous attempt #1 FAILED" in prompt
    assert "required diff evidence contains no file change" in prompt
    assert "create or modify the" in prompt
    # A task without failures keeps the clean prompt.
    task_b = by_key["Run the tests over A's work"]
    clean = services.runtime._task_prompt_with_retry(task_b, actor_id=owner.id)
    assert "Previous attempt" not in clean


def test_retry_prompt_best_effort_on_worker_errors(two_task_execution):
    services, owner, project, repo, execution, by_key = two_task_execution
    task_a = by_key["Create the module"]

    class _Boom:
        def list_attempts(self, *a, **k):
            raise RuntimeError("db closed")

    original = services.runtime._worker
    try:
        object.__setattr__(services.runtime, "_worker", _Boom())
        prompt = services.runtime._task_prompt_with_retry(task_a, actor_id=owner.id)
        assert "Objective:" in prompt
    finally:
        object.__setattr__(services.runtime, "_worker", original)


# ----------------------------------------------------------------------
# B10 refinement — generative objectives cannot pass on dependency work
# ----------------------------------------------------------------------


def test_objective_change_verb_detection():
    from zero.app.agent_runtime import _objective_expects_changes

    assert _objective_expects_changes("Create the test module at the test location")
    assert _objective_expects_changes("Fix the failing import in app.py")
    assert _objective_expects_changes("Write docs for the API")
    assert not _objective_expects_changes(
        "Capture the final consolidated diff of all added and modified files"
    )
    assert not _objective_expects_changes(
        "Report the existing docstring style from representative modules"
    )


def test_generative_task_rejects_dependency_only_diff(two_task_execution):
    """The live hole: 'create the test module' completed without creating
    the file because the cumulative fallback carried greeting.py. The
    runtime gate must reject a generative objective whose diff evidence
    is only the no-change fallback."""
    services, owner, project, repo, execution, by_key = two_task_execution
    task_b = by_key["Run the tests over A's work"]
    placeholder = (
        "--- Cumulative execution diff ---\n"
        "(this task made no changes on top of its dependency "
        "branches; the diff below is the whole execution's "
        "change set against the repository base revision)\n\n"
        "diff --git a/module.py b/module.py\n"
    )
    # The gate path validates evidence through _validate_evidence_artifacts
    # via the runtime internals; here we pin the decision function.
    from zero.app.agent_runtime import (
        _TASK_MADE_NO_CHANGES_MARKER,
        _objective_expects_changes,
    )

    assert _TASK_MADE_NO_CHANGES_MARKER in placeholder
    # "Run the tests over A's work" — a run/verify objective — is NOT
    # generative (no create/write/... verb), so it may rely on the
    # cumulative fallback. The "Create the module" objective is.
    assert not _objective_expects_changes(task_b.objective)
    assert _objective_expects_changes(by_key["Create the module"].objective)


def test_resumed_execution_clears_stale_blocker(two_task_execution):
    """Claiming work on a paused execution must clear the retry blocker
    (live: executions showed 'awaiting automatic task retry' while
    running)."""
    services, owner, project, repo, execution, by_key = two_task_execution
    task_a = by_key["Create the module"]
    # Stamp the execution paused with the retry blocker.
    services.runtime._worker._execution_repo.update_execution_state(
        execution.id, "paused", blocker_reason="awaiting automatic task retry"
    )
    # Claim the task the way the scheduler does.
    services.runtime._worker.claim_task(
        execution_id=execution.id,
        task_id=task_a.id,
        project_id=project.id,
        actor_id=owner.id,
        lease_owner="wave12-test-worker",
        source="system",
    )
    row = services.runtime._worker._execution_repo.get_execution(execution.id)
    assert row.state == "running"
    assert not row.blocker_reason


# ----------------------------------------------------------------------
# B14 — workspace tools must be granted to execution agent scopes
# ----------------------------------------------------------------------


def test_workspace_tools_auto_granted_by_config_sync(test_settings, tmp_path):
    """B14: the live deployment denied EVERY workspace tool call ("No
    grant for tool ... in scope ...") because only internet_search was
    ever auto-granted. config_sync must grant all four workspace tools
    to main_worker AND sub_agent_type, idempotently."""
    from zero.app.config_sync import _ensure_workspace_tool_grants

    database = Database(test_settings)
    apply_migrations(database)
    s = build_services(test_settings, database)
    owner = s.identity.create_user(display_name="Owner")
    project = s.identity.create_project(owner_id=owner.id, name="P")

    # Register the workspace tools exactly the way the composition
    # root does (ToolService.register_worktree_tools).
    s.tools.register_worktree_tools(worktree_service=s.worktree)

    _ensure_workspace_tool_grants(s, project, owner.id)
    grants = s.tools._tool_repo.list_grants_for_project(project.id)
    granted = {(g.agent_scope) for g in grants}
    assert {"main_worker", "sub_agent_type"} <= granted
    by_scope = {}
    for g in grants:
        tool_name = s.tools._tool_repo.get_tool_by_id(g.tool_id).name
        by_scope.setdefault(g.agent_scope, set()).add(tool_name)
    for scope in ("main_worker", "sub_agent_type"):
        for name in ("read_file", "write_file", "run_command", "capture_diff"):
            assert name in by_scope.get(scope, set()), (scope, name, by_scope)

    # Idempotent: second run must not duplicate or crash.
    _ensure_workspace_tool_grants(s, project, owner.id)
    assert len(s.tools._tool_repo.list_grants_for_project(project.id)) == len(grants)
