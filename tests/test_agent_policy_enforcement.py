"""Dynamic agent-type execution-policy enforcement tests.

Per the release audit (Phase 1, "Connect the agent model to runtime"):
the AgentType record must be binding for the running agent — model
policy overrides the request parameters, permitted_tools narrow the
tool surface server-side, context budget caps the context window,
instances are leased with atomic concurrency enforcement, and instance
lifecycle persists across success and failure.
"""

from __future__ import annotations

import pytest

from zero.app.services import build_services
from zero.app.worker_service import TaskSpec
from zero.config import Settings
from zero.domain.agent_types import ConcurrencyLimitExceededError
from zero.domain.plans import PlanRevisionContent
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations


@pytest.fixture
def services(test_settings: Settings):
    database = Database(test_settings)
    apply_migrations(database)
    return build_services(test_settings, database)


def _make_project_with_type(services, *, max_concurrent: int = 1, permitted_tools=()):
    owner = services.identity.create_user(display_name="Policy owner")
    project = services.identity.create_project(owner_id=owner.id, name="Policy")
    agent_type = services.agent_types.create_type(
        project_id=project.id,
        actor_id=owner.id,
        name="implementer",
        responsibility="Implements approved backend work.",
        memory_scope="backend decisions only",
        permitted_tools=tuple(permitted_tools),
        model_policy={"provider": "fake", "model": "fake-standard"},
        context_budget_tokens=123456,
        max_concurrent_instances=max_concurrent,
    )
    return owner, project, agent_type


def _approved_execution(services, owner, project, *, agent_type_id=None):
    event = services.plans.ingest_conversation_event(
        project_id=project.id,
        actor_id=owner.id,
        source="web",
        origin_kind="authenticated_human",
        content="Run the approved task.",
    )
    plan = services.plans.create_plan(project_id=project.id, actor_id=owner.id)
    services.plans.propose_revision(
        plan_id=plan.id,
        project_id=project.id,
        actor_id=owner.id,
        content=PlanRevisionContent(
            objective="Produce a provider response",
            scope=("backend",),
            constraints=(),
            acceptance_criteria=("A provider response is durably recorded",),
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
        idempotency_key="policy-approval",
    )
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        project_id=project.id,
        actor_id=owner.id,
        task_specs=[
            TaskSpec(
                key="policy-task",
                objective="Produce a provider response",
                permitted_scope=("backend",),
                expected_evidence=("provider_response",),
                agent_type_id=agent_type_id,
            )
        ],
    )
    task = services.worker.list_tasks(
        execution.id,
        project_id=execution.project_id,
        actor_id=owner.id,
    )[0]
    assert task.agent_type_id == agent_type_id
    return execution, task


def test_scheduler_assigns_default_agent_type_to_created_tasks(services) -> None:
    owner, project, _agent_type = _make_project_with_type(services)
    event = services.plans.ingest_conversation_event(
        project_id=project.id,
        actor_id=owner.id,
        source="web",
        origin_kind="authenticated_human",
        content="Scheduler-created work should carry an agent type.",
    )
    plan = services.plans.create_plan(project_id=project.id, actor_id=owner.id)
    services.plans.propose_revision(
        plan_id=plan.id,
        project_id=project.id,
        actor_id=owner.id,
        content=PlanRevisionContent(
            objective="Scheduler work",
            scope=(),
            constraints=(),
            acceptance_criteria=("A durable execution is created",),
            risks=(),
            unresolved_questions=(),
            source_event_ids=(event.id,),
        ),
    )
    services.plans.approve_revision(
        plan_id=plan.id,
        project_id=project.id,
        actor_id=owner.id,
        expected_revision_number=1,
        idempotency_key="scheduler-approval",
    )

    tick = services.scheduler.run_once(
        project_id=project.id,
        actor_id=owner.id,
        lease_owner="test-scheduler",
        provider="fake",
        model_name="fake-standard",
    )
    assert tick.handoffs_claimed == 1
    executions = services.worker._execution_repo.list_executions_for_project(project.id)
    assert executions, "expected a durable execution from the scheduler tick"
    tasks = services.worker.list_tasks(
        executions[0].id,
        project_id=project.id,
        actor_id=owner.id,
    )
    assert tasks[0].agent_type_id == _agent_type.id.value


def test_instance_lifecycle_persists_completed_state(services) -> None:
    owner, project, agent_type = _make_project_with_type(services)
    execution, task = _approved_execution(services, owner, project, agent_type_id=None)
    result = services.runtime.run_task(
        execution_id=execution.id,
        project_id=project.id,
        task_id=task.id,
        actor_id=owner.id,
        lease_owner="runtime-worker",
        provider="fake",
        model_name="fake-standard",
        agent_type_id=agent_type.id,
    )
    assert result.task.state == "completed"
    assert result.agent_type_id == agent_type.id.value
    assert result.agent_instance_id is not None
    from zero.domain.agent_types import AgentInstanceId

    instance = services.runtime._agent_type_repo.get_instance(
        AgentInstanceId(result.agent_instance_id)
    )
    assert instance.state == "completed"


