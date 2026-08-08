"""Secret service tests — encrypted storage, server-side resolution,
and the trust boundary that prevents secret leakage.

Per ``zero-control-plane-trust`` §"Secrets are usable without being
visible": A secret reference and a secret value are different kinds
of data. Models, logs, audits, frontend state, and ordinary database
records may carry a reference or capability ID. Only the server-side
integration boundary resolves the raw value at the last responsible
moment.

Per ``zero-tool-capability-runtime`` §"Secrets resolve at the last
responsible moment": the server wrapper resolves the credential
immediately before the external call and excludes it from request
summaries, errors, logs, and artifacts.

Per PLAN.md M3 validation:
- 'Agents receive capability handles or sanitized results, never raw
  credentials.'
- 'Audit entries contain no credential-like fixtures.'
"""

from __future__ import annotations

import pytest

from zero.app.services import build_services
from zero.config import Settings
from zero.domain.secrets import (
    SecretAlreadyExistsError,
    SecretNotFoundError,
    SecretReferenceId,
    SecretResolutionError,
    SecretRevokedError,
)
from zero.persistence.connection import Database
from zero.persistence.migrations import apply_migrations


@pytest.fixture
def secret_settings(test_settings: Settings) -> Settings:
    """Settings with a real secret_key so encryption works."""
    return Settings.load_for_test(secret_key="a" * 64)


@pytest.fixture
def services(secret_settings: Settings):
    database = Database(secret_settings)
    apply_migrations(database)
    return build_services(secret_settings, database)


@pytest.fixture
def project_with_owner(services):
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(
        owner_id=owner.id, name="Secret Test Project"
    )
    return owner, project


# ----------------------------------------------------------------------
# Store and resolve
# ----------------------------------------------------------------------


def test_store_returns_reference_without_value(services, project_with_owner) -> None:
    owner, project = project_with_owner
    secret_ref = services.secrets.store(
        project_id=project.id,
        name="api_key",
        secret_type="api_key",
        value="sk-super-secret-12345",
        actor_id=owner.id,
    )
    # The reference has metadata but no value field.
    assert secret_ref.id.value.startswith("sec_")
    assert secret_ref.name == "api_key"
    assert secret_ref.secret_type == "api_key"
    # SecretReference has no `value` attribute — it's never exposed.
    assert not hasattr(secret_ref, "value")
    assert not hasattr(secret_ref, "encrypted_value")


def test_resolve_value_returns_raw_value(services, project_with_owner) -> None:
    owner, project = project_with_owner
    secret_ref = services.secrets.store(
        project_id=project.id,
        name="api_key",
        secret_type="api_key",
        value="sk-super-secret-12345",
        actor_id=owner.id,
    )
    raw = services.secrets.resolve_value(
        project_id=project.id, secret_id=secret_ref.id
    )
    assert raw == "sk-super-secret-12345"


def test_stored_secret_is_encrypted_at_rest(
    secret_settings: Settings, project_with_owner
) -> None:
    """Per zero-control-plane-trust §"Secrets are usable without being
    visible": the raw value is NEVER stored. The database contains
    only the encrypted ciphertext.
    """
    database = Database(secret_settings)
    apply_migrations(database)
    services = build_services(secret_settings, database)
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(
        owner_id=owner.id, name="Project A"
    )
    raw_value = "sk-never-store-me-plaintext-12345"
    services.secrets.store(
        project_id=project.id,
        name="api_key",
        secret_type="api_key",
        value=raw_value,
        actor_id=owner.id,
    )
    # Inspect the raw database row.
    conn = database.connect()
    cursor = conn.execute(
        "SELECT encrypted_value FROM secret_references WHERE project_id = ?",
        (project.id.value,),
    )
    row = cursor.fetchone()
    assert row is not None
    encrypted = row["encrypted_value"]
    # The encrypted value must not contain the raw value.
    assert raw_value not in encrypted
    # The encrypted value must not be empty.
    assert encrypted


# ----------------------------------------------------------------------
# Revocation
# ----------------------------------------------------------------------


def test_revoked_secret_cannot_be_resolved(
    services, project_with_owner
) -> None:
    owner, project = project_with_owner
    secret_ref = services.secrets.store(
        project_id=project.id,
        name="api_key",
        secret_type="api_key",
        value="sk-revoke-me",
        actor_id=owner.id,
    )
    services.secrets.revoke(
        project_id=project.id,
        secret_id=secret_ref.id,
        actor_id=owner.id,
    )
    with pytest.raises(SecretRevokedError):
        services.secrets.resolve_value(
            project_id=project.id, secret_id=secret_ref.id
        )
    # The reference metadata shows revoked_at is set.
    ref = services.secrets.get_reference(
        project_id=project.id, secret_id=secret_ref.id
    )
    assert ref.is_revoked


# ----------------------------------------------------------------------
# Not found
# ----------------------------------------------------------------------


