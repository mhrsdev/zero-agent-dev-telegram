from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from zero.app.interface_transport_service import InterfaceTransportService
from zero.app.result_delivery_service import ResultDeliveryError
from zero.app.services import build_services
from zero.app.worker_service import TaskSpec
from zero.config import Settings
from zero.domain.execution import ExecutionId, ExecutionNotFoundError
from zero.domain.plans import PlanRevisionContent
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations


@pytest.fixture
def services(test_settings):
    database = Database(test_settings)
    apply_migrations(database)
    return build_services(test_settings, database)


def _create_execution(services, owner, project):
    event = services.plans.ingest_conversation_event(
        project_id=project.id,
        actor_id=owner.id,
        source="web",
        origin_kind="authenticated_human",
        content="Implement and report the change.",
    )
    plan = services.plans.create_plan(project_id=project.id, actor_id=owner.id)
    services.plans.propose_revision(
        plan_id=plan.id,
        project_id=project.id,
        actor_id=owner.id,
        content=PlanRevisionContent(
            objective="Implement and report the change",
            scope=("src",),
            constraints=(),
            acceptance_criteria=("A result is delivered",),
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
        idempotency_key="delivery-approval",
    )
    return services.worker.create_execution_from_handoff(
        handoff_id=handoff.id,
        project_id=project.id,
        actor_id=owner.id,
        task_specs=[TaskSpec(objective="Implement and report")],
    )


def test_result_delivery_queue_is_idempotent_and_project_scoped(services) -> None:
    owner = services.identity.create_user(display_name="Delivery owner")
    project = services.identity.create_project(owner_id=owner.id, name="Delivery project")
    binding = services.interfaces.create_binding(
        project_id=project.id,
        actor_id=owner.id,
        platform="telegram",
        chat_id="9001",
        topic_id="7",
        is_enabled=True,
    )
    execution = _create_execution(services, owner, project)
    services.worker._execution_repo.update_execution_state(execution.id, "completed")

    first = services.result_delivery.enqueue_execution_result(
        project_id=project.id,
        execution_id=execution.id,
        binding_id=binding.id,
        actor_id=owner.id,
        content="Execution completed with evidence.",
    )
    second = services.result_delivery.enqueue_execution_result(
        project_id=project.id,
        execution_id=execution.id,
        binding_id=binding.id,
        actor_id=owner.id,
        content="Execution completed with evidence.",
    )

    assert first.id == second.id
    assert first.state == "pending"
    assert services.result_delivery.list_pending(project.id) == [first]

    other_owner = services.identity.create_user(display_name="Other owner")
    other_project = services.identity.create_project(
        owner_id=other_owner.id,
        name="Other delivery project",
    )
    with pytest.raises(ExecutionNotFoundError):
        services.result_delivery.enqueue_execution_result(
            project_id=other_project.id,
            execution_id=ExecutionId(execution.id.value),
            binding_id=binding.id,
            actor_id=other_owner.id,
            content="Cross-project result.",
        )


def test_result_delivery_requires_terminal_execution(services) -> None:
    owner = services.identity.create_user(display_name="Pending owner")
    project = services.identity.create_project(owner_id=owner.id, name="Pending project")
    binding = services.interfaces.create_binding(
        project_id=project.id,
        actor_id=owner.id,
        platform="telegram",
        chat_id="9002",
        topic_id=None,
        is_enabled=True,
    )
    execution = _create_execution(services, owner, project)

    with pytest.raises(ResultDeliveryError, match="terminal"):
        services.result_delivery.enqueue_execution_result(
            project_id=project.id,
            execution_id=execution.id,
            binding_id=binding.id,
            actor_id=owner.id,
            content="Premature result.",
        )


class _MessageResponse:
    status_code = 200

    @staticmethod
    def json():
        return {"ok": True, "result": {"message_id": 321}}


class _MessageTransport:
    def __init__(self) -> None:
        self.calls = []

    def request(self, method, url, *, headers=None, json=None, timeout=None):
        self.calls.append((method, url, headers, json, timeout))
        return _MessageResponse()


class _AmbiguousResponse:
    status_code = 200

    @staticmethod
    def json():
        return {"ok": True, "result": {}}


class _AmbiguousTransport(_MessageTransport):
    def request(self, method, url, *, headers=None, json=None, timeout=None):
        self.calls.append((method, url, headers, json, timeout))
        return _AmbiguousResponse()


class _RequestErrorTransport(_MessageTransport):
    def request(self, method, url, *, headers=None, json=None, timeout=None):
        self.calls.append((method, url, headers, json, timeout))
        raise httpx.RequestError(
            "synthetic post-dispatch failure",
            request=httpx.Request("POST", url),
        )


class _FailureResponse:
    status_code = 400

    @staticmethod
    def json():
        return {"ok": False}


class _FailureTransport(_MessageTransport):
    def request(self, method, url, *, headers=None, json=None, timeout=None):
        self.calls.append((method, url, headers, json, timeout))
        return _FailureResponse()


def test_transport_bridge_resolves_binding_secret_and_returns_message_id(test_settings) -> None:
    settings = Settings.load_for_test(
        secret_key=SecretStr("synthetic-test-secret-material-0123456789")
    )
    database = Database(settings)
    apply_migrations(database)
    services = build_services(settings, database)
    owner = services.identity.create_user(display_name="Transport owner")
    project = services.identity.create_project(owner_id=owner.id, name="Transport project")
    secret = services.secrets.store(
        project_id=project.id,
        name="telegram-bot",
        secret_type="token",
        value="synthetic-telegram-token",
        actor_id=owner.id,
    )
    binding = services.interfaces.create_binding(
        project_id=project.id,
        actor_id=owner.id,
        platform="telegram",
        chat_id="9003",
        bot_token_ref=secret.id.value,
        is_enabled=True,
    )
    transport = _MessageTransport()
    bridge = InterfaceTransportService(
        services.interfaces,
        services.interfaces._repo,
        settings,
        secret_service=services.secrets,
        transport=transport,
    )

    message_id = bridge.send_message(
        project_id=project.id,
        binding_id=binding.id,
        actor_id=owner.id,
        text="Completed <safely>.",
    )

    assert message_id == "321"
    assert len(transport.calls) == 1
    method, url, _headers, payload, _timeout = transport.calls[0]
    assert method == "POST"
    assert "/sendMessage" in url
    assert payload["chat_id"] == "9003"
    assert payload["text"] == "Completed &lt;safely&gt;."


def test_result_delivery_drain_records_provider_receipt(test_settings) -> None:
    settings = Settings.load_for_test(
        secret_key=SecretStr("synthetic-test-secret-material-0123456789")
    )
    database = Database(settings)
    apply_migrations(database)
    transport = _MessageTransport()
    services = build_services(settings, database, messaging_transport=transport)
    owner = services.identity.create_user(display_name="Drain owner")
    project = services.identity.create_project(owner_id=owner.id, name="Drain project")
    secret = services.secrets.store(
        project_id=project.id,
        name="telegram-drain-bot",
        secret_type="token",
        value="synthetic-drain-token",
        actor_id=owner.id,
    )
    binding = services.interfaces.create_binding(
        project_id=project.id,
        actor_id=owner.id,
        platform="telegram",
        chat_id="9004",
        bot_token_ref=secret.id.value,
        is_enabled=True,
    )
    execution = _create_execution(services, owner, project)
    services.worker._execution_repo.update_execution_state(execution.id, "completed")
    services.result_delivery.enqueue_execution_result(
        project_id=project.id,
        execution_id=execution.id,
        binding_id=binding.id,
        actor_id=owner.id,
        content="Durable result.",
    )

    delivered = services.result_delivery.drain_once(project_id=project.id)

    assert delivered is not None
    assert delivered.state == "sent"
    assert delivered.external_message_id == "321"
    assert delivered.attempt_count == 1
    assert len(transport.calls) == 1


def test_result_delivery_marks_ambiguous_provider_outcome_unknown(test_settings) -> None:
    settings = Settings.load_for_test(
        secret_key=SecretStr("synthetic-test-secret-material-0123456789")
    )
    database = Database(settings)
    apply_migrations(database)
    transport = _AmbiguousTransport()
    services = build_services(settings, database, messaging_transport=transport)
    owner = services.identity.create_user(display_name="Unknown owner")
    project = services.identity.create_project(owner_id=owner.id, name="Unknown project")
    secret = services.secrets.store(
        project_id=project.id,
        name="telegram-unknown-bot",
        secret_type="token",
        value="synthetic-unknown-token",
        actor_id=owner.id,
    )
    binding = services.interfaces.create_binding(
        project_id=project.id,
        actor_id=owner.id,
        platform="telegram",
        chat_id="9005",
        bot_token_ref=secret.id.value,
        is_enabled=True,
    )
    execution = _create_execution(services, owner, project)
    services.worker._execution_repo.update_execution_state(execution.id, "completed")
    services.result_delivery.enqueue_execution_result(
        project_id=project.id,
        execution_id=execution.id,
        binding_id=binding.id,
        actor_id=owner.id,
        content="Ambiguous result.",
    )

    delivered = services.result_delivery.drain_once(project_id=project.id)

    assert delivered is not None
    assert delivered.state == "unknown"
    assert services.result_delivery.drain_once(project_id=project.id) is None


def test_result_delivery_marks_httpx_request_error_unknown(test_settings) -> None:
    settings = Settings.load_for_test(
        secret_key=SecretStr("synthetic-test-secret-material-0123456789")
    )
    database = Database(settings)
    apply_migrations(database)
    transport = _RequestErrorTransport()
    services = build_services(settings, database, messaging_transport=transport)
    owner = services.identity.create_user(display_name="Request error owner")
    project = services.identity.create_project(owner_id=owner.id, name="Request error project")
    secret = services.secrets.store(
        project_id=project.id,
        name="telegram-request-error-bot",
        secret_type="token",
        value="synthetic-request-error-token",
        actor_id=owner.id,
    )
    binding = services.interfaces.create_binding(
        project_id=project.id,
        actor_id=owner.id,
        platform="telegram",
        chat_id="9006",
        bot_token_ref=secret.id.value,
        is_enabled=True,
    )
    execution = _create_execution(services, owner, project)
    services.worker._execution_repo.update_execution_state(execution.id, "completed")
    services.result_delivery.enqueue_execution_result(
        project_id=project.id,
        execution_id=execution.id,
        binding_id=binding.id,
        actor_id=owner.id,
        content="Request error result.",
    )

    delivered = services.result_delivery.drain_once(project_id=project.id)

    assert delivered is not None
    assert delivered.state == "unknown"


def test_result_delivery_definite_failure_is_delayed_for_retry(test_settings) -> None:
    settings = Settings.load_for_test(
        secret_key=SecretStr("synthetic-test-secret-material-0123456789")
    )
    database = Database(settings)
    apply_migrations(database)
    transport = _FailureTransport()
    services = build_services(settings, database, messaging_transport=transport)
    owner = services.identity.create_user(display_name="Failure owner")
    project = services.identity.create_project(owner_id=owner.id, name="Failure project")
    secret = services.secrets.store(
        project_id=project.id,
        name="telegram-failure-bot",
        secret_type="token",
        value="synthetic-failure-token",
        actor_id=owner.id,
    )
    binding = services.interfaces.create_binding(
        project_id=project.id,
        actor_id=owner.id,
        platform="telegram",
        chat_id="9006",
        bot_token_ref=secret.id.value,
        is_enabled=True,
    )
    execution = _create_execution(services, owner, project)
    services.worker._execution_repo.update_execution_state(execution.id, "completed")
    services.result_delivery.enqueue_execution_result(
        project_id=project.id,
        execution_id=execution.id,
        binding_id=binding.id,
        actor_id=owner.id,
        content="Retryable result.",
    )

    failed = services.result_delivery.drain_once(project_id=project.id)

    assert failed is not None
    assert failed.state == "failed"
    assert failed.attempt_count == 1
    assert failed.next_attempt_at > failed.updated_at
    assert services.result_delivery.drain_once(project_id=project.id) is None


def test_result_delivery_stale_claim_is_fenced_as_unknown(services) -> None:
    owner = services.identity.create_user(display_name="Recovery owner")
    project = services.identity.create_project(owner_id=owner.id, name="Recovery project")
    binding = services.interfaces.create_binding(
        project_id=project.id,
        actor_id=owner.id,
        platform="telegram",
        chat_id="9007",
        is_enabled=True,
    )
    execution = _create_execution(services, owner, project)
    services.worker._execution_repo.update_execution_state(execution.id, "completed")
    services.result_delivery.enqueue_execution_result(
        project_id=project.id,
        execution_id=execution.id,
        binding_id=binding.id,
        actor_id=owner.id,
        content="Recovery result.",
    )
    claimed = services.result_delivery._interface_repo.claim_result_delivery(
        project.id,
        lease_seconds=300,
    )
    assert claimed is not None
    services.database.connect().execute(
        "UPDATE result_deliveries SET lease_expires_at = "
        "strftime('%Y-%m-%dT%H:%M:%fZ','now','-1 second') WHERE id = ?",
        (claimed.id.value,),
    )
    services.database.connect().commit()

    assert services.result_delivery.recover_stale(project.id) == 1
    recovered = services.result_delivery.list_for_project(project.id)[0]
    assert recovered.state == "unknown"
    assert services.result_delivery.drain_once(project_id=project.id) is None
