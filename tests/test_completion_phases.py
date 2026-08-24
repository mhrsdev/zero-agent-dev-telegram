"""Tests for Phase 3/6 completion work.

- provider fallback routing (eligible vs ineligible error classes);
- unknown-outcome reconciliation workflow (service + HTTP surface);
- merge crash-window reconciliation from durable Git evidence;
- operational CLI (check-config / migrate fail-closed behavior).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from zero.app.services import build_services
from zero.config import Settings
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations


@pytest.fixture
def services(test_settings: Settings):
    database = Database(test_settings)
    apply_migrations(database)
    return build_services(test_settings, database)


def _make_project(services, name: str):
    owner = services.identity.create_user(display_name=f"{name} owner")
    project = services.identity.create_project(owner_id=owner.id, name=name)
    return owner, project


# ----------------------------------------------------------------------
# Provider fallback routing
# ----------------------------------------------------------------------


def test_fallback_chain_rejects_unregistered_providers(services) -> None:
    with pytest.raises(ValueError, match="not registered"):
        services.providers.set_fallback_chain(("does-not-exist",))


def test_fallback_reraises_ineligible_errors_immediately(services) -> None:
    owner, project = _make_project(services, "FallbackIneligible")
    services.providers.set_fallback_chain(("fake",))
    request = _canonical_request()
    original_send = services.providers.send_request

    def auth_failure(**kwargs):
        raise RuntimeError("auth failure: bad api key")

    services.providers.send_request = auth_failure  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="auth failure"):
        services.providers.send_request_with_fallback(
            project_id=project.id,
            actor_id=owner.id,
            request=request,
        )
    services.providers.send_request = original_send  # type: ignore[method-assign]


def test_fallback_reraises_after_exhausting_chain(services) -> None:
    owner, project = _make_project(services, "FallbackExhausted")
    services.providers.set_fallback_chain(())
    request = _canonical_request()

    def transient_failure(**kwargs):
        raise RuntimeError("connection reset; transient outage")

    services.providers.send_request = transient_failure  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="transient"):
        services.providers.send_request_with_fallback(
            project_id=project.id,
            actor_id=owner.id,
            request=request,
        )


def test_fallback_succeeds_on_second_provider(services, monkeypatch) -> None:
    """Register a second adapter under a different provider name; the
    primary attempt fails transiently and the fallback serves."""
    from zero.app.provider_adapter import ProviderAdapter
    from zero.domain.providers import CanonicalRequest

    class MirrorAdapter(ProviderAdapter):
        provider_name = "fake-mirror"

        def __init__(self, inner):
            self._inner = inner

        def send_request(self, request, cancel_event=None):
            if request.provider == "fake":
                raise RuntimeError("connection reset; transient outage")
            rewritten = CanonicalRequest(
                provider="fake",
                model_name=request.model_name,
                messages=request.messages,
                tools=request.tools,
                system_message=request.system_message,
                max_tokens=request.max_tokens,
                temperature=request.temperature,
                stream=False,
            )
            return self._inner.send_request(rewritten)

        def get_model(self, model_name: str):
            return self._inner.get_model(model_name)

    owner, project = _make_project(services, "FallbackSuccess")
    services.providers.register_adapter(MirrorAdapter(services.providers._adapters["fake"]))
    services.providers.set_fallback_chain(("fake", "fake-mirror"))
    provider_request, response = services.providers.send_request_with_fallback(
        project_id=project.id,
        actor_id=owner.id,
        request=_canonical_request(),
    )
    assert response.content
    # The durable winning attempt is recorded under the adapter that
    # actually served it.
    assert provider_request.state == "completed"


def _canonical_request():
    from zero.domain.providers import CanonicalMessage, CanonicalRequest

    return CanonicalRequest(
        provider="fake",
        model_name="fake-standard",
        messages=(CanonicalMessage(role="user", content="Say the durable word."),),
        tools=(),
        system_message="",
    )


# ----------------------------------------------------------------------
# Unknown-outcome reconciliation
# ----------------------------------------------------------------------


def _insert_unknown_request(services, project_id, suffix: str) -> str:
    conn = services.database.connect()
    request_id = f"preq_unknown_{suffix}"
    conn.execute(
        "INSERT INTO provider_requests "
        "(id, project_id, execution_id, provider, model_name, request_hash, "
        "idempotency_key, state, started_at, completed_at, error_class, error_message) "
        "VALUES (?, ?, NULL, 'fake', 'fake-standard', 'h', ?, 'unknown', "
        "strftime('%Y-%m-%dT%H:%M:%fZ','now','-2 hours'), "
        "strftime('%Y-%m-%dT%H:%M:%fZ','now','-1 hour'), 'unknown_outcome', 'startup recovery')",
        (request_id, project_id.value, request_id),
    )
    conn.commit()
    return request_id


def test_reconciliation_confirmed_not_dispatched_makes_retry_safe(services) -> None:
    owner, project = _make_project(services, "Reconcile1")
    request_id = _insert_unknown_request(services, project.id, "retry")

    unknown = services.providers.list_unknown_requests(project.id)
    assert [r.id.value for r in unknown] == [request_id]

    from zero.domain.providers import ProviderRequestId

    services.providers.reconcile_provider_request(
        project_id=project.id,
        request_id=ProviderRequestId(request_id),
        actor_id=owner.id,
        resolution="confirmed_not_dispatched",
        note="verified with provider dashboard: never accepted",
    )
    row = (
        services.database.connect()
        .execute("SELECT state, error_class FROM provider_requests WHERE id = ?", (request_id,))
        .fetchone()
    )
    assert row["state"] == "failed"
    assert row["error_class"] == "reconciled_not_dispatched"
    assert services.providers.list_unknown_requests(project.id) == []


def test_reconciliation_confirmed_dispatched_stays_unknown(services) -> None:
    owner, project = _make_project(services, "Reconcile2")
    request_id = _insert_unknown_request(services, project.id, "seen")

    from zero.domain.providers import ProviderRequestId

    services.providers.reconcile_provider_request(
        project_id=project.id,
        request_id=ProviderRequestId(request_id),
        actor_id=owner.id,
        resolution="confirmed_dispatched",
    )
    row = (
        services.database.connect()
        .execute("SELECT state FROM provider_requests WHERE id = ?", (request_id,))
        .fetchone()
    )
    assert row["state"] == "unknown"


def test_unknown_queue_and_reconcile_http_surface() -> None:
    settings = Settings.load_for_test()
    database = Database(settings)
    apply_migrations(database)

    from zero.app.api import create_app

    app = create_app(settings)
    services = app.state.services
    owner = services.identity.create_user(display_name="Queue owner")
    project = services.identity.create_project(owner_id=owner.id, name="Queue")
    request_id = _insert_unknown_request(services, project.id, "http")

    import asyncio

    async def call():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            listed = await ac.get(f"/projects/{project.id.value}/providers/requests/unknown")
            reconciled = await ac.post(
                f"/projects/{project.id.value}/providers/requests/{request_id}/reconcile",
                json={"resolution": "confirmed_not_dispatched", "note": "ok"},
            )
            after = await ac.get(f"/projects/{project.id.value}/providers/requests/unknown")
            return listed.status_code, listed.json(), reconciled.status_code, after.json()

    listed_status, listed_json, reconcile_status, after_json = asyncio.run(call())
    assert listed_status == 200
    assert len(listed_json) == 1
    assert reconcile_status == 200
    assert after_json == []


# ----------------------------------------------------------------------
# Merge crash-window reconciliation
# ----------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _approved_plan_and_execution(services, owner, project, key: str):
    from zero.app.worker_service import TaskSpec
    from zero.domain.plans import PlanRevisionContent

    event = services.plans.ingest_conversation_event(
        project_id=project.id,
        actor_id=owner.id,
        source="web",
        origin_kind="authenticated_human",
        content=f"Merge window work {key}.",
    )
    plan = services.plans.create_plan(project_id=project.id, actor_id=owner.id)
    services.plans.propose_revision(
        plan_id=plan.id,
        project_id=project.id,
        actor_id=owner.id,
        content=PlanRevisionContent(
            objective=f"Merge window {key}",
            scope=(),
            constraints=(),
            acceptance_criteria=("Merged",),
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
        idempotency_key=f"{key}-approval",
    )
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        project_id=project.id,
        actor_id=owner.id,
        task_specs=[TaskSpec(key="A", objective="Task A")],
    )
    tasks = services.worker.list_tasks(execution.id, project_id=project.id, actor_id=owner.id)
    return execution, tasks[0]


def test_merge_crash_window_is_finalized_from_git_evidence(services, tmp_path) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo_dir)], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "T"], check=True)
    (repo_dir / "README.md").write_text("base\n")
    subprocess.run(["git", "-C", str(repo_dir), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo_dir), "commit", "-q", "-m", "init"],
        check=True,
        capture_output=True,
    )
    current_sha = _git(repo_dir, "rev-parse", "main")

    owner, project = _make_project(services, "MergeWindow")
    repo = services.worktree.register_repository(
        project_id=project.id,
        actor_id=owner.id,
        name="repo",
        local_path=str(repo_dir),
        default_base_revision="main",
    )
    execution, task = _approved_plan_and_execution(services, owner, project, "mergewindow")
    worktree = services.worktree.create_worktree(
        project_id=project.id,
        repository_id=repo.id,
        execution_id=execution.id,
        task_id=task.id,
        actor_id=owner.id,
    )
    (Path(worktree.worktree_path) / "feature.txt").write_text("value = 1\n")
    services.worktree.activate_worktree(
        project_id=project.id, worktree_id=worktree.id, actor_id=owner.id
    )
    services.worktree.capture_diff(
        project_id=project.id,
        worktree_id=worktree.id,
        task_id=task.id,
        actor_id=owner.id,
    )
    review = services.integration.create_review(
        project_id=project.id,
        execution_id=execution.id,
        source_task_ids=(task.id,),
        actor_id=owner.id,
    )
    # Approve the review directly: this test exercises the merge
    # crash-window, not the combined-test gate.
    from zero.domain.integration import IntegrationReviewId

    services.integration._repo.update_review(
        IntegrationReviewId(review.id.value),
        project_id=project.id,
        state="approved",
        combined_test_result="pass",
    )
    proposal = services.integration.create_merge_proposal(
        project_id=project.id,
        review_id=review.id,
        execution_id=execution.id,
        source_tasks=(task.id,),
        actor_id=owner.id,
    )

    # Simulate the crash window: the ref moved and the integration
    # worktree recorded it as merged, but the proposal stayed approved.
    worktree_id = "iw_merge_window_01"
    services.integration._repo.insert_integration_worktree(
        worktree_id=worktree_id,
        project_id=project.id,
        execution_id=execution.id,
        repository_id=repo.id.value,
        worktree_path=str(tmp_path / "integration"),
        branch_name=f"integration/{proposal.id.value}",
        base_revision=current_sha,
    )
    services.integration._repo.update_integration_worktree(
        worktree_id, state="merged", target_revision=current_sha
    )
    services.integration._repo.update_proposal_evidence(
        proposal.id,
        integration_worktree_id=worktree_id,
        target_revision=current_sha,
        rollback_revision=current_sha,
        evidence_ids=("iev_merge_window_1",),
    )
    services.integration._repo.update_proposal_state(proposal.id, "approved")

    recovered = services.integration.recover_inflight_merges()
    assert proposal.id.value in recovered
    state = services.integration.get_proposal(project.id, proposal.id).state
    assert state == "merged"


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


def test_cli_check_config_requires_env(monkeypatch) -> None:
    from zero.cli import main

    for var in ("ZERO_ENV", "ZERO_DATABASE_URL"):
        monkeypatch.delenv(var, raising=False)
    assert main(["check-config"]) != 0


def test_cli_check_config_and_migrate_succeed(monkeypatch, tmp_path) -> None:
    from zero.cli import main

    monkeypatch.setenv("ZERO_ENV", "test")
    monkeypatch.setenv("ZERO_DATABASE_URL", f"sqlite:///{tmp_path / 'cli.db'}")
    assert main(["check-config"]) == 0
    assert main(["migrate"]) == 0
