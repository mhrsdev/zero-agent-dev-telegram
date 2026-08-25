"""GAP 9 tests: memory delta extraction and durable knowledge writes."""

from __future__ import annotations

import json

import pytest

from zero.app.memory_delta import (
    MemoryDeltaWriter,
    extract_memory_deltas,
)
from zero.app.services import build_services
from zero.app.worker_service import TaskSpec
from zero.config import Settings
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations

LLM_STYLE_SUMMARY = """## Session checkpoint

- Current goal: ship the retry backoff for failed tasks.
- Accepted decisions:
  - Use exponential backoff capped at 3600 seconds.
  - Honor provider Retry-After headers before the formula.
- Modified artifacts: src/zero/app/retry_backoff.py
- Unresolved tasks:
  - Wire the scheduler gating.
- Blockers or failures:
  - First migration attempt collided with an existing column name.
- Next safe action: run the full suite again.
"""

FALLBACK_SUMMARY = """Compaction summary
- Current goal: derived from the plan contract and latest user objective; 3 source message(s) were considered.
- Accepted decisions: only decisions present in the retained messages above; typed execution state remains authoritative.
- Modified artifacts: none are asserted by this fallback summary; durable artifacts and diffs remain the source of truth.
- Unresolved tasks: unchanged; the execution graph state is authoritative.
- Blockers or failures: none recorded by this summary.
- Next safe action: continue the task from the durable execution state and re-derive context from typed snapshots.

Source digest:
Message counts: user=2, tool=1."""


class TestExtractMemoryDeltas:
    def test_extracts_decisions_and_failures(self):
        records = extract_memory_deltas(LLM_STYLE_SUMMARY)
        kinds = [r.kind for r in records]
        assert kinds.count("decision") == 2
        assert kinds.count("failure") == 1
        assert any("exponential backoff" in r.content for r in records)
        assert any("Retry-After" in r.content for r in records)

    def test_fallback_template_yields_nothing(self):
        assert extract_memory_deltas(FALLBACK_SUMMARY) == []

    def test_empty_and_none_safe(self):
        assert extract_memory_deltas("") == []
        assert extract_memory_deltas("   ") == []

    def test_non_bullet_lines_ignored(self):
        summary = (
            "- Accepted decisions:\n"
            "We agreed on prose without a bullet.\n"
            "  - real bullet decision\n"
            "- Blockers or failures:\n"
            "  - a failure happened\n"
            "- Next safe action: continue."
        )
        records = extract_memory_deltas(summary)
        assert [r.content for r in records] == [
            "real bullet decision",
            "a failure happened",
        ]

    def test_redaction_applies_to_extracted_content(self):
        summary = (
            "- Accepted decisions:\n"
            "  - store the api_key=sk-secret123 in the vault\n"
            "- Blockers or failures:\n"
            "  - none\n"
            "- Next safe action: continue."
        )
        records = extract_memory_deltas(summary)
        joined = " ".join(r.content for r in records)
        assert "sk-secret123" not in joined


@pytest.fixture
def services(test_settings: Settings):
    database = Database(test_settings)
    apply_migrations(database)
    return build_services(test_settings, database)


class TestMemoryDeltaWriter:
    def _setup(self, services):
        owner = services.identity.create_user(display_name="md owner")
        project = services.identity.create_project(owner_id=owner.id, name="MD")
        agent_type = services.agent_types.create_type(
            project_id=project.id,
            actor_id=owner.id,
            name="worker",
            responsibility="does work",
            memory_scope="project",
        )
        return owner, project, agent_type

    def test_writes_knowledge_and_artifact(self, services):
        owner, project, agent_type = self._setup(services)
        from zero.domain.execution import ExecutionId

        writer = MemoryDeltaWriter(
            artifact_service=services.artifacts,
            agent_type_service=services.agent_types,
        )
        artifact_id = writer.write(
            project_id=project.id,
            execution_id=ExecutionId("exec_memdelta000000000000"),
            actor_id=owner.id,
            agent_type_id=agent_type.id,
            compaction_record_id=_compaction_id(),
            summary=LLM_STYLE_SUMMARY,
        )
        assert artifact_id is not None
        knowledge = services.agent_types.list_knowledge_for_type(
            project.id, agent_type.id, actor_id=owner.id
        )
        kinds = sorted(k.kind for k in knowledge)
        assert "decision" in kinds and "failure" in kinds
        payload = json.loads(
            services.artifacts.get_artifact(
                project_id=project.id, actor_id=owner.id, artifact_id=artifact_id
            ).content
        )
        assert len(payload["records"]) == 3

    def test_disabled_or_missing_type_writes_nothing(self, services):
        owner, project, _agent_type = self._setup(services)
        from zero.domain.execution import ExecutionId

        writer = MemoryDeltaWriter(
            artifact_service=services.artifacts,
            agent_type_service=services.agent_types,
        )
        result = writer.write(
            project_id=project.id,
            execution_id=ExecutionId("exec_memdelta000000000001"),
            actor_id=owner.id,
            agent_type_id=None,
            compaction_record_id=_compaction_id(),
            summary=LLM_STYLE_SUMMARY,
        )
        assert result is None

    def test_fallback_summary_writes_nothing(self, services):
        owner, project, agent_type = self._setup(services)
        from zero.domain.execution import ExecutionId

        before = len(
            services.agent_types.list_knowledge_for_type(
                project.id, agent_type.id, actor_id=owner.id
            )
        )
        writer = MemoryDeltaWriter(
            artifact_service=services.artifacts,
            agent_type_service=services.agent_types,
        )
        result = writer.write(
            project_id=project.id,
            execution_id=ExecutionId("exec_memdelta000000000002"),
            actor_id=owner.id,
            agent_type_id=agent_type.id,
            compaction_record_id=_compaction_id(),
            summary=FALLBACK_SUMMARY,
        )
        assert result is None
        assert (
            len(
                services.agent_types.list_knowledge_for_type(
                    project.id, agent_type.id, actor_id=owner.id
                )
            )
            == before
        )


