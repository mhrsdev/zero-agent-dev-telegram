"""Secret reference repository.

Stores encrypted secret values. The encryption key is derived from
``ZERO_SECRET_KEY`` in :mod:`zero.app.secrets`. This repository only
stores and retrieves the ciphertext; it never decrypts.

Per ``zero-control-plane-trust`` §"Secrets are usable without being
visible": the raw value is resolved only at the capability boundary,
never in domain/app/audit.
"""

from __future__ import annotations

import sqlite3

from zero.domain.identity import ProjectId
from zero.domain.secrets import (
    SecretAlreadyExistsError,
    SecretNotFoundError,
    SecretReference,
    SecretReferenceId,
)
from zero.persistence.connection import Database


def _row_to_secret_reference(row: sqlite3.Row | tuple) -> SecretReference:
    return SecretReference(
        id=SecretReferenceId(row["id"]),
        project_id=ProjectId(row["project_id"]),
        name=row["name"],
        secret_type=row["secret_type"],  # type: ignore[arg-type]
        created_at=row["created_at"],
        revoked_at=row["revoked_at"],
    )


class SecretRepository:
    """Database-backed secret reference repository.

    The ``encrypted_value`` column is stored but never returned by
    public read methods. Only :meth:`get_encrypted_value` returns it,
    and only for the capability boundary to decrypt at invocation time.
    """

    def __init__(self, database: Database) -> None:
        self._database = database

    def insert(
        self,
        secret_ref: SecretReference,
        encrypted_value: str,
        *,
        commit: bool = True,
    ) -> None:
        conn = self._database.connect()
        try:
            conn.execute(
                "INSERT INTO secret_references "
                "(id, project_id, name, secret_type, encrypted_value) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    secret_ref.id.value,
                    secret_ref.project_id.value,
                    secret_ref.name,
                    secret_ref.secret_type,
                    encrypted_value,
                ),
            )
            if commit:
                conn.commit()
        except sqlite3.IntegrityError as exc:
            if commit:
                conn.rollback()
            if "UNIQUE" in str(exc):
                raise SecretAlreadyExistsError(
                    f"Secret {secret_ref.name!r} already exists in "
                    f"project {secret_ref.project_id}"
                ) from exc
            raise

    def get_by_id(
        self,
        project_id: ProjectId,
        secret_id: SecretReferenceId,
    ) -> SecretReference:
        """Return the secret reference, scoped to the given project.

        Per ``zero-project-isolation-evidence`` §"Scope begins before
        access": the query filters by ``project_id`` before any row is
        loaded. A secret from another project is never returned even
        if its ID is guessed.
        """
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, name, secret_type, created_at, revoked_at "
            "FROM secret_references WHERE id = ? AND project_id = ?",
            (secret_id.value, project_id.value),
        )
        row = cursor.fetchone()
        if row is None:
            raise SecretNotFoundError(
                f"Secret {secret_id} not found in project {project_id}"
            )
        return _row_to_secret_reference(row)

    def get_by_name(
        self,
        project_id: ProjectId,
        name: str,
    ) -> SecretReference:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, name, secret_type, created_at, revoked_at "
            "FROM secret_references WHERE project_id = ? AND name = ?",
            (project_id.value, name),
        )
        row = cursor.fetchone()
        if row is None:
            raise SecretNotFoundError(
                f"Secret {name!r} not found in project {project_id}"
            )
        return _row_to_secret_reference(row)

    def get_encrypted_value(
        self,
        project_id: ProjectId,
        secret_id: SecretReferenceId,
    ) -> str:
        """Return the encrypted ciphertext for the secret.

        Only the capability boundary calls this method. The ciphertext
        is decrypted immediately before an external call and dropped
        from memory after the call returns or fails.

        Per ``zero-control-plane-trust`` §"Secrets are usable without
        being visible": only the server-side integration boundary
        resolves the raw value at the last responsible moment.
        """
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT encrypted_value FROM secret_references "
            "WHERE id = ? AND project_id = ?",
            (secret_id.value, project_id.value),
        )
        row = cursor.fetchone()
        if row is None:
            raise SecretNotFoundError(
                f"Secret {secret_id} not found in project {project_id}"
            )
        return row["encrypted_value"]

    def revoke(
        self,
        project_id: ProjectId,
        secret_id: SecretReferenceId,
        revoked_at: str,
        *,
        commit: bool = True,
    ) -> None:
        conn = self._database.connect()
        cursor = conn.execute(
            "UPDATE secret_references SET revoked_at = ? "
            "WHERE id = ? AND project_id = ? AND revoked_at IS NULL",
            (revoked_at, secret_id.value, project_id.value),
        )
        if cursor.rowcount == 0:
            # Either the secret doesn't exist, or it's already revoked.
            # We treat both as "not revocable in this state".
            existing = self._exists(project_id, secret_id)
            if not existing:
                raise SecretNotFoundError(
                    f"Secret {secret_id} not found in project {project_id}"
                )
            # Already revoked — idempotent success.
            return
        if commit:
            conn.commit()

    def _exists(
        self, project_id: ProjectId, secret_id: SecretReferenceId
    ) -> bool:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT 1 FROM secret_references WHERE id = ? AND project_id = ?",
            (secret_id.value, project_id.value),
        )
        return cursor.fetchone() is not None

    def list_for_project(self, project_id: ProjectId) -> list[SecretReference]:
        """List all secret references in a project.

        Returns metadata only — never the encrypted value.
        """
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, name, secret_type, created_at, revoked_at "
            "FROM secret_references WHERE project_id = ? "
            "ORDER BY created_at",
            (project_id.value,),
        )
        return [_row_to_secret_reference(row) for row in cursor.fetchall()]
