"""Regressions found during the 2026-08-28 REAL full-feature run.

1. Gateway data loss: ``_process_message`` stored the conversation event
   with the 200-char redacted *log preview* (``_event_content``), so any
   Telegram message longer than 200 chars was silently truncated before
   the planner saw it. The real-run LLM planner correctly refused such
   truncated requests as "not actionable".
2. Plugin restart: ``load_plugins`` reported healthy plugins as failed
   ("ToolAlreadyExistsError") on every reboot, because tool rows are
   durable but the handler map is process-local.
"""

from __future__ import annotations

import pytest

from zero.config import Settings
from zero.domain.interfaces import NormalizedEvent
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations


@pytest.fixture
def services(test_settings: Settings):
    database = Database(test_settings)
    apply_migrations(database)
    from zero.app.services import build_services

    return build_services(test_settings, database)


@pytest.fixture
def project_with_owner_and_binding(services):
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(owner_id=owner.id, name="Project A")
    services.identity.link_external_identity(
        user_id=owner.id,
        platform="telegram",
        external_id="7086634092",
        external_username="owner",
        verified=True,
    )
    binding = services.interfaces.create_binding(
        project_id=project.id,
        actor_id=owner.id,
        platform="telegram",
        chat_id="100",
        topic_id=None,
        is_enabled=True,
    )
    return owner, project, binding


class TestGatewayStoresFullContent:
    def test_long_message_content_survives_intact(self, services, project_with_owner_and_binding):
        """A >200-char message must reach the planner in full."""
        owner, project, _binding = project_with_owner_and_binding
        long_message = (
            "Build the textkit package. " + "x" * 300
        )  # 327 chars, well past the old 200-char cut
        result = services.interfaces.process_inbound_event(
            NormalizedEvent(
                platform="telegram",
                external_event_id="evt-long-1",
                external_actor_id="7086634092",
                chat_id="100",
                topic_id=None,
                event_kind="message",
                content=long_message,
            )
        )
        assert result.processing_result == "processed"
        events = services.plans.list_conversation_events(
            project_id=project.id, actor_id=owner.id, limit=10
        )
        assert events, "conversation event must be ingested"
        stored = events[0].content
        assert len(stored) == len(long_message), (
            f"message truncated: stored {len(stored)} of {len(long_message)} chars"
        )
        assert stored.endswith(long_message[-20:]), "tail of the message must survive"

    def test_short_message_still_processed(self, services, project_with_owner_and_binding):
        _owner, _project, _binding = project_with_owner_and_binding
        result = services.interfaces.process_inbound_event(
            NormalizedEvent(
                platform="telegram",
                external_event_id="evt-short-1",
                external_actor_id="7086634092",
                chat_id="100",
                topic_id=None,
                event_kind="message",
                content="short but actionable: add a login form",
            )
        )
        assert result.processing_result == "processed"


class TestPluginIdempotentRestart:
    def _write_plugin(self, directory, name, tool_name):
        import textwrap

        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text(
            textwrap.dedent(
                f"""
                def register(ctx):
                    ctx.tool_registry.register_tool(
                        name={tool_name!r},
                        description="restart probe",
                        input_schema={{"type": "object", "properties": {{}}}},
                        output_schema={{"type": "object"}},
                        handler_key="plugin:{tool_name}",
                        handler=lambda data, ctx: {{"ok": True}},
                        inline=True,
                    )
                """
            ),
            encoding="utf-8",
        )
        return path

    def test_same_plugin_loads_cleanly_twice(self, services, monkeypatch, tmp_path):
        """Reload after 'restart' (same durable registry, fresh handler map)
        must count the plugin as loaded, not failed."""
        from zero.manage.plugins.registry import load_plugins

        monkeypatch.setenv("ZERO_HOME", str(tmp_path / "home"))
        monkeypatch.setenv("ZERO_SYSTEM_PLUGIN_DIR", str(tmp_path / "nonexistent"))
        self._write_plugin(tmp_path / "home" / "plugins", "probe.py", "restart_probe")

        first = load_plugins(tool_service=services.tools)
        assert "user:probe.py" in first

        # second boot: durable tool row exists, process handler map fresh
        second_services_services = services  # same registry simulates durable rows
        second = load_plugins(tool_service=second_services_services.tools)
        assert "user:probe.py" in second, (
            "plugin must reload idempotently after restart, got failures"
        )

        # and the tool remains invocable through the standard pipeline
        tool = services.tools._tool_repo.get_tool_by_name("restart_probe")
        owner = services.identity.create_user(display_name="probe owner")
        project = services.identity.create_project(owner_id=owner.id, name="Probe")
        services.tools.grant_tool(
            project_id=project.id,
            actor_id=owner.id,
            tool_id=tool.id,
            agent_scope="main_worker",
            source="system",
        )
        result = services.tools.invoke(
            project_id=project.id,
            actor_id=owner.id,
            agent_scope="main_worker",
            tool_name="restart_probe",
            input_data={},
            source="system",
        )
        assert result.output == {"ok": True}