def _compaction_id():
    from zero.domain.context import CompactionRecordId

    return CompactionRecordId("comp_memdelta00000000000000")


class TestCompactionIntegration:
    def test_compact_with_llm_summary_and_optin_populates_field(self, services):
        """Full path: compact() → deltas written → field set."""
        from zero.domain.context import CompactionRecord

        owner = services.identity.create_user(display_name="ci owner")
        project = services.identity.create_project(owner_id=owner.id, name="CI")
        agent_type = services.agent_types.create_type(
            project_id=project.id,
            actor_id=owner.id,
            name="md-worker",
            responsibility="work",
            memory_scope="project",
            model_policy={"memory_delta_enabled": "1"},
        )
        event = services.plans.ingest_conversation_event(
            project_id=project.id,
            actor_id=owner.id,
            source="web",
            origin_kind="authenticated_human",
            content="go",
        )
        plan = services.plans.create_plan(project_id=project.id, actor_id=owner.id)
        from zero.domain.plans import PlanRevisionContent

        services.plans.propose_revision(
            plan_id=plan.id,
            project_id=project.id,
            actor_id=owner.id,
            content=PlanRevisionContent(
                objective="compact me",
                scope=("backend",),
                constraints=(),
                acceptance_criteria=("ok",),
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
            idempotency_key="md-approval",
        )
        execution = services.worker.create_execution_from_handoff(
            handoff_id=handoff.id,
            project_id=project.id,
            actor_id=owner.id,
            task_specs=[
                TaskSpec(
                    key="t",
                    objective="work",
                    permitted_scope=("backend",),
                    expected_evidence=("provider_response",),
                ),
            ],
        )

        record = services.compaction.compact(
            project_id=project.id,
            execution_id=execution.id,
            actor_id=owner.id,
            system_message="sys",
            user_prefix=f"Project {project.id.value}",
            plan_contract="objective",
            execution_snapshot="{}",
            conversation_messages=[{"role": "user", "content": "hello"}],
            context_window=200_000,
            summary=LLM_STYLE_SUMMARY,
            agent_type_id=agent_type.id,
            memory_delta_enabled=True,
        )
        assert isinstance(record, CompactionRecord)
        assert record.memory_delta_artifact_id is not None
        knowledge = services.agent_types.list_knowledge_for_type(
            project.id, agent_type.id, actor_id=owner.id
        )
        assert len(knowledge) >= 3

    def test_compact_without_optin_leaves_field_null(self, services):
        owner = services.identity.create_user(display_name="nopt owner")
        project = services.identity.create_project(owner_id=owner.id, name="NoOpt")
        event = services.plans.ingest_conversation_event(
            project_id=project.id,
            actor_id=owner.id,
            source="web",
            origin_kind="authenticated_human",
            content="go",
        )
        plan = services.plans.create_plan(project_id=project.id, actor_id=owner.id)
        from zero.domain.plans import PlanRevisionContent

        services.plans.propose_revision(
            plan_id=plan.id,
            project_id=project.id,
            actor_id=owner.id,
            content=PlanRevisionContent(
                objective="compact me too",
                scope=("backend",),
                constraints=(),
                acceptance_criteria=("ok",),
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
            idempotency_key="noopt-approval",
        )
        execution = services.worker.create_execution_from_handoff(
            handoff_id=handoff.id,
            project_id=project.id,
            actor_id=owner.id,
            task_specs=[
                TaskSpec(
                    key="t",
                    objective="work",
                    permitted_scope=("backend",),
                    expected_evidence=("provider_response",),
                ),
            ],
        )
        record = services.compaction.compact(
            project_id=project.id,
            execution_id=execution.id,
            actor_id=owner.id,
            system_message="sys",
            user_prefix=f"Project {project.id.value}",
            plan_contract="objective",
            execution_snapshot="{}",
            conversation_messages=[{"role": "user", "content": "hello"}],
            context_window=200_000,
            summary=LLM_STYLE_SUMMARY,
        )
        assert record.memory_delta_artifact_id is None


@pytest.mark.parametrize(
    "summary",
    ["", FALLBACK_SUMMARY],
)
def test_writer_never_crashes_on_degenerate_summaries(services, summary):
    owner = services.identity.create_user(display_name="deg owner")
    project = services.identity.create_project(owner_id=owner.id, name="Deg")
    agent_type = services.agent_types.create_type(
        project_id=project.id,
        actor_id=owner.id,
        name="deg-worker",
        responsibility="work",
        memory_scope="project",
    )
    from zero.domain.context import CompactionRecordId
    from zero.domain.execution import ExecutionId

    writer = MemoryDeltaWriter(
        artifact_service=services.artifacts,
        agent_type_service=services.agent_types,
    )
    result = writer.write(
        project_id=project.id,
        execution_id=ExecutionId("exec_memdelta000000000003"),
        actor_id=owner.id,
        agent_type_id=agent_type.id,
        compaction_record_id=CompactionRecordId("comp_memdelta00000000000001"),
        summary=summary,
    )
    assert result is None
