"""Identity repository — users, projects, memberships, external identities.

Per ``zero-project-isolation-evidence`` §"Scope begins before access":
all project-scoped queries filter by ``project_id`` before content is
loaded. We never retrieve globally and filter after ranking.

Per ``zero-control-plane-trust`` §"Atomicity follows the business
fact": operations that represent one fact should not leave half-facts.
Creating a project + creating the owner membership is one atomic
business fact; we use a single transaction.
"""

from __future__ import annotations

import sqlite3

from zero.domain.identity import (
    DuplicateExternalIdentityError,
    DuplicateUserError,
    ExternalIdentity,
    ExternalIdentityId,
    ExternalIdentityNotVerifiedError,
    ExternalPlatform,
    MembershipAlreadyExistsError,
    MembershipNotFoundError,
    Project,
    ProjectId,
    ProjectMembership,
    ProjectNotFoundError,
    ProjectScope,
    User,
    UserId,
    UserNotFoundError,
)
from zero.persistence.connection import Database


def _row_to_user(row: sqlite3.Row | tuple) -> User:
    return User(
        id=UserId(row["id"]),
        display_name=row["display_name"],
        status=row["status"],
        created_at=row["created_at"],
    )


def _row_to_project(row: sqlite3.Row | tuple) -> Project:
    return Project(
        id=ProjectId(row["id"]),
        name=row["name"],
        owner_user_id=UserId(row["owner_user_id"]),
        created_at=row["created_at"],
    )


def _row_to_membership(row: sqlite3.Row | tuple) -> ProjectMembership:
    return ProjectMembership(
        project_id=ProjectId(row["project_id"]),
        user_id=UserId(row["user_id"]),
        role=row["role"],  # type: ignore[arg-type]
        created_at=row["created_at"],
    )


def _row_to_external_identity(row: sqlite3.Row | tuple) -> ExternalIdentity:
    return ExternalIdentity(
        id=ExternalIdentityId(row["id"]),
        user_id=UserId(row["user_id"]),
        platform=row["platform"],  # type: ignore[arg-type]
        external_id=row["external_id"],
        external_username=row["external_username"],
        verified_at=row["verified_at"],
        created_at=row["created_at"],
    )