class TestFtsQueryWhitelist:
    def test_operational_characters_no_longer_crash(self, services, project_with_owner_and_binding):
        """Real-run crash: 'fts5: syntax error near "/"' — task objectives
        like 'textkit/__init__.py' went into FTS MATCH raw."""
        owner, project, _binding = project_with_owner_and_binding
        services.artifacts.ingest_rag_document(
            project_id=project.id,
            actor_id=owner.id,
            source_type="manual",
            source_id="d1",
            title="layout",
            content="textkit package layout documentation",
            state="approved",
        )
        nasty_queries = [
            "textkit/__init__.py",
            "c++ review",
            "a & b #tag 50%",
            "run (tests) now?",
            "deploy; rm -rf /",
            "quote 'single \"double",
        ]
        for q in nasty_queries:
            results = services.artifacts.search_rag(
                project_id=project.id, actor_id=owner.id, query=q
            )
            assert isinstance(results, list)

    def test_meaningful_terms_still_match(self, services, project_with_owner_and_binding):
        owner, project, _binding = project_with_owner_and_binding
        services.artifacts.ingest_rag_document(
            project_id=project.id,
            actor_id=owner.id,
            source_type="manual",
            source_id="d2",
            title="Auth design",
            content="The auth module uses OAuth2 with JWT tokens",
            state="approved",
        )
        results = services.artifacts.search_rag(
            project_id=project.id, actor_id=owner.id, query="auth OAuth2 JWT"
        )
        assert any(r.title == "Auth design" for r, _ in results)


class TestContextVersionPersistence:
    def test_second_context_version_persists(self, services, project_with_owner_and_binding):
        """Real run: the second build_context for an execution dropped the
        version with IntegrityError (UNIQUE(execution_id) WHERE active=1)."""
        owner, project, _binding = project_with_owner_and_binding
        event = services.plans.ingest_conversation_event(
            project_id=project.id,
            actor_id=owner.id,
            source="web",
            origin_kind="authenticated_human",
            content="build something",
        )
        plan = services.plans.create_plan(project_id=project.id, actor_id=owner.id)
        from zero.domain.plans import PlanRevisionContent

        services.plans.propose_revision(
            plan_id=plan.id,
            project_id=project.id,
            actor_id=owner.id,
            content=PlanRevisionContent(
                objective="implement the thing",
                scope=("src",),
                constraints=(),
                acceptance_criteria=("done",),
                risks=(),
                unresolved_questions=(),
                source_event_ids=(event.id,),
            ),
        )
        _approval, handoff = services.plans.approve_revision(
            plan_id=plan.id,
            project_id=project.id,
            actor_id=owner.id,
            expected_revision_number=1,
            idempotency_key="cv-test",
        )
        from zero.app.worker_service import TaskSpec

        execution = services.worker.create_execution_from_handoff(
            handoff_id=handoff.id,
            project_id=project.id,
            actor_id=owner.id,
            task_specs=[TaskSpec(key="t1", objective="do it")],
        )
        for i in range(3):
            text, _ledger = services.context_builder.build_context(
                project_id=project.id,
                execution_id=execution.id,
                actor_id=owner.id,
                agent_type_id=None,
                system_message="sys",
                user_prefix="prefix",
                plan_contract="contract",
                execution_snapshot="{}",
                conversation_tail=[],
                query=f"query {i}",
                context_window=100_000,
            )
            assert text
        active = services.compaction.get_active_context(execution.id)
        assert active is not None, "an active context version must survive repeated builds"
        assert active.version == 3, "all three versions must persist with the latest active"