def _two_task_execution(services, owner, project):
    event = services.plans.ingest_conversation_event(
        project_id=project.id,
        actor_id=owner.id,
        source="web",
        origin_kind="authenticated_human",
        content="Two tasks for concurrency checks.",
    )
    plan = services.plans.create_plan(project_id=project.id, actor_id=owner.id)
    services.plans.propose_revision(
        plan_id=plan.id,
        project_id=project.id,
        actor_id=owner.id,
        content=PlanRevisionContent(
            objective="Concurrency work",
            scope=(),
            constraints=(),
            acceptance_criteria=("Both tasks are durable",),
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
        idempotency_key="concurrency-approval",
    )
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        project_id=project.id,
        actor_id=owner.id,
        task_specs=[
            TaskSpec(key="A", objective="Task A"),
            TaskSpec(key="B", objective="Task B"),
        ],
    )
    return services.worker.list_tasks(
        execution.id,
        project_id=project.id,
        actor_id=owner.id,
    )


def test_concurrency_limit_enforced_atomically_by_repository(services) -> None:
    owner, project, agent_type = _make_project_with_type(services, max_concurrent=1)
    repo = services.runtime._agent_type_repo
    task_a, task_b = _two_task_execution(services, owner, project)

    first = repo.lease_instance_for_task(
        project_id=project.id,
        type_id=agent_type.id,
        task_id=task_a.id,
    )
    assert first.state == "running"
    with pytest.raises(ConcurrencyLimitExceededError):
        repo.lease_instance_for_task(
            project_id=project.id,
            type_id=agent_type.id,
            task_id=task_b.id,
        )
    repo.finish_instance(first.id, "completed")
    second = repo.lease_instance_for_task(
        project_id=project.id,
        type_id=agent_type.id,
        task_id=task_b.id,
    )
    assert second.state == "running"


def test_runtime_fails_task_when_concurrency_exhausted(services) -> None:
    owner, project, agent_type = _make_project_with_type(services, max_concurrent=1)
    repo = services.runtime._agent_type_repo
    task_a, task_b = _two_task_execution(services, owner, project)
    repo.lease_instance_for_task(
        project_id=project.id,
        type_id=agent_type.id,
        task_id=task_a.id,
    )
    from zero.app.agent_runtime import RuntimeEvidenceError

    with pytest.raises(RuntimeEvidenceError, match="concurrency limit"):
        services.runtime.run_task(
            execution_id=task_b.execution_id,
            project_id=project.id,
            task_id=task_b.id,
            actor_id=owner.id,
            lease_owner="runtime-worker",
            provider="fake",
            model_name="fake-standard",
            agent_type_id=agent_type.id,
        )
    final_attempt = services.worker.list_attempts(
        task_b.id,
        project_id=project.id,
        actor_id=owner.id,
    )[-1]
    assert final_attempt.state == "failed"


def test_cross_project_agent_type_is_refused(services) -> None:
    _owner_a, _project_a, agent_type = _make_project_with_type(services)
    owner_b = services.identity.create_user(display_name="Other owner")
    project_b = services.identity.create_project(owner_id=owner_b.id, name="Other")
    execution_b, task_b = _approved_execution(services, owner_b, project_b)
    from zero.app.agent_runtime import RuntimeEvidenceError

    with pytest.raises(RuntimeEvidenceError, match="does not exist"):
        services.runtime.run_task(
            execution_id=execution_b.id,
            project_id=project_b.id,
            task_id=task_b.id,
            actor_id=owner_b.id,
            lease_owner="runtime-worker",
            provider="fake",
            model_name="fake-standard",
            agent_type_id=agent_type.id,
        )


def test_unassigned_policy_is_unconstrained() -> None:
    from zero.app.agent_runtime import ResolvedAgentPolicy

    policy = ResolvedAgentPolicy()
    assert policy.provider_override is None
    assert policy.model_override is None
    assert policy.permitted_tools is None


def test_resolved_policy_reads_type_fields(services) -> None:
    owner, project, agent_type = _make_project_with_type(
        services,
        permitted_tools=("read_file", "write_file"),
    )
    from zero.domain.execution import ExecutionId, Task, TaskId

    execution, _task = _approved_execution(services, owner, project)
    task = Task(
        id=TaskId("task_policyresolve0"),
        execution_id=execution.id,
        project_id=project.id,
        objective="x",
        permitted_scope=(),
        expected_evidence=(),
        state="pending",
        created_at="",
        updated_at="",
        agent_type_id=agent_type.id.value,
    )
    policy_result = services.runtime.resolve_agent_policy(project_id=project.id, task=task)
    assert policy_result.provider_override == "fake"
    assert policy_result.model_override == "fake-standard"
    assert policy_result.permitted_tools is not None
    assert set(policy_result.permitted_tools) == {"read_file", "write_file"}
    assert str(ExecutionId("exec_x"))  # import sanity; prefix validated elsewhere
