from __future__ import annotations

import pytest
from pydantic import SecretStr

from zero.app.services import build_services
from zero.app.worktree_service import WorktreeService
from zero.config import ConfigError, Settings
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations


@pytest.fixture
def built_services(test_settings: Settings):
    database = Database(test_settings)
    apply_migrations(database)
    return build_services(test_settings, database)


def test_test_settings_select_explicit_bounded_host_mode() -> None:
    settings = Settings.load_for_test()

    assert settings.worktree_isolation_mode == "host_bounded"


def test_disabled_worktree_isolation_fails_closed_before_spawn(built_services) -> None:
    worktree = built_services.worktree
    worktree._isolation_mode = "disabled"

    with pytest.raises(Exception, match="isolation"):
        worktree._require_execution_isolation()


def test_production_rejects_bounded_host_execution() -> None:
    with pytest.raises(ConfigError, match="host-bounded"):
        Settings(
            zero_env="production",
            database_url="sqlite:////var/lib/zero/zero.db",
            secret_key="x" * 32,
            auth_required=True,
            bootstrap_token="b" * 64,
            worktree_isolation_mode="host_bounded",
        )._enforce_fail_closed_rules()


def test_rebuilding_services_rebinds_persistent_worktree_tool_handlers(tmp_path) -> None:
    settings = Settings(
        zero_env="development",
        database_url=f"sqlite:///{tmp_path / 'zero.db'}",
        secret_key=SecretStr("x" * 32),
        auth_required=False,
        worktree_root=str(tmp_path / "worktrees"),
        worktree_isolation_mode="host_bounded",
    )
    database = Database(settings)
    apply_migrations(database)

    build_services(settings, database)
    rebuilt = build_services(settings, database)

    assert {
        "zero.workspace.read_file",
        "zero.workspace.write_file",
        "zero.workspace.run_command",
        "zero.workspace.capture_diff",
    } <= set(rebuilt.tools._handlers)


def test_worktree_constructor_defaults_to_bounded_mode_for_direct_test_use(built_services) -> None:
    assert isinstance(built_services.worktree, WorktreeService)
    assert built_services.worktree._isolation_mode == "host_bounded"


def test_task_file_operations_are_scoped_to_the_owned_worktree(
    tmp_path, test_settings: Settings
) -> None:
    """Coding workers may mutate only normalized paths inside their task tree."""
    import subprocess

    from tests.test_agent_runtime import _approved_execution

    settings = Settings.load_for_test(
        worktree_root=str(tmp_path / "worktrees"),
        worktree_allowed_commands=("git", "python3"),
    )
    database = Database(settings)
    apply_migrations(database)
    services = build_services(settings, database)
    owner, project, execution, task = _approved_execution(services)

    repository_path = tmp_path / "repo"
    repository_path.mkdir()
    subprocess.run(["git", "init", str(repository_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repository_path), "config", "core.autocrlf", "false"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repository_path), "config", "user.email", "test@test.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository_path), "config", "user.name", "Zero Test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository_path), "config", "user.email", "zero@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository_path), "config", "user.name", "Zero Test"], check=True
    )
    (repository_path / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository_path), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(repository_path), "commit", "-m", "base"], check=True, capture_output=True
    )
    repository = services.worktree.register_repository(
        project_id=project.id,
        actor_id=owner.id,
        name="test-repo",
        local_path=str(repository_path),
    )
    worktree = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repository.id,
        execution_id=execution.id,
        task_id=task.id,
        actor_id=owner.id,
    )

    services.worktree.write_file(
        project_id=project.id,
        worktree_id=worktree.id,
        task_id=task.id,
        actor_id=owner.id,
        relative_path="src/hello.py",
        content="print('hello')\n",
    )
    assert (
        services.worktree.read_file(
            project_id=project.id,
            worktree_id=worktree.id,
            task_id=task.id,
            actor_id=owner.id,
            relative_path="src/hello.py",
        )
        == "print('hello')\n"
    )
    with pytest.raises(Exception, match="path|outside|traversal"):
        services.worktree.write_file(
            project_id=project.id,
            worktree_id=worktree.id,
            task_id=task.id,
            actor_id=owner.id,
            relative_path="../escape.txt",
            content="must not escape\n",
        )