class TestRestartReleasesInstanceLeases:
    def test_recovered_task_releases_agent_type_instances(
        self, services, project_with_owner_and_binding
    ):
        """Real-run failure: a worker killed mid-task left its agent-type
        instance 'running' forever, so the type's concurrency budget was
        exhausted and every later claim failed with
        ConcurrencyLimitExceededError. recover_after_restart must release
        the leaked leases of reclaimed tasks."""
        from zero.domain.agent_types import AgentInstanceState

        owner, project, _binding = project_with_owner_and_binding
        agent_type = services.agent_types.create_type(
            project_id=project.id,
            actor_id=owner.id,
            name="Solo",
            responsibility="one at a time",
            memory_scope="",
            max_concurrent_instances=1,
        )
        event = services.plans.ingest_conversation_event(
            project_id=project.id,
            actor_id=owner.id,
            source="web",
            origin_kind="authenticated_human",
            content="crash recovery scenario",
        )
        plan = services.plans.create_plan(project_id=project.id, actor_id=owner.id)
        from zero.app.worker_service import TaskSpec
        from zero.domain.plans import PlanRevisionContent

        services.plans.propose_revision(
            plan_id=plan.id,
            project_id=project.id,
            actor_id=owner.id,
            content=PlanRevisionContent(
                objective="interrupted task scenario",
                scope=("src",),
                constraints=(),
                acceptance_criteria=("done",),
                risks=(),
                unresolved_questions=(),
                source_event_ids=(event.id,),
            ),
        )
        _approval, handoff = services.plans.approve_revision(
            plan_id=plan.id,
            project_id=project.id,
            actor_id=owner.id,
            expected_revision_number=1,
            idempotency_key="lease-test",
        )
        execution = services.worker.create_execution_from_handoff(
            handoff_id=handoff.id,
            project_id=project.id,
            actor_id=owner.id,
            task_specs=[
                TaskSpec(key="a", objective="first"),
                TaskSpec(key="b", objective="second"),
            ],
        )
        tasks = services.worker.list_tasks(
            execution.id, project_id=project.id, actor_id=owner.id
        )
        victim = tasks[0]
        services.worker.claim_task(
            execution_id=execution.id,
            task_id=victim.id,
            lease_owner="doomed-worker",
            project_id=project.id,
            actor_id=owner.id,
        )
        # the runtime (AgentRuntime.run_task) leases an agent-type
        # instance for the claimed task — reproduce that lease directly
        services.agent_types._repo.lease_instance_for_task(
            project_id=project.id, type_id=agent_type.id, task_id=victim.id
        )
        # type budget 1/1 now in use
        running = services.agent_types._repo.count_running_instances(agent_type.id)
        assert running == 1

        # expire the lease by backdating, then run restart recovery
        attempts = services.worker._execution_repo.list_attempts_for_task(
            victim.id, project_id=project.id
        )
        conn = services.worker._execution_repo._database.connect()
        conn.execute(
            "UPDATE task_attempts SET lease_expires_at=? WHERE id=?",
            ("2000-01-01T00:00:00.000000Z", attempts[-1].id.value),
        )
        conn.commit()
        services.worker.recover_after_restart(
            execution_id=execution.id, project_id=project.id, actor_id=owner.id
        )
        released = services.agent_types._repo.count_running_instances(agent_type.id)
        assert released == 0, "recovery must release leaked instance leases"

        # and the next claim succeeds (budget available again)
        nxt = services.worker.list_ready_tasks(
            execution.id, project_id=project.id, actor_id=owner.id
        )
        assert any(t.id == victim.id for t in nxt)
        attempt2 = services.worker.claim_task(
            execution_id=execution.id,
            task_id=victim.id,
            lease_owner="fresh-worker",
            project_id=project.id,
            actor_id=owner.id,
        )
        assert attempt2.state == "running"
        assert AgentInstanceState