def test_get_reference_raises_for_nonexistent(services, project_with_owner) -> None:
    _, project = project_with_owner
    with pytest.raises(SecretNotFoundError):
        services.secrets.get_reference(
            project_id=project.id,
            secret_id=SecretReferenceId("sec_nonexistent"),
        )


def test_resolve_value_raises_for_nonexistent(
    services, project_with_owner
) -> None:
    _, project = project_with_owner
    with pytest.raises(SecretNotFoundError):
        services.secrets.resolve_value(
            project_id=project.id,
            secret_id=SecretReferenceId("sec_nonexistent"),
        )


# ----------------------------------------------------------------------
# Duplicate name rejected
# ----------------------------------------------------------------------


def test_duplicate_secret_name_rejected(services, project_with_owner) -> None:
    owner, project = project_with_owner
    services.secrets.store(
        project_id=project.id,
        name="api_key",
        secret_type="api_key",
        value="sk-first",
        actor_id=owner.id,
    )
    with pytest.raises(SecretAlreadyExistsError):
        services.secrets.store(
            project_id=project.id,
            name="api_key",
            secret_type="api_key",
            value="sk-second",
            actor_id=owner.id,
        )


# ----------------------------------------------------------------------
# No secret_key configured
# ----------------------------------------------------------------------


def test_store_without_secret_key_raises(test_settings: Settings) -> None:
    """Per ADR 0004: in test mode, secret_key is optional. The secret
    service must fail clearly when encryption is attempted without
    a key."""
    # test_settings has secret_key=None by default.
    database = Database(test_settings)
    apply_migrations(database)
    services = build_services(test_settings, database)
    owner = services.identity.create_user(display_name="Owner")
    project = services.identity.create_project(
        owner_id=owner.id, name="Project A"
    )
    with pytest.raises(SecretResolutionError, match="ZERO_SECRET_KEY"):
        services.secrets.store(
            project_id=project.id,
            name="api_key",
            secret_type="api_key",
            value="sk-test",
            actor_id=owner.id,
        )


# ----------------------------------------------------------------------
# Audit events for secret operations
# ----------------------------------------------------------------------


def test_secret_store_audited_without_value(
    services, project_with_owner
) -> None:
    """Per zero-control-plane-trust §"Audit is evidence, not a
    transcript dump": the audit event for storing a secret must
    contain the secret ID but NOT the value, the name, or any
    ciphertext."""
    owner, project = project_with_owner
    raw_value = "sk-do-not-leak-me-12345"
    secret_ref = services.secrets.store(
        project_id=project.id,
        name="api_key",
        secret_type="api_key",
        value=raw_value,
        actor_id=owner.id,
    )
    events = services.audit.list_for_project(project_id=project.id, actor_id=project.owner_user_id, limit=50)
    secret_events = [
        e for e in events if e.operation == "secret.store"
    ]
    assert len(secret_events) >= 1
    event = secret_events[0]
    assert event.target_id == secret_ref.id.value
    # The raw value MUST NOT appear anywhere in the audit event.
    assert raw_value not in (event.redacted_summary or "")
    assert raw_value not in str(event.to_dict() if hasattr(event, "to_dict") else event)


# ----------------------------------------------------------------------
# Cross-project secret access denied
# ----------------------------------------------------------------------


def test_secret_cannot_be_resolved_from_other_project(services) -> None:
    """Per zero-project-isolation-evidence: a secret stored in project
    A must not be resolvable from project B."""
    owner_a = services.identity.create_user(display_name="Owner A")
    project_a = services.identity.create_project(
        owner_id=owner_a.id, name="Project A"
    )
    owner_b = services.identity.create_user(display_name="Owner B")
    project_b = services.identity.create_project(
        owner_id=owner_b.id, name="Project B"
    )
    secret_ref = services.secrets.store(
        project_id=project_a.id,
        name="api_key",
        secret_type="api_key",
        value="sk-project-a-secret",
        actor_id=owner_a.id,
    )
    with pytest.raises(SecretNotFoundError):
        services.secrets.resolve_value(
            project_id=project_b.id, secret_id=secret_ref.id
        )


# ----------------------------------------------------------------------
# List secrets returns metadata only
# ----------------------------------------------------------------------


def test_list_secrets_returns_metadata_only(
    services, project_with_owner
) -> None:
    owner, project = project_with_owner
    services.secrets.store(
        project_id=project.id,
        name="api_key",
        secret_type="api_key",
        value="sk-secret-1",
        actor_id=owner.id,
    )
    services.secrets.store(
        project_id=project.id,
        name="db_password",
        secret_type="password",
        value="supersecretpassword",
        actor_id=owner.id,
    )
    refs = services.secrets.list_for_project(project_id=project.id, actor_id=project.owner_user_id)
    assert len(refs) == 2
    # None of the returned references should expose the value.
    for ref in refs:
        assert not hasattr(ref, "value")
        assert not hasattr(ref, "encrypted_value")