def test_runtime_uses_tools_worktree_and_real_diff_postcondition(tmp_path) -> None:
    """A coding task is not complete until a worktree diff is durable."""
    import json
    import subprocess

    from tests.test_agent_runtime import _approved_execution
    from zero.app.agent_runtime import AgentRuntime
    from zero.domain.ids import generate_provider_request_id
    from zero.domain.providers import (
        CanonicalResponse,
        ProviderRequest,
        ToolCallResult,
    )

    settings = Settings.load_for_test(
        worktree_root=str(tmp_path / "worktrees"),
        worktree_allowed_commands=("git", "pytest"),
    )
    database = Database(settings)
    apply_migrations(database)
    services = build_services(settings, database)
    owner, project, execution, task = _approved_execution(
        services,
        expected_evidence=("diff",),
        objective="Modify the repository",
    )
    repository_path = tmp_path / "repo"
    repository_path.mkdir()
    subprocess.run(["git", "init", str(repository_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repository_path), "config", "core.autocrlf", "false"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repository_path), "config", "user.email", "zero@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository_path), "config", "user.name", "Zero Test"], check=True
    )
    (repository_path / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository_path), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(repository_path), "commit", "-m", "base"], check=True, capture_output=True
    )
    repository = services.worktree.register_repository(
        project_id=project.id,
        actor_id=owner.id,
        name="test-repo",
        local_path=str(repository_path),
    )
    tools = services.tools.register_worktree_tools(services.worktree)
    for tool in tools:
        services.tools.grant_tool(
            project_id=project.id,
            actor_id=owner.id,
            tool_id=tool.id,
            agent_scope="main_worker",
        )

    calls = 0

    def scripted_provider(
        *, project_id, actor_id, execution_id, request, cancel_event, source, **_kwargs
    ):
        assert not cancel_event.is_set()
        nonlocal calls
        calls += 1
        provider_request = ProviderRequest(
            id=__import__(
                "zero.domain.providers", fromlist=["ProviderRequestId"]
            ).ProviderRequestId(generate_provider_request_id()),
            project_id=project_id,
            execution_id=execution_id,
            provider="fake",
            model_name="fake-standard",
            request_hash=f"runtime-script-{calls}",
            state="completed",
            started_at="now",
        )
        if calls == 1:
            response = CanonicalResponse(
                content="writing",
                tool_calls=(
                    ToolCallResult(
                        tool_name="write_file",
                        tool_call_id="write-1",
                        arguments=json.dumps(
                            {"relative_path": "hello.py", "content": "print('hello')\n"}
                        ),
                        result="",
                    ),
                ),
                finish_reason="tool_calls",
            )
        else:
            response = CanonicalResponse(content="done", finish_reason="stop")
        return provider_request, response

    services.providers.send_request = scripted_provider
    runtime = AgentRuntime(
        worker=services.worker,
        providers=services.providers,
        artifacts=services.artifacts,
        authorization=services.authorization,
        tools=services.tools,
        worktrees=services.worktree,
        context_builder=services.context_builder,
    )
    result = runtime.run_task(
        execution_id=execution.id,
        project_id=project.id,
        task_id=task.id,
        actor_id=owner.id,
        lease_owner="coding-worker",
        provider="fake",
        model_name="fake-standard",
        repository_id=repository.id,
        tool_names=("write_file",),
    )

    assert result.task.state == "completed"
    assert result.worktree_id is not None
    assert len(result.evidence_artifact_ids) >= 2
    diffs = services.artifacts.list_artifacts(project_id=project.id, actor_id=owner.id, kind="diff")
    assert diffs and "hello.py" in diffs[-1].content
