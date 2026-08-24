"""Secret service — encrypted storage and server-side resolution.

Per ``zero-control-plane-trust`` §"Secrets are usable without being
visible": A secret reference and a secret value are different kinds
of data. Models, logs, audits, frontend state, and ordinary database
records may carry a reference or capability ID. Only the server-side
integration boundary resolves the raw value at the last responsible
moment.

Per ``zero-tool-capability-runtime`` §"Secrets resolve at the last
responsible moment": The registry stores a secret reference, not a
raw value in model-visible metadata. The server wrapper resolves the
credential immediately before the external call and excludes it from
request summaries, errors, logs, and artifacts.

Implementation:
- Encryption uses Fernet (symmetric authenticated encryption) from
  the ``cryptography`` package.
- The Fernet key is derived from ``ZERO_SECRET_KEY`` using HKDF.
- The encrypted ciphertext is stored in the ``secret_references``
  table.
- The raw value is decrypted only inside
  :meth:`SecretService.resolve_value`, which is called only by the
  tool capability runtime at invocation time.
- The raw value is held in memory for the minimum time necessary and
  is never logged, never audited, never returned to a client.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from zero.app.authorization_service import AuthorizationService
from zero.config import Settings
from zero.domain.audit import AuditEvent, AuditEventId, AuditSource
from zero.domain.identity import ProjectId, UserId
from zero.domain.ids import (
    generate_audit_event_id,
    generate_secret_reference_id,
)
from zero.domain.secrets import (
    SecretReference,
    SecretReferenceId,
    SecretResolutionError,
    SecretRevokedError,
    SecretType,
)
from zero.persistence.repositories.audit_repository import AuditRepository
from zero.persistence.repositories.secret_repository import (
    SecretRepository,
)


def _now_utc_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _derive_fernet_key(secret_key_material: str) -> bytes:
    """Derive a URL-safe base64 Fernet key from ``ZERO_SECRET_KEY``.

    HKDF-SHA256 with a fixed info string gives us a deterministic,
    well-distributed 32-byte key. We base64-encode it (URL-safe) so
    Fernet can consume it directly. We do NOT use the raw secret key
    directly; HKDF adds a layer of key-separation between the
    application's signing key and the encryption key.
    """
    material = secret_key_material.encode("utf-8")
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"zero-develop/secret-encryption/v1",
    )
    key = hkdf.derive(material)
    return base64.urlsafe_b64encode(key)


def derive_key_id(secret_key_material: str) -> str:
    """A short, non-secret identifier of the current encryption key.

    Stored alongside each ciphertext so a key rotation produces a
    precise "wrong key version" error instead of an opaque decrypt
    failure. The identifier is a truncated hash and does not disclose
    the key material.
    """
    return hashlib.sha256(secret_key_material.encode("utf-8")).hexdigest()[:16]


class SecretService:
    """Server-side secret storage and resolution.

    The service exposes:

    - :meth:`store`: encrypt and persist a secret value, returning a
      :class:`SecretReference` (metadata only).
    - :meth:`resolve_value`: decrypt and return the raw value. This
      method is called ONLY by the tool capability runtime at
      invocation time. It is never called by HTTP handlers, audit,
      logs, or model-facing code.
    - :meth:`revoke`: mark a secret as revoked. Revoked secrets
      cannot be resolved.
    - :meth:`get_reference`: return metadata for a secret, never the
      raw value.
    """

    def __init__(
        self,
        secret_repo: SecretRepository,
        audit_repo: AuditRepository,
        settings: Settings,
        authorization: AuthorizationService,
    ) -> None:
        self._secret_repo = secret_repo
        self._audit_repo = audit_repo
        self._settings = settings
        self._authorization = authorization
        self._fernet: Fernet | None = None
        self._key_id: str | None = None

    def _key_material(self) -> str:
        if self._settings.secret_key is None:
            raise SecretResolutionError(
                "Cannot encrypt/decrypt secrets: ZERO_SECRET_KEY is not set"
            )
        key_material = self._settings.secret_key.get_secret_value()
        if not key_material.strip():
            raise SecretResolutionError("Cannot encrypt/decrypt secrets: ZERO_SECRET_KEY is blank")
        return key_material

    def _get_fernet(self) -> Fernet:
        if self._fernet is not None:
            return self._fernet
        key_material = self._key_material()
        key = _derive_fernet_key(key_material)
        self._fernet = Fernet(key)
        return self._fernet

    def _current_key_id(self) -> str:
        if self._key_id is None:
            self._key_id = derive_key_id(self._key_material())
        return self._key_id

    def store(
        self,
        *,
        project_id: ProjectId,
        name: str,
        secret_type: SecretType,
        value: str,
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> SecretReference:
        """Encrypt and persist a secret value.

        Returns the :class:`SecretReference` (metadata only). The raw
        value is never stored, never logged, never audited.
        """
        if not name or not name.strip():
            raise ValueError("secret name must not be empty")
        if not value:
            raise ValueError("secret value must not be empty")
        self._authorization.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="secret.manage",
            source=source,
        )
        fernet = self._get_fernet()
        encrypted = fernet.encrypt(value.encode("utf-8")).decode("ascii")
        secret_ref = SecretReference(
            id=SecretReferenceId(generate_secret_reference_id()),
            project_id=project_id,
            name=name.strip(),
            secret_type=secret_type,
            created_at=_now_utc_iso(),
        )
        self._secret_repo.insert(secret_ref, encrypted, key_id=self._current_key_id())
        self._audit_repo.insert(
            AuditEvent(
                id=AuditEventId(generate_audit_event_id()),
                project_id=project_id,
                actor_id=actor_id,
                source=source,
                operation="secret.store",
                target_type="secret_reference",
                target_id=secret_ref.id.value,
                result="success",
                # Summary intentionally omits the value, the name, and
                # any ciphertext. The secret ID is enough for
                # correlation.
                redacted_summary=f"Stored secret reference {secret_ref.id.value}",
                created_at=_now_utc_iso(),
            )
        )
        return secret_ref

    def resolve_value(
        self,
        *,
        project_id: ProjectId,
        secret_id: SecretReferenceId,
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> str:
        """Decrypt and return the raw secret value.

        .. warning::

            This method returns the raw secret. It MUST be called only
            by the tool capability runtime at invocation time. The
            caller is responsible for dropping the returned string
            from memory as soon as the external call completes.

        Per ``zero-tool-capability-runtime`` §"Secrets resolve at the
        last responsible moment": the server wrapper resolves the
        credential immediately before the external call and excludes
        it from request summaries, errors, logs, and artifacts.
        """
        self._authorization.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="secret.manage",
            source=source,
        )
        secret_ref = self._secret_repo.get_by_id(project_id, secret_id)
        if secret_ref.is_revoked:
            raise SecretRevokedError(f"Secret {secret_id} has been revoked")
        encrypted, stored_key_id = self._secret_repo.get_encrypted_record(project_id, secret_id)
        current_key_id = self._current_key_id()
        if stored_key_id is not None and stored_key_id != current_key_id:
            # The key rotated after this secret was written; the stored
            # key-id makes the failure precise and actionable instead of
            # an opaque InvalidToken.
            raise SecretResolutionError(
                f"Secret {secret_id} was encrypted with key version {stored_key_id!r}, "
                f"but the configured ZERO_SECRET_KEY resolves to {current_key_id!r}; "
                "restore the original key or re-store the secret"
            )
        fernet = self._get_fernet()
        try:
            raw = fernet.decrypt(encrypted.encode("ascii"))
        except InvalidToken as exc:
            # The ciphertext is corrupt or (for legacy rows without a
            # stamped key id) the key has changed. We MUST NOT include
            # the ciphertext in the error message.
            raise SecretResolutionError(f"Failed to decrypt secret {secret_id}") from exc
        return raw.decode("utf-8")

    def get_reference(
        self,
        *,
        project_id: ProjectId,
        secret_id: SecretReferenceId,
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> SecretReference:
        """Return metadata for a secret. Never returns the raw value."""
        self._authorization.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="secret.manage",
            source=source,
        )
        return self._secret_repo.get_by_id(project_id, secret_id)

    def get_reference_by_name(
        self,
        *,
        project_id: ProjectId,
        name: str,
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> SecretReference:
        self._authorization.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="secret.manage",
            source=source,
        )
        return self._secret_repo.get_by_name(project_id, name)

    def revoke(
        self,
        *,
        project_id: ProjectId,
        secret_id: SecretReferenceId,
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> None:
        """Mark a secret as revoked. Revoked secrets cannot be resolved."""
        self._authorization.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="secret.manage",
            source=source,
        )
        self._secret_repo.revoke(project_id, secret_id, _now_utc_iso())
        self._audit_repo.insert(
            AuditEvent(
                id=AuditEventId(generate_audit_event_id()),
                project_id=project_id,
                actor_id=actor_id,
                source=source,
                operation="secret.revoke",
                target_type="secret_reference",
                target_id=secret_id.value,
                result="success",
                redacted_summary=f"Revoked secret reference {secret_id.value}",
                created_at=_now_utc_iso(),
            )
        )

    def list_for_project(
        self,
        *,
        project_id: ProjectId,
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> list[SecretReference]:
        """List all secret references in a project. Metadata only."""
        self._authorization.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="secret.manage",
            source=source,
        )
        return self._secret_repo.list_for_project(project_id)