class IdentityRepository:
    """Database-backed identity repository.

    All write operations commit their own transactions. Read operations
    do not commit. Callers may pass ``commit=False`` to write methods
    to participate in a larger transaction; in that case the caller is
    responsible for committing.
    """

    def __init__(self, database: Database) -> None:
        self._database = database

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    def insert_user(self, user: User, *, commit: bool = True) -> None:
        conn = self._database.connect()
        try:
            conn.execute(
                "INSERT INTO users (id, display_name, status) VALUES (?, ?, ?)",
                (user.id.value, user.display_name, user.status),
            )
            if commit:
                conn.commit()
        except sqlite3.IntegrityError as exc:
            if commit:
                conn.rollback()
            if "UNIQUE" in str(exc) and "users.id" in str(exc):
                raise DuplicateUserError(f"User {user.id} already exists") from exc
            raise

    def get_user(self, user_id: UserId) -> User:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, display_name, status, created_at FROM users WHERE id = ?",
            (user_id.value,),
        )
        row = cursor.fetchone()
        if row is None:
            raise UserNotFoundError(f"User {user_id} not found")
        return _row_to_user(row)

    def user_exists(self, user_id: UserId) -> bool:
        try:
            self.get_user(user_id)
            return True
        except UserNotFoundError:
            return False

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    def insert_project(
        self,
        project: Project,
        *,
        owner_membership: ProjectMembership | None = None,
        commit: bool = True,
    ) -> None:
        """Insert a project and optionally its owner membership atomically.

        Per ``zero-control-plane-trust`` §"Atomicity follows the
        business fact": creating a project + creating the owner
        membership is one atomic business fact. We use a single
        transaction so a partial failure cannot leave a project
        without an owner.
        """
        conn = self._database.connect()
        try:
            conn.execute(
                "INSERT INTO projects (id, name, owner_user_id) VALUES (?, ?, ?)",
                (project.id.value, project.name, project.owner_user_id.value),
            )
            if owner_membership is not None:
                conn.execute(
                    "INSERT INTO project_memberships (project_id, user_id, role) VALUES (?, ?, ?)",
                    (
                        owner_membership.project_id.value,
                        owner_membership.user_id.value,
                        owner_membership.role,
                    ),
                )
            if commit:
                conn.commit()
        except sqlite3.IntegrityError:
            if commit:
                conn.rollback()
            raise

    def get_project(self, project_id: ProjectId) -> Project:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, name, owner_user_id, created_at FROM projects WHERE id = ?",
            (project_id.value,),
        )
        row = cursor.fetchone()
        if row is None:
            raise ProjectNotFoundError(f"Project {project_id} not found")
        return _row_to_project(row)

    def project_exists(self, project_id: ProjectId) -> bool:
        try:
            self.get_project(project_id)
            return True
        except ProjectNotFoundError:
            return False

    def list_projects(self) -> list[Project]:
        """Return every project, ordered by creation.

        Used by the managed background workers to host autonomous work
        for the whole deployment. Authorization still happens per
        project through the normal service boundary.
        """
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, name, owner_user_id, created_at FROM projects ORDER BY created_at"
        )
        return [_row_to_project(row) for row in cursor.fetchall()]

    # ------------------------------------------------------------------
    # Memberships
    # ------------------------------------------------------------------

    def insert_membership(self, membership: ProjectMembership, *, commit: bool = True) -> None:
        conn = self._database.connect()
        try:
            conn.execute(
                "INSERT INTO project_memberships (project_id, user_id, role) VALUES (?, ?, ?)",
                (
                    membership.project_id.value,
                    membership.user_id.value,
                    membership.role,
                ),
            )
            if commit:
                conn.commit()
        except sqlite3.IntegrityError as exc:
            if commit:
                conn.rollback()
            if "UNIQUE" in str(exc):
                raise MembershipAlreadyExistsError(
                    f"User {membership.user_id} is already a member of "
                    f"project {membership.project_id}"
                ) from exc
            raise

    def delete_membership(
        self,
        project_id: ProjectId,
        user_id: UserId,
        *,
        commit: bool = True,
    ) -> None:
        """Remove a membership. Raises if the membership does not exist."""
        conn = self._database.connect()
        cursor = conn.execute(
            "DELETE FROM project_memberships WHERE project_id = ? AND user_id = ?",
            (project_id.value, user_id.value),
        )
        if cursor.rowcount == 0:
            raise MembershipNotFoundError(f"User {user_id} is not a member of project {project_id}")
        if commit:
            conn.commit()

    def get_membership(self, project_id: ProjectId, user_id: UserId) -> ProjectMembership | None:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT project_id, user_id, role, created_at "
            "FROM project_memberships WHERE project_id = ? AND user_id = ?",
            (project_id.value, user_id.value),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return _row_to_membership(row)

    def list_memberships_for_project(self, project_id: ProjectId) -> list[ProjectMembership]:
        """List all memberships in a project.

        Per ``zero-project-isolation-evidence`` §"Scope begins before
        access": the query filters by ``project_id`` before any row is
        loaded.
        """
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT project_id, user_id, role, created_at "
            "FROM project_memberships WHERE project_id = ? "
            "ORDER BY created_at",
            (project_id.value,),
        )
        return [_row_to_membership(row) for row in cursor.fetchall()]

    def list_memberships_for_user(self, user_id: UserId) -> list[ProjectMembership]:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT project_id, user_id, role, created_at "
            "FROM project_memberships WHERE user_id = ? "
            "ORDER BY created_at",
            (user_id.value,),
        )
        return [_row_to_membership(row) for row in cursor.fetchall()]

    def resolve_scope(self, project_id: ProjectId, actor_id: UserId) -> ProjectScope:
        """Resolve the actor's role in a project.

        Returns a :class:`ProjectScope` with ``is_member=True`` and the
        actor's role if the actor is a member, or
        ``is_member=False`` otherwise.

        Per ``zero-project-isolation-evidence`` §"Scope begins before
        access": scope is established before any project-scoped data is
        loaded.
        """
        membership = self.get_membership(project_id, actor_id)
        if membership is None:
            return ProjectScope.for_non_member(project_id, actor_id)
        return ProjectScope.for_member(project_id, actor_id, membership.role)

    # ------------------------------------------------------------------
    # External identities
    # ------------------------------------------------------------------

    def insert_external_identity(self, identity: ExternalIdentity, *, commit: bool = True) -> None:
        conn = self._database.connect()
        try:
            conn.execute(
                "INSERT INTO external_identities "
                "(id, user_id, platform, external_id, external_username, verified_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    identity.id.value,
                    identity.user_id.value,
                    identity.platform,
                    identity.external_id,
                    identity.external_username,
                    identity.verified_at,
                ),
            )
            if commit:
                conn.commit()
        except sqlite3.IntegrityError as exc:
            if commit:
                conn.rollback()
            if "UNIQUE" in str(exc) and "external_identities" in str(exc):
                raise DuplicateExternalIdentityError(
                    f"External identity {identity.platform}:{identity.external_id} "
                    f"is already linked to a Zero User"
                ) from exc
            raise

    def get_external_identity(
        self,
        platform: ExternalPlatform,
        external_id: str,
    ) -> ExternalIdentity | None:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, user_id, platform, external_id, external_username, "
            "verified_at, created_at FROM external_identities "
            "WHERE platform = ? AND external_id = ?",
            (platform, external_id),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return _row_to_external_identity(row)

    def list_external_identities_for_user(self, user_id: UserId) -> list[ExternalIdentity]:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, user_id, platform, external_id, external_username, "
            "verified_at, created_at FROM external_identities "
            "WHERE user_id = ? ORDER BY created_at",
            (user_id.value,),
        )
        return [_row_to_external_identity(row) for row in cursor.fetchall()]

    def mark_external_identity_verified(
        self,
        identity_id: ExternalIdentityId,
        verified_at: str,
        *,
        commit: bool = True,
    ) -> None:
        conn = self._database.connect()
        cursor = conn.execute(
            "UPDATE external_identities SET verified_at = ? WHERE id = ?",
            (verified_at, identity_id.value),
        )
        if cursor.rowcount == 0:
            raise UserNotFoundError(f"External identity {identity_id} not found")
        if commit:
            conn.commit()

    def require_verified_external_identity(
        self,
        platform: ExternalPlatform,
        external_id: str,
    ) -> ExternalIdentity:
        """Return the verified external identity, or raise.

        Per ``zero-control-plane-trust`` §"Identity is a link, not a
        name": external IDs are links, not identities. The link must
        be verified before it can be used for authentication.
        """
        identity = self.get_external_identity(platform, external_id)
        if identity is None:
            raise UserNotFoundError(f"No external identity found for {platform}:{external_id}")
        if identity.verified_at is None:
            raise ExternalIdentityNotVerifiedError(
                f"External identity {platform}:{external_id} is not verified"
            )
        return identity
