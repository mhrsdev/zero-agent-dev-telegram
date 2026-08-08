"""Secret reference domain types.

Per ``zero-control-plane-trust`` §"Secrets are usable without being
visible": A secret reference and a secret value are different kinds of
data. Models, logs, audits, frontend state, and ordinary database
records may carry a reference or capability ID. Only the server-side
integration boundary resolves the raw value at the last responsible
moment.

Per ``zero-tool-capability-runtime`` §"Secrets resolve at the last
responsible moment": The registry stores a secret reference, not a
raw value in model-visible metadata. The server wrapper resolves the
credential immediately before the external call and excludes it from
request summaries, errors, logs, and artifacts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from zero.domain.identity import ProjectId

SecretType = Literal["api_key", "token", "password", "other"]

#: Prefix for Secret Reference IDs.
SECRET_REFERENCE_ID_PREFIX = "sec_"


@dataclass(frozen=True)
class SecretReferenceId:
    """Stable server-issued ID for a secret reference."""

    value: str

    def __post_init__(self) -> None:
        if not self.value or not isinstance(self.value, str):
            raise ValueError("SecretReferenceId must be a non-empty string")
        if not self.value.startswith(SECRET_REFERENCE_ID_PREFIX):
            raise ValueError(
                f"SecretReferenceId must start with "
                f"{SECRET_REFERENCE_ID_PREFIX!r}; got {self.value!r}"
            )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class SecretReference:
    """A server-side reference to a secret.

    The ``encrypted_value`` is stored server-side and is never:

    - returned to a client (even masked);
    - included in audit events;
    - included in logs or metrics;
    - passed to a model prompt or tool description;
    - included in backups without encryption.

    The raw value is resolved only at the capability boundary
    (:mod:`zero.app.tools`) immediately before an external call, and
    is dropped from memory after the call returns or fails.

    Attributes:
        id: stable server-issued ID.
        project_id: the project this secret belongs to. Secrets are
            project-scoped per ``zero-project-isolation-evidence``.
        name: human-readable name for the secret (e.g.
            ``"search_provider_api_key"``). Unique within a project.
        secret_type: the kind of secret (api_key, token, ...).
        created_at: ISO-8601 timestamp.
        revoked_at: ISO-8601 timestamp when the secret was revoked,
            or ``None`` if active. Revoked secrets cannot be resolved.
    """

    id: SecretReferenceId
    project_id: ProjectId
    name: str
    secret_type: SecretType
    created_at: str = ""
    revoked_at: str | None = None

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None


# ----------------------------------------------------------------------
# Typed failures
# ----------------------------------------------------------------------


class SecretError(RuntimeError):
    """Base class for secret-domain typed failures."""


class SecretNotFoundError(SecretError):
    """No secret reference exists with the given ID or name."""


class SecretAlreadyExistsError(SecretError):
    """A secret with the same name already exists in this project."""


class SecretRevokedError(SecretError):
    """The secret reference has been revoked and cannot be resolved."""


class SecretResolutionError(SecretError):
    """The secret value could not be resolved.

    This is raised when the encryption key is missing, the ciphertext
    is corrupt, or the secret store is unavailable. The error message
    MUST NOT include the raw secret value or the ciphertext.
    """
