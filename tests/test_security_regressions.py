"""Regression coverage for production-only failures and trust boundaries."""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from zero.app.api import create_app
from zero.app.auth_service import AuthenticationError
from zero.app.services import build_services
from zero.app.worker_service import TaskSpec
from zero.config import Settings
from zero.domain.identity import IdentityError, ProjectId, UserId, UserNotFoundError
from zero.domain.plans import PlanRevisionContent
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations


def _file_services(tmp_path):
    settings = Settings.load_for_test(database_url=f"sqlite:///{tmp_path / 'zero.db'}")
    database = Database(settings)
    apply_migrations(database)
    return build_services(settings, database)


def test_api_errors_do_not_expose_internal_exception_text(tmp_path, monkeypatch) -> None:
    settings = Settings.load_for_test(database_url=f"sqlite:///{tmp_path / 'api-errors.db'}")
    app = create_app(settings)
    marker = "internal-path=/srv/hidden/request-failure"

    def leak_error(*_args, **_kwargs):
        raise IdentityError(marker)

    monkeypatch.setattr(app.state.services.identity, "create_project", leak_error)

    from httpx import ASGITransport

    async def exercise() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="https://zero.test",
        ) as client:
            response = await client.post(
                "/projects",
                json={"owner_id": "zu_owner", "name": "Project"},
            )
        assert response.status_code == 400
        assert marker not in response.text
        assert "request failed" in response.text

    import anyio

    anyio.run(exercise)


def test_plan_transaction_is_atomic_on_file_sqlite(tmp_path) -> None:
    services = _file_services(tmp_path)
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(owner_id=owner.id, name="Project")
    event = services.plans.ingest_conversation_event(
        project_id=project.id,
        actor_id=owner.id,
        source="web",
        origin_kind="authenticated_human",
        content="Implement the approved change.",
    )
    plan = services.plans.create_plan(project_id=project.id, actor_id=owner.id)
    revision = services.plans.propose_revision(
        plan_id=plan.id,
        project_id=project.id,
        actor_id=owner.id,
        content=PlanRevisionContent(
            objective="Implement the approved change",
            scope=(),
            constraints=(),
            acceptance_criteria=("The change works",),
            risks=(),
            unresolved_questions=(),
            source_event_ids=(event.id,),
        ),
    )
    approval, handoff = services.plans.approve_revision(
        plan_id=plan.id,
        project_id=project.id,
        actor_id=owner.id,
        expected_revision_number=1,
        idempotency_key="file-db-approval",
    )
    execution = services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        project_id=project.id,
        actor_id=owner.id,
        task_specs=[TaskSpec(key="task", objective="Run the task")],
    )
    task = services.worker.list_ready_tasks(
        execution.id,
        project_id=execution.project_id,
        actor_id=services.identity.get_project(execution.project_id).owner_user_id,
    )[0]
    attempt = services.worker.claim_task(
        execution_id=execution.id,
        task_id=task.id,
        lease_owner="test-worker",
        project_id=execution.project_id,
        actor_id=services.identity.get_project(execution.project_id).owner_user_id,
    )

    reopened = Database(Settings.load_for_test(database_url=f"sqlite:///{tmp_path / 'zero.db'}"))
    persisted = build_services(
        Settings.load_for_test(database_url=f"sqlite:///{tmp_path / 'zero.db'}"), reopened
    )
    assert (
        persisted.plans.get_plan(
            plan.id,
            project_id=project.id,
            actor_id=owner.id,
        ).current_state
        == "approved"
    )
    assert (
        persisted.plans.get_current_revision(
            plan.id,
            project_id=project.id,
            actor_id=owner.id,
        ).id
        == revision.id
    )
    assert (
        persisted.plans.get_handoff(
            handoff.id,
            project_id=project.id,
            actor_id=owner.id,
        ).id
        == handoff.id
    )
    assert approval.revision_id == revision.id
    assert (
        persisted.worker.get_execution(
            execution.id,
            project_id=execution.project_id,
            actor_id=owner.id,
        ).state
        == "running"
    )
    assert (
        persisted.worker.list_attempts(
            task.id,
            project_id=task.project_id,
            actor_id=owner.id,
        )[0].id
        == attempt.id
    )