class TestSchedulerResolvesRepository:
    def test_run_without_repository_id_gets_workspace_evidence(
        self, services, project_with_owner_and_binding, tmp_path, monkeypatch
    ):
        """Real-run integration gap: the managed worker host never passes
        repository_id, so coding tasks ran with NO workspace tools. The
        scheduler must resolve the project's single repository itself."""
        from zero.domain.plans import PlanRevisionContent

        owner, project, _binding = project_with_owner_and_binding
        # register exactly one repository
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        import subprocess

        subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo_dir, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo_dir, check=True)
        (repo_dir / "README.md").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=repo_dir, check=True)
        services.worktree.register_repository(
            project_id=project.id,
            actor_id=owner.id,
            name="solo-repo",
            local_path=str(repo_dir),
            source="system",
        )
        event = services.plans.ingest_conversation_event(
            project_id=project.id,
            actor_id=owner.id,
            source="web",
            origin_kind="authenticated_human",
            content="coding task scenario",
        )
        plan = services.plans.create_plan(project_id=project.id, actor_id=owner.id)
        services.plans.propose_revision(
            plan_id=plan.id,
            project_id=project.id,
            actor_id=owner.id,
            content=PlanRevisionContent(
                objective="write a module",
                scope=("src",),
                constraints=(),
                acceptance_criteria=("module exists",),
                risks=(),
                unresolved_questions=(),
                source_event_ids=(event.id,),
            ),
        )
        _approval, handoff = services.plans.approve_revision(
            plan_id=plan.id,
            project_id=project.id,
            actor_id=owner.id,
            expected_revision_number=1,
            idempotency_key="repo-resolve-test",
        )
        # run_once WITHOUT repository_id (the managed-worker-host shape).
        # The task now runs inside a real worktree demanding real diff
        # evidence; the fake provider changes no files, so the runtime
        # correctly FAILS it — that refusal is the enforcement we are
        # asserting for (before the fix, tasks ran toolless and
        # "completed" on bare provider responses).
        try:
            services.scheduler.run_once(
                project_id=project.id,
                actor_id=owner.id,
                lease_owner="test",
                provider="fake",
                model_name="fake-standard",
            )
        except RuntimeError:
            pass  # expected: no genuine diff evidence from a no-op agent
        from zero.domain.execution import ExecutionId

        h = services.plans.get_handoff(
            handoff.id, project_id=project.id, actor_id=owner.id
        )
        assert h.execution_id is not None
        tasks = services.worker.list_tasks(
            ExecutionId(h.execution_id), project_id=project.id, actor_id=owner.id
        )
        assert tasks, "execution must have tasks"
        assert all(
            set(t.expected_evidence) >= {"diff", "test_report", "exit_status"}
            for t in tasks
        ), f"tasks must require workspace evidence, got {tasks[0].expected_evidence}"


class TestDecomposerPerTaskEvidence:
    def test_evidence_field_parsed_and_validated(self):
        """Real-run: uniform diff evidence failed read-only tasks ('read
        NOTES.md' changes no files). The decomposer graph now accepts an
        optional per-task 'evidence' array (validated), and the scheduler
        prefers it over the uniform default."""
        import json as _json

        from zero.app.task_decomposition import validate_decomposition

        graph = validate_decomposition(
            _json.dumps(
                [
                    {
                        "key": "read_notes",
                        "objective": "Read NOTES.md and report conventions",
                        "evidence": ["provider_response"],
                    },
                    {
                        "key": "implement",
                        "objective": "Create stats.py with the functions",
                        "evidence": ["diff", "test_report", "exit_status"],
                        "depends_on": ["read_notes"],
                    },
                    {"key": "vague", "objective": "No explicit choice"},
                ]
            )
        )
        assert graph is not None
        by_key = {s.key: s for s in graph.specs}
        assert by_key["read_notes"].expected_evidence == ("provider_response",)
        assert by_key["implement"].expected_evidence == ("diff", "test_report", "exit_status")
        assert by_key["vague"].expected_evidence == ()

        # invalid labels invalidate the whole graph (fail closed)
        assert (
            validate_decomposition(
                _json.dumps([{"key": "a", "objective": "x", "evidence": ["hacky"]}])
            )
            is None
        )
        assert (
            validate_decomposition(
                _json.dumps([{"key": "a", "objective": "x", "evidence": "diff"}])
            )
            is None
        )
