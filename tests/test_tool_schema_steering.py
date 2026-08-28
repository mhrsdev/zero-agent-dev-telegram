"""Regression tests for real-run bug 11 (tool-calling hardening), found live
on 2026-08-28 during the 5-minute complete run:

Bug 11a — ``capture_diff`` declared a zero-property object schema with
``additionalProperties=False``, so a frontier model that naturally passed
arguments (``base``, ``paths``, …) failed input validation FIVE consecutive
times in one real task (audit: ``input validation failed``), burned its tool
rounds, and never recovered the tool. Fixes verified here:

  1. the declared schema tolerates (ignores) extra keys for the no-argument
     read-only tool, and the description says "takes NO arguments";
  2. the model-facing validation failure for a genuine zero-argument tool
     (empty properties + closed schema) explicitly steers: "call it with an
     empty object {}";
  3. the worktree-tool denial for context-less callers (delegation
     sub-agents) names the policy instead of a mystery.

Bug 11b — the decomposer gave a "capture the final diff" aggregation task
only ``["provider_response"]`` evidence; the prompts now require ``["diff"]``
for aggregation tasks. Covered by test_decomposition evidence tests.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from tests.test_agent_runtime import _approved_execution
from zero.app.services import build_services
from zero.config import Settings
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations


@pytest.fixture
def services(tmp_path):
    settings = Settings.load_for_test(
        worktree_root=str(tmp_path / "worktrees"),
        worktree_allowed_commands=("git", "python3"),
    )
    database = Database(settings)
    apply_migrations(database)
    built = build_services(settings, database)
    # Test settings skip server-owned tool registration (fail-closed test
    # hygiene); mirror the production composition path explicitly so the
    # declared capture_diff contract is observable here.
    built.tools.register_worktree_tools(built.worktree)
    return built


def _repo_with_commit(services, tmp_path, project):
    repository_path = tmp_path / "repo"
    repository_path.mkdir(exist_ok=True)
    if not (repository_path / ".git").exists():
        subprocess.run(
            ["git", "init", str(repository_path)], check=True, capture_output=True
        )
        for args in (
            ["config", "user.email", "zero@example.invalid"],
            ["config", "user.name", "Zero Test"],
        ):
            subprocess.run(
                ["git", "-C", str(repository_path), *args], check=True, capture_output=True
            )
        (repository_path / "README.md").write_text("base\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(repository_path), "add", "README.md"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repository_path), "commit", "-m", "base"],
            check=True,
            capture_output=True,
        )
    return services.worktree.register_repository(
        project_id=project.id,
        actor_id=project.owner_user_id,
        name="test-repo",
        local_path=str(repository_path),
    )


def test_capture_diff_declaration_tolerates_extra_arguments(services) -> None:
    """Bug 11a: the no-argument read-only tool must not reject extra keys."""
    tool = services.tools.get_tool_by_name("capture_diff")
    assert tool is not None
    schema = tool.input_schema
    assert schema.get("type") == "object"
    assert schema.get("additionalProperties") is True, (
        "capture_diff must tolerate (ignore) extra keys — a frontier model "
        "naturally passes arguments and 5 validation failures were observed live"
    )
    description = tool.description.lower()
    assert "no arguments" in description or "takes no arguments" in description

    # Extra keys validate against the declared schema.
    import jsonschema

    jsonschema.validate(instance={"base": "HEAD~1", "paths": ["a"]}, schema=schema)


def test_capture_diff_succeeds_with_extra_arguments_end_to_end(
    services, tmp_path
) -> None:
    """The exact live failure: model calls capture_diff with arguments —
    must succeed (extra keys ignored), not fail input validation."""
    owner, project, execution, task = _approved_execution(services)
    repository = _repo_with_commit(services, tmp_path, project)
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
    tool = services.tools.get_tool_by_name("capture_diff")
    services.tools.grant_tool(
        project_id=project.id,
        actor_id=project.owner_user_id,
        tool_id=tool.id,
        agent_scope="main_worker",
    )
    result = services.tools.invoke(
        project_id=project.id,
        actor_id=owner.id,
        agent_scope="main_worker",
        tool_name="capture_diff",
        # The live-failing shape: extra arguments a model invents.
        input_data={"base": "HEAD~1", "paths": ["src/hello.py"]},
        execution_id=execution.id.value,
        task_id=task.id.value,
        source="system",
    )
    assert result.status == "success"
    assert result.output["artifact_id"]
    assert result.output["content"].strip(), "diff artifact must be non-empty"
    assert "src" in result.output["content"]


def test_zero_argument_tool_validation_failure_steers_empty_object(services) -> None:
    """A closed zero-property schema must tell the model to send {}."""
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(owner_id=owner.id, name="NoArg Project")
    tool = services.tools.register_tool(
        name="noargs",
        description="A tool that genuinely takes no arguments.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        output_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler_key="noargs",
        handler=lambda input_data, context: {"result": "ok"},
    )
    services.tools.grant_tool(
        project_id=project.id,
        actor_id=project.owner_user_id,
        tool_id=tool.id,
        agent_scope="main_worker",
    )
    with pytest.raises(Exception) as excinfo:
        services.tools.invoke(
            project_id=project.id,
            actor_id=owner.id,
            agent_scope="main_worker",
            tool_name="noargs",
            input_data={"invented": "arg"},
        )
    message = str(excinfo.value)
    assert "failed validation" in message
    assert "empty object {}" in message, (
        "the model-facing error must steer: call it with an empty object {}"
    )


def test_capture_diff_json_description_reaches_model_render(services) -> None:
    """The rendered tool contract must carry the no-arguments instruction."""
    tool = services.tools.get_tool_by_name("capture_diff")
    rendered = json.dumps({"name": tool.name, "description": tool.description})
    assert "NO arguments" in rendered or "no arguments" in rendered


def test_decomposition_prompts_require_diff_evidence_for_aggregation_tasks() -> None:
    """Bug 11b: a capture-the-final-diff task must require ["diff"] evidence."""
    from zero.app.task_decomposition import (
        _EMITTED_TASK_SCHEMA,
        DECOMPOSITION_SYSTEM_PROMPT,
        DECOMPOSITION_SYSTEM_PROMPT_STRICT,
    )

    for prompt in (DECOMPOSITION_SYSTEM_PROMPT, DECOMPOSITION_SYSTEM_PROMPT_STRICT):
        assert "capture" in prompt.lower() and "final diff" in prompt.lower(), (
            "decomposer guidance must map aggregation/diff-capture tasks to diff evidence"
        )
    schema_text = json.dumps(_EMITTED_TASK_SCHEMA)
    assert "capture the final diff" in schema_text