def test_create_plan_rolls_back_when_audit_fails(tmp_path, monkeypatch) -> None:
    services = _file_services(tmp_path)
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(owner_id=owner.id, name="Project")

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(services.plans._audit_repo, "insert", fail_audit)
    with pytest.raises(RuntimeError, match="injected audit failure"):
        services.plans.create_plan(project_id=project.id, actor_id=owner.id)

    conn = services.database.connect()
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM plans WHERE project_id = ?",
            (project.id.value,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 0


def test_nested_transactions_cannot_commit_outer_or_failed_inner_work(
    tmp_path: Path,
) -> None:
    settings = Settings.load_for_test(
        database_url=f"sqlite:///{tmp_path / 'nested-transaction.db'}"
    )
    database = Database(settings)
    apply_migrations(database)
    services = build_services(settings, database)

    with pytest.raises(RuntimeError, match="outer failure"), database.transaction():
        outer_user = services.identity.create_user(display_name="Outer")
        with pytest.raises(RuntimeError, match="inner failure"), database.transaction():
            services.identity.create_user(display_name="Inner")
            raise RuntimeError("inner failure")
        with pytest.raises(UserNotFoundError):
            services.identity.get_user(UserId("zu_missing"))
        assert services.identity.get_user(outer_user.id).id == outer_user.id
        raise RuntimeError("outer failure")

    conn = database.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 0
    finally:
        conn.close()


def test_add_member_rolls_back_when_audit_fails(tmp_path, monkeypatch) -> None:
    services = _file_services(tmp_path)
    owner = services.identity.create_user(display_name="Owner")
    member = services.identity.create_user(display_name="Member")
    project = services.identity.create_project(owner_id=owner.id, name="Project")

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(services.identity._audit_repo, "insert", fail_audit)
    with pytest.raises(RuntimeError, match="injected audit failure"):
        services.identity.add_member(
            project_id=project.id,
            actor_id=owner.id,
            member_id=member.id,
            role="member",
        )

    assert services.identity.resolve_scope(project.id, member.id).role is None


def test_topology_migration_rolls_back_on_file_sqlite(tmp_path, monkeypatch) -> None:
    services = _file_services(tmp_path)
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(owner_id=owner.id, name="Project")
    source = services.agent_types.create_type(
        project_id=project.id,
        actor_id=owner.id,
        name="Source",
        responsibility="Own the source",
        memory_scope="Source facts",
    )
    record = services.agent_types.add_knowledge(
        project_id=project.id,
        type_id=source.id,
        actor_id=owner.id,
        kind="fact",
        content="must survive rollback",
    )
    reassign = services.agent_types._repo.reassign_knowledge

    def fail_after_write(*args, **kwargs):
        reassign(*args, **kwargs)
        raise RuntimeError("injected topology failure")

    monkeypatch.setattr(services.agent_types._repo, "reassign_knowledge", fail_after_write)
    with pytest.raises(RuntimeError, match="injected topology failure"):
        services.agent_types.split_type(
            project_id=project.id,
            source_type_id=source.id,
            actor_id=owner.id,
            destination_specs=[("Destination", "Own destination", "Facts")],
            knowledge_routing={"Destination": [record.id]},
        )

    reopened = _file_services(tmp_path)
    types = reopened.agent_types.list_types(project.id, include_archived=True)
    assert [(item.name, item.state) for item in types] == [("Source", "active")]
    persisted_record = reopened.agent_types.list_knowledge_for_type(
        project.id, source.id, actor_id=owner.id, include_archived=True
    )[0]
    assert persisted_record.agent_type_id == source.id
    assert persisted_record.state == record.state
    assert reopened.agent_types.list_snapshots(project.id) == []


@pytest.mark.anyio
async def test_authenticated_actor_cannot_be_spoofed(tmp_path) -> None:
    settings = Settings.load_for_test(
        database_url=f"sqlite:///{tmp_path / 'auth.db'}",
        auth_required=True,
        secret_key=SecretStr("s" * 48),
        bootstrap_token=SecretStr("b" * 48),
    )
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://zero.test") as client:
        assert (await client.get("/healthz")).status_code == 200
        assert (await client.post("/users", json={"display_name": "Untrusted"})).status_code == 401

        bootstrap = await client.post(
            "/auth/bootstrap",
            headers={"X-Zero-Bootstrap-Token": "b" * 48},
            json={"display_name": "Owner"},
        )
        assert bootstrap.status_code == 201
        credentials = bootstrap.json()
        owner_id = credentials["user"]["id"]
        bearer = {"Authorization": f"Bearer {credentials['access_token']}"}

        created = await client.post(
            "/projects",
            headers=bearer,
            json={"owner_id": owner_id, "name": "Owned"},
        )
        assert created.status_code == 201

        spoofed = await client.post(
            "/projects",
            headers=bearer,
            json={"owner_id": "zu_attacker000000000000000000", "name": "Nope"},
        )
        assert spoofed.status_code == 403

        services = app.state.services
        other = services.identity.create_user(display_name="Isolated User", source="system")
        isolated = services.identity.create_project(
            owner_id=other.id, name="Isolated Project", source="system"
        )
        other_token, _ = services.auth.issue_access_token(other.id)
        other_bearer = {"Authorization": f"Bearer {other_token}"}

        for _ in range(20):
            disposable_token, _ = services.auth.issue_access_token(UserId(owner_id))
            services.auth.revoke(disposable_token, UserId(owner_id))
            with pytest.raises(AuthenticationError):
                services.auth.authenticate(disposable_token)
        viewer = services.identity.create_user(display_name="Viewer", source="system")
        viewer_token, _ = services.auth.issue_access_token(viewer.id)
        viewer_bearer = {"Authorization": f"Bearer {viewer_token}"}

        services.identity.add_member(
            project_id=ProjectId(created.json()["id"]),
            actor_id=UserId(owner_id),
            member_id=viewer.id,
            role="viewer",
        )
        candidate = services.identity.create_user(display_name="Candidate", source="system")
        denied_member_add = await client.post(
            f"/projects/{created.json()['id']}/members",
            headers=viewer_bearer,
            json={"member_id": candidate.id.value, "role": "member"},
        )
        assert denied_member_add.status_code == 403

        project_id = ProjectId(created.json()["id"])
        assert (
            await client.post(
                f"/projects/{project_id.value}/secrets",
                headers=viewer_bearer,
                json={"name": "blocked", "secret_type": "token", "value": "x"},
            )
        ).status_code == 403
        echo = services.tools.register_echo_tool()
        assert (
            await client.post(
                f"/projects/{project_id.value}/tool-grants",
                headers=viewer_bearer,
                json={"tool_id": echo.id.value, "agent_scope": "main_worker"},
            )
        ).status_code == 403
        services.tools.grant_tool(
            project_id=project_id,
            actor_id=UserId(owner_id),
            tool_id=echo.id,
            agent_scope="main_worker",
        )
        assert (
            await client.post(
                f"/projects/{project_id.value}/tool-invocations",
                headers=viewer_bearer,
                json={
                    "tool_name": "echo",
                    "agent_scope": "main_worker",
                    "input_data": {"message": "blocked"},
                },
            )
        ).status_code == 403
        assert (
            await client.get(f"/projects/{project_id.value}/audit", headers=viewer_bearer)
        ).status_code == 403
        viewer_audit = await client.get("/web/audit", headers=viewer_bearer)
        assert viewer_audit.status_code == 200
        assert project_id.value not in viewer_audit.text

        assert (await client.get(f"/users/{owner_id}", headers=viewer_bearer)).status_code == 403
        assert (
            await client.post(
                f"/users/{owner_id}/external-identities",
                headers=viewer_bearer,
                json={
                    "platform": "telegram",
                    "external_id": "untrusted-other-link",
                    "verified": True,
                },
            )
        ).status_code == 403
        self_link = await client.post(
            f"/users/{owner_id}/external-identities",
            headers=bearer,
            json={
                "platform": "telegram",
                "external_id": "self-asserted-link",
                "verified": True,
            },
        )
        assert self_link.status_code == 201
        assert self_link.json()["verified_at"] is None

        assert (
            await client.get(f"/web/projects/{isolated.id.value}", headers=bearer)
        ).status_code == 404
        project_list = await client.get("/web/projects", headers=bearer)
        assert "Isolated Project" not in project_list.text
        user_list = await client.get("/web/users", headers=bearer)
        assert "Isolated User" not in user_list.text
        audit = await client.get("/web/audit", headers=bearer)
        assert isolated.id.value not in audit.text
        assert (await client.get("/web/projects", headers=other_bearer)).status_code == 200

        rejected_login = await client.post(
            "/web/login",
            data={"access_token": credentials["access_token"]},
            headers={"Origin": "https://evil.test"},
            follow_redirects=False,
        )
        assert rejected_login.status_code == 403
        assert "set-cookie" not in rejected_login.headers

        login = await client.post(
            "/web/login",
            data={"access_token": credentials["access_token"]},
            headers={"Origin": "https://zero.test"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        assert "httponly" in login.headers["set-cookie"].lower()
        assert (await client.get("/web/")).status_code == 200
        logout = await client.post(
            "/web/logout",
            headers={"Origin": "https://zero.test"},
            follow_redirects=False,
        )
        assert logout.status_code == 303
        assert (await client.get("/web/")).status_code == 401

        assert (
            await client.post(
                "/auth/bootstrap",
                headers={"X-Zero-Bootstrap-Token": "b" * 48},
                json={"display_name": "Second owner"},
            )
        ).status_code == 409
        assert (
            await client.get(
                f"/projects/{created.json()['id']}",
                headers={"Authorization": "Bearer invalid"},
            )
        ).status_code == 401
